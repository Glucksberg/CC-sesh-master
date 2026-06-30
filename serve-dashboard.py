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
import time
import threading
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
        with open(event_file, 'a') as f:
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
CODEX_INDEX_FILE = Path.home() / '.codex' / 'session_index.jsonl'
_kimi_share = os.environ.get('KIMI_SHARE_DIR', '').strip()
KIMI_DIR = Path(_kimi_share) if _kimi_share else Path.home() / '.kimi' / 'sessions'
DROID_DIR = Path.home() / '.factory' / 'sessions'

# ─── WSL directory discovery ───────────────────────────────────────────────────
WSL_PROJECTS_DIRS = []   # additional ~/.claude/projects from WSL distros
WSL_CODEX_DIRS = []      # additional ~/.codex/sessions from WSL distros
WSL_CODEX_ARCHIVED_DIRS = []  # additional ~/.codex/archived_sessions from WSL distros
WSL_OPENCLAW_DIRS = []   # additional ~/.openclaw/agents from WSL distros
WSL_KIMI_DIRS = []       # additional ~/.kimi/sessions from WSL distros
WSL_DROID_DIRS = []      # additional ~/.factory/sessions from WSL distros

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
    global WSL_OPENCLAW_DIRS, WSL_KIMI_DIRS, WSL_DROID_DIRS
    for wsl_home in _discover_wsl_homes():
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

try:
    _init_wsl_dirs()
except Exception as e:
    print(f"WSL discovery failed (non-fatal): {e}", file=sys.stderr)

ALL_CODEX_DIRS = [CODEX_DIR, CODEX_ARCHIVED_DIR] + WSL_CODEX_DIRS + WSL_CODEX_ARCHIVED_DIRS

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

            meta = self._extract_metadata(jsonl_file, size)
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
    def _extract_metadata(path, size):
        meta = {'event_count': max(1, size // 500)}

        try:
            with open(path, 'r', errors='replace') as f:
                # First few lines
                for _ in range(8):
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
                with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
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
        session_names = self._load_codex_session_names(base_dir)
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

            raw_id = meta.get('id') or jsonl_file.stem
            sid = 'codex-' + raw_id
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
                'slug': session_names.get(raw_id) or meta.get('slug', ''),
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
    def _load_codex_session_names(base_dir):
        """Load renamed Codex thread titles from ~/.codex/session_index.jsonl."""
        index_file = CODEX_INDEX_FILE
        try:
            if base_dir != CODEX_DIR:
                index_file = base_dir.parent / 'session_index.jsonl'
        except OSError:
            pass

        names = {}
        try:
            with open(index_file, 'r', errors='replace') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = obj.get('id')
                    name = obj.get('thread_name')
                    clean_name = str(name).strip() if name else ''
                    if sid and clean_name:
                        names[sid] = clean_name
        except OSError:
            pass
        return names

    @staticmethod
    def _extract_codex_metadata(path, size):
        meta = {'event_count': max(1, size // 500)}
        try:
            with open(path, 'r', errors='replace') as f:
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
            with open(wire_path, 'r', errors='replace') as f:
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
                with open(context_path, 'r', errors='replace') as f:
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
                with open(settings_path, 'r', errors='replace') as sf:
                    settings = json.loads(sf.read())
                    m = settings.get('model', '')
                    if m:
                        meta['model'] = m
            except (OSError, json.JSONDecodeError):
                pass

        try:
            with open(path, 'r', errors='replace') as f:
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

        if not dirs:
            return set()

        cmd = [
            'rg', '-l', '--fixed-strings', '--max-filesize', '50M',
            '--glob', '*.jsonl',
            '--glob', '!*openclaw-workspace*', '--glob', '!*observer*',
            query,
        ] + dirs

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            paths = result.stdout.strip().split('\n') if result.stdout.strip() else []
        except (subprocess.TimeoutExpired, OSError):
            return set()

        # Map file paths back to session IDs via reverse lookup
        with self._lock:
            path_to_id = {s['_path']: sid for sid, s in self._sessions.items()}

        matched_ids = set()
        for p in paths:
            p = p.strip()
            if not p:
                continue
            sid = path_to_id.get(p)
            if sid:
                matched_ids.add(sid)

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

        if source and source in ('claude', 'openclaw', 'codex', 'kimi', 'droid'):
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
    ])

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
    def read_events(cls, path, after=None, before=None, limit=100, types=None, full=False):
        events = []
        try:
            file_size = os.path.getsize(path)

            if cls._is_codex_path(path):
                events = cls._read_codex_events(
                    path, file_size, after=after, before=before,
                    limit=limit, types=types, full=full,
                )
            elif cls._is_kimi_path(path):
                events = cls._read_kimi_events(
                    path, file_size, after=after, before=before,
                    limit=limit, types=types, full=full,
                )
            elif before:
                events = cls._read_before_cursor(path, limit, types, before)
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
    def _read_before_cursor(cls, path, limit, types, before):
        """Read display events before a given UUID cursor."""
        events = []
        with open(path, 'r', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                ev = cls._parse_event(line)
                if not ev or ev['type'] in cls._SKIP_TYPES:
                    continue
                if types and ev['type'] not in types:
                    continue
                if ev.get('uuid') == before:
                    break
                events.append(ev)
        return events[-limit:]

    @classmethod
    def _read_full(cls, path, limit, types):
        """Read all events from beginning of file (for export/copy)."""
        events = []
        with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
                f.seek(max(0, file_size - TAIL_READ_BYTES))
                f.readline()
                for line in f:
                    ev = cls._parse_event(line.strip())
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
            return events[-limit:]

        with open(path, 'r', errors='replace') as f:
            for line in f:
                ev = cls._parse_event(line.strip())
                if ev and ev['type'] not in cls._SKIP_TYPES:
                    if not types or ev['type'] in types:
                        events.append(ev)
        return events[-limit:]

    # ── Codex event reading ────────────────────────────────────────────────

    @classmethod
    def _read_codex_events(cls, path, file_size, after=None, before=None,
                           limit=100, types=None, full=False):
        """Read events from a Codex JSONL file using line-number cursors."""
        events = []

        if full:
            with open(path, 'r', errors='replace') as f:
                for line_num, line in enumerate(f):
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

        # Parse before cursor: "codex-<line_num>"
        if before and before.startswith('codex-'):
            try:
                before_line = int(before.split('-', 1)[1])
            except (ValueError, IndexError):
                before_line = -1

            with open(path, 'r', errors='replace') as f:
                for line_num, line in enumerate(f):
                    if line_num >= before_line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    ev = cls._parse_codex_event(line, line_num)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
            return events[-limit:]

        # Parse after cursor: "codex-<line_num>"
        if after and after.startswith('codex-'):
            try:
                after_line = int(after.split('-', 1)[1])
            except (ValueError, IndexError):
                after_line = -1

            # Cursor-based: scan from beginning to find the line
            found = False
            with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
                f.seek(max(0, file_size - TAIL_READ_BYTES))
                f.readline()  # discard partial
                start_offset = f.tell()
                # Count lines before this offset
                f.seek(0)
                line_offset = 0
                while f.tell() < start_offset:
                    f.readline()
                    line_offset += 1
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
        with open(path, 'r', errors='replace') as f:
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
    def _read_kimi_events(cls, path, file_size, after=None, before=None,
                          limit=100, types=None, full=False):
        """Read events from a Kimi wire.jsonl file using line-number cursors."""
        events = []

        if full:
            with open(path, 'r', errors='replace') as f:
                for line_num, line in enumerate(f):
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

        if before and before.startswith('kimi-'):
            try:
                before_line = int(before[5:])
            except (ValueError, IndexError):
                return events

            if before_line < 0:
                return events

            with open(path, 'r', errors='replace') as f:
                for line_num, line in enumerate(f):
                    if line_num >= before_line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    ev = cls._parse_kimi_event(line, line_num)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
            return events[-limit:]

        if after and after.startswith('kimi-'):
            try:
                after_line = int(after[5:])
            except (ValueError, IndexError):
                return events

            if after_line < 0:
                return events

            found = False
            with open(path, 'r', errors='replace') as f:
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

        # For large files, tail-read but keep exact line-number cursors.
        if file_size > TAIL_READ_BYTES:
            with open(path, 'r', errors='replace') as f:
                f.seek(max(0, file_size - TAIL_READ_BYTES))
                f.readline()
                start_offset = f.tell()
                f.seek(0)
                line_offset = 0
                while f.tell() < start_offset:
                    f.readline()
                    line_offset += 1
                f.seek(start_offset)
                for rel_num, line in enumerate(f):
                    line = line.strip()
                    if not line:
                        continue
                    ev = cls._parse_kimi_event(line, line_offset + rel_num)
                    if ev and ev['type'] not in cls._SKIP_TYPES:
                        if not types or ev['type'] in types:
                            events.append(ev)
            return events[-limit:]

        # Full scan for small files
        with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
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

    @staticmethod
    def get_raw_event(path, event_uuid):
        try:
            # Codex/Kimi: line-number based lookup
            for prefix in ('codex-', 'kimi-'):
                if event_uuid.startswith(prefix):
                    try:
                        target_line = int(event_uuid[len(prefix):])
                    except (ValueError, IndexError):
                        return None
                    with open(path, 'r', errors='replace') as f:
                        for i, line in enumerate(f):
                            if i == target_line:
                                return json.loads(line.strip())
                    return None

            size = os.path.getsize(path)
            # Try tail first
            if size > TAIL_READ_BYTES:
                with open(path, 'r', errors='replace') as f:
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
            with open(path, 'r', errors='replace') as f:
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

    @staticmethod
    def get_stats(path):
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
            with open(path, 'r', errors='replace') as f:
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
                offset=int(qs.get('offset', ['0'])[0]),
                limit=min(int(qs.get('limit', ['50'])[0]), 200),
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
            before = qs.get('before', [None])[0]
            full = qs.get('full', [''])[0] == '1'
            limit = min(int(qs.get('limit', ['100'])[0]), 10000 if full else 500)
            types_raw = qs.get('types', [None])[0]
            types = set(types_raw.split(',')) if types_raw else None

            events = SessionEventReader.read_events(
                path, after=after, before=before,
                limit=limit, types=types, full=full,
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

    # ── Shared helpers ────────────────────────────────────────────────────

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

    def collect_status(self):
        processes = []
        total_mem = 0
        counts = {'claude': 0, 'mcp': 0, 'worker': 0, 'chroma': 0}

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
                    counts['mcp'] += 1
                elif 'worker-service' in cmdline:
                    proc_type = 'worker'
                    counts['worker'] += 1
                elif 'chroma' in cmdline:
                    proc_type = 'chroma'
                    counts['chroma'] += 1
                else:
                    counts['claude'] += 1

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
                with open(today_file, 'r') as f:
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
            with open(filepath, 'r') as f:
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
                    with open(event_file, 'r') as f:
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
        request_line = str(args[0]) if args else format
        if '/api/' not in request_line:
            return
        print(f"[API] {request_line}")


# ─── Server ────────────────────────────────────────────────────────────────────

class ReuseAddrTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


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
