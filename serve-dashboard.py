#!/usr/bin/env python3
"""
Claude Process Monitor & Session Viewer Dashboard Server
Serves the dashboard HTML and provides API endpoints for real-time data
"""

import http.server
import socketserver
import json
import subprocess
import os
import sys
import ctypes
import time
import threading
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from collections import deque
from datetime import datetime, timedelta
import re

# Suppress subprocess console windows on Windows (prevents screen flashing)
if os.name == "nt":
    _orig_subprocess_run = subprocess.run
    _orig_subprocess_popen = subprocess.Popen

    def _run_no_window(*args, **kwargs):
        if "creationflags" not in kwargs:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        return _orig_subprocess_run(*args, **kwargs)

    class _PopenNoWindow(_orig_subprocess_popen):
        def __init__(self, *args, **kwargs):
            if "creationflags" not in kwargs:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.run = _run_no_window
    subprocess.Popen = _PopenNoWindow

PORT = 37778
SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR = SCRIPT_DIR / "logs"

# ─── Process State Tracker (shared across requests) ─────────────────────────
_previous_pids = {}   # pid -> {type, memory, cmdline}
_tracker_lock = threading.Lock()


def _write_event(event_dict):
    """Append an event to today's event log file."""
    today = datetime.now().strftime('%Y%m%d')
    event_file = LOG_DIR / f'events_{today}.jsonl'
    try:
        with open(event_file, 'a', encoding='utf-8') as f:
            f.write(json.dumps(event_dict) + '\n')
    except (OSError, IOError) as e:
        print(f"Warning: Could not write event: {e}", file=sys.stderr)


def _detect_and_log_events(current_processes, total_mem):
    """Compare current processes with previous state, log SPAWN/EXIT events."""
    global _previous_pids
    now = datetime.now().isoformat()

    current_pids = {}
    for p in current_processes:
        current_pids[p['pid']] = {
            'type': p.get('type', 'unknown'),
            'memory': p.get('memory', 0),
        }

    new_events = []

    with _tracker_lock:
        prev = _previous_pids

        # Detect SPAWNs (pids in current but not in previous)
        for pid, info in current_pids.items():
            if pid not in prev:
                ev = {
                    'ts': now,
                    'event': 'SPAWN',
                    'pid': str(pid),
                    'details': info['type'],
                    'total_mem_mb': total_mem,
                }
                new_events.append(ev)

        # Detect EXITs (pids in previous but not in current)
        for pid, info in prev.items():
            if pid not in current_pids:
                ev = {
                    'ts': now,
                    'event': 'EXIT',
                    'pid': str(pid),
                    'details': info['type'],
                    'total_mem_mb': total_mem,
                }
                new_events.append(ev)

        # Detect memory anomalies (any single process > 2GB)
        for pid, info in current_pids.items():
            if info['memory'] > 2048:
                prev_mem = prev.get(pid, {}).get('memory', 0)
                if prev_mem <= 2048:
                    ev = {
                        'ts': now,
                        'event': 'ANOMALY_MEM',
                        'pid': str(pid),
                        'details': f"{info['type']} using {info['memory']}MB",
                        'total_mem_mb': total_mem,
                    }
                    new_events.append(ev)

        _previous_pids = current_pids

    for ev in new_events:
        _write_event(ev)

    return new_events
CLAUDE_DIR = Path.home() / '.claude'
PROJECTS_DIR = CLAUDE_DIR / 'projects'
HISTORY_FILE = CLAUDE_DIR / 'history.jsonl'
OPENCLAW_DIR = Path.home() / '.openclaw' / 'agents'
CODEX_DIR = Path.home() / '.codex' / 'sessions'
CODEX_ARCHIVED_DIR = Path.home() / '.codex' / 'archived_sessions'
_kimi_share = os.environ.get('KIMI_SHARE_DIR', '').strip()
KIMI_DIR = Path(_kimi_share) if _kimi_share else Path.home() / '.kimi' / 'sessions'
DROID_DIR = Path.home() / '.factory' / 'sessions'
OPENCODE_DB = Path.home() / '.local' / 'share' / 'opencode' / 'opencode.db'
OPENCODE_STORAGE_DIR = Path.home() / '.local' / 'share' / 'opencode' / 'storage'

# ─── WSL directory discovery ───────────────────────────────────────────────────
WSL_PROJECTS_DIRS = []   # additional ~/.claude/projects from WSL distros
WSL_CODEX_DIRS = []      # additional ~/.codex/sessions from WSL distros
WSL_CODEX_ARCHIVED_DIRS = []  # additional ~/.codex/archived_sessions from WSL distros
WSL_OPENCLAW_DIRS = []   # additional ~/.openclaw/agents from WSL distros
WSL_KIMI_DIRS = []       # additional ~/.kimi/sessions from WSL distros
WSL_DROID_DIRS = []      # additional ~/.factory/sessions from WSL distros
WSL_HOMES = []           # discovered WSL home paths (//wsl.localhost/<distro>/home/...)
HERMES_BENCH_DIRS = []   # dirs holding eval run folders (e.g. <project>/results)
HERMES_SESSION_DIRS = [] # ~/.hermes/sessions (Windows + WSL homes)
HERMES_STATE = None      # {'distro': ..., 'db': ...} live SessionDB in WSL

def _discover_wsl_distros():
    """List installed WSL distro names (docker-desktop filtered out)."""
    if sys.platform != 'win32':
        return []
    try:
        result = subprocess.run(
            ['wsl', '-l', '-q'], capture_output=True, timeout=5,
        )
        raw = result.stdout
        try:
            text = raw.decode('utf-16-le')
        except (UnicodeDecodeError, ValueError):
            text = raw.decode('utf-8', errors='replace')
        distros = [d.strip().strip('\x00') for d in text.splitlines()
                   if d.strip().strip('\x00')]
        return [d for d in distros if 'docker' not in d.lower()]
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []


def _discover_wsl_homes():
    """Discover home directories from installed WSL distros."""
    if sys.platform != 'win32':
        return []
    try:
        result = subprocess.run(
            ['wsl', '-l', '-q'], capture_output=True, timeout=5,
        )
        raw = result.stdout
        # wsl -l outputs UTF-16LE on Windows
        try:
            text = raw.decode('utf-16-le')
        except (UnicodeDecodeError, ValueError):
            text = raw.decode('utf-8', errors='replace')
        distros = [d.strip().strip('\x00') for d in text.splitlines() if d.strip().strip('\x00')]
        # Filter out docker-desktop distros
        distros = [d for d in distros if 'docker' not in d.lower()]
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
        return []

    homes = []
    for distro in distros:
        try:
            result = subprocess.run(
                ['wsl', '-d', distro, '-e', 'bash', '-c', 'echo $HOME'],
                capture_output=True, text=True, timeout=5,
            )
            home = result.stdout.strip()
            if home:
                wsl_path = Path(f'//wsl.localhost/{distro}{home}')
                if wsl_path.exists():
                    homes.append(wsl_path)
        except (subprocess.TimeoutExpired, OSError):
            continue
    return homes

def _init_wsl_dirs():
    """Populate WSL_*_DIRS lists from discovered WSL home directories."""
    global WSL_PROJECTS_DIRS, WSL_CODEX_DIRS, WSL_CODEX_ARCHIVED_DIRS
    global WSL_OPENCLAW_DIRS, WSL_KIMI_DIRS, WSL_DROID_DIRS, WSL_HOMES
    WSL_HOMES = _discover_wsl_homes()
    for wsl_home in WSL_HOMES:
        proj = wsl_home / '.claude' / 'projects'
        if proj.exists():
            WSL_PROJECTS_DIRS.append(proj)
        codex = wsl_home / '.codex' / 'sessions'
        if codex.exists():
            WSL_CODEX_DIRS.append(codex)
        codex_archived = wsl_home / '.codex' / 'archived_sessions'
        if codex_archived.exists():
            WSL_CODEX_ARCHIVED_DIRS.append(codex_archived)
        openclaw = wsl_home / '.openclaw' / 'agents'
        if openclaw.exists():
            WSL_OPENCLAW_DIRS.append(openclaw)
        kimi = wsl_home / '.kimi' / 'sessions'
        if kimi.exists():
            WSL_KIMI_DIRS.append(kimi)
        droid = wsl_home / '.factory' / 'sessions'
        if droid.exists():
            WSL_DROID_DIRS.append(droid)
    total = (
        len(WSL_PROJECTS_DIRS) + len(WSL_CODEX_DIRS)
        + len(WSL_CODEX_ARCHIVED_DIRS) + len(WSL_OPENCLAW_DIRS)
        + len(WSL_KIMI_DIRS) + len(WSL_DROID_DIRS)
    )
    if total:
        print(f"WSL: found {total} additional session directories")

def _init_hermes_bench_dirs():
    """Populate HERMES_BENCH_DIRS with dirs that hold eval run folders.

    Each entry is a directory whose subdirectories are individual runs
    (e.g. <project>/results/<run-name>/). Override with the HERMES_BENCH_DIRS
    env var (os.pathsep-separated).
    """
    global HERMES_BENCH_DIRS
    env = os.environ.get('HERMES_BENCH_DIRS', '').strip()
    if env:
        for part in env.split(os.pathsep):
            p = Path(part.strip())
            if p.is_dir():
                HERMES_BENCH_DIRS.append(p)
        return
    for wsl_home in WSL_HOMES:
        for cand in sorted(wsl_home.glob('*/workspace/projects/*/results')):
            if cand.is_dir():
                HERMES_BENCH_DIRS.append(cand)
    if HERMES_BENCH_DIRS:
        print(f"Hermes bench: found {len(HERMES_BENCH_DIRS)} run directories")

def _init_hermes_session_dirs():
    """Populate HERMES_SESSION_DIRS with ~/.hermes/sessions locations.

    On WSL, ~/.hermes is often a symlink (e.g. into a sandbox home) that the
    //wsl.localhost file sharing does not follow, so a couple of common
    real-path candidates are probed as fallback. Override everything with the
    HERMES_SESSION_DIRS env var (os.pathsep-separated).
    """
    global HERMES_SESSION_DIRS, HERMES_STATE
    env = os.environ.get('HERMES_SESSION_DIRS', '').strip()
    cands = []
    if env:
        cands = [Path(p.strip()) for p in env.split(os.pathsep) if p.strip()]
    else:
        local = Path.home() / '.hermes' / 'sessions'
        if local.is_dir():
            cands.append(local)
        for wsl_home in WSL_HOMES:
            cands.extend([
                wsl_home / '.hermes' / 'sessions',
                wsl_home / 'hermes-cristalina-sandbox' / 'home' / 'sessions',
            ])
    for c in cands:
        if c.is_dir() and c not in HERMES_SESSION_DIRS:
            HERMES_SESSION_DIRS.append(c)
    if HERMES_SESSION_DIRS:
        print(f"Hermes sessions: found {len(HERMES_SESSION_DIRS)} session directories")

    # Locate the live SessionDB (state.db) inside WSL distros. It is read
    # through a bridge script executed inside WSL — the 3GB+ database must
    # not be touched over the 9P file share.
    for distro in _discover_wsl_distros():
        try:
            probe = subprocess.run(
                ['wsl', '-d', distro, '-e', 'bash', '-c',
                 '[ -f ~/.hermes/state.db ] && echo OK'],
                capture_output=True, text=True, timeout=8,
            )
            if 'OK' not in (probe.stdout or ''):
                continue
            path = subprocess.run(
                ['wsl', '-d', distro, '-e', 'bash', '-c',
                 'readlink -f ~/.hermes/state.db'],
                capture_output=True, text=True, timeout=8,
            ).stdout.strip()
            if path:
                HERMES_STATE = {'distro': distro, 'db': path}
                print(f"Hermes state.db: {distro}:{path}")
                break
        except (subprocess.TimeoutExpired, OSError):
            continue

try:
    _init_wsl_dirs()
except Exception as e:
    print(f"WSL discovery failed (non-fatal): {e}", file=sys.stderr)

try:
    _init_hermes_bench_dirs()
except Exception as e:
    print(f"Hermes bench discovery failed (non-fatal): {e}", file=sys.stderr)

try:
    _init_hermes_session_dirs()
except Exception as e:
    print(f"Hermes session discovery failed (non-fatal): {e}", file=sys.stderr)


def _wsl_hermes_bridge(args, timeout=15):
    """Run hermes_state_bridge.py inside WSL and return parsed JSON."""
    if not HERMES_STATE:
        return None
    p = str(SCRIPT_DIR / 'hermes_state_bridge.py').replace('\\', '/')
    m = re.match(r'([A-Za-z]):/(.*)', p)
    if not m:
        return None
    wsl_script = f'/mnt/{m.group(1).lower()}/{m.group(2)}'
    cmd = (['wsl', '-d', HERMES_STATE['distro'], '-e', 'python3',
            wsl_script, HERMES_STATE['db']]
           + [str(a) for a in args])
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout,
            stdin=subprocess.DEVNULL,
            encoding='utf-8', errors='replace',
        )
        out = (result.stdout or '').strip()
        if result.returncode != 0 or not out:
            return None
        return json.loads(out)
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError, ValueError):
        return None

ALL_CODEX_DIRS = [CODEX_DIR, CODEX_ARCHIVED_DIR] + WSL_CODEX_DIRS + WSL_CODEX_ARCHIVED_DIRS

def _find_rg():
    """Locate a ripgrep binary (PATH first, then known bundled copies)."""
    exe = shutil.which('rg')
    if exe:
        return exe
    for cand in (
        Path.home() / '.local' / 'share' / 'opencode' / 'bin' / 'rg.exe',
        Path.home() / 'scoop' / 'shims' / 'rg.exe',
    ):
        if cand.exists():
            return str(cand)
    return None

MODEL_CONTEXT_WINDOWS = {
    'opus-4': 200_000,
    'sonnet-4': 200_000,
    'haiku-4': 200_000,
    'sonnet-3': 200_000,
    'haiku-3': 200_000,
    'opus-3': 200_000,
    'o3': 200_000,
    'o4-mini': 200_000,
    'gpt-4.1': 1_047_576,
    'gpt-4o': 128_000,
    'gpt-5': 200_000,
    'kimi-k2.5': 131_072,
    'moonshot': 131_072,
}
MODEL_CONTEXT_DEFAULT = 200_000

# Allowed origins for CORS (localhost only)
ALLOWED_ORIGINS = {'http://localhost:37778', 'http://127.0.0.1:37778', 'null'}

# Truncation limits for session events
MAX_TOOL_RESULT_LEN = 2048
MAX_TOOL_INPUT_LEN = 1024
MAX_TEXT_LEN = 10240
TAIL_READ_BYTES = 512 * 1024  # 512KB for tail reads on large files


# ─── Session Index ─────────────────────────────────────────────────────────────

class SessionIndex:
    """Cached index of all Claude Code sessions. Rebuilds every 30s."""

    def __init__(self):
        self._sessions = {}
        self._subagents = {}   # parentId -> [subagent, ...]
        self._subagent_paths = {}  # filename_stem -> path
        self._projects = set()
        self._last_rebuild = 0
        self._rebuild_interval = 30
        self._lock = threading.Lock()
        self._rebuild_lock = threading.Lock()

    def _needs_rebuild(self):
        return time.time() - self._last_rebuild > self._rebuild_interval

    def ensure_fresh(self):
        if self._needs_rebuild():
            with self._rebuild_lock:
                if self._needs_rebuild():  # double-check under lock
                    self.rebuild()

    def rebuild(self):
        sessions = {}
        subagents = {}      # parentId -> [subagent_info, ...]
        subagent_paths = {} # filename_stem -> path str
        projects = set()

        # ── Scan ~/.claude/projects/ ─────────────────────────────────
        if PROJECTS_DIR.exists():
            for proj_dir in PROJECTS_DIR.iterdir():
                if not proj_dir.is_dir():
                    continue

                proj_name = proj_dir.name
                if 'observer' in proj_name.lower():
                    continue
                # Skip OpenClaw subagent workspace dirs (shown via parent's subagent panel)
                if 'openclaw-workspace' in proj_name:
                    continue

                # Clean project display name
                display = proj_name
                if display.startswith('-home-dev-'):
                    display = display[10:]
                elif display.startswith('-home-dev'):
                    display = display[9:] or 'home'
                display = display.replace('-', '/') if display else 'home'

                self._scan_session_dir(
                    proj_dir, display, sessions, subagents,
                    subagent_paths, projects, source='claude',
                )

        # ── Scan WSL ~/.claude/projects/ ────────────────────────────
        for wsl_proj_dir in WSL_PROJECTS_DIRS:
            if wsl_proj_dir.exists():
                for proj_dir in wsl_proj_dir.iterdir():
                    if not proj_dir.is_dir():
                        continue
                    proj_name = proj_dir.name
                    if 'observer' in proj_name.lower():
                        continue
                    if 'openclaw-workspace' in proj_name:
                        continue
                    display = proj_name
                    if display.startswith('-home-k1-'):
                        display = display[9:]
                    elif display.startswith('-home-k1'):
                        display = display[8:] or 'home'
                    elif display.startswith('-mnt-c-'):
                        display = display[7:]
                    display = display.replace('-', '/') if display else 'home'
                    display = 'wsl/' + display
                    self._scan_session_dir(
                        proj_dir, display, sessions, subagents,
                        subagent_paths, projects, source='claude',
                    )

        # ── Scan ~/.openclaw/agents/*/sessions/ ──────────────────────
        for openclaw_dir in [OPENCLAW_DIR] + WSL_OPENCLAW_DIRS:
            if openclaw_dir.exists():
                for agent_dir in openclaw_dir.iterdir():
                    if not agent_dir.is_dir():
                        continue
                    sess_dir = agent_dir / 'sessions'
                    if not sess_dir.is_dir():
                        continue
                    display = 'openclaw/' + agent_dir.name
                    self._scan_session_dir(
                        sess_dir, display, sessions, subagents,
                        subagent_paths, projects, source='openclaw',
                    )

        # ── Scan ~/.codex/sessions/ (Windows + WSL) ────────────────────
        for codex_dir in [CODEX_DIR, CODEX_ARCHIVED_DIR] + WSL_CODEX_DIRS + WSL_CODEX_ARCHIVED_DIRS:
            if codex_dir.exists():
                self._scan_codex_sessions(codex_dir, sessions, projects)

        # ── Scan ~/.kimi/sessions/ (Windows + WSL) ───────────────────
        for kimi_dir in [KIMI_DIR] + WSL_KIMI_DIRS:
            if kimi_dir.exists():
                self._scan_kimi_sessions(kimi_dir, sessions, projects)

        # ── Scan ~/.factory/sessions/ (Droid) (Windows + WSL) ────────
        for droid_dir in [DROID_DIR] + WSL_DROID_DIRS:
            if droid_dir.exists():
                self._scan_droid_sessions(droid_dir, sessions, projects)

        # ── Scan Hermes eval runs (WSL + HERMES_BENCH_DIRS) ──────────
        for bench_dir in HERMES_BENCH_DIRS:
            if bench_dir.exists():
                self._scan_hermes_bench_runs(bench_dir, sessions, projects)

        # Hermes agent conversation sessions (Windows + WSL)
        for hermes_dir in HERMES_SESSION_DIRS:
            if hermes_dir.exists():
                self._scan_hermes_sessions(hermes_dir, sessions, projects)

        # Live Hermes SessionDB (state.db inside WSL)
        if HERMES_STATE:
            self._scan_hermes_state_sessions(sessions, projects)

        # ── Scan OpenCode sessions (SQLite) ─────────────────────────
        if OPENCODE_DB.exists():
            self._scan_opencode_sessions(sessions, projects)
        legacy_sess_dir = OPENCODE_STORAGE_DIR / 'session'
        if legacy_sess_dir.is_dir():
            self._scan_opencode_legacy_sessions(legacy_sess_dir, sessions,
                                                projects)

        with self._lock:
            self._sessions = sessions
            self._subagents = subagents
            self._subagent_paths = subagent_paths
            self._projects = projects
            self._last_rebuild = time.time()

    def _scan_session_dir(self, sess_dir, display, sessions, subagents,
                          subagent_paths, projects, source='claude'):
        """Scan a directory for UUID-named .jsonl session files."""
        for jsonl_file in sess_dir.glob('*.jsonl'):
            sid = jsonl_file.stem
            if not re.match(
                r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
                sid,
            ):
                continue

            try:
                st = jsonl_file.stat()
                mtime = st.st_mtime
                size = st.st_size
            except OSError:
                continue

            if size == 0:
                continue

            meta = self._extract_metadata(jsonl_file, size, source)
            if meta is None:
                continue

            projects.add(display)
            age = time.time() - mtime
            active = age < 120 and not meta.get('has_stop', False)

            # Scan subagents for this session (on-disk)
            sub_dir = sess_dir / sid / 'subagents'
            sub_list = []
            if sub_dir.is_dir():
                for sa_file in sub_dir.glob('*.jsonl'):
                    sa_info = self._extract_subagent_info(sa_file, sid)
                    if sa_info:
                        sub_list.append(sa_info)
                        subagent_paths[sa_file.stem] = str(sa_file)
                sub_list.sort(key=lambda x: x['mtime'], reverse=True)

            if sub_list:
                subagents[sid] = sub_list

            # Count inline subagents (OpenClaw sessions_spawn)
            inline_count = meta.get('inline_subagent_count', 0)

            # Duration
            first_ts = meta.get('first_ts')
            duration = int(mtime - first_ts) if first_ts else 0

            # Context %
            last_input = meta.get('last_input_tokens', 0)
            model = meta.get('model', '')
            ctx_size = self._get_context_size(model)
            context_pct = round((last_input / ctx_size) * 100) if ctx_size and last_input else 0

            sessions[sid] = {
                'sessionId': sid,
                'project': display,
                'source': source,
                'slug': meta.get('slug', ''),
                'status': 'active' if active else 'completed',
                'model': meta.get('model', ''),
                'version': meta.get('version', ''),
                'mtime': mtime,
                'size': size,
                'eventCount': meta.get('event_count', 0),
                'cwd': meta.get('cwd', ''),
                'gitBranch': meta.get('gitBranch', ''),
                'subagentCount': len(sub_list) + inline_count,
                'duration': duration,
                'contextPct': context_pct,
                '_path': str(jsonl_file),
            }

    # ── metadata helpers ──────────────────────────────────────────────────

    @staticmethod
    def _get_context_size(model):
        """Get context window size for a model via substring match."""
        if not model:
            return MODEL_CONTEXT_DEFAULT
        m = model.lower()
        for key, size in MODEL_CONTEXT_WINDOWS.items():
            if key in m:
                return size
        return MODEL_CONTEXT_DEFAULT

    @staticmethod
    def _extract_metadata(path, size, source='claude'):
        meta = {'event_count': max(1, size // 500)}

        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                # First few lines
                for _ in range(64 if source == 'claude' else 8):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')

                        # Capture first timestamp
                        if 'first_ts' not in meta:
                            ts = obj.get('timestamp')
                            if ts:
                                if isinstance(ts, str):
                                    try:
                                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                        meta['first_ts'] = dt.timestamp()
                                    except (ValueError, TypeError):
                                        pass
                                elif isinstance(ts, (int, float)):
                                    meta['first_ts'] = ts / 1000 if ts > 1e12 else ts

                        # Claude Code native format
                        if not meta.get('slug'):
                            meta['slug'] = obj.get('slug', '')
                        if source == 'claude':
                            if t == 'custom-title':
                                title = obj.get('customTitle')
                                if isinstance(title, str) and title.strip():
                                    meta['slug'] = title.strip().replace('\n', ' ')[:80]
                                    meta['_custom_title'] = True
                            elif t == 'ai-title' and not meta.get('_custom_title'):
                                title = obj.get('aiTitle')
                                if isinstance(title, str) and title.strip():
                                    meta['slug'] = title.strip().replace('\n', ' ')[:80]
                            elif t == 'agent-name' and not meta.get('slug'):
                                title = obj.get('agentName')
                                if isinstance(title, str):
                                    meta['slug'] = title.strip().replace('\n', ' ')[:80]
                        if not meta.get('cwd'):
                            meta['cwd'] = obj.get('cwd', '')
                        if not meta.get('gitBranch'):
                            meta['gitBranch'] = obj.get('gitBranch', '')

                        if t == 'assistant' and not meta.get('model'):
                            meta['model'] = obj.get('message', {}).get('model', '')

                        # OpenClaw format: first line is type=session
                        if t == 'session':
                            if not meta.get('cwd'):
                                meta['cwd'] = obj.get('cwd', '')
                            ver = obj.get('version', '')
                            if ver and not meta.get('version'):
                                meta['version'] = str(ver)
                            meta['_openclaw'] = True

                        # OpenClaw message format
                        if t == 'message':
                            msg = obj.get('message', {})
                            role = msg.get('role', '')
                            if role == 'assistant' and not meta.get('model'):
                                model = msg.get('model', '')
                                if model and model != 'delivery-mirror':
                                    meta['model'] = model

                        # Claude Code version string
                        if not meta.get('version') and isinstance(obj.get('version'), str):
                            meta['version'] = obj['version']

                    except (json.JSONDecodeError, KeyError):
                        pass

                # Last few lines via seek
                seek_back = min(size, 8192)
                f.seek(max(0, size - seek_back))
                if size > seek_back:
                    f.readline()  # discard partial
                tail = f.read()

                for line in reversed(tail.strip().split('\n')):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')
                        if t == 'system' and obj.get('subtype') == 'stop_hook_summary':
                            meta['has_stop'] = True

                        # Capture last input_tokens from assistant messages
                        # Total context = input_tokens + cache_read + cache_creation
                        if 'last_input_tokens' not in meta:
                            usage = None
                            if t == 'assistant':
                                usage = obj.get('message', {}).get('usage', {})
                            elif t == 'message':
                                msg = obj.get('message', {})
                                if msg.get('role') == 'assistant':
                                    usage = msg.get('usage', {})
                            if usage and isinstance(usage, dict):
                                it = (usage.get('input_tokens', 0)
                                      + usage.get('cache_read_input_tokens', 0)
                                      + usage.get('cache_creation_input_tokens', 0)
                                      + usage.get('cacheRead', 0)
                                      + usage.get('cacheWrite', 0))
                                if it:
                                    meta['last_input_tokens'] = it

                        # Always take model from tail (most recent)
                        if not meta.get('_tail_model'):
                            if t == 'assistant':
                                m = obj.get('message', {}).get('model', '')
                                if m:
                                    meta['model'] = m
                                    meta['_tail_model'] = True
                            elif t == 'message':
                                msg = obj.get('message', {})
                                if msg.get('role') == 'assistant':
                                    m = msg.get('model', '')
                                    if m and m != 'delivery-mirror':
                                        meta['model'] = m
                                        meta['_tail_model'] = True
                        if not meta.get('slug') and obj.get('slug'):
                            meta['slug'] = obj['slug']

                        # Stop once we have input_tokens (or model as fallback)
                        if 'last_input_tokens' in meta and (meta.get('model') or meta.get('slug')):
                            break
                    except (json.JSONDecodeError, KeyError):
                        pass

        except OSError:
            return None

        # Lightweight count of inline subagents (accepted sessions_spawn only)
        if meta.get('_openclaw'):
            try:
                count = 0
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        if 'sessions_spawn' in line and '"accepted"' in line:
                            count += 1
                if count:
                    meta['inline_subagent_count'] = count
            except OSError:
                pass

        return meta

    @staticmethod
    def _extract_subagent_info(path, parent_id):
        """Extract info from a subagent JSONL file."""
        try:
            st = path.stat()
            size = st.st_size
            mtime = st.st_mtime
        except OSError:
            return None

        if size == 0:
            return None

        filename = path.stem  # e.g. agent-aprompt_suggestion-fcffe7
        # Parse agent type from filename
        agent_type = 'task-agent'
        if 'prompt_suggestion' in filename:
            agent_type = 'prompt_suggestion'
        elif 'compact' in filename:
            agent_type = 'compact'

        info = {
            'id': filename,
            'parentId': parent_id,
            'agentType': agent_type,
            'filename': path.name,
            'size': size,
            'mtime': mtime,
            'eventCount': max(1, size // 500),
            'slug': '',
            'model': '',
        }

        # Read first few lines for metadata
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if not info['slug']:
                            info['slug'] = obj.get('slug', '')
                        if not info['model'] and obj.get('type') == 'assistant':
                            info['model'] = obj.get('message', {}).get('model', '')
                    except (json.JSONDecodeError, KeyError):
                        pass
        except OSError:
            pass

        # Determine active state
        age = time.time() - mtime
        info['status'] = 'active' if age < 120 else 'completed'

        return info

    # ── Codex session scanning ─────────────────────────────────────────────

    def _scan_codex_sessions(self, base_dir, sessions, projects):
        """Scan ~/.codex/sessions/ for rollout-*.jsonl files."""
        for jsonl_file in base_dir.rglob('rollout-*.jsonl'):
            try:
                st = jsonl_file.stat()
                mtime = st.st_mtime
                size = st.st_size
            except OSError:
                continue
            if size == 0:
                continue

            meta = self._extract_codex_metadata(jsonl_file, size)
            if meta is None:
                continue

            sid = 'codex-' + meta.get('id', jsonl_file.stem)
            cwd = meta.get('cwd', '')
            # Project display: codex/ + relative cwd
            home_prefix = str(Path.home()) + '/'
            rel = cwd.replace(home_prefix, '').rstrip('/') if cwd else ''
            display = 'codex/' + rel if rel else 'codex'
            projects.add(display)

            age = time.time() - mtime
            active = age < 120 and not meta.get('has_complete', False)

            # Duration
            first_ts = meta.get('first_ts')
            duration = int(mtime - first_ts) if first_ts else 0

            # Context %
            last_input = meta.get('last_input_tokens', 0)
            model = meta.get('model', '')
            ctx_size = self._get_context_size(model)
            context_pct = round((last_input / ctx_size) * 100) if ctx_size and last_input else 0

            sessions[sid] = {
                'sessionId': sid,
                'project': display,
                'source': 'codex',
                'slug': meta.get('slug', ''),
                'status': 'active' if active else 'completed',
                'model': meta.get('model', ''),
                'version': meta.get('version', ''),
                'mtime': mtime,
                'size': size,
                'eventCount': meta.get('event_count', 0),
                'cwd': cwd,
                'gitBranch': meta.get('gitBranch', ''),
                'subagentCount': 0,
                'duration': duration,
                'contextPct': context_pct,
                '_path': str(jsonl_file),
            }

    @staticmethod
    def _extract_codex_metadata(path, size):
        meta = {'event_count': max(1, size // 500)}
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                # Read first ~30 lines for session_meta, turn_context, first user msg
                for _ in range(30):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')
                        payload = obj.get('payload', {})
                        if not isinstance(payload, dict):
                            continue

                        # Capture first timestamp
                        if 'first_ts' not in meta:
                            ts = raw_ts = obj.get('timestamp', '')
                            if ts:
                                if isinstance(ts, str):
                                    try:
                                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                        meta['first_ts'] = dt.timestamp()
                                    except (ValueError, TypeError):
                                        pass
                                elif isinstance(ts, (int, float)):
                                    meta['first_ts'] = ts / 1000 if ts > 1e12 else ts

                        if t == 'session_meta':
                            # Only use the FIRST session_meta's id — subagent
                            # files may include the parent's session_meta as
                            # line 1, which would overwrite the subagent's own
                            # id and cause the parent session to be shadowed.
                            if 'id' not in meta:
                                meta['id'] = payload.get('id', '')
                            if not meta.get('cwd'):
                                meta['cwd'] = payload.get('cwd', '')
                            if not meta.get('version'):
                                meta['version'] = payload.get('cli_version', '')
                            git = payload.get('git', {})
                            if isinstance(git, dict) and not meta.get('gitBranch'):
                                meta['gitBranch'] = git.get('branch', '')

                        elif t == 'turn_context':
                            if not meta.get('model'):
                                meta['model'] = payload.get('model', '')

                        elif t == 'event_msg':
                            pt = payload.get('type', '')
                            # event_msg/user_message has the actual user-typed text
                            if pt == 'user_message' and not meta.get('slug'):
                                text = payload.get('message', '').strip()
                                if text:
                                    meta['slug'] = text[:80].replace('\n', ' ').strip()

                        elif t == 'response_item':
                            pt = payload.get('type', '')
                            # Fallback: use first real user message as slug
                            if pt == 'message' and payload.get('role') == 'user':
                                if not meta.get('slug'):
                                    content = payload.get('content', [])
                                    if isinstance(content, list):
                                        for block in content:
                                            if isinstance(block, dict) and block.get('type') == 'input_text':
                                                text = block.get('text', '').strip()
                                                # Skip system/developer injected content
                                                if (text.startswith('#') or text.startswith('<')
                                                        or len(text) > 500 or not text):
                                                    continue
                                                meta['slug'] = text[:80].replace('\n', ' ').strip()
                                                break

                    except (json.JSONDecodeError, KeyError):
                        pass

                # Tail read for task_complete and model
                seek_back = min(size, 4096)
                f.seek(max(0, size - seek_back))
                if size > seek_back:
                    f.readline()
                tail = f.read()

                for line in reversed(tail.strip().split('\n')):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')
                        payload = obj.get('payload', {})
                        if not isinstance(payload, dict):
                            continue
                        if t == 'event_msg' and payload.get('type') == 'task_complete':
                            meta['has_complete'] = True
                        if not meta.get('_tail_model') and t == 'turn_context':
                            m = payload.get('model', '')
                            if m:
                                meta['model'] = m
                                meta['_tail_model'] = True
                        if meta.get('has_complete') and meta.get('model'):
                            break
                    except (json.JSONDecodeError, KeyError):
                        pass

        except OSError:
            return None

        return meta

    # ── Kimi session scanning ─────────────────────────────────────────────

    def _scan_kimi_sessions(self, base_dir, sessions, projects):
        """Scan ~/.kimi/sessions/{workdir_md5}/{session_id}/ for wire.jsonl files."""
        if not base_dir.exists():
            return
        for hash_dir in base_dir.iterdir():
            if not hash_dir.is_dir():
                continue
            for sess_dir in hash_dir.iterdir():
                if not sess_dir.is_dir():
                    continue
                wire_path = sess_dir / 'wire.jsonl'
                if not wire_path.exists():
                    continue
                try:
                    st = wire_path.stat()
                    mtime = st.st_mtime
                    size = st.st_size
                except OSError:
                    continue
                if size == 0:
                    continue

                meta = self._extract_kimi_metadata(sess_dir, size)
                if meta is None:
                    continue

                sid = 'kimi-' + sess_dir.name
                cwd = meta.get('cwd', '')
                home_prefix = str(Path.home()) + '/'
                rel = cwd.replace(home_prefix, '').rstrip('/') if cwd else ''
                display = 'kimi/' + rel if rel else 'kimi/' + hash_dir.name
                projects.add(display)

                age = time.time() - mtime
                active = age < 120 and not meta.get('has_turn_end', False)

                first_ts = meta.get('first_ts')
                duration = int(mtime - first_ts) if first_ts else 0

                last_input = meta.get('last_input_tokens', 0)
                model = meta.get('model', '')
                ctx_size = self._get_context_size(model)
                context_pct = round((last_input / ctx_size) * 100) if ctx_size and last_input else 0

                sessions[sid] = {
                    'sessionId': sid,
                    'project': display,
                    'source': 'kimi',
                    'slug': meta.get('slug', ''),
                    'status': 'active' if active else 'completed',
                    'model': model,
                    'version': meta.get('version', ''),
                    'mtime': mtime,
                    'size': size,
                    'eventCount': meta.get('event_count', 0),
                    'cwd': cwd,
                    'gitBranch': meta.get('gitBranch', ''),
                    'subagentCount': 0,
                    'duration': duration,
                    'contextPct': context_pct,
                    '_path': str(wire_path),
                }

    @staticmethod
    def _extract_kimi_metadata(sess_dir, wire_size):
        """Extract metadata from a Kimi session directory (wire.jsonl + context.jsonl)."""
        meta = {'event_count': max(1, wire_size // 500)}
        wire_path = sess_dir / 'wire.jsonl'
        context_path = sess_dir / 'context.jsonl'

        try:
            # Head scan wire.jsonl for TurnBegin (model)
            with open(wire_path, 'r', encoding='utf-8', errors='replace') as f:
                for _ in range(30):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')

                        if 'first_ts' not in meta:
                            ts = obj.get('timestamp')
                            if ts:
                                if isinstance(ts, str):
                                    try:
                                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                        meta['first_ts'] = dt.timestamp()
                                    except (ValueError, TypeError):
                                        pass
                                elif isinstance(ts, (int, float)):
                                    meta['first_ts'] = ts / 1000 if ts > 1e12 else ts

                        if t == 'TurnBegin' and not meta.get('model'):
                            meta['model'] = obj.get('model', '') or (obj.get('payload') or {}).get('model', '')
                        if t == 'TurnBegin' and not meta.get('version'):
                            meta['version'] = obj.get('version', '') or (obj.get('payload') or {}).get('version', '')
                    except (json.JSONDecodeError, KeyError):
                        pass

                # Tail scan wire.jsonl for TurnEnd
                seek_back = min(wire_size, 8192)
                f.seek(max(0, wire_size - seek_back))
                if wire_size > seek_back:
                    f.readline()
                tail = f.read()

                for line in reversed(tail.strip().split('\n')):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')
                        if t == 'TurnEnd':
                            meta['has_turn_end'] = True
                            usage = obj.get('usage') or (obj.get('payload') or {}).get('usage', {})
                            if usage and isinstance(usage, dict):
                                it = (usage.get('input_tokens', 0)
                                      + usage.get('cache_read_input_tokens', 0)
                                      + usage.get('cache_creation_input_tokens', 0))
                                if it and 'last_input_tokens' not in meta:
                                    meta['last_input_tokens'] = it
                        if not meta.get('_tail_model') and t == 'TurnBegin':
                            m = obj.get('model', '') or (obj.get('payload') or {}).get('model', '')
                            if m:
                                meta['model'] = m
                                meta['_tail_model'] = True
                        if meta.get('has_turn_end') and meta.get('model'):
                            break
                    except (json.JSONDecodeError, KeyError):
                        pass

            # Head scan context.jsonl for cwd, first user prompt (slug)
            if context_path.exists():
                with open(context_path, 'r', encoding='utf-8', errors='replace') as f:
                    for _ in range(10):
                        line = f.readline()
                        if not line:
                            break
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            if not meta.get('cwd') and obj.get('cwd'):
                                meta['cwd'] = obj['cwd']
                            if not meta.get('gitBranch') and obj.get('gitBranch'):
                                meta['gitBranch'] = obj['gitBranch']
                            if not meta.get('slug'):
                                role = obj.get('role', '')
                                if role == 'user':
                                    content = obj.get('content', '')
                                    if isinstance(content, str) and content:
                                        if (len(content) <= 500
                                                and not content.startswith('#')
                                                and not content.startswith('<')):
                                            meta['slug'] = content[:80].replace('\n', ' ').strip()
                        except (json.JSONDecodeError, KeyError):
                            pass

        except OSError:
            return None

        return meta

    # ── Droid session scanning (.factory/sessions) ──────────────────────

    @staticmethod
    def _clean_droid_project_display(name):
        display = name
        if display.startswith('-home-dev-'):
            display = display[10:]
        elif display.startswith('-home-dev'):
            display = display[9:] or 'home'
        elif display.startswith('-home-k1-'):
            display = display[9:]
        elif display.startswith('-home-k1'):
            display = display[8:] or 'home'
        elif display.startswith('-mnt-c-'):
            display = display[7:]
        return 'droid/' + (display.replace('-', '/') if display else 'home')

    @staticmethod
    def _droid_display_from_cwd(cwd):
        if not cwd:
            return 'droid'
        norm = cwd.replace('\\', '/').strip()
        home = str(Path.home()).replace('\\', '/').rstrip('/') + '/'
        if norm.startswith(home):
            norm = norm[len(home):]
        elif norm.startswith('/mnt/c/Users/Markus/'):
            norm = norm[len('/mnt/c/Users/Markus/'):]
        elif norm.startswith('/home/k1/'):
            norm = norm[len('/home/k1/'):]
        norm = norm.strip('/')
        return 'droid/' + norm if norm else 'droid'

    def _scan_droid_file(self, jsonl_file, display, sessions, projects):
        sid_raw = jsonl_file.stem
        if not re.match(
            r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
            sid_raw,
        ):
            return

        try:
            st = jsonl_file.stat()
            mtime = st.st_mtime
            size = st.st_size
        except OSError:
            return
        if size == 0:
            return

        meta = self._extract_droid_metadata(jsonl_file, size)
        if meta is None:
            return

        project_display = display or self._droid_display_from_cwd(meta.get('cwd', ''))
        sid = 'droid-' + sid_raw
        projects.add(project_display)

        age = time.time() - mtime
        active = age < 120

        first_ts = meta.get('first_ts')
        duration = int(mtime - first_ts) if first_ts else 0

        last_input = meta.get('last_input_tokens', 0)
        model = meta.get('model', '')
        ctx_size = self._get_context_size(model)
        context_pct = round((last_input / ctx_size) * 100) if ctx_size and last_input else 0

        sessions[sid] = {
            'sessionId': sid,
            'project': project_display,
            'source': 'droid',
            'slug': meta.get('slug', ''),
            'status': 'active' if active else 'completed',
            'model': model,
            'version': meta.get('version', ''),
            'mtime': mtime,
            'size': size,
            'eventCount': meta.get('event_count', 0),
            'cwd': meta.get('cwd', ''),
            'gitBranch': meta.get('gitBranch', ''),
            'subagentCount': 0,
            'duration': duration,
            'contextPct': context_pct,
            '_path': str(jsonl_file),
        }

    def _scan_droid_sessions(self, base_dir, sessions, projects):
        """Scan ~/.factory/sessions/ for flat or project-nested UUID .jsonl files."""
        if not base_dir.exists():
            return

        for jsonl_file in base_dir.glob('*.jsonl'):
            self._scan_droid_file(jsonl_file, None, sessions, projects)

        for proj_dir in base_dir.iterdir():
            if not proj_dir.is_dir():
                continue

            display = self._clean_droid_project_display(proj_dir.name)

            for jsonl_file in proj_dir.glob('*.jsonl'):
                self._scan_droid_file(jsonl_file, display, sessions, projects)

    @staticmethod
    def _extract_droid_metadata(path, size):
        """Extract metadata from a Droid (.factory) session JSONL file."""
        meta = {'event_count': max(1, size // 500)}

        # Try reading model from companion .settings.json
        settings_path = path.with_suffix('.settings.json')
        if settings_path.exists():
            try:
                with open(settings_path, 'r', encoding='utf-8', errors='replace') as sf:
                    settings = json.loads(sf.read())
                    m = settings.get('model', '')
                    if m:
                        meta['model'] = m
            except (OSError, json.JSONDecodeError):
                pass

        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for _ in range(15):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        t = obj.get('type', '')

                        # Capture first timestamp
                        if 'first_ts' not in meta:
                            ts = obj.get('timestamp')
                            if ts:
                                if isinstance(ts, str):
                                    try:
                                        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                                        meta['first_ts'] = dt.timestamp()
                                    except (ValueError, TypeError):
                                        pass
                                elif isinstance(ts, (int, float)):
                                    meta['first_ts'] = ts / 1000 if ts > 1e12 else ts

                        # session_start has title, sessionTitle, cwd
                        if t == 'session_start':
                            if not meta.get('slug'):
                                # Prefer sessionTitle (cleaner) over title (full prompt)
                                stitle = obj.get('sessionTitle', '')
                                title = obj.get('title', '')
                                chosen = stitle if stitle and stitle not in ('New Session', 'new session') else title
                                if chosen and chosen not in ('New Session', 'new session'):
                                    meta['slug'] = chosen[:80].replace('\n', ' ').strip()
                            if not meta.get('cwd'):
                                meta['cwd'] = obj.get('cwd', '')
                            if not meta.get('version'):
                                ver = obj.get('version', '')
                                if ver:
                                    meta['version'] = str(ver)

                        # message type with assistant role may have model
                        if t == 'message':
                            msg = obj.get('message', {})
                            role = msg.get('role', '')
                            if role == 'assistant' and not meta.get('model'):
                                m = msg.get('model', '')
                                if m:
                                    meta['model'] = m
                            # User message as slug fallback
                            if role == 'user' and not meta.get('slug'):
                                content = msg.get('content', [])
                                if isinstance(content, list):
                                    for block in content:
                                        if isinstance(block, dict) and block.get('type') == 'text':
                                            text = block.get('text', '').strip()
                                            if text and not meta.get('cwd'):
                                                m = re.search(r'Current folder:\s*([^\r\n]+)', text)
                                                if m:
                                                    meta['cwd'] = m.group(1).strip()
                                            if text and not meta.get('gitBranch'):
                                                m = re.search(r'Current branch:\s*([^\r\n]+)', text)
                                                if m:
                                                    meta['gitBranch'] = m.group(1).strip()
                                            if (text and len(text) <= 500
                                                    and not text.startswith('<')
                                                    and not text.startswith('#')):
                                                meta['slug'] = text[:80].replace('\n', ' ').strip()
                                                break

                    except (json.JSONDecodeError, KeyError):
                        pass

                # Tail scan for last model (only if not already found in settings.json)
                if not meta.get('model'):
                    seek_back = min(size, 8192)
                    f.seek(max(0, size - seek_back))
                    if size > seek_back:
                        f.readline()
                    tail = f.read()

                    for line in reversed(tail.strip().split('\n')):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            t = obj.get('type', '')

                            if t == 'message':
                                msg = obj.get('message', {})
                                if msg.get('role') == 'assistant':
                                    m = msg.get('model', '')
                                    if m:
                                        meta['model'] = m
                                        break
                        except (json.JSONDecodeError, KeyError):
                            pass

        except OSError:
            return None

        return meta

    # ── Hermes benchmark run scanning ──────────────────────────────────────

    @staticmethod
    def _read_json_file(path):
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def _scan_hermes_bench_runs(self, base_dir, sessions, projects):
        """Scan a results directory for Hermes eval runs.

        A run is any subdirectory containing a run_manifest.json /
        recovery_manifest.json or predictions/retry JSONL output.
        """
        if not base_dir.exists():
            return
        project_name = base_dir.parent.name
        display = 'hermes/' + project_name
        projects.add(display)

        for run_dir in base_dir.iterdir():
            if not run_dir.is_dir():
                continue

            manifest = self._read_json_file(run_dir / 'run_manifest.json')
            recovery = None
            if manifest is None:
                recovery = self._read_json_file(run_dir / 'recovery_manifest.json')
                if recovery is None:
                    continue

            try:
                st = run_dir.stat()
                mtime = st.st_mtime
            except OSError:
                continue

            watcher = self._read_json_file(run_dir / '.watcher_state.json') or {}
            progress = self._read_json_file(run_dir / 'progress.json') or {}

            # Model: prefer explicit model path from manifests
            raw_model = ''
            if isinstance(manifest, dict):
                raw_model = manifest.get('model', '')
            elif isinstance(recovery, dict):
                raw_model = recovery.get('model', '')
            model = Path(raw_model.replace('\\', '/')).name if raw_model else ''

            # Progress: take the freshest of watcher_state / progress.json
            candidates_done = [
                v for v in (
                    watcher.get('done', watcher.get('completed')),
                    progress.get('processed_count', progress.get('completed')),
                ) if isinstance(v, (int, float))
            ]
            done = int(max(candidates_done)) if candidates_done else 0
            total = watcher.get('total',
                    progress.get('total_count', progress.get('total')))
            status = watcher.get('status') or progress.get('status') or ''
            accuracy = watcher.get('accuracy', watcher.get('merged_accuracy'))

            # Report score (full eval runs)
            report = None
            reports_dir = run_dir / 'reports'
            if reports_dir.is_dir():
                for rep_file in sorted(reports_dir.rglob('*.json')):
                    report = self._read_json_file(rep_file)
                    if report and report.get('score') is not None:
                        break
            if report and accuracy is None:
                accuracy = report.get('score')

            report_num = (report or {}).get('num')
            if isinstance(report_num, (int, float)):
                if not total:
                    total = int(report_num)
                # A published report means all samples were processed
                if done < report_num:
                    done = int(report_num)
            if isinstance(total, (int, float)) and done >= total > 0:
                status = 'completed'

            pretty = (report or {}).get('dataset_pretty_name', '')
            bench_name = (manifest or {}).get('benchmark', '') if isinstance(manifest, dict) else ''

            # Slug: benchmark + score when available
            done_total = f"{done}/{total if total is not None else '?'}"
            kind = (recovery or {}).get('kind', '') if isinstance(recovery, dict) else ''
            if kind == 'conditional_truncation_recovery':
                slug = f"truncation-recovery {done_total}"
                if accuracy is not None:
                    slug += f" · {accuracy:.1%} merged"
            elif accuracy is not None:
                slug = f"{pretty or bench_name}: {accuracy:.1%} ({done_total})"
            elif status == 'running':
                slug = f"{pretty or bench_name}: running {done_total}"
            else:
                slug = pretty or bench_name or project_name

            active = status == 'running' or (time.time() - mtime) < 120

            # Size + event count from prediction/retry JSONL files
            size = 0
            pred_files = []
            preds_dir = run_dir / 'predictions'
            retry_file = run_dir / 'retry_results.jsonl'
            if preds_dir.is_dir():
                pred_files = [p for p in sorted(preds_dir.rglob('*.jsonl'))]
            if not pred_files and retry_file.exists():
                pred_files = [retry_file]
            event_count = 0
            size_files = list(pred_files)
            if reports_dir.is_dir():
                size_files.extend(p for p in reports_dir.rglob('*') if p.is_file())
            latest_artifact = mtime
            for pf in size_files:
                try:
                    st_pf = pf.stat()
                    size += st_pf.st_size
                    if st_pf.st_mtime > latest_artifact:
                        latest_artifact = st_pf.st_mtime
                except OSError:
                    continue
            if pred_files:
                try:
                    with open(pred_files[0], 'rb') as f:
                        event_count = sum(1 for _ in f)
                except OSError:
                    event_count = max(1, size // 800)

            # Duration approximated from manifest mtime (written at run start)
            first_ts = None
            for probe in ('run_manifest.json', 'recovery_manifest.json'):
                p = run_dir / probe
                if p.exists():
                    try:
                        first_ts = p.stat().st_mtime
                    except OSError:
                        pass
                    break
            duration = int(latest_artifact - first_ts) if first_ts else 0

            sid = f'bench-{project_name}-{run_dir.name}'
            sessions[sid] = {
                'sessionId': sid,
                'project': display,
                'source': 'hermes-bench',
                'slug': slug,
                'status': 'active' if active else 'completed',
                'model': model,
                'version': (manifest or {}).get('evalscope_version', '') if isinstance(manifest, dict) else '',
                'mtime': mtime,
                'size': size,
                'eventCount': event_count,
                'cwd': (manifest or {}).get('api_url', '') if isinstance(manifest, dict) else '',
                'gitBranch': '',
                'subagentCount': 0,
                'duration': duration,
                'contextPct': 0,
                '_path': str(run_dir),
            }

    # ── Hermes agent session scanning (~/.hermes/sessions) ────────────────

    _hermes_meta_cache = {}  # {path: (mtime, size, meta_dict)}

    @classmethod
    def _extract_hermes_metadata(cls, path, st):
        """Cheap head/tail regex extraction for a Hermes session JSON.

        Results are cached per (mtime, size) so the 30s rebuild only pays
        full parsing for new or changed files.
        """
        cached = cls._hermes_meta_cache.get(str(path))
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]

        meta = {}
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                head = f.read(64 * 1024)
                for key in ('model', 'platform', 'session_start'):
                    m = re.search(rf'"{key}":\s*"([^"]*)"', head)
                    if m:
                        meta[key] = m.group(1)

                # Slug: last short "content" string in the file tail
                f.seek(max(0, st.st_size - 16 * 1024))
                tail = f.read(st.st_size)
            best = ''
            for m in re.finditer(r'"content":\s*"((?:[^"\\]|\\.)*)"', tail):
                text = m.group(1)
                if len(text) > 200:
                    continue
                try:
                    text = json.loads(f'"{text}"')
                except (json.JSONDecodeError, ValueError):
                    pass
                if 3 <= len(text) <= 120 and not text.startswith('<'):
                    best = text
            if not best:
                m = re.search(r'"content":\s*"((?:[^"\\]|\\.)*)"', tail)
                if m:
                    best = m.group(1)[:80]
            meta['slug'] = best.replace('\n', ' ').strip()
        except OSError:
            return None

        cls._hermes_meta_cache[str(path)] = (st.st_mtime, st.st_size, meta)
        if len(cls._hermes_meta_cache) > 5000:
            # Keep the newest half when the cache grows unbounded
            keys = list(cls._hermes_meta_cache.keys())
            for k in keys[:len(keys) // 2]:
                del cls._hermes_meta_cache[k]
        return meta

    def _scan_hermes_sessions(self, base_dir, sessions, projects):
        """Scan a ~/.hermes/sessions directory for conversation JSON files."""
        if not base_dir.exists():
            return

        for f in base_dir.glob('session_*.json'):
            if f.name == 'sessions.json':
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if st.st_size == 0:
                continue

            meta = self._extract_hermes_metadata(f, st)
            if meta is None:
                continue

            sid = 'hermes-' + f.stem
            platform = meta.get('platform', '')
            display = 'hermes/' + platform if platform else 'hermes'
            projects.add(display)

            age = time.time() - st.st_mtime
            first_ts = None
            start = meta.get('session_start')
            if start:
                try:
                    first_ts = datetime.fromisoformat(start).timestamp()
                except (ValueError, TypeError):
                    pass

            sessions[sid] = {
                'sessionId': sid,
                'project': display,
                'source': 'hermes',
                'slug': meta.get('slug', '')[:80],
                'status': 'active' if age < 300 else 'completed',
                'model': meta.get('model', ''),
                'version': '',
                'mtime': st.st_mtime,
                'size': st.st_size,
                'eventCount': max(1, st.st_size // 800),
                'cwd': '',
                'gitBranch': '',
                'subagentCount': 0,
                'duration': int(st.st_mtime - first_ts) if first_ts else 0,
                'contextPct': 0,
                '_path': str(f),
            }

    def _scan_hermes_state_sessions(self, sessions, projects):
        """Scan the live Hermes SessionDB (via WSL bridge)."""
        rows = _wsl_hermes_bridge(['list', 500], timeout=30)
        if not isinstance(rows, list):
            return
        now = time.time()
        for r in rows:
            sid_raw = str(r.get('id') or '')
            if not re.match(r'^[A-Za-z0-9_.-]+$', sid_raw):
                continue
            sid = 'hs-' + sid_raw

            source = r.get('source') or ''
            display = 'hermes/' + source if source else 'hermes'
            projects.add(display)

            started = r.get('started_at') or 0
            ended = r.get('ended_at')
            active = ended is None or (now - ended) < 120

            tokens_in = r.get('input_tokens') or 0
            cache_read = r.get('cache_read_tokens') or 0
            model = r.get('model') or ''
            ctx_size = self._get_context_size(model)
            context_pct = (round((tokens_in + cache_read) / ctx_size * 100)
                           if ctx_size and tokens_in else 0)

            slug = ((r.get('title') or '').strip()
                    or (r.get('last_activity') or '').strip()
                    or model)
            try:
                mtime = float(ended or started or 0)
            except (TypeError, ValueError):
                mtime = 0

            sessions[sid] = {
                'sessionId': sid,
                'project': display,
                'source': 'hermes',
                'slug': slug.replace('\n', ' ')[:80],
                'status': 'active' if active else 'completed',
                'model': model,
                'version': '',
                'mtime': mtime,
                'size': 0,
                'eventCount': r.get('message_count') or 0,
                'cwd': r.get('cwd') or '',
                'gitBranch': r.get('git_branch') or '',
                'subagentCount': 0,
                'duration': int((ended or now) - started) if started else 0,
                'contextPct': context_pct,
                '_path': f'hermes-state://{sid_raw}',
            }

    # ── OpenCode session scanning (SQLite) ────────────────────────────────

    @staticmethod
    def _open_opencode_db_ro():
        uri = OPENCODE_DB.as_posix()
        if os.name == 'nt':
            uri = '/' + uri
        return sqlite3.connect(f'file:{uri}?mode=ro', uri=True)

    @staticmethod
    def _opencode_display(directory, projects):
        """Build the project display name for an OpenCode session."""
        home_prefix = str(Path.home()).replace('\\', '/') + '/'
        dir_norm = str(directory or '').replace('\\', '/')
        rel = dir_norm.replace(home_prefix, '').rstrip('/') if dir_norm else ''
        display = 'opencode/' + rel if rel else 'opencode'
        projects.add(display)
        return display

    def _scan_opencode_sessions(self, sessions, projects):
        """Scan the OpenCode SQLite store for sessions."""
        try:
            con = self._open_opencode_db_ro()
        except Exception:
            return
        try:
            rows = con.execute(
                'SELECT id, directory, title, version, agent, model,'
                ' tokens_input, tokens_output, tokens_cache_read, cost,'
                ' time_created, time_updated FROM session'
                ' ORDER BY time_updated DESC LIMIT 2000'
            ).fetchall()
            # One grouped query for message counts (avoids per-session queries)
            msg_counts = {}
            for ses_id_, cnt in con.execute(
                'SELECT session_id, COUNT(*) FROM message GROUP BY session_id'
            ):
                msg_counts[ses_id_] = cnt
        except Exception:
            return
        finally:
            con.close()

        now = time.time()
        for (sid_raw, directory, title, version, agent, model_raw,
             tokens_in, tokens_out, cache_read, cost,
             t_created, t_updated) in rows:

            ses_id = sid_raw
            sid = 'opencode-' + ses_id

            # Model stored as JSON {"id": ..., "providerID": ..., "variant": ...}
            model_id = ''
            try:
                mobj = json.loads(model_raw) if model_raw else {}
                if isinstance(mobj, dict):
                    model_id = mobj.get('id', '')
            except (json.JSONDecodeError, ValueError):
                model_id = model_raw or ''

            msg_count = msg_counts.get(ses_id) or max(1, int((tokens_out or 0) / 300))

            record = self._build_opencode_record(
                ses_id=sid_raw,
                directory=directory,
                title=title,
                version=version,
                model=model_id,
                tokens_in=tokens_in,
                cache_read=cache_read,
                msg_count=msg_count,
                t_created=t_created,
                t_updated=t_updated,
                now=now,
                path=f'{OPENCODE_DB}::{ses_id}',
            )
            record['project'] = self._opencode_display(directory, projects)
            sessions[sid] = record

    def _scan_opencode_legacy_sessions(self, sess_dir, sessions, projects):
        """Scan the pre-SQLite OpenCode JSON store (storage/session/...)."""
        now = time.time()
        for proj_dir in sess_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob('ses_*.json'):
                try:
                    d = json.loads(f.read_text(encoding='utf-8', errors='replace'))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
                ses_id = d.get('id') or f.stem
                if not ses_id:
                    continue
                sid = 'opencode-' + ses_id
                if sid in sessions:
                    continue  # DB store wins on collision

                t_created = (d.get('time') or {}).get('created') or 0
                t_updated = (d.get('time') or {}).get('updated') or 0

                # Single pass over messages: count, token sums, latest model
                msg_dir = OPENCODE_STORAGE_DIR / 'message' / ses_id
                msg_count = 0
                tokens_in = tokens_out = cache_read = 0
                last_ts_model = ''
                latest_assistant_ts = -1
                if msg_dir.is_dir():
                    for mf in msg_dir.glob('msg_*.json'):
                        msg_count += 1
                        try:
                            md = json.loads(mf.read_text(encoding='utf-8', errors='replace'))
                        except (OSError, json.JSONDecodeError, ValueError):
                            continue
                        if md.get('role') != 'assistant':
                            continue
                        tk = md.get('tokens') or {}
                        tokens_in += tk.get('input', 0) or 0
                        tokens_out += tk.get('output', 0) or 0
                        cache_read += (tk.get('cache') or {}).get('read', 0) or 0
                        ts_ = ((md.get('time') or {}).get('created')) or 0
                        if ts_ >= latest_assistant_ts and md.get('modelID'):
                            latest_assistant_ts = ts_
                            last_ts_model = md['modelID']

                record = self._build_opencode_record(
                    ses_id=ses_id,
                    directory=d.get('directory'),
                    title=d.get('title'),
                    version=d.get('version'),
                    model=last_ts_model,
                    tokens_in=tokens_in,
                    cache_read=cache_read,
                    msg_count=max(msg_count, 1),
                    t_created=t_created,
                    t_updated=t_updated,
                    now=now,
                    path=f'{OPENCODE_STORAGE_DIR}::{ses_id}',
                )
                record['project'] = self._opencode_display(
                    d.get('directory'), projects)
                sessions[sid] = record

    def _build_opencode_record(self, ses_id, directory, title, version, model,
                               tokens_in, cache_read, msg_count,
                               t_created, t_updated, now, path):
        """Assemble the unified session record shared by both OpenCode stores."""
        updated = (t_updated or t_created or 0) / 1000
        created = (t_created or 0) / 1000
        active = (now - updated) < 120

        last_input = (tokens_in or 0) + (cache_read or 0)
        ctx_size = self._get_context_size(model)
        context_pct = round((last_input / ctx_size) * 100) if ctx_size and last_input else 0

        return {
            'sessionId': 'opencode-' + ses_id,
            'source': 'opencode',
            'slug': str(title or '').replace('\n', ' ')[:80],
            'status': 'active' if active else 'completed',
            'model': model,
            'version': str(version or ''),
            'mtime': updated,
            'size': 0,
            'eventCount': msg_count,
            'cwd': str(directory or ''),
            'gitBranch': '',
            'subagentCount': 0,
            'duration': int(updated - created) if created else 0,
            'contextPct': context_pct,
            '_path': path,
        }

    # ── content search via ripgrep ────────────────────────────────────────

    _content_cache = {}  # class-level: {cache_key: {'ids': set, 'time': float}}
    _CONTENT_CACHE_TTL = 60

    def content_search(self, query, source=None):
        """Search session file contents using ripgrep. Returns set of session IDs."""
        if not query or len(query) < 2 or len(query) > 100:
            return set()

        # Strip control characters (null bytes, etc)
        query = re.sub(r'[\x00-\x1f]', '', query)
        if len(query) < 2:
            return set()

        cache_key = (query, source)
        cached = self._content_cache.get(cache_key)
        if cached and time.time() - cached['time'] < self._CONTENT_CACHE_TTL:
            return cached['ids']

        # Evict stale entries if cache grows large
        now = time.time()
        if len(self._content_cache) > 200:
            self._content_cache = {
                k: v for k, v in self._content_cache.items()
                if now - v['time'] < self._CONTENT_CACHE_TTL
            }

        # Build search directories based on source filter
        dirs = []
        if source in (None, 'claude'):
            if PROJECTS_DIR.exists():
                dirs.append(str(PROJECTS_DIR))
            for d in WSL_PROJECTS_DIRS:
                if d.exists():
                    dirs.append(str(d))
        if source in (None, 'openclaw'):
            if OPENCLAW_DIR.exists():
                dirs.append(str(OPENCLAW_DIR))
            for d in WSL_OPENCLAW_DIRS:
                if d.exists():
                    dirs.append(str(d))
        if source in (None, 'codex'):
            if CODEX_DIR.exists():
                dirs.append(str(CODEX_DIR))
            if CODEX_ARCHIVED_DIR.exists():
                dirs.append(str(CODEX_ARCHIVED_DIR))
            for d in WSL_CODEX_DIRS:
                if d.exists():
                    dirs.append(str(d))
            for d in WSL_CODEX_ARCHIVED_DIRS:
                if d.exists():
                    dirs.append(str(d))
        if source in (None, 'kimi'):
            if KIMI_DIR.exists():
                dirs.append(str(KIMI_DIR))
            for d in WSL_KIMI_DIRS:
                if d.exists():
                    dirs.append(str(d))
        if source in (None, 'droid'):
            if DROID_DIR.exists():
                dirs.append(str(DROID_DIR))
            for d in WSL_DROID_DIRS:
                if d.exists():
                    dirs.append(str(d))
        if source in (None, 'hermes-bench'):
            for d in HERMES_BENCH_DIRS:
                if d.exists():
                    dirs.append(str(d))

        # OpenCode lives in SQLite — search via LIKE instead of ripgrep
        oc_ids = set()
        if (source in (None, 'opencode')) and OPENCODE_DB.exists():
            try:
                con = self._open_opencode_db_ro()
                like = f'%{query}%'
                for table in ('part', 'message'):
                    for r in con.execute(
                        f'SELECT DISTINCT session_id FROM {table}'
                        ' WHERE data LIKE ? LIMIT 500',
                        (like,),
                    ):
                        oc_ids.add('opencode-' + r[0])
                con.close()
            except Exception:
                pass

        if not dirs and not oc_ids:
            return set()

        paths = []
        rg_bin = _find_rg()
        if dirs and rg_bin:
            cmd = [
                rg_bin, '-l', '--fixed-strings', '--max-filesize', '50M',
                '--glob', '*.jsonl',
                '--glob', '!*openclaw-workspace*', '--glob', '!*observer*',
                query,
            ] + dirs

            try:
                result = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=10,
                    stdin=subprocess.DEVNULL,
                )
                paths = result.stdout.strip().split('\n') if result.stdout.strip() else []
            except (subprocess.TimeoutExpired, OSError):
                paths = []

        # Map file paths back to session IDs via reverse lookup.
        # Bench runs are directories (_path = run dir), so files found
        # inside them map to the parent run by prefix.
        with self._lock:
            path_to_id = {s['_path']: sid for sid, s in self._sessions.items()
                          if s.get('source') != 'hermes-bench'}
            bench_dirs = [(os.path.normcase(os.path.normpath(s['_path'])), sid)
                          for sid, s in self._sessions.items()
                          if s.get('source') == 'hermes-bench']

        matched_ids = set()
        for p in paths:
            p = p.strip()
            if not p:
                continue
            sid = path_to_id.get(p)
            if not sid:
                pn = os.path.normcase(os.path.normpath(p))
                for bn, b_sid in bench_dirs:
                    if pn.startswith(bn + os.sep):
                        sid = b_sid
                        break
            if sid:
                matched_ids.add(sid)
        matched_ids |= oc_ids

        self._content_cache[cache_key] = {'ids': matched_ids, 'time': time.time()}
        return matched_ids

    # ── query interface ───────────────────────────────────────────────────

    def get_sessions(self, project=None, status=None, search=None,
                     date_from=None, date_to=None, sort='mtime',
                     offset=0, limit=50, source=None, content_q=None):
        self.ensure_fresh()

        with self._lock:
            sessions = list(self._sessions.values())
            all_projects = sorted(self._projects)

        # Collect available sources
        sources = sorted(set(s.get('source', 'claude') for s in sessions))

        if source and source in ('claude', 'openclaw', 'codex', 'kimi', 'droid',
                                 'hermes-bench', 'opencode', 'hermes'):
            sessions = [s for s in sessions if s.get('source') == source]
        if project:
            sessions = [s for s in sessions if s['project'] == project]
        if status and status != 'all':
            sessions = [s for s in sessions if s['status'] == status]
        if search:
            q = search.lower()
            sessions = [s for s in sessions
                        if q in s.get('slug', '').lower()
                        or q in s.get('project', '').lower()
                        or q in s.get('sessionId', '').lower()]
        if content_q and len(content_q) >= 2:
            matched_ids = self.content_search(content_q, source)
            sessions = [s for s in sessions if s['sessionId'] in matched_ids]
        if date_from:
            try:
                ts = datetime.strptime(date_from, '%Y-%m-%d').timestamp()
                sessions = [s for s in sessions if s['mtime'] >= ts]
            except ValueError:
                pass
        if date_to:
            try:
                ts = datetime.strptime(date_to, '%Y-%m-%d').timestamp() + 86400
                sessions = [s for s in sessions if s['mtime'] <= ts]
            except ValueError:
                pass

        sessions.sort(key=lambda s: s['mtime'], reverse=True)
        total = len(sessions)
        page = sessions[offset:offset + limit]

        result = [{k: v for k, v in s.items() if k != '_path'} for s in page]

        return {
            'sessions': result, 'total': total,
            'projects': all_projects, 'sources': sources,
            'offset': offset, 'limit': limit,
        }

    def get_session_path(self, session_id):
        self.ensure_fresh()
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                return s.get('_path')
            # Check if it's a subagent filename stem
            sa_path = self._subagent_paths.get(session_id)
            if sa_path:
                return sa_path
        # Validate format before filesystem fallback (prevent path traversal)
        if not re.match(r'^[a-zA-Z0-9_-]+$', session_id):
            return None
        # Fallback filesystem scan (Claude + Droid use {uuid}.jsonl naming)
        raw_id = session_id
        if session_id.startswith('droid-'):
            raw_id = session_id[6:]

        droid_bases = [DROID_DIR] + WSL_DROID_DIRS
        for base in [PROJECTS_DIR, DROID_DIR] + WSL_PROJECTS_DIRS + WSL_DROID_DIRS:
            if base.exists():
                if base in droid_bases:
                    p = (base / f'{raw_id}.jsonl').resolve()
                    try:
                        p.relative_to(base.resolve())
                    except ValueError:
                        p = None
                    if p and p.exists():
                        return str(p)
                for d in base.iterdir():
                    if not d.is_dir():
                        continue
                    p = (d / f'{raw_id}.jsonl').resolve()
                    try:
                        p.relative_to(base.resolve())
                    except ValueError:
                        continue
                    if p.exists():
                        return str(p)
        return None

    def get_subagents(self, session_id):
        """Get subagents for a parent session."""
        self.ensure_fresh()
        with self._lock:
            return list(self._subagents.get(session_id, []))


# ─── Session Event Reader ──────────────────────────────────────────────────────

class SessionEventReader:
    """Reads and processes events from session JSONL files."""

    _SKIP_TYPES = frozenset([
        'file-history-snapshot', 'queue-operation',
        'session', 'thinking_level_change', 'custom',
        # Claude Code metadata/noise lines: no uuid, nothing to render.
        # Skipping keeps the event limit for real messages and stops the
        # poll cursor from being overwritten with an empty uuid.
        'attachment', 'last-prompt', 'atis-latch', 'mode', 'permission-mode',
        'ai-title', 'custom-title', 'agent-name', 'pr-link',
        'bridge-session', 'file-history-delta', 'cost-state',
    ])

    # path -> (size, mtime, {byte_offset: line_count}). Line-number cursors
    # for Codex/Kimi tail reads need the newline count before the tail
    # offset; counting the whole file on every poll is O(size).
    _line_offset_cache = {}

    _CODEX_TYPES = frozenset([
        'session_meta', 'response_item', 'event_msg', 'turn_context',
    ])

    @staticmethod
    def _is_codex_path(path):
        """Check if path is under any Codex sessions directory (Windows or WSL)."""
        s = str(path)
        return any(s.startswith(str(d)) for d in ALL_CODEX_DIRS)

    @staticmethod
    def _is_kimi_path(path):
        """Check if path is under any Kimi sessions directory (Windows or WSL)."""
        s = str(path)
        return any(s.startswith(str(d)) for d in [KIMI_DIR] + WSL_KIMI_DIRS)

    @staticmethod
    def _is_opencode_path(path):
        """Check if path is an OpenCode SQLite pseudo-path (<db>::<sessionId>)."""
        return str(path).startswith(str(OPENCODE_DB) + '::')

    @staticmethod
    def _is_legacy_opencode_path(path):
        """Check if path is a legacy OpenCode pseudo-path (<storage>::<sessionId>)."""
        return str(path).startswith(str(OPENCODE_STORAGE_DIR) + '::')

    @staticmethod
    def _is_hermes_session_path(path):
        """Check if path is a Hermes agent session JSON file."""
        s = str(path)
        return s.endswith('.json') and any(
            s.startswith(str(d)) for d in HERMES_SESSION_DIRS
        )

    @staticmethod
    def _is_hermes_state_path(path):
        """Check if path is a live Hermes SessionDB pseudo-path."""
        return str(path).startswith('hermes-state://')

    @staticmethod
    def _hermes_content_text(content):
        """Flatten an OpenAI-style content field into display text."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for b in content:
                if isinstance(b, dict) and b.get('type') == 'text' and b.get('text'):
                    texts.append(b['text'])
            return '\n'.join(texts)
        if content is None:
            return ''
        try:
            return json.dumps(content, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(content)

    @classmethod
    def _convert_openai_messages(cls, prefix, pairs, fallback_model=''):
        """Convert OpenAI-style messages into unified events.

        pairs: iterable of (key, message_dict) — key becomes the numeric
        cursor id embedded in each event uuid.
        """
        converted = []  # (key, event)
        for key, m in pairs:
            role = m.get('role', '')
            if role == 'system':
                continue
            uuid = f'{prefix}-{key}'
            ts = m.get('_ts_iso') or ''
            if not ts and isinstance(m.get('timestamp'), (int, float)):
                ts = cls._ms_to_iso(m['timestamp'] * 1000)

            if role == 'user':
                text = cls._hermes_content_text(m.get('content'))
                if not text.strip():
                    continue
                ev = {
                    'type': 'user',
                    'uuid': uuid,
                    'timestamp': ts,
                    'userType': 'external',
                    'text': text[:MAX_TEXT_LEN],
                    'truncated': len(text) > MAX_TEXT_LEN,
                }
                converted.append((key, ev))

            elif role == 'assistant':
                text = cls._hermes_content_text(m.get('content'))
                ev = {
                    'type': 'assistant',
                    'uuid': uuid,
                    'timestamp': ts,
                    'model': m.get('model', '') or fallback_model,
                    'usage': {},
                    'stop_reason': '',
                }
                tool_uses = []
                for tc in m.get('tool_calls') or []:
                    fn = tc.get('function') or {}
                    args = fn.get('arguments', '{}')
                    if not isinstance(args, str):
                        try:
                            args = json.dumps(args, ensure_ascii=False)
                        except (TypeError, ValueError):
                            args = str(args)
                    truncated = len(args) > MAX_TOOL_INPUT_LEN
                    tool_uses.append({
                        'type': 'tool_use',
                        'id': tc.get('id', ''),
                        'name': fn.get('name', ''),
                        'input': args[:MAX_TOOL_INPUT_LEN],
                        'truncated': truncated,
                    })
                if tool_uses:
                    ev['tool_uses'] = tool_uses
                if text.strip():
                    ev['text'] = text[:MAX_TEXT_LEN]
                    ev['truncated'] = len(text) > MAX_TEXT_LEN
                if text.strip() or tool_uses:
                    converted.append((key, ev))

            elif role == 'tool':
                output = cls._hermes_content_text(m.get('content'))
                ev = {
                    'type': 'user',
                    'uuid': uuid,
                    'timestamp': ts,
                    'userType': 'tool_result',
                    'tool_results': [{
                        'tool_use_id': m.get('tool_call_id', ''),
                        'type': 'tool_result',
                        'content': output[:MAX_TOOL_RESULT_LEN],
                        'truncated': len(output) > MAX_TOOL_RESULT_LEN,
                    }],
                }
                converted.append((key, ev))

        return converted

    @staticmethod
    def _select_converted(converted, prefix, after, limit, types):
        """Cursor/limit/types selection over converted (key, event) pairs."""
        if after:
            m = re.match(rf'{prefix}-(\d+)$', str(after))
            idx = int(m.group(1)) if m else -1
            selected = [(k, ev) for k, ev in converted if k > idx][:limit]
        else:
            selected = converted[-limit:]
        return [
            ev for _, ev in selected
            if not types or ev['type'] in types
        ]

    @classmethod
    def _read_hermes_events(cls, path, after=None, limit=100, types=None):
        """Read a Hermes session JSON (OpenAI-style messages) into unified events."""
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return []

        pairs = list(enumerate(data.get('messages') or []))
        converted = cls._convert_openai_messages(
            'hmsg', pairs, data.get('model', ''))
        return cls._select_converted(
            converted, 'hmsg', after, limit, types)

    @classmethod
    def _read_hermes_state_events(cls, path, after=None, limit=100, types=None):
        """Read a live Hermes SessionDB session via the WSL bridge."""
        sid = str(path).split('://', 1)[1]
        msgs = _wsl_hermes_bridge(['msgs', sid], timeout=30)
        if not isinstance(msgs, list):
            return []
        pairs = []
        for m in msgs:
            try:
                key = int(m.get('id'))
            except (TypeError, ValueError):
                continue
            pairs.append((key, m))
        converted = cls._convert_openai_messages('hstate', pairs)
        return cls._select_converted(
            converted, 'hstate', after, limit, types)

    @classmethod
    def _get_hermes_stats(cls, path):
        stats = {
            'model': '', 'version': '', 'cwd': '', 'gitBranch': '',
            'tokens': {
                'input': 0, 'output': 0,
                'cache_read': 0, 'cache_creation': 0,
            },
            'tool_calls': {},
            'event_counts': {},
            'first_timestamp': '',
            'last_timestamp': '',
            'duration_seconds': 0,
        }
        try:
            st = os.stat(path)
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return stats

        stats['model'] = data.get('model', '')
        start = data.get('session_start', '')
        if start:
            stats['first_timestamp'] = start
            try:
                start_ts = datetime.fromisoformat(start).timestamp()
                stats['last_timestamp'] = datetime.fromtimestamp(
                    st.st_mtime).isoformat()
                stats['duration_seconds'] = int(st.st_mtime - start_ts)
            except (ValueError, TypeError, OSError):
                pass

        for m in data.get('messages') or []:
            role = m.get('role', '?')
            if role == 'system':
                continue
            stats['event_counts'][role] = stats['event_counts'].get(role, 0) + 1
            for tc in m.get('tool_calls') or []:
                name = ((tc.get('function') or {}).get('name')) or 'unknown'
                stats['tool_calls'][name] = stats['tool_calls'].get(name, 0) + 1
        return stats

    @classmethod
    def _get_hermes_state_stats(cls, path):
        sid = str(path).split('://', 1)[1]
        stats = {
            'model': '', 'version': '', 'cwd': '', 'gitBranch': '',
            'tokens': {
                'input': 0, 'output': 0,
                'cache_read': 0, 'cache_creation': 0,
            },
            'tool_calls': {},
            'event_counts': {},
            'first_timestamp': '',
            'last_timestamp': '',
            'duration_seconds': 0,
        }
        d = _wsl_hermes_bridge(['stats', sid], timeout=15)
        if not isinstance(d, dict) or d.get('error'):
            return stats
        started = d.get('started_at')
        ended = d.get('ended_at')
        stats.update({
            'model': d.get('model') or '',
            'cwd': d.get('cwd') or '',
            'gitBranch': d.get('git_branch') or '',
            'tokens': {
                'input': d.get('input_tokens') or 0,
                'output': d.get('output_tokens') or 0,
                'cache_read': d.get('cache_read_tokens') or 0,
                'cache_creation': d.get('cache_write_tokens') or 0,
            },
            'tool_calls': d.get('tool_calls') or {},
            'event_counts': d.get('roles') or {},
            'first_timestamp': cls._ms_to_iso(
                (started or 0) * 1000) if started else '',
            'last_timestamp': cls._ms_to_iso(
                (ended or 0) * 1000) if ended else '',
            'duration_seconds': int(ended - started) if ended and started else 0,
        })
        return stats

    @staticmethod
    def _is_hermes_bench_path(path):
        """Check if path is a Hermes benchmark run directory."""
        s = str(path)
        return os.path.isdir(s) and any(
            s.startswith(str(d)) for d in HERMES_BENCH_DIRS
        )

    @staticmethod
    def _ms_to_iso(ms):
        """Convert millisecond epoch to ISO timestamp string."""
        if not ms:
            return ''
        try:
            return datetime.fromtimestamp(ms / 1000).isoformat()
        except (ValueError, OSError, OverflowError):
            return ''

    @staticmethod
    def extract_compaction_summary(events):
        """Extract the final compact-summary text from a subagent event stream."""
        if not events:
            return {'timestamp': '', 'summary': ''}

        last_ts = ''
        last_text = ''
        for ev in events:
            if ev.get('type') == 'assistant' and ev.get('text'):
                last_ts = ev.get('timestamp', '')
                last_text = ev.get('text', '').strip()

        if not last_text:
            return {'timestamp': '', 'summary': ''}

        match = re.search(r'<summary>\s*(.*?)\s*</summary>', last_text, re.S | re.I)
        if match:
            summary = match.group(1).strip()
            if summary:
                last_text = summary

        return {
            'timestamp': last_ts,
            'summary': last_text,
        }

    @classmethod
    def read_events(cls, path, after=None, limit=100, types=None, full=False):
        events = []
        try:
            if cls._is_opencode_path(path):
                events = cls._read_opencode_events(path, after, limit, types)
            elif cls._is_legacy_opencode_path(path):
                events = cls._read_opencode_legacy_events(path, after, limit, types)
            elif cls._is_hermes_state_path(path):
                events = cls._read_hermes_state_events(path, after, limit, types)
            elif cls._is_hermes_session_path(path):
                events = cls._read_hermes_events(path, after, limit, types)
            elif cls._is_hermes_bench_path(path):
                events = cls._read_hermes_bench_events(path, after, limit, types)
            else:
                file_size = os.path.getsize(path)
                if cls._is_codex_path(path):
                    events = cls._read_codex_events(path, file_size, after, limit, types)
                elif cls._is_kimi_path(path):
                    events = cls._read_kimi_events(path, file_size, after, limit, types)
                elif after:
                    events = cls._read_after_cursor(path, file_size, after, limit, types)
                elif full:
                    events = cls._read_full(path, limit, types)
                else:
                    events = cls._read_tail(path, file_size, limit, types)
        except OSError as e:
            print(f"Error reading session: {e}", file=sys.stderr)

        return events

    @classmethod
    def _read_full(cls, path, limit, types):
        """Read all events from beginning of file (for export/copy)."""
        events = []
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                ev = cls._parse_event(line.strip())
                if ev and ev['type'] not in cls._SKIP_TYPES:
                    if not types or ev['type'] in types:
                        events.append(ev)
                        if len(events) >= limit:
                            break
        return events

    @classmethod
    def _read_after_cursor(cls, path, file_size, after, limit, types):
        """Read events after a given UUID cursor."""
        events = []
        found = False

        # Try tail section first (cursor is usually near the end)
        if file_size > TAIL_READ_BYTES:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(max(0, file_size - TAIL_READ_BYTES))
                f.readline()
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if not found:
                        try:
                            obj = json.loads(line)
                            if obj.get('uuid') == after or obj.get('id') == after:
                                found = True
                        except json.JSONDecodeError:
                            pass
                        continue
                    ev = cls._parse_event(line)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
                        if len(events) >= limit:
                            return events

        if not found:
            # Full scan
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if not found:
                        try:
                            obj = json.loads(line)
                            if obj.get('uuid') == after or obj.get('id') == after:
                                found = True
                        except json.JSONDecodeError:
                            pass
                        continue
                    ev = cls._parse_event(line)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
                        if len(events) >= limit:
                            break

        return events

    @classmethod
    def _read_tail(cls, path, file_size, limit, types):
        """Read last N events from a session file."""
        events = []

        if file_size > TAIL_READ_BYTES:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(max(0, file_size - TAIL_READ_BYTES))
                f.readline()
                for line in f:
                    ev = cls._parse_event(line.strip())
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
            return events[-limit:]

        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                ev = cls._parse_event(line.strip())
                if ev and ev['type'] not in cls._SKIP_TYPES:
                    if not types or ev['type'] in types:
                        events.append(ev)
        return events[-limit:]

    # ── Codex event reading ────────────────────────────────────────────────

    @classmethod
    def _count_lines_before(cls, path, offset, file_size):
        """Count newlines in [0, offset) with a per-file mtime-keyed cache."""
        try:
            st = os.stat(path)
        except OSError:
            st = None
        key = str(path)
        entry = cls._line_offset_cache.get(key)
        valid = entry and entry[0] == file_size and st and entry[1] == st.st_mtime
        if valid and offset in entry[2]:
            return entry[2][offset]

        count = 0
        with open(path, 'rb') as f:
            remaining = offset
            while remaining > 0:
                chunk = f.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                count += chunk.count(b'\n')
                remaining -= len(chunk)

        if len(cls._line_offset_cache) > 2000:
            cls._line_offset_cache.clear()
            entry = None
        if valid:
            entry[2][offset] = count
        else:
            cls._line_offset_cache[key] = (
                file_size, st.st_mtime if st else 0, {offset: count},
            )
        return count

    @classmethod
    def _read_codex_events(cls, path, file_size, after=None, limit=100, types=None):
        """Read events from a Codex JSONL file using line-number cursors."""
        events = []

        # Parse after cursor: "codex-<line_num>"
        if after and after.startswith('codex-'):
            try:
                after_line = int(after.split('-', 1)[1])
            except (ValueError, IndexError):
                after_line = -1

            # Cursor-based: scan from beginning to find the line
            found = False
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line_num, line in enumerate(f):
                    if not found:
                        if line_num == after_line:
                            found = True
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    ev = cls._parse_codex_event(line, line_num)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
                        if len(events) >= limit:
                            break
            return events

        # Initial load (no cursor): use tail-read for large files
        if file_size > TAIL_READ_BYTES:
            # Count total lines first to get correct line numbers
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                f.seek(max(0, file_size - TAIL_READ_BYTES))
                f.readline()  # discard partial
                start_offset = f.tell()
                line_offset = cls._count_lines_before(path, start_offset, file_size)
                # Now read from offset with correct line numbers
                f.seek(start_offset)
                for rel_num, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    ev = cls._parse_codex_event(line, line_offset + rel_num)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
            return events[-limit:]

        # Small files: full scan
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                ev = cls._parse_codex_event(line, line_num)
                if ev and ev['type'] not in cls._SKIP_TYPES:
                    if not types or ev['type'] in types:
                        events.append(ev)
        return events[-limit:]

    @staticmethod
    def _parse_codex_event(line, line_num=0):
        """Parse a Codex JSONL line into the unified event format."""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None

        t = raw.get('type', '')
        payload = raw.get('payload', {})
        if not isinstance(payload, dict):
            return None
        pt = payload.get('type', '')
        ts = raw.get('timestamp', '')
        uuid = f'codex-{line_num}'

        # Skip non-display events
        if t == 'session_meta' or t == 'turn_context':
            return None
        if t == 'event_msg' and pt in ('token_count', 'agent_reasoning',
                                        'agent_message', 'user_message',
                                        'context_compacted'):
            return None

        # ── event_msg: task_started / task_complete → system ─────────
        if t == 'event_msg':
            if pt in ('task_started', 'task_complete'):
                return {
                    'type': 'system',
                    'uuid': uuid,
                    'timestamp': ts,
                    'subtype': pt,
                    'level': '',
                }
            return None

        # ── response_item ────────────────────────────────────────────
        if t == 'response_item':
            role = payload.get('role', '')
            content = payload.get('content', [])

            # User message
            if pt == 'message' and role == 'user':
                texts = []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'input_text':
                            texts.append(block.get('text', '')[:MAX_TEXT_LEN])
                # Skip system-injected context
                if texts and (texts[0].startswith('# AGENTS.md')
                              or texts[0].startswith('# Collaboration Mode')
                              or texts[0].startswith('<environment_context>')
                              or texts[0].startswith('<permissions')
                              or texts[0].startswith('<collaboration_mode')):
                    return None
                if not texts:
                    return None
                joined = '\n'.join(texts)
                return {
                    'type': 'user',
                    'uuid': uuid,
                    'timestamp': ts,
                    'userType': 'external',
                    'text': joined[:MAX_TEXT_LEN],
                    'truncated': len(joined) > MAX_TEXT_LEN,
                }

            # Developer message (system instructions) — skip
            if pt == 'message' and role == 'developer':
                return None

            # Assistant message
            if pt == 'message' and role == 'assistant':
                texts = []
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get('type') == 'output_text':
                            texts.append(block.get('text', '')[:MAX_TEXT_LEN])
                result = {
                    'type': 'assistant',
                    'uuid': uuid,
                    'timestamp': ts,
                    'model': '',
                    'usage': {},
                    'stop_reason': '',
                }
                if texts:
                    result['text'] = '\n'.join(texts)
                return result

            # Reasoning (thinking) — skip display, already covered by agent_reasoning event_msg
            if pt == 'reasoning':
                return None

            # Function call
            if pt == 'function_call':
                name = payload.get('name', '')
                args = payload.get('arguments', '{}')
                if isinstance(args, str) and len(args) > MAX_TOOL_INPUT_LEN:
                    truncated = True
                    args = args[:MAX_TOOL_INPUT_LEN]
                else:
                    truncated = False
                return {
                    'type': 'assistant',
                    'uuid': uuid,
                    'timestamp': ts,
                    'model': '',
                    'usage': {},
                    'stop_reason': '',
                    'tool_uses': [{
                        'type': 'tool_use',
                        'id': payload.get('call_id', ''),
                        'name': name,
                        'input': args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)[:MAX_TOOL_INPUT_LEN],
                        'truncated': truncated,
                    }],
                }

            # Function call output
            if pt == 'function_call_output':
                output = payload.get('output', '')
                return {
                    'type': 'user',
                    'uuid': uuid,
                    'timestamp': ts,
                    'userType': 'tool_result',
                    'tool_results': [{
                        'tool_use_id': payload.get('call_id', ''),
                        'type': 'tool_result',
                        'content': output[:MAX_TOOL_RESULT_LEN],
                        'truncated': len(output) > MAX_TOOL_RESULT_LEN,
                    }],
                }

            # Custom tool call (apply_patch etc)
            if pt == 'custom_tool_call':
                inp = payload.get('input', '')
                if isinstance(inp, str) and len(inp) > MAX_TOOL_INPUT_LEN:
                    truncated = True
                    inp = inp[:MAX_TOOL_INPUT_LEN]
                else:
                    truncated = False
                return {
                    'type': 'assistant',
                    'uuid': uuid,
                    'timestamp': ts,
                    'model': '',
                    'usage': {},
                    'stop_reason': '',
                    'tool_uses': [{
                        'type': 'tool_use',
                        'id': payload.get('call_id', ''),
                        'name': payload.get('name', ''),
                        'input': inp,
                        'truncated': truncated,
                    }],
                }

            # Custom tool call output
            if pt == 'custom_tool_call_output':
                output = payload.get('output', '')
                return {
                    'type': 'user',
                    'uuid': uuid,
                    'timestamp': ts,
                    'userType': 'tool_result',
                    'tool_results': [{
                        'tool_use_id': payload.get('call_id', ''),
                        'type': 'tool_result',
                        'content': output[:MAX_TOOL_RESULT_LEN],
                        'truncated': len(output) > MAX_TOOL_RESULT_LEN,
                    }],
                }

            # Reasoning
            if pt == 'reasoning':
                summary = payload.get('summary', [])
                summary_text = ''
                if isinstance(summary, list):
                    parts = [item.get('text', '') for item in summary
                             if isinstance(item, dict) and item.get('type') == 'summary_text']
                    summary_text = '\n'.join(parts)
                result = {
                    'type': 'assistant',
                    'uuid': uuid,
                    'timestamp': ts,
                    'model': '',
                    'usage': {},
                    'stop_reason': '',
                    'hasThinking': True,
                }
                if summary_text:
                    result['text'] = summary_text[:MAX_TEXT_LEN]
                return result

        return None

    # ── Kimi event reading ────────────────────────────────────────────────

    @classmethod
    def _read_kimi_events(cls, path, file_size, after=None, limit=100, types=None):
        """Read events from a Kimi wire.jsonl file using line-number cursors."""
        events = []

        if after and after.startswith('kimi-'):
            try:
                after_line = int(after[5:])
            except (ValueError, IndexError):
                return events

            if after_line < 0:
                return events

            found = False
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line_num, line in enumerate(f):
                    if not found:
                        if line_num == after_line:
                            found = True
                        continue
                    line = line.strip()
                    if not line:
                        continue
                    ev = cls._parse_kimi_event(line, line_num)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
                        if len(events) >= limit:
                            break
            return events

        # For large files, tail-read to avoid loading entire file
        if file_size > TAIL_READ_BYTES:
            with open(path, 'rb') as f:
                seek_back = min(file_size, TAIL_READ_BYTES)
                f.seek(max(0, file_size - seek_back))
                if file_size > seek_back:
                    f.readline()  # skip partial line
                start_offset = f.tell()
                tail = f.read().decode('utf-8', errors='replace')
            line_offset = cls._count_lines_before(path, start_offset, file_size)
            for i, line in enumerate(tail.split('\n')):
                line = line.strip()
                if not line:
                    continue
                ev = cls._parse_kimi_event(line, line_offset + i)
                if ev and ev['type'] not in cls._SKIP_TYPES:
                    if not types or ev['type'] in types:
                        events.append(ev)
            return events[-limit:]

        # Full scan for small files
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            for line_num, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                ev = cls._parse_kimi_event(line, line_num)
                if ev and ev['type'] not in cls._SKIP_TYPES:
                    if not types or ev['type'] in types:
                        events.append(ev)
        return events[-limit:]

    @staticmethod
    def _parse_kimi_event(line, line_num=0):
        """Parse a Kimi wire.jsonl line into the unified event format."""
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None

        t = raw.get('type', '')
        ts = raw.get('timestamp', '')
        uuid = f'kimi-{line_num}'

        if t == 'TurnBegin':
            return {
                'type': 'system', 'uuid': uuid, 'timestamp': ts,
                'subtype': 'turn_begin', 'level': '',
                'model': raw.get('model', '') or (raw.get('payload') or {}).get('model', ''),
            }
        if t == 'TurnEnd':
            return {
                'type': 'system', 'uuid': uuid, 'timestamp': ts,
                'subtype': 'turn_end', 'level': '',
                'usage': raw.get('usage') or (raw.get('payload') or {}).get('usage'),
            }
        if t == 'StepBegin':
            return None
        if t == 'ContentPart':
            text = raw.get('text', '') or raw.get('content', '') or (raw.get('payload') or {}).get('text', '')
            if not text:
                return None
            return {
                'type': 'assistant', 'uuid': uuid, 'timestamp': ts,
                'model': '', 'usage': {}, 'stop_reason': '',
                'text': text[:MAX_TEXT_LEN],
                'truncated': len(text) > MAX_TEXT_LEN,
            }
        if t == 'ToolCall':
            name = raw.get('name', '') or (raw.get('payload') or {}).get('name', '')
            args = raw.get('arguments') or raw.get('input') or (raw.get('payload') or {}).get('arguments', {})
            inp = json.dumps(args) if not isinstance(args, str) else args
            return {
                'type': 'assistant', 'uuid': uuid, 'timestamp': ts,
                'model': '', 'usage': {}, 'stop_reason': '',
                'tool_uses': [{
                    'type': 'tool_use',
                    'id': raw.get('id', uuid),
                    'name': name,
                    'input': inp[:MAX_TOOL_INPUT_LEN],
                    'truncated': len(inp) > MAX_TOOL_INPUT_LEN,
                }],
            }
        if t == 'ToolResult':
            raw_output = raw.get('output', '') or raw.get('content', '') or (raw.get('payload') or {}).get('output', '')
            output = raw_output if isinstance(raw_output, str) else json.dumps(raw_output)
            is_error = raw.get('is_error', False) or (raw.get('payload') or {}).get('is_error', False)
            result = {
                'type': 'user', 'uuid': uuid, 'timestamp': ts,
                'userType': 'tool_result',
                'tool_results': [{
                    'tool_use_id': raw.get('tool_call_id', '') or raw.get('id', ''),
                    'type': 'tool_result',
                    'content': output[:MAX_TOOL_RESULT_LEN],
                    'truncated': len(output) > MAX_TOOL_RESULT_LEN,
                }],
            }
            if is_error:
                result['tool_results'][0]['is_error'] = True
            return result

        return None

    # ── single-event parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_event(line):
        if not line:
            return None
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            return None

        t = raw.get('type')
        if t in ('file-history-snapshot', 'queue-operation',
                 'session', 'thinking_level_change', 'custom'):
            return None

        # ── OpenClaw format: type="message" with message.role ─────────
        if t == 'message':
            msg = raw.get('message', {})
            role = msg.get('role', '')
            t = {'user': 'user', 'assistant': 'assistant',
                 'toolResult': 'user'}.get(role)
            if not t:
                return None

            result = {
                'type': t,
                'uuid': raw.get('id', ''),
                'timestamp': raw.get('timestamp', ''),
            }

            if role == 'user':
                result['userType'] = 'external'
                content = msg.get('content', '')
                if isinstance(content, str):
                    result['text'] = content[:MAX_TEXT_LEN]
                    result['truncated'] = len(content) > MAX_TEXT_LEN
                elif isinstance(content, list):
                    texts = [b.get('text', '')[:MAX_TEXT_LEN]
                             for b in content
                             if isinstance(b, dict) and b.get('type') == 'text']
                    if texts:
                        joined = '\n'.join(texts)
                        result['text'] = joined[:MAX_TEXT_LEN]
                        result['truncated'] = len(joined) > MAX_TEXT_LEN

            elif role == 'toolResult':
                result['userType'] = 'external'
                content = msg.get('content', [])
                tool_call_id = msg.get('toolCallId', '')
                if isinstance(content, list):
                    parts = [b.get('text', '')
                             for b in content
                             if isinstance(b, dict) and b.get('type') == 'text']
                    full = '\n'.join(parts)
                elif isinstance(content, str):
                    full = content
                else:
                    full = ''
                is_err = msg.get('details', {}).get('exitCode', 0) != 0
                result['tool_results'] = [{
                    'tool_use_id': tool_call_id,
                    'type': 'tool_result',
                    'content': full[:MAX_TOOL_RESULT_LEN],
                    'truncated': len(full) > MAX_TOOL_RESULT_LEN,
                    **(({'is_error': True}) if is_err else {}),
                }]

            elif role == 'assistant':
                model = msg.get('model', '')
                if model == 'delivery-mirror':
                    model = ''
                result['model'] = model
                result['usage'] = msg.get('usage', {})
                result['stop_reason'] = msg.get('stop_reason', '')

                texts = []
                tool_uses = []
                content = msg.get('content', [])
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get('type')
                        if bt == 'text':
                            texts.append(block.get('text', '')[:MAX_TEXT_LEN])
                        elif bt == 'toolCall':
                            inp = json.dumps(
                                block.get('arguments', {}),
                                ensure_ascii=False,
                            )
                            tool_uses.append({
                                'type': 'tool_use',
                                'id': block.get('id', ''),
                                'name': block.get('name', ''),
                                'input': inp[:MAX_TOOL_INPUT_LEN],
                                'truncated': len(inp) > MAX_TOOL_INPUT_LEN,
                            })
                        elif bt == 'thinking':
                            result['hasThinking'] = True

                if texts:
                    result['text'] = '\n'.join(texts)
                if tool_uses:
                    result['tool_uses'] = tool_uses

            return result

        # ── Claude Code native format ─────────────────────────────────
        result = {
            'type': t,
            'uuid': raw.get('uuid', ''),
            'timestamp': raw.get('timestamp', ''),
        }

        if t == 'user':
            msg = raw.get('message', {})
            content = msg.get('content', '')
            result['userType'] = raw.get('userType', 'external')

            if isinstance(content, str):
                result['text'] = content[:MAX_TEXT_LEN]
                result['truncated'] = len(content) > MAX_TEXT_LEN
            elif isinstance(content, list):
                texts = []
                tool_results = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    bt = block.get('type')
                    if bt == 'text':
                        texts.append(block.get('text', '')[:MAX_TEXT_LEN])
                    elif bt == 'tool_result':
                        tr = {
                            'tool_use_id': block.get('tool_use_id', ''),
                            'type': 'tool_result',
                        }
                        rc = block.get('content', '')
                        if isinstance(rc, list):
                            parts = [
                                i.get('text', '')
                                for i in rc
                                if isinstance(i, dict) and i.get('type') == 'text'
                            ]
                            full = '\n'.join(parts)
                        elif isinstance(rc, str):
                            full = rc
                        else:
                            full = ''
                        tr['content'] = full[:MAX_TOOL_RESULT_LEN]
                        tr['truncated'] = len(full) > MAX_TOOL_RESULT_LEN
                        if block.get('is_error'):
                            tr['is_error'] = True
                        tool_results.append(tr)

                if texts:
                    joined = '\n'.join(texts)
                    result['text'] = joined[:MAX_TEXT_LEN]
                    result['truncated'] = len(joined) > MAX_TEXT_LEN
                if tool_results:
                    result['tool_results'] = tool_results

        elif t == 'assistant':
            msg = raw.get('message', {})
            result['model'] = msg.get('model', '')
            result['usage'] = msg.get('usage', {})
            result['stop_reason'] = msg.get('stop_reason', '')

            texts = []
            tool_uses = []
            for block in msg.get('content', []):
                if not isinstance(block, dict):
                    continue
                bt = block.get('type')
                if bt == 'text':
                    texts.append(block.get('text', '')[:MAX_TEXT_LEN])
                elif bt == 'tool_use':
                    inp = json.dumps(block.get('input', {}), ensure_ascii=False)
                    tool_uses.append({
                        'type': 'tool_use',
                        'id': block.get('id', ''),
                        'name': block.get('name', ''),
                        'input': inp[:MAX_TOOL_INPUT_LEN],
                        'truncated': len(inp) > MAX_TOOL_INPUT_LEN,
                    })
                elif bt == 'thinking':
                    result['hasThinking'] = True

            if texts:
                result['text'] = '\n'.join(texts)
            if tool_uses:
                result['tool_uses'] = tool_uses

        elif t == 'progress':
            data = raw.get('data', {})
            result['progress'] = {
                'type': data.get('type', ''),
                'content': str(data.get('content', ''))[:200],
            }

        elif t == 'system':
            result['subtype'] = raw.get('subtype', '')
            result['level'] = raw.get('level', '')

        return result

    # ── OpenCode event reading (SQLite) ────────────────────────────────

    @classmethod
    def _read_opencode_events(cls, path, after=None, limit=100, types=None):
        """Read OpenCode messages/parts from SQLite into unified events."""
        ses_id = str(path).split('::', 1)[1]
        events = []

        try:
            con = SessionIndex._open_opencode_db_ro()
        except Exception:
            return events

        try:
            rows = con.execute(
                'SELECT id, time_created, data FROM message'
                ' WHERE session_id = ? ORDER BY time_created',
                (ses_id,),
            ).fetchall()

            skipping = bool(after)
            for msg_id, t_created, data_raw in rows:
                uuid = 'ocmsg-' + msg_id
                if skipping:
                    if after == uuid or after.startswith(uuid + '-tr'):
                        skipping = False
                    continue
                try:
                    mdata = json.loads(data_raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                role = mdata.get('role', '')
                ts = cls._ms_to_iso(t_created)

                parts = []
                try:
                    prows = con.execute(
                        'SELECT data FROM part'
                        ' WHERE session_id = ? AND message_id = ?'
                        ' ORDER BY time_created',
                        (ses_id, msg_id),
                    ).fetchall()
                    for r in prows:
                        try:
                            parts.append(json.loads(r[0]))
                        except (json.JSONDecodeError, ValueError):
                            continue
                except sqlite3.Error:
                    parts = []

                evs = cls._parse_opencode_message(uuid, ts, role, mdata, parts)
                # Emit the whole message atomically so a mid-message limit
                # can never strand its tool results on the next poll.
                batch = [
                    ev for ev in evs
                    if ev['type'] not in cls._SKIP_TYPES
                    and (not types or ev['type'] in types)
                ]
                events.extend(batch)
                if len(events) >= limit:
                    break
        except sqlite3.Error:
            pass
        finally:
            con.close()

        return events

    @classmethod
    def _read_opencode_legacy_events(cls, path, after=None, limit=100, types=None):
        """Read messages/parts from the legacy OpenCode JSON store."""
        ses_id = str(path).split('::', 1)[1]
        events = []

        msg_dir = OPENCODE_STORAGE_DIR / 'message' / ses_id
        if not msg_dir.is_dir():
            return events
        part_root = OPENCODE_STORAGE_DIR / 'part'

        msgs = []
        for mf in msg_dir.glob('msg_*.json'):
            try:
                d = json.loads(mf.read_text(encoding='utf-8', errors='replace'))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            t = ((d.get('time') or {}).get('created')) or 0
            msgs.append((t, mf.stem, d))
        msgs.sort(key=lambda x: (x[0], x[1]))

        skipping = bool(after)
        for t_created, _name, mdata in msgs:
            uuid = 'ocmsg-' + mdata.get('id', _name)
            if skipping:
                if after == uuid or after.startswith(uuid + '-tr'):
                    skipping = False
                continue
            role = mdata.get('role', '')
            ts = cls._ms_to_iso(t_created)

            parts = []
            p_dir = part_root / mdata.get('id', _name)
            if p_dir.is_dir():
                for pf in sorted(p_dir.glob('prt_*.json')):
                    try:
                        parts.append(json.loads(pf.read_text(encoding='utf-8', errors='replace')))
                    except (OSError, json.JSONDecodeError, ValueError):
                        continue

            evs = cls._parse_opencode_message(uuid, ts, role, mdata, parts)
            batch = [
                ev for ev in evs
                if ev['type'] not in cls._SKIP_TYPES
                and (not types or ev['type'] in types)
            ]
            events.extend(batch)
            if len(events) >= limit:
                break

        return events

    @staticmethod
    def _parse_opencode_message(uuid, ts, role, mdata, parts):
        """Convert one OpenCode message (+parts) into unified event(s)."""
        events = []
        texts = []
        tool_uses = []
        tool_results = []

        for i, p in enumerate(parts):
            pt = p.get('type', '')
            if pt == 'text':
                texts.append(p.get('text', ''))
            elif pt == 'tool':
                state = p.get('state') or {}
                call_id = p.get('callID', '')
                inp = state.get('input') or {}
                inp_str = inp if isinstance(inp, str) else json.dumps(inp, ensure_ascii=False)
                truncated_in = len(inp_str) > MAX_TOOL_INPUT_LEN
                tool_uses.append({
                    'type': 'tool_use',
                    'id': call_id,
                    'name': p.get('tool', ''),
                    'input': inp_str[:MAX_TOOL_INPUT_LEN],
                    'truncated': truncated_in,
                })
                if state.get('status') == 'completed':
                    out = state.get('output', '')
                    out_str = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                    tool_results.append({
                        'tool_use_id': call_id,
                        'type': 'tool_result',
                        'content': out_str[:MAX_TOOL_RESULT_LEN],
                        'truncated': len(out_str) > MAX_TOOL_RESULT_LEN,
                    })

        joined = '\n'.join(texts)

        if role == 'user':
            ev = {
                'type': 'user',
                'uuid': uuid,
                'timestamp': ts,
                'userType': 'external',
            }
            if joined:
                ev['text'] = joined[:MAX_TEXT_LEN]
                ev['truncated'] = len(joined) > MAX_TEXT_LEN
            events.append(ev)
        elif role == 'assistant':
            tokens = mdata.get('tokens', {}) or {}
            cache = tokens.get('cache', {}) or {}
            usage = {
                'input_tokens': tokens.get('input', 0),
                'output_tokens': tokens.get('output', 0),
                'cache_read_input_tokens': cache.get('read', 0),
                'cache_creation_input_tokens': cache.get('write', 0),
            }
            ev = {
                'type': 'assistant',
                'uuid': uuid,
                'timestamp': ts,
                'model': mdata.get('modelID', ''),
                'usage': usage,
                'stop_reason': mdata.get('finish', ''),
            }
            if joined:
                ev['text'] = joined[:MAX_TEXT_LEN]
                ev['truncated'] = len(joined) > MAX_TEXT_LEN
            if tool_uses:
                ev['tool_uses'] = tool_uses
            events.append(ev)

        # Tool results as follow-up user events (codex-style)
        for j, tr in enumerate(tool_results):
            events.append({
                'type': 'user',
                'uuid': f'{uuid}-tr{j}',
                'timestamp': ts,
                'userType': 'tool_result',
                'tool_results': [tr],
            })

        return events

    # ── Hermes benchmark event reading ──────────────────────────────────

    @staticmethod
    def _bench_pred_files(run_dir):
        preds_dir = run_dir / 'predictions'
        files = list(preds_dir.rglob('*.jsonl')) if preds_dir.is_dir() else []
        if not files:
            retry = run_dir / 'retry_results.jsonl'
            if retry.exists():
                files = [retry]
        return files

    @classmethod
    def _build_bench_line_events(cls, line_idx, raw):
        """Build (user question, assistant answer) events from one prediction/retry line."""
        ts = ''
        uuid_q = f'bench-{line_idx}-q'
        uuid_a = f'bench-{line_idx}-a'

        if 'messages' in raw:
            # Full prediction line (evalscope run)
            prompt_msgs = raw.get('messages') or []
            q_text = ''
            for m in prompt_msgs:
                if m.get('role') == 'user':
                    c = m.get('content', '')
                    q_text = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
                    break
            mo = raw.get('model_output') or {}
            choices = mo.get('choices') or [{}]
            msg = (choices[0] or {}).get('message') or {}
            blocks = msg.get('content')
            a_parts = []
            if isinstance(blocks, list):
                for b in blocks:
                    if not isinstance(b, dict):
                        continue
                    if b.get('type') == 'reasoning' and b.get('reasoning'):
                        a_parts.append(str(b['reasoning']))
                    elif b.get('type') == 'text' and b.get('text'):
                        a_parts.append(str(b['text']))
            elif isinstance(blocks, str):
                a_parts.append(blocks)
            usage = mo.get('usage') or {}
            latency = mo.get('time', '')
            meta = raw.get('metadata') or {}
            correct = meta.get('correct_answer', '')
            if correct:
                a_parts.append(f"\n---\nCorrect answer: {correct}")
        else:
            # Recovery retry line (compact result summary; content not saved)
            idx = raw.get('sample_index', line_idx)
            q_text = f"[Truncation recovery retry — sample {idx} (question content not saved)]"
            verdict = 'correct' if raw.get('correct') else 'incorrect/unfinished'
            extracted = 'yes' if raw.get('answer_extracted') else 'no'
            a_parts = [
                f"Retry result: {verdict}",
                f"Answer extracted: {extracted} · finish: {raw.get('finish_reason', '')}"
                f" · latency: {raw.get('latency_s', 0):.0f}s",
            ]
            usage = {
                'input_tokens': raw.get('input_tokens', 0),
                'output_tokens': raw.get('output_tokens', 0),
            }
            latency = raw.get('latency_s', '')

        q_text = q_text.strip() or '[empty prompt]'
        q_event = {
            'type': 'user',
            'uuid': uuid_q,
            'timestamp': ts,
            'userType': 'external',
            'text': q_text[:MAX_TEXT_LEN],
            'truncated': len(q_text) > MAX_TEXT_LEN,
        }

        answer_text = '\n\n'.join(a_parts).strip()
        usage_out = {
            'input_tokens': usage.get('input_tokens', 0) or 0,
            'output_tokens': usage.get('output_tokens', 0) or 0,
        }
        a_event = {
            'type': 'assistant',
            'uuid': uuid_a,
            'timestamp': ts,
            'model': '',
            'usage': usage_out,
            'stop_reason': raw.get('finish_reason', '') or '',
        }
        if answer_text:
            a_event['text'] = answer_text[:MAX_TEXT_LEN]
            a_event['truncated'] = len(answer_text) > MAX_TEXT_LEN
        else:
            a_event['text'] = '[no output]'
        if latency not in ('', None):
            try:
                a_event['latency_seconds'] = round(float(latency), 1)
            except (TypeError, ValueError):
                pass
        return q_event, a_event

    @classmethod
    def _read_hermes_bench_events(cls, path, after=None, limit=100, types=None):
        """Read a Hermes eval run directory into unified events."""
        run_dir = Path(path)

        manifest = SessionIndex._read_json_file(run_dir / 'run_manifest.json')
        if manifest is None:
            manifest = SessionIndex._read_json_file(run_dir / 'recovery_manifest.json')
        watcher = SessionIndex._read_json_file(run_dir / '.watcher_state.json') or {}

        events = []

        # Run config summary
        cfg_uuid = 'bench-config'
        show_config = not after and (not types or 'assistant' in types)
        if show_config:
            cfg_lines = []
            if isinstance(manifest, dict):
                kind = manifest.get('kind')
                if kind:
                    cfg_lines.append(f"Run kind: {kind}")
                model = manifest.get('model', '')
                if model:
                    cfg_lines.append(f"Model: `{Path(model.replace('\\', '/')).name}`")
                for key in ('benchmark', 'dataset_hub', 'evalscope_version', 'api_url'):
                    if manifest.get(key):
                        cfg_lines.append(f"{key}: {manifest[key]}")
                gc = manifest.get('generation_config') or {}
                if gc:
                    cfg_lines.append(
                        "Sampling: temp={temperature}, top_p={top_p}, top_k={top_k},"
                        " max_tokens={max_tokens}".format(**{**{
                            'temperature': '?', 'top_p': '?', 'top_k': '?',
                            'max_tokens': '?'}, **gc})
                    )
            if cfg_lines:
                events.append({
                    'type': 'assistant',
                    'uuid': cfg_uuid,
                    'timestamp': '',
                    'model': '',
                    'usage': {},
                    'stop_reason': '',
                    'text': '**Benchmark config**\n\n' + '\n'.join(cfg_lines),
                })
        elif after == cfg_uuid:
            pass  # cursor on config: proceed to lines

        # Per-sample events with cursor support
        pred_files = cls._bench_pred_files(run_dir)
        after_idx = -1
        if after:
            m = re.match(r'bench-(\d+)-[qa]$', after)
            if m:
                after_idx = int(m.group(1))
            elif after == 'bench-report':
                after_idx = 10 ** 12   # everything already consumed
            # 'bench-config' → after_idx stays -1 (config is emitted above
            # only on the initial load, so nothing to skip)

        remaining = max(0, limit - len(events))
        reached_eof = True
        for pf in pred_files:
            try:
                with open(pf, 'r', encoding='utf-8', errors='replace') as f:
                    for i, line in enumerate(f):
                        if i <= after_idx:
                            continue
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        q_ev, a_ev = cls._build_bench_line_events(i, raw)
                        for ev in (q_ev, a_ev):
                            if ev['type'] in cls._SKIP_TYPES:
                                continue
                            if types and ev['type'] not in types:
                                continue
                            events.append(ev)
                            remaining -= 1
                        if remaining <= 0:
                            reached_eof = False
                            break
            except OSError:
                continue
            if remaining <= 0:
                break

        # Final report summary — only once the whole stream was consumed,
        # so it never lands mid-pagination.
        if reached_eof and (not types or 'assistant' in types):
            report = None
            reports_dir = run_dir / 'reports'
            if reports_dir.is_dir():
                for rep_file in sorted(reports_dir.rglob('*.json')):
                    candidate = SessionIndex._read_json_file(rep_file)
                    if candidate and candidate.get('score') is not None:
                        report = candidate
                        break
            rep_bits = []
            if report:
                score = report.get('score')
                num = report.get('num', '')
                name = report.get('dataset_pretty_name') or report.get('dataset_name', '')
                pct = f"{score:.1%}" if isinstance(score, (int, float)) else '?'
                rep_bits.append(f"Final score ({name}): **{pct}** over {num} samples")
            progress = SessionIndex._read_json_file(run_dir / 'progress.json') or {}
            candidates_done = [
                v for v in (
                    watcher.get('done', watcher.get('completed')),
                    progress.get('processed_count', progress.get('completed')),
                ) if isinstance(v, (int, float))
            ]
            done = int(max(candidates_done)) if candidates_done else None
            total = watcher.get('total',
                    progress.get('total_count', progress.get('total')))
            status = watcher.get('status') or progress.get('status') or ''
            if isinstance(total, (int, float)) and done is not None and done >= total > 0:
                status = 'completed'
            if status:
                rep_bits.append(f"Status: {status}" +
                                (f" ({done}/{int(total)})" if done is not None and total else ""))
            acc = watcher.get('accuracy', watcher.get('merged_accuracy'))
            if acc is not None:
                try:
                    rep_bits.append(f"Merged accuracy: {float(acc):.1%}")
                except (TypeError, ValueError):
                    pass
            if rep_bits:
                events.append({
                    'type': 'assistant',
                    'uuid': 'bench-report',
                    'timestamp': '',
                    'model': '',
                    'usage': {},
                    'stop_reason': '',
                    'text': '**Result**\n\n' + '\n'.join(rep_bits),
                })

        return events

    # ── inline subagent scanning (OpenClaw sessions_spawn) ──────────────

    @staticmethod
    def scan_inline_subagents(path):
        """Parse sessions_spawn tool calls to find ephemeral subagents.

        OpenClaw subagents are spawned via the sessions_spawn tool and don't
        persist as separate JSONL files. We extract their metadata from
        the parent session's event stream.
        """
        calls = {}   # toolCallId -> {task, model, timestamp}
        results = {} # toolCallId -> {status, childSessionKey, runId, error}

        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line or 'sessions_spawn' not in line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    msg = raw.get('message', {})
                    role = msg.get('role', '')
                    content = msg.get('content', [])
                    ts = raw.get('timestamp', '')

                    # Assistant message with toolCall blocks
                    if isinstance(content, list):
                        for block in content:
                            if (isinstance(block, dict)
                                    and block.get('type') == 'toolCall'
                                    and block.get('name') == 'sessions_spawn'):
                                cid = block.get('id', '')
                                args = block.get('arguments', {})
                                calls[cid] = {
                                    'task': args.get('task', ''),
                                    'model': args.get('model', ''),
                                    'mode': args.get('mode', 'run'),
                                    'timestamp': ts,
                                }

                    # toolResult role
                    if role == 'toolResult' and msg.get('toolName') == 'sessions_spawn':
                        tcid = msg.get('toolCallId', '')
                        rc = ''
                        if isinstance(content, list):
                            for b in content:
                                if isinstance(b, dict) and b.get('type') == 'text':
                                    rc = b.get('text', '')
                                    break
                        elif isinstance(content, str):
                            rc = content
                        if rc:
                            try:
                                parsed = json.loads(rc)
                                results[tcid] = {
                                    'status': parsed.get('status', ''),
                                    'childSessionKey': parsed.get('childSessionKey', ''),
                                    'runId': parsed.get('runId', ''),
                                    'error': parsed.get('error', ''),
                                }
                            except (json.JSONDecodeError, KeyError):
                                pass

        except OSError:
            return []

        # Build subagent list from paired calls + results (accepted only)
        subagents = []
        for tcid, result in results.items():
            status = result.get('status', 'unknown')
            if status != 'accepted':
                continue  # skip failed spawn attempts

            call = calls.get(tcid, {})
            child_key = result.get('childSessionKey', '')
            # Extract UUID from childSessionKey like "agent:tester:subagent:UUID"
            parts = child_key.split(':')
            sa_id = parts[-1] if len(parts) >= 4 else child_key

            task = call.get('task', '')
            # Use first 60 chars of task as slug
            slug = task[:60].replace('\n', ' ').strip()
            if len(task) > 60:
                slug += '...'

            subagents.append({
                'id': 'oc-spawn-' + sa_id[:12],
                'parentId': '',  # filled by caller
                'agentType': 'openclaw-spawn',
                'slug': slug,
                'model': call.get('model', ''),
                'status': 'completed',
                'ephemeral': True,
                'spawnStatus': status,
                'runId': result.get('runId', ''),
                'childSessionKey': child_key,
                'timestamp': call.get('timestamp', ''),
                'mtime': 0,  # no file mtime
            })

        return subagents

    # ── raw event & stats ─────────────────────────────────────────────────

    # ── raw event & stats ─────────────────────────────────────────────────

    @classmethod
    def _get_opencode_raw_event(cls, path, event_uuid):
        ses_id = str(path).split('::', 1)[1]
        msg_id = event_uuid
        if msg_id.startswith('ocmsg-'):
            msg_id = msg_id[len('ocmsg-'):]
        m = re.match(r'^(msg_.+?)-tr\d*$', msg_id)
        if m:
            msg_id = m.group(1)

        try:
            con = SessionIndex._open_opencode_db_ro()
        except Exception:
            return None
        try:
            row = con.execute(
                'SELECT id, time_created, time_updated, data FROM message'
                ' WHERE session_id = ? AND id = ?',
                (ses_id, msg_id),
            ).fetchone()
            if not row:
                return None
            raw = json.loads(row[3])
            parts = []
            for r in con.execute(
                'SELECT data FROM part'
                ' WHERE session_id = ? AND message_id = ? ORDER BY time_created',
                (ses_id, msg_id),
            ):
                try:
                    parts.append(json.loads(r[0]))
                except (json.JSONDecodeError, ValueError):
                    continue
            raw['_parts'] = parts
            return raw
        except (sqlite3.Error, json.JSONDecodeError, ValueError):
            return None
        finally:
            con.close()

    @classmethod
    def _get_hermes_bench_raw_event(cls, path, event_uuid):
        run_dir = Path(path)

        if event_uuid == 'bench-config':
            manifest = SessionIndex._read_json_file(run_dir / 'run_manifest.json')
            if manifest is None:
                manifest = SessionIndex._read_json_file(run_dir / 'recovery_manifest.json')
            return manifest

        if event_uuid == 'bench-report':
            reports_dir = run_dir / 'reports'
            if reports_dir.is_dir():
                for rep_file in sorted(reports_dir.rglob('*.json')):
                    report = SessionIndex._read_json_file(rep_file)
                    if report and report.get('score') is not None:
                        return report
            return None

        m = re.match(r'bench-(\d+)-[qa]$', event_uuid)
        if not m:
            return None
        line_idx = int(m.group(1))
        for pf in cls._bench_pred_files(run_dir):
            try:
                with open(pf, 'r', encoding='utf-8', errors='replace') as f:
                    for i, line in enumerate(f):
                        if i == line_idx:
                            try:
                                return json.loads(line.strip())
                            except json.JSONDecodeError:
                                return None
            except OSError:
                continue
        return None

    @classmethod
    def _get_opencode_legacy_raw_event(cls, path, event_uuid):
        ses_id = str(path).split('::', 1)[1]
        msg_id = event_uuid
        if msg_id.startswith('ocmsg-'):
            msg_id = msg_id[len('ocmsg-'):]
        m = re.match(r'^(msg_.+?)-tr\d*$', msg_id)
        if m:
            msg_id = m.group(1)

        msg_file = OPENCODE_STORAGE_DIR / 'message' / ses_id / f'{msg_id}.json'
        try:
            raw = json.loads(msg_file.read_text(encoding='utf-8', errors='replace'))
        except (OSError, json.JSONDecodeError, ValueError):
            return None

        parts = []
        p_dir = OPENCODE_STORAGE_DIR / 'part' / msg_id
        if p_dir.is_dir():
            for pf in sorted(p_dir.glob('prt_*.json')):
                try:
                    parts.append(json.loads(pf.read_text(encoding='utf-8', errors='replace')))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
        raw['_parts'] = parts
        return raw

    @classmethod
    def get_raw_event(cls, path, event_uuid):
        if cls._is_opencode_path(path):
            return cls._get_opencode_raw_event(path, event_uuid)
        if cls._is_legacy_opencode_path(path):
            return cls._get_opencode_legacy_raw_event(path, event_uuid)
        if cls._is_hermes_state_path(path):
            sid = str(path).split('://', 1)[1]
            m = re.match(r'hstate-(\d+)$', event_uuid)
            if not m:
                return None
            raw = _wsl_hermes_bridge(['raw', sid, m.group(1)], timeout=12)
            return raw if isinstance(raw, dict) and not raw.get('error') else None
        if cls._is_hermes_bench_path(path):
            return cls._get_hermes_bench_raw_event(path, event_uuid)
        if cls._is_hermes_session_path(path):
            m = re.match(r'hmsg-(\d+)$', event_uuid)
            if m:
                try:
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        data = json.load(f)
                    idx = int(m.group(1))
                    messages = data.get('messages') or []
                    if 0 <= idx < len(messages):
                        return messages[idx]
                except (OSError, json.JSONDecodeError, ValueError):
                    pass
            return None
        try:
            # Codex/Kimi: line-number based lookup
            for prefix in ('codex-', 'kimi-'):
                if event_uuid.startswith(prefix):
                    try:
                        target_line = int(event_uuid[len(prefix):])
                    except (ValueError, IndexError):
                        return None
                    with open(path, 'r', encoding='utf-8', errors='replace') as f:
                        for i, line in enumerate(f):
                            if i == target_line:
                                return json.loads(line.strip())
                    return None

            size = os.path.getsize(path)
            # Try tail first
            if size > TAIL_READ_BYTES:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    f.seek(max(0, size - TAIL_READ_BYTES))
                    f.readline()
                    for line in f:
                        try:
                            obj = json.loads(line.strip())
                            if obj.get('uuid') == event_uuid or obj.get('id') == event_uuid:
                                return obj
                        except json.JSONDecodeError:
                            pass
            # Full scan
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        if obj.get('uuid') == event_uuid or obj.get('id') == event_uuid:
                            return obj
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
        return None

    @classmethod
    def _get_opencode_stats(cls, path):
        ses_id = str(path).split('::', 1)[1]
        stats = {
            'model': '', 'version': '', 'cwd': '', 'gitBranch': '',
            'tokens': {
                'input': 0, 'output': 0,
                'cache_read': 0, 'cache_creation': 0,
            },
            'tool_calls': {},
            'event_counts': {},
            'first_timestamp': '',
            'last_timestamp': '',
            'duration_seconds': 0,
        }
        try:
            con = SessionIndex._open_opencode_db_ro()
        except Exception:
            return stats
        try:
            row = con.execute(
                'SELECT directory, version, model, tokens_input, tokens_output,'
                ' tokens_cache_read, tokens_cache_write, time_created, time_updated'
                ' FROM session WHERE id = ?',
                (ses_id,),
            ).fetchone()
            if not row:
                return stats
            stats['cwd'] = str(row[0] or '')
            stats['version'] = str(row[1] or '')
            try:
                mobj = json.loads(row[2]) if row[2] else {}
                if isinstance(mobj, dict):
                    stats['model'] = mobj.get('id', '')
            except (json.JSONDecodeError, ValueError):
                stats['model'] = row[2] or ''
            stats['tokens'] = {
                'input': row[3] or 0,
                'output': row[4] or 0,
                'cache_read': row[5] or 0,
                'cache_creation': row[6] or 0,
            }
            t0, t1 = row[7] or 0, row[8] or 0
            stats['first_timestamp'] = cls._ms_to_iso(t0)
            stats['last_timestamp'] = cls._ms_to_iso(t1)
            stats['duration_seconds'] = int((t1 - t0) / 1000) if t1 and t0 else 0

            for r in con.execute(
                'SELECT data FROM message WHERE session_id = ?', (ses_id,)
            ):
                try:
                    role = json.loads(r[0]).get('role', '?')
                except (json.JSONDecodeError, ValueError):
                    role = '?'
                stats['event_counts'][role] = stats['event_counts'].get(role, 0) + 1

            for r in con.execute(
                'SELECT data FROM part'
                ' WHERE session_id = ? LIMIT 20000',
                (ses_id,),
            ):
                try:
                    pdata = json.loads(r[0])
                except (json.JSONDecodeError, ValueError):
                    continue
                if pdata.get('type') != 'tool':
                    continue
                tool_name = pdata.get('tool', '') or 'unknown'
                stats['tool_calls'][tool_name] = stats['tool_calls'].get(tool_name, 0) + 1
        except sqlite3.Error:
            pass
        finally:
            con.close()
        return stats

    @classmethod
    def _get_hermes_bench_stats(cls, path):
        run_dir = Path(path)
        stats = {
            'model': '', 'version': '', 'cwd': '', 'gitBranch': '',
            'tokens': {
                'input': 0, 'output': 0,
                'cache_read': 0, 'cache_creation': 0,
            },
            'tool_calls': {},
            'event_counts': {'sample': 0, 'correct': 0},
            'first_timestamp': '',
            'last_timestamp': '',
            'duration_seconds': 0,
        }
        manifest = SessionIndex._read_json_file(run_dir / 'run_manifest.json')
        if manifest is None:
            manifest = SessionIndex._read_json_file(run_dir / 'recovery_manifest.json')
        if isinstance(manifest, dict):
            model = manifest.get('model', '')
            stats['model'] = Path(model.replace('\\', '/')).name if model else ''
            stats['version'] = str(manifest.get('evalscope_version', '') or '')
            stats['cwd'] = str(manifest.get('api_url', '') or '')

        first_ts = last_ts = None
        probe = run_dir / 'run_manifest.json'
        if not probe.exists():
            probe = run_dir / 'recovery_manifest.json'
        try:
            if probe.exists():
                first_ts = probe.stat().st_mtime
            last_ts = run_dir.stat().st_mtime
        except OSError:
            pass

        for pf in cls._bench_pred_files(run_dir):
            try:
                st = pf.stat()
                if first_ts is None or st.st_mtime < first_ts:
                    first_ts = st.st_mtime
                if last_ts is None or st.st_mtime > last_ts:
                    last_ts = st.st_mtime
                with open(pf, 'r', encoding='utf-8', errors='replace') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            raw = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        stats['event_counts']['sample'] += 1
                        if 'messages' in raw:
                            usage = ((raw.get('model_output') or {}).get('usage') or {})
                            stats['tokens']['input'] += usage.get('input_tokens', 0) or 0
                            stats['tokens']['output'] += usage.get('output_tokens', 0) or 0
                        else:
                            stats['tokens']['input'] += raw.get('input_tokens', 0) or 0
                            stats['tokens']['output'] += raw.get('output_tokens', 0) or 0
                            if raw.get('correct'):
                                stats['event_counts']['correct'] += 1
            except OSError:
                continue

        if first_ts:
            stats['first_timestamp'] = datetime.fromtimestamp(first_ts).isoformat()
        if last_ts:
            stats['last_timestamp'] = datetime.fromtimestamp(last_ts).isoformat()
        if first_ts and last_ts:
            stats['duration_seconds'] = int(last_ts - first_ts)

        # Final score from report
        reports_dir = run_dir / 'reports'
        if reports_dir.is_dir():
            for rep_file in sorted(reports_dir.rglob('*.json')):
                report = SessionIndex._read_json_file(rep_file)
                if report and report.get('score') is not None:
                    stats['event_counts']['final_score'] = report.get('score')
                    break
        return stats

    @classmethod
    def _get_opencode_legacy_stats(cls, path):
        ses_id = str(path).split('::', 1)[1]
        stats = {
            'model': '', 'version': '', 'cwd': '', 'gitBranch': '',
            'tokens': {
                'input': 0, 'output': 0,
                'cache_read': 0, 'cache_creation': 0,
            },
            'tool_calls': {},
            'event_counts': {},
            'first_timestamp': '',
            'last_timestamp': '',
            'duration_seconds': 0,
        }

        sess_file = None
        sess_dir = OPENCODE_STORAGE_DIR / 'session'
        if sess_dir.is_dir():
            for pd in sess_dir.iterdir():
                cand = pd / f'{ses_id}.json'
                if cand.exists():
                    sess_file = cand
                    break
        if sess_file:
            try:
                d = json.loads(sess_file.read_text(encoding='utf-8', errors='replace'))
                stats['cwd'] = str(d.get('directory') or '')
                stats['version'] = str(d.get('version') or '')
            except (OSError, json.JSONDecodeError, ValueError):
                pass

        msg_dir = OPENCODE_STORAGE_DIR / 'message' / ses_id
        if not msg_dir.is_dir():
            return stats

        first_ms = last_ms = None
        last_model_ts = -1
        msg_ids = []
        for mf in msg_dir.glob('msg_*.json'):
            try:
                d = json.loads(mf.read_text(encoding='utf-8', errors='replace'))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            msg_ids.append(d.get('id') or mf.stem)
            role = d.get('role', '?')
            stats['event_counts'][role] = stats['event_counts'].get(role, 0) + 1
            t0 = ((d.get('time') or {}).get('created')) or 0
            if role == 'assistant':
                tk = d.get('tokens') or {}
                stats['tokens']['input'] += tk.get('input', 0) or 0
                stats['tokens']['output'] += tk.get('output', 0) or 0
                cache = tk.get('cache') or {}
                stats['tokens']['cache_read'] += cache.get('read', 0) or 0
                stats['tokens']['cache_creation'] += cache.get('write', 0) or 0
                if t0 >= last_model_ts and d.get('modelID'):
                    last_model_ts = t0
                    stats['model'] = d['modelID']
            if t0:
                if first_ms is None or t0 < first_ms:
                    first_ms = t0
                if last_ms is None or t0 > last_ms:
                    last_ms = t0

        # Tool parts live under part/<messageID>/ — check each message's dir
        p_root = OPENCODE_STORAGE_DIR / 'part'
        for mid in msg_ids:
            p_dir = p_root / mid
            if not p_dir.is_dir():
                continue
            for pf in p_dir.glob('prt_*.json'):
                try:
                    pdata = json.loads(pf.read_text(encoding='utf-8', errors='replace'))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
                if pdata.get('type') != 'tool':
                    continue
                tool_name = pdata.get('tool', '') or 'unknown'
                stats['tool_calls'][tool_name] = stats['tool_calls'].get(tool_name, 0) + 1

        stats['first_timestamp'] = cls._ms_to_iso(first_ms)
        stats['last_timestamp'] = cls._ms_to_iso(last_ms)
        if first_ms and last_ms:
            stats['duration_seconds'] = int((last_ms - first_ms) / 1000)
        return stats

    @classmethod
    def get_stats(cls, path):
        if cls._is_opencode_path(path):
            return cls._get_opencode_stats(path)
        if cls._is_legacy_opencode_path(path):
            return cls._get_opencode_legacy_stats(path)
        if cls._is_hermes_state_path(path):
            return cls._get_hermes_state_stats(path)
        if cls._is_hermes_session_path(path):
            return cls._get_hermes_stats(path)
        if cls._is_hermes_bench_path(path):
            return cls._get_hermes_bench_stats(path)
        stats = {
            'model': '', 'version': '', 'cwd': '', 'gitBranch': '',
            'tokens': {
                'input': 0, 'output': 0,
                'cache_read': 0, 'cache_creation': 0,
            },
            'tool_calls': {},
            'event_counts': {},
            'first_timestamp': '',
            'last_timestamp': '',
            'duration_seconds': 0,
        }
        first_ts = last_ts = None

        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        raw = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    et = raw.get('type', '')
                    stats['event_counts'][et] = stats['event_counts'].get(et, 0) + 1

                    ts = raw.get('timestamp', '')
                    if ts:
                        if isinstance(ts, (int, float)):
                            ts = datetime.fromtimestamp(
                                ts / 1000 if ts > 1e12 else ts
                            ).isoformat()
                        if first_ts is None:
                            first_ts = ts
                        last_ts = ts

                    if not stats['cwd'] and raw.get('cwd'):
                        stats['cwd'] = raw['cwd']
                    if not stats['gitBranch'] and raw.get('gitBranch'):
                        stats['gitBranch'] = raw['gitBranch']
                    if not stats['version'] and raw.get('version'):
                        stats['version'] = str(raw['version'])

                    msg = raw.get('message', {})

                    # Claude Code native: type == 'assistant'
                    if et == 'assistant':
                        if not stats['model'] and msg.get('model'):
                            stats['model'] = msg['model']
                        usage = msg.get('usage', {})
                        stats['tokens']['input'] += usage.get('input_tokens', 0)
                        stats['tokens']['output'] += usage.get('output_tokens', 0)
                        stats['tokens']['cache_read'] += usage.get(
                            'cache_read_input_tokens', 0
                        )
                        stats['tokens']['cache_creation'] += usage.get(
                            'cache_creation_input_tokens', 0
                        )
                        for block in msg.get('content', []):
                            if isinstance(block, dict) and block.get('type') == 'tool_use':
                                name = block.get('name', 'unknown')
                                stats['tool_calls'][name] = (
                                    stats['tool_calls'].get(name, 0) + 1
                                )

                    # OpenClaw format: type == 'message'
                    elif et == 'message':
                        role = msg.get('role', '')
                        if role == 'assistant':
                            model = msg.get('model', '')
                            if not stats['model'] and model and model != 'delivery-mirror':
                                stats['model'] = model
                            usage = msg.get('usage', {})
                            # OpenClaw uses 'input'/'output', Claude uses 'input_tokens'/'output_tokens'
                            stats['tokens']['input'] += (
                                usage.get('input_tokens', 0) or usage.get('input', 0)
                            )
                            stats['tokens']['output'] += (
                                usage.get('output_tokens', 0) or usage.get('output', 0)
                            )
                            stats['tokens']['cache_read'] += (
                                usage.get('cache_read_input_tokens', 0) or usage.get('cacheRead', 0)
                            )
                            stats['tokens']['cache_creation'] += (
                                usage.get('cache_creation_input_tokens', 0) or usage.get('cacheWrite', 0)
                            )
                            for block in msg.get('content', []):
                                if isinstance(block, dict) and block.get('type') == 'toolCall':
                                    name = block.get('name', 'unknown')
                                    stats['tool_calls'][name] = (
                                        stats['tool_calls'].get(name, 0) + 1
                                    )

                    # Codex format
                    elif et in ('session_meta', 'turn_context',
                                'response_item', 'event_msg'):
                        payload = raw.get('payload', {})
                        if not isinstance(payload, dict):
                            continue
                        pt = payload.get('type', '')

                        if et == 'session_meta':
                            if not stats['cwd']:
                                stats['cwd'] = payload.get('cwd', '')
                            if not stats['version']:
                                stats['version'] = payload.get('cli_version', '')
                            git = payload.get('git', {})
                            if isinstance(git, dict) and not stats['gitBranch']:
                                stats['gitBranch'] = git.get('branch', '')

                        elif et == 'turn_context':
                            if not stats['model']:
                                stats['model'] = payload.get('model', '')
                            if not stats['cwd']:
                                stats['cwd'] = payload.get('cwd', '')

                        elif et == 'event_msg' and pt == 'token_count':
                            info = payload.get('info')
                            if isinstance(info, dict):
                                usage = info.get('total_token_usage', {})
                                if isinstance(usage, dict):
                                    # Cumulative — overwrite with latest
                                    inp = usage.get('input_tokens', 0)
                                    out = usage.get('output_tokens', 0)
                                    cached = usage.get('cached_input_tokens', 0)
                                    reasoning = usage.get('reasoning_output_tokens', 0)
                                    if inp:
                                        stats['tokens']['input'] = inp
                                    if out:
                                        stats['tokens']['output'] = out + reasoning
                                    if cached:
                                        stats['tokens']['cache_read'] = cached

                        elif et == 'response_item':
                            if pt in ('function_call', 'custom_tool_call'):
                                name = payload.get('name', 'unknown')
                                stats['tool_calls'][name] = (
                                    stats['tool_calls'].get(name, 0) + 1
                                )

                    # Kimi format
                    elif et == 'TurnBegin':
                        if not stats['model']:
                            stats['model'] = raw.get('model', '') or (raw.get('payload') or {}).get('model', '')
                        if not stats['version']:
                            stats['version'] = raw.get('version', '')
                    elif et == 'TurnEnd':
                        # Kimi TurnEnd reports per-turn token usage (additive across turns)
                        usage = raw.get('usage') or (raw.get('payload') or {}).get('usage', {})
                        if isinstance(usage, dict):
                            stats['tokens']['input'] += usage.get('input_tokens', 0)
                            stats['tokens']['output'] += usage.get('output_tokens', 0)
                            stats['tokens']['cache_read'] += usage.get('cache_read_input_tokens', 0)
                            stats['tokens']['cache_creation'] += usage.get('cache_creation_input_tokens', 0)
                    elif et == 'ToolCall':
                        name = raw.get('name', '') or (raw.get('payload') or {}).get('name', 'unknown')
                        stats['tool_calls'][name] = stats['tool_calls'].get(name, 0) + 1

        except OSError as e:
            print(f"Error reading stats: {e}", file=sys.stderr)

        stats['first_timestamp'] = first_ts or ''
        stats['last_timestamp'] = last_ts or ''

        if first_ts and last_ts:
            try:
                t1 = datetime.fromisoformat(str(first_ts).replace('Z', '+00:00'))
                t2 = datetime.fromisoformat(str(last_ts).replace('Z', '+00:00'))
                stats['duration_seconds'] = max(0, int((t2 - t1).total_seconds()))
            except (ValueError, TypeError):
                pass

        return stats


# ─── Global index instance ─────────────────────────────────────────────────────

session_index = SessionIndex()


# ─── HTTP Handler ──────────────────────────────────────────────────────────────

class TrackerAPIHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SCRIPT_DIR), **kwargs)

    def end_headers(self):
        origin = self.headers.get('Origin', '')
        if origin in ALLOWED_ORIGINS:
            self.send_header('Access-Control-Allow-Origin', origin)
        else:
            self.send_header('Access-Control-Allow-Origin', 'http://localhost:37778')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Cache-Control', 'no-cache')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        # ── Process monitor API (existing) ────────────────────────────────
        if path == '/api/tracker/status':
            self.handle_status()
        elif path == '/api/tracker/events':
            self.handle_events()
        elif path == '/api/tracker/history':
            self.handle_history()

        # ── Session viewer API (new) ──────────────────────────────────────
        elif path == '/api/sessions/list':
            self.handle_session_list(qs)
        elif re.match(r'^/api/sessions/[^/]+/events$', path):
            sid = path.split('/')[3]
            self.handle_session_events(sid, qs)
        elif re.match(r'^/api/sessions/[^/]+/stats$', path):
            sid = path.split('/')[3]
            self.handle_session_stats(sid)
        elif re.match(r'^/api/sessions/[^/]+/subagents$', path):
            sid = path.split('/')[3]
            self.handle_session_subagents(sid)
        elif re.match(r'^/api/sessions/[^/]+/compaction-summaries$', path):
            sid = path.split('/')[3]
            self.handle_session_compaction_summaries(sid)
        elif re.match(r'^/api/sessions/[^/]+/raw-event/[^/]+$', path):
            parts = path.split('/')
            self.handle_raw_event(parts[3], parts[5])

        # ── Static files ──────────────────────────────────────────────────
        elif path == '/':
            self.path = '/dashboard.html'
            super().do_GET()
        else:
            super().do_GET()

    # ── Process monitor handlers (existing) ───────────────────────────────

    def handle_status(self):
        try:
            data = self.collect_status()
            self.send_json(data)
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_events(self):
        try:
            events = self.get_recent_events(limit=50)
            self.send_json({'events': events})
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_history(self):
        try:
            history = self.get_daily_history(days=7)
            self.send_json({'history': history})
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    # ── Session viewer handlers (new) ─────────────────────────────────────

    def handle_session_list(self, qs):
        try:
            data = session_index.get_sessions(
                project=qs.get('project', [None])[0],
                status=qs.get('status', [None])[0],
                search=qs.get('search', [None])[0],
                date_from=qs.get('date_from', [None])[0],
                date_to=qs.get('date_to', [None])[0],
                sort=qs.get('sort', ['mtime'])[0],
                offset=self._qint(qs, 'offset', 0),
                limit=min(self._qint(qs, 'limit', 50), 200),
                source=qs.get('source', [None])[0],
                content_q=qs.get('content_q', [None])[0],
            )
            self.send_json(data)
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_session_events(self, session_id, qs):
        try:
            path = session_index.get_session_path(session_id)
            if not path:
                self.send_json({'error': 'Session not found'}, status=404)
                return

            after = qs.get('after', [None])[0]
            full = qs.get('full', [''])[0] == '1'
            limit = min(self._qint(qs, 'limit', 100), 10000 if full else 500)
            types_raw = qs.get('types', [None])[0]
            types = set(types_raw.split(',')) if types_raw else None

            events = SessionEventReader.read_events(
                path, after=after, limit=limit, types=types, full=full,
            )
            self.send_json({'events': events, 'sessionId': session_id})
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_session_stats(self, session_id):
        try:
            path = session_index.get_session_path(session_id)
            if not path:
                self.send_json({'error': 'Session not found'}, status=404)
                return
            stats = SessionEventReader.get_stats(path)
            self.send_json(stats)
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_session_subagents(self, session_id):
        try:
            subagents = session_index.get_subagents(session_id)

            # Also scan for inline (ephemeral) subagents from sessions_spawn
            path = session_index.get_session_path(session_id)
            if path:
                inline = SessionEventReader.scan_inline_subagents(path)
                for sa in inline:
                    sa['parentId'] = session_id
                subagents = subagents + inline

            self.send_json({
                'sessionId': session_id,
                'subagents': subagents,
                'total': len(subagents),
            })
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_session_compaction_summaries(self, session_id):
        try:
            subagents = session_index.get_subagents(session_id)
            compact_agents = [
                sa for sa in subagents
                if sa.get('agentType') == 'compact' and not sa.get('ephemeral')
            ]
            compact_agents.sort(key=lambda sa: (
                sa.get('mtime', 0),
                sa.get('filename', ''),
                sa.get('id', ''),
            ))

            summaries = []
            for idx, sa in enumerate(compact_agents, start=1):
                path = session_index.get_session_path(sa.get('id', ''))
                if not path:
                    continue

                events = SessionEventReader.read_events(
                    path, after=None, limit=10000, types=None, full=True,
                )
                extracted = SessionEventReader.extract_compaction_summary(events)
                if not extracted.get('summary'):
                    continue

                summaries.append({
                    'index': idx,
                    'subagentId': sa.get('id', ''),
                    'timestamp': extracted.get('timestamp', '') or '',
                    'summary': extracted.get('summary', ''),
                    'slug': sa.get('slug', ''),
                    'model': sa.get('model', ''),
                })

            self.send_json({
                'sessionId': session_id,
                'summaries': summaries,
                'total': len(summaries),
            })
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    def handle_raw_event(self, session_id, event_uuid):
        try:
            path = session_index.get_session_path(session_id)
            if not path:
                self.send_json({'error': 'Session not found'}, status=404)
                return
            event = SessionEventReader.get_raw_event(path, event_uuid)
            if not event:
                self.send_json({'error': 'Event not found'}, status=404)
                return
            self.send_json(event)
        except Exception as e:
            self.send_json({'error': str(e)}, status=500)

    # ── Shared helpers ───────────────────────────────────────────────────

    @staticmethod
    def _qint(qs, key, default):
        """Parse an integer query param, falling back on bad input."""
        try:
            return int(qs.get(key, [default])[0])
        except (TypeError, ValueError):
            return default

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _get_today_event_file(self):
        today = datetime.now().strftime('%Y%m%d')
        return LOG_DIR / f'events_{today}.jsonl'

    def _get_event_files_by_date(self):
        files = sorted(LOG_DIR.glob('events_*.jsonl'), reverse=True)
        if not files:
            files = sorted(LOG_DIR.glob('session_*_events.jsonl'), reverse=True)
        return files

    # ── Windows status collection ───────────────────────────────────────

    _PS_COLLECT_SCRIPT = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$cpu=@{};"
        "Get-CimInstance Win32_PerfFormattedData_PerfProc_Process"
        " -Filter \"Name != 'Idle' AND Name != '_Total'\" | ForEach-Object"
        " { $cpu[[uint32]$_.IDProcess]=[double]$_.PercentProcessorTime };"
        "$procs=@();"
        "Get-CimInstance Win32_Process | ForEach-Object {"
        " $cl=$_.CommandLine; if(-not $cl){return}"
        " $l=$cl.ToLower();"
        " if(-not $l.Contains('claude')){return}"
        " if($l.Contains('claude-tracker') -or $l.Contains('claude-monitor')"
        "   -or $l.Contains('serve-dashboard')){return}"
        " $t='claude-code';"
        " if($l.Contains('mcp-server')){$t='mcp-server'}"
        " elseif($l.Contains('worker-service')){$t='worker'}"
        " elseif($l.Contains('chroma')){$t='chroma'}"
        " $c=$cpu[[uint32]$_.ProcessId];"
        " $procs += [pscustomobject]@{ pid=$_.ProcessId; type=$t;"
        "   mem=[math]::Round([double]$_.WorkingSetSize/1MB);"
        "   cpu=[math]::Round([double]$c,1); state='S' }"
        " };"
        "$swap=0;"
        "Get-CimInstance Win32_PageFileUsage | ForEach-Object"
        " { $swap += $_.CurrentUsage };"
        "[pscustomobject]@{ swap=$swap; procs=$procs }"
        " | ConvertTo-Json -Compress -Depth 3"
    )

    _WIN_STATUS_CACHE = {'ts': 0.0, 'data': None}
    _WIN_STATUS_LOCK = threading.Lock()
    _WIN_STATUS_TTL = 1.5  # seconds; dashboard polls /status every ~2-3s

    def _collect_windows_status(self):
        """Claude processes via CIM + system memory via GlobalMemoryStatusEx."""
        system = {'available_mb': 0, 'total_mb': 0, 'swap_used_mb': 0}
        processes = []
        total_mem = 0

        # Memory is instant and dependency-free: no /proc on Windows.
        try:
            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ('dwLength', ctypes.c_ulong),
                    ('dwMemoryLoad', ctypes.c_ulong),
                    ('ullTotalPhys', ctypes.c_ulonglong),
                    ('ullAvailPhys', ctypes.c_ulonglong),
                    ('ullTotalPageFile', ctypes.c_ulonglong),
                    ('ullAvailPageFile', ctypes.c_ulonglong),
                    ('ullTotalVirtual', ctypes.c_ulonglong),
                    ('ullAvailVirtual', ctypes.c_ulonglong),
                    ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
                ]

            st = _MemoryStatusEx()
            st.dwLength = ctypes.sizeof(st)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                total_mb = st.ullTotalPhys // 1048576
                avail_mb = st.ullAvailPhys // 1048576
                # Commit charge minus physical usage approximates swap use.
                swap_mb = max(
                    0,
                    (st.ullTotalPageFile - st.ullAvailPageFile) // 1048576
                    - (total_mb - avail_mb),
                )
                system = {
                    'available_mb': avail_mb,
                    'total_mb': total_mb,
                    'swap_used_mb': swap_mb,
                }
        except (AttributeError, OSError):
            pass

        try:
            result = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive',
                 '-Command', self._PS_COLLECT_SCRIPT],
                capture_output=True, timeout=10,
            )
            out = (result.stdout or b'').decode('utf-8', errors='replace').strip()
            payload = json.loads(out) if out else {}
            if not isinstance(payload, dict):
                payload = {}
            if isinstance(payload.get('swap'), (int, float)):
                system['swap_used_mb'] = int(payload['swap'])
            rows = payload.get('procs') or []
            if isinstance(rows, dict):
                rows = [rows]
            for r in rows:
                try:
                    mem_mb = int(r['mem'] or 0)
                    total_mem += mem_mb
                    processes.append({
                        'pid': str(r['pid']),
                        'type': r['type'] or 'claude-code',
                        'memory': mem_mb,
                        'cpu': float(r['cpu'] or 0),
                        'state': 'S',
                    })
                except (KeyError, TypeError, ValueError):
                    continue
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError,
                ValueError) as e:
            print(f"Error collecting Windows processes: {e}", file=sys.stderr)

        return processes, system, total_mem

    def collect_status(self):
        now = time.time()
        if os.name == 'nt':
            with self._WIN_STATUS_LOCK:
                cached = self._WIN_STATUS_CACHE
                if cached['data'] is not None \
                        and now - cached['ts'] < self._WIN_STATUS_TTL:
                    return cached['data']
                processes, system, total_mem = self._collect_windows_status()
                data = self._compose_status(processes, system, total_mem)
                cached['ts'] = now
                cached['data'] = data
                return data
        processes, system, total_mem = self._collect_unix_status()
        return self._compose_status(processes, system, total_mem)

    def _compose_status(self, processes, system, total_mem):
        counts = {'claude': 0, 'mcp': 0, 'worker': 0, 'chroma': 0}
        for p in processes:
            if p['type'] == 'mcp-server':
                counts['mcp'] += 1
            elif p['type'] == 'worker':
                counts['worker'] += 1
            elif p['type'] == 'chroma':
                counts['chroma'] += 1
            else:
                counts['claude'] += 1

        log_date = datetime.now().strftime('%Y%m%d')

        # Detect process changes and log events
        _detect_and_log_events(processes, total_mem)

        # Re-read recent events (now includes any just-logged ones)
        recent_events = self.get_recent_events(limit=20)
        # Re-count anomalies from updated file
        anomaly_count = 0
        today_file = self._get_today_event_file()
        if today_file.exists():
            try:
                with open(today_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        if '"ANOMALY' in line:
                            anomaly_count += 1
            except (OSError, IOError):
                pass

        system_used_mb = max(0, system.get('total_mb', 0) - system.get('available_mb', 0))

        return {
            'processes': processes,
            'total_mem_mb': total_mem,
            'system_used_mb': system_used_mb,
            'counts': f"claude:{counts['claude']},mcp:{counts['mcp']},worker:{counts['worker']},chroma:{counts['chroma']}",
            'system': system,
            'recent_events': recent_events,
            'session_id': log_date,
            'anomaly_count': anomaly_count,
        }

    def _collect_unix_status(self):
        processes = []
        total_mem = 0

        try:
            result = subprocess.run(
                ['ps', 'aux', '--no-headers'],
                capture_output=True, text=True, timeout=5,
            )

            for line in result.stdout.split('\n'):
                if 'claude' not in line.lower():
                    continue
                if any(x in line for x in ('claude-tracker', 'claude-monitor', 'serve-dashboard')):
                    continue

                parts = line.split()
                if len(parts) < 11:
                    continue

                pid = parts[1]
                cpu = float(parts[2]) if parts[2] else 0
                mem_kb = int(parts[5]) if parts[5].isdigit() else 0
                mem_mb = mem_kb // 1024
                state = parts[7] if len(parts) > 7 else 'S'
                cmdline = ' '.join(parts[10:])

                proc_type = 'claude-code'
                if 'mcp-server' in cmdline:
                    proc_type = 'mcp-server'
                elif 'worker-service' in cmdline:
                    proc_type = 'worker'
                elif 'chroma' in cmdline:
                    proc_type = 'chroma'

                total_mem += mem_mb
                processes.append({
                    'pid': pid, 'type': proc_type,
                    'memory': mem_mb, 'cpu': cpu,
                    'state': state[0] if state else 'S',
                })

        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            print(f"Error collecting processes: {e}")

        system = {'available_mb': 0, 'total_mb': 0, 'swap_used_mb': 0}
        try:
            with open('/proc/meminfo', 'r') as f:
                meminfo = f.read()
                for line in meminfo.split('\n'):
                    if line.startswith('MemAvailable:'):
                        system['available_mb'] = int(line.split()[1]) // 1024
                    elif line.startswith('MemTotal:'):
                        system['total_mb'] = int(line.split()[1]) // 1024

            result = subprocess.run(['free', '-m'], capture_output=True, text=True, timeout=5)
            for line in result.stdout.split('\n'):
                if line.startswith('Swap:'):
                    parts = line.split()
                    if len(parts) >= 3:
                        system['swap_used_mb'] = int(parts[2])
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            print(f"Warning: Could not read system memory: {e}", file=sys.stderr)

        return processes, system, total_mem

    def get_recent_events(self, limit=50):
        events = []

        today_file = self._get_today_event_file()
        if today_file.exists():
            events = self._read_last_events(today_file, limit)

        if len(events) < limit:
            remaining = limit - len(events)
            yesterday = (datetime.now() - timedelta(days=1)).strftime('%Y%m%d')
            yesterday_file = LOG_DIR / f'events_{yesterday}.jsonl'
            if yesterday_file.exists():
                older = self._read_last_events(yesterday_file, remaining)
                events = older + events

        if not events:
            legacy = sorted(LOG_DIR.glob('session_*_events.jsonl'), reverse=True)
            if legacy:
                events = self._read_last_events(legacy[0], limit)

        return list(reversed(events))

    def _read_last_events(self, filepath, limit):
        events = []
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                last_lines = deque(f, maxlen=limit)
                for line in last_lines:
                    try:
                        events.append(json.loads(line.strip()))
                    except json.JSONDecodeError:
                        pass
        except (OSError, IOError) as e:
            print(f"Error reading events from {filepath}: {e}", file=sys.stderr)
        return events

    def get_daily_history(self, days=7):
        history = []

        for i in range(days):
            day = datetime.now() - timedelta(days=i)
            date_str = day.strftime('%Y%m%d')
            event_file = LOG_DIR / f'events_{date_str}.jsonl'

            summary = {
                'date': day.strftime('%Y-%m-%d'),
                'total_events': 0, 'anomalies': 0, 'spawns': 0,
                'exits': 0, 'cleanups': 0, 'peak_mem_mb': 0,
                'has_data': False,
            }

            if event_file.exists():
                summary['has_data'] = True
                try:
                    with open(event_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            summary['total_events'] += 1
                            try:
                                ev = json.loads(line.strip())
                                et = ev.get('event', '')
                                if et.startswith('ANOMALY'):
                                    summary['anomalies'] += 1
                                elif et == 'SPAWN':
                                    summary['spawns'] += 1
                                elif et == 'EXIT':
                                    summary['exits'] += 1
                                elif et == 'AUTO_CLEANUP':
                                    summary['cleanups'] += 1
                                mem = ev.get('total_mem_mb', 0)
                                if mem > summary['peak_mem_mb']:
                                    summary['peak_mem_mb'] = mem
                            except json.JSONDecodeError:
                                pass
                except (OSError, IOError) as e:
                    print(f"Warning: Could not read {event_file}: {e}", file=sys.stderr)

            history.append(summary)

        return history

    def log_message(self, format, *args):
        if '/api/' not in args[0]:
            return
        print(f"[API] {args[0]}")


# ─── Server ────────────────────────────────────────────────────────────────────

class ReuseAddrTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    """Threaded so slow requests (ripgrep content search, big Codex tails)
    never block the dashboard's concurrent pollers."""
    allow_reuse_address = True
    daemon_threads = True


def main():
    LOG_DIR.mkdir(exist_ok=True)

    # Pre-build session index
    print("Building session index...")
    session_index.rebuild()
    count = len(session_index._sessions)
    print(f"Indexed {count} sessions across {len(session_index._projects)} projects")

    print(f"""
+===========================================================+
|   Claude Process Monitor & Session Viewer                  |
+===========================================================+

  Dashboard:  http://localhost:{PORT}/
  API:        http://localhost:{PORT}/api/tracker/status
  Sessions:   http://localhost:{PORT}/api/sessions/list
  History:    http://localhost:{PORT}/api/tracker/history

  Press Ctrl+C to stop
""")

    with ReuseAddrTCPServer(("127.0.0.1", PORT), TrackerAPIHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            httpd.shutdown()


if __name__ == '__main__':
    main()
