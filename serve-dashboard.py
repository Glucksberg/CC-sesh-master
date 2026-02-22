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

PORT = 37778
SCRIPT_DIR = Path(__file__).parent.resolve()
LOG_DIR = SCRIPT_DIR / "logs"
CLAUDE_DIR = Path.home() / '.claude'
PROJECTS_DIR = CLAUDE_DIR / 'projects'
HISTORY_FILE = CLAUDE_DIR / 'history.jsonl'

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

        if not PROJECTS_DIR.exists():
            with self._lock:
                self._sessions = sessions
                self._subagents = subagents
                self._subagent_paths = subagent_paths
                self._projects = projects
                self._last_rebuild = time.time()
            return

        for proj_dir in PROJECTS_DIR.iterdir():
            if not proj_dir.is_dir():
                continue

            proj_name = proj_dir.name
            if 'observer' in proj_name.lower():
                continue

            # Clean project display name
            display = proj_name
            if display.startswith('-home-dev-'):
                display = display[10:]
            elif display.startswith('-home-dev'):
                display = display[9:] or 'home'
            display = display.replace('-', '/') if display else 'home'

            for jsonl_file in proj_dir.glob('*.jsonl'):
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

                # Scan subagents for this session
                sub_dir = proj_dir / sid / 'subagents'
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

                sessions[sid] = {
                    'sessionId': sid,
                    'project': display,
                    'slug': meta.get('slug', ''),
                    'status': 'active' if active else 'completed',
                    'model': meta.get('model', ''),
                    'version': meta.get('version', ''),
                    'mtime': mtime,
                    'size': size,
                    'eventCount': meta.get('event_count', 0),
                    'cwd': meta.get('cwd', ''),
                    'gitBranch': meta.get('gitBranch', ''),
                    'subagentCount': len(sub_list),
                    '_path': str(jsonl_file),
                }

        with self._lock:
            self._sessions = sessions
            self._subagents = subagents
            self._subagent_paths = subagent_paths
            self._projects = projects
            self._last_rebuild = time.time()

    # ── metadata helpers ──────────────────────────────────────────────────

    @staticmethod
    def _extract_metadata(path, size):
        meta = {'event_count': max(1, size // 500)}

        try:
            with open(path, 'r', errors='replace') as f:
                # First few lines
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if not meta.get('slug'):
                            meta['slug'] = obj.get('slug', '')
                        if not meta.get('cwd'):
                            meta['cwd'] = obj.get('cwd', '')
                        if not meta.get('version'):
                            meta['version'] = obj.get('version', '')
                        if not meta.get('gitBranch'):
                            meta['gitBranch'] = obj.get('gitBranch', '')
                        if not meta.get('model') and obj.get('type') == 'assistant':
                            meta['model'] = obj.get('message', {}).get('model', '')
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
                        if obj.get('type') == 'system' and obj.get('subtype') == 'stop_hook_summary':
                            meta['has_stop'] = True
                        if not meta.get('model') and obj.get('type') == 'assistant':
                            meta['model'] = obj.get('message', {}).get('model', '')
                        if not meta.get('slug') and obj.get('slug'):
                            meta['slug'] = obj['slug']
                        break
                    except (json.JSONDecodeError, KeyError):
                        pass

        except OSError:
            return None

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

    # ── query interface ───────────────────────────────────────────────────

    def get_sessions(self, project=None, status=None, search=None,
                     date_from=None, date_to=None, sort='mtime',
                     offset=0, limit=50):
        self.ensure_fresh()

        with self._lock:
            sessions = list(self._sessions.values())
            all_projects = sorted(self._projects)

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
            'projects': all_projects, 'offset': offset, 'limit': limit,
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
        # Fallback filesystem scan
        if PROJECTS_DIR.exists():
            for d in PROJECTS_DIR.iterdir():
                if not d.is_dir():
                    continue
                p = (d / f'{session_id}.jsonl').resolve()
                try:
                    p.relative_to(PROJECTS_DIR.resolve())
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

    _SKIP_TYPES = frozenset(['file-history-snapshot', 'queue-operation'])

    @classmethod
    def read_events(cls, path, after=None, limit=100, types=None):
        events = []
        try:
            file_size = os.path.getsize(path)

            if after:
                events = cls._read_after_cursor(path, file_size, after, limit, types)
            else:
                events = cls._read_tail(path, file_size, limit, types)
        except OSError as e:
            print(f"Error reading session: {e}", file=sys.stderr)

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
                            if obj.get('uuid') == after:
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
                            if obj.get('uuid') == after:
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
        if t in ('file-history-snapshot', 'queue-operation'):
            return None

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

    # ── raw event & stats ─────────────────────────────────────────────────

    @staticmethod
    def get_raw_event(path, event_uuid):
        try:
            size = os.path.getsize(path)
            # Try tail first
            if size > TAIL_READ_BYTES:
                with open(path, 'r', errors='replace') as f:
                    f.seek(max(0, size - TAIL_READ_BYTES))
                    f.readline()
                    for line in f:
                        try:
                            obj = json.loads(line.strip())
                            if obj.get('uuid') == event_uuid:
                                return obj
                        except json.JSONDecodeError:
                            pass
            # Full scan
            with open(path, 'r', errors='replace') as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        if obj.get('uuid') == event_uuid:
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
                        stats['version'] = raw['version']

                    if et == 'assistant':
                        msg = raw.get('message', {})
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
            limit = min(int(qs.get('limit', ['100'])[0]), 500)
            types_raw = qs.get('types', [None])[0]
            types = set(types_raw.split(',')) if types_raw else None

            events = SessionEventReader.read_events(
                path, after=after, limit=limit, types=types,
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
            self.send_json({
                'sessionId': session_id,
                'subagents': subagents,
                'total': len(subagents),
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

        recent_events = self.get_recent_events(limit=20)
        anomaly_count = 0
        log_date = None

        today_file = self._get_today_event_file()
        if today_file.exists():
            log_date = datetime.now().strftime('%Y%m%d')
            try:
                with open(today_file, 'r') as f:
                    for line in f:
                        if '"ANOMALY' in line:
                            anomaly_count += 1
            except (OSError, IOError) as e:
                print(f"Warning: Could not count anomalies: {e}", file=sys.stderr)
        else:
            event_files = self._get_event_files_by_date()
            if event_files:
                name = event_files[0].stem
                if name.startswith('events_'):
                    log_date = name.replace('events_', '')
                else:
                    log_date = name.replace('session_', '').replace('_events', '')
                try:
                    with open(event_files[0], 'r') as f:
                        for line in f:
                            if '"ANOMALY' in line:
                                anomaly_count += 1
                except (OSError, IOError) as e:
                    print(f"Warning: Could not count anomalies: {e}", file=sys.stderr)

        return {
            'processes': processes,
            'total_mem_mb': total_mem,
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
        if '/api/' not in args[0]:
            return
        print(f"[API] {args[0]}")


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

    with ReuseAddrTCPServer(("", PORT), TrackerAPIHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nShutting down...")
            httpd.shutdown()


if __name__ == '__main__':
    main()
