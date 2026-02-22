# CC-sesh-master

Real-time monitoring dashboard for Claude Code sessions and processes.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## What it does

**Two views in one dashboard:**

- **Process Monitor** — Live tracking of Claude Code processes (RAM, CPU, lifecycle events, anomaly detection)
- **Session Viewer** — Real-time conversation viewer for Claude Code sessions (messages, tool calls, token usage)

The dashboard reads Claude Code's native JSONL session files from `~/.claude/projects/` and displays them in a cyberpunk-themed web UI.

## Quick Start

```bash
python3 serve-dashboard.py
# Open http://localhost:37778
```

### With PM2 (recommended)

```bash
pm2 start serve-dashboard.py --name tracker-dashboard --interpreter python3
pm2 save
```

## Features

### Process Monitor
- Live process count, memory usage, MCP server tracking
- Event feed (spawn, exit, anomaly, state changes)
- Memory timeline chart
- Active process table with CPU/memory/state

### Session Viewer
- Browse 1000+ sessions across all projects
- Filter by project, status (active/completed), date range, search
- Real-time conversation view with auto-scroll
- Collapsible tool call cards with input/output
- "Show full" expansion for truncated content
- Session stats (model, tokens, duration, tool usage)
- 3-second polling for active sessions
- **Subagent visibility** — see spawned subagents (task-agents, compact, prompt_suggestion) per session
- Navigate into subagent conversations with back-to-parent navigation
- Export any session or subagent conversation to Markdown

## Architecture

```
serve-dashboard.py    # Python HTTP server (port 37778)
  ├─ /                # Serves dashboard.html
  ├─ /api/tracker/*   # Process monitor endpoints
  └─ /api/sessions/*  # Session viewer endpoints

dashboard.html        # Single-file frontend (HTML + CSS + JS)
claude-tracker.sh     # Process lifecycle tracker (daemon/interactive)
analyze-session.sh    # Post-session analysis tool
```

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/tracker/status` | Current process status + recent events |
| `GET /api/tracker/events` | Recent process events |
| `GET /api/tracker/history` | Daily summary (7 days) |
| `GET /api/sessions/list` | Filterable session list |
| `GET /api/sessions/:id/events` | Session events (cursor-based) |
| `GET /api/sessions/:id/stats` | Session statistics |
| `GET /api/sessions/:id/subagents` | List subagents for a session |
| `GET /api/sessions/:id/raw-event/:uuid` | Full untruncated event |

### Session List Params

`project`, `status`, `search`, `date_from`, `date_to`, `sort`, `offset`, `limit`

### Event Pagination

Use `after=<uuid>` for cursor-based incremental updates. The server reads from the tail of large files (>512KB) for performance.

## Data Sources

- **Session files:** `~/.claude/projects/<project>/<sessionId>.jsonl`
- **Subagent files:** `~/.claude/projects/<project>/<sessionId>/subagents/*.jsonl`
- **Process events:** `logs/events_YYYYMMDD.jsonl` (written by claude-tracker.sh)

## Requirements

- Python 3.8+
- Claude Code installed (`~/.claude/` directory)
- No external Python dependencies

## Process Tracker

The `claude-tracker.sh` script monitors Claude Code processes independently:

```bash
# Interactive mode (live terminal UI)
./claude-tracker.sh

# Daemon mode (background, writes event logs)
./claude-tracker.sh --daemon

# Status snapshot
./claude-tracker.sh --status

# Benchmark (4-hour stress test)
./claude-tracker.sh --benchmark
```

## License

MIT
