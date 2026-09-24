#!/usr/bin/env python3
"""Bridge for reading the Hermes SessionDB (state.db) from inside WSL.

Invoked by serve-dashboard.py via `wsl -d <distro> -- python3 <this file> ...`.
All output is a single JSON document on stdout.

Usage:
  hermes_state_bridge.py <db> list <limit>
  hermes_state_bridge.py <db> msgs <session_id>
  hermes_state_bridge.py <db> stats <session_id>
  hermes_state_bridge.py <db> raw <session_id> <message_id>
"""

import json
import sqlite3
import sys

MAX_CONTENT = 20000


def _clip(text, limit=MAX_CONTENT):
    if text is None:
        return ''
    s = text if isinstance(text, str) else str(text)
    return {'v': s[:limit], 'trunc': len(s) > limit}


def main():
    if len(sys.argv) < 3:
        print(json.dumps({'error': 'usage'}))
        return
    db, mode = sys.argv[1], sys.argv[2]
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    con.row_factory = sqlite3.Row
    try:
        if mode == 'list':
            limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400
            # Cap per source so high-frequency cron runs can't crowd out
            # cli / telegram / subagent sessions in the listing.
            rows = con.execute(
                """
                SELECT id, source, model, started_at, ended_at, title,
                       message_count, tool_call_count,
                       input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens,
                       cwd, git_branch, last_activity_description,
                       estimated_cost_usd
                FROM (
                    SELECT s.*, ROW_NUMBER() OVER (
                        PARTITION BY s.source
                        ORDER BY COALESCE(s.ended_at, s.started_at) DESC
                    ) AS rn
                    FROM sessions s
                    WHERE COALESCE(s.hidden, 0) = 0
                      AND COALESCE(s.archived, 0) = 0
                )
                WHERE rn <= 200
                ORDER BY COALESCE(ended_at, started_at) DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                lad = d.pop('last_activity_description') or ''
                d['last_activity'] = lad[:160]
                d['estimated_cost_usd'] = d.get('estimated_cost_usd') or 0
                out.append(d)
            print(json.dumps(out, ensure_ascii=False))

        elif mode == 'msgs':
            sid = sys.argv[3]
            rows = con.execute(
                """
                SELECT id, role, content, tool_calls, tool_call_id,
                       tool_name, timestamp, token_count, finish_reason
                FROM messages WHERE session_id = ? ORDER BY id
                """,
                (sid,),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                c = _clip(d.get('content'))
                d['content'] = c['v']
                d['_content_truncated'] = c['trunc']
                tc = d.get('tool_calls')
                if tc and not isinstance(tc, (dict, list)):
                    try:
                        d['tool_calls'] = json.loads(tc)
                    except (json.JSONDecodeError, ValueError):
                        pass
                out.append(d)
            print(json.dumps(out, ensure_ascii=False))

        elif mode == 'stats':
            sid = sys.argv[3]
            row = con.execute(
                """
                SELECT source, model, started_at, ended_at,
                       message_count, tool_call_count,
                       input_tokens, output_tokens,
                       cache_read_tokens, cache_write_tokens,
                       reasoning_tokens, estimated_cost_usd, cwd, git_branch
                FROM sessions WHERE id = ?
                """,
                (sid,),
            ).fetchone()
            if not row:
                print(json.dumps({'error': 'not found'}))
                return
            tools = con.execute(
                """
                SELECT tool_name, COUNT(*) AS n FROM messages
                WHERE session_id = ? AND tool_name IS NOT NULL
                GROUP BY tool_name ORDER BY n DESC
                """,
                (sid,),
            ).fetchall()
            roles = con.execute(
                """
                SELECT role, COUNT(*) AS n FROM messages
                WHERE session_id = ? GROUP BY role
                """,
                (sid,),
            ).fetchall()
            d = dict(row)
            d['tool_calls'] = {r['tool_name']: r['n'] for r in tools}
            d['roles'] = {r['role']: r['n'] for r in roles}
            print(json.dumps(d, ensure_ascii=False))

        elif mode == 'raw':
            sid, mid = sys.argv[3], sys.argv[4]
            row = con.execute(
                "SELECT * FROM messages WHERE session_id = ? AND id = ?",
                (sid, mid),
            ).fetchone()
            if not row:
                print(json.dumps({'error': 'not found'}))
                return
            print(json.dumps(dict(row), ensure_ascii=False))

        else:
            print(json.dumps({'error': 'unknown mode'}))
    except sqlite3.Error as exc:
        print(json.dumps({'error': str(exc)}))
    finally:
        con.close()


if __name__ == '__main__':
    main()
