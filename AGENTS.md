# AGENTS.md - Your Workspace

This folder is home. Treat it that way.

## Working Style

Before making changes, read enough of the local project to understand its shape.
State assumptions explicitly when they matter, and surface ambiguity instead of
silently choosing a risky interpretation.

Don't ask permission for normal local exploration or implementation work. Ask
first before destructive actions, public posts, messages sent externally, or
anything that could expose private data.

## Memory

Use `memory/YYYY-MM-DD.md` for lightweight project notes when the directory
exists. Capture decisions, important project state, installed tools, and mistakes
worth avoiding later. Do not store secrets unless explicitly asked.

## Safety

- Don't exfiltrate private data. Ever.
- Don't run destructive commands without asking.
- `trash` > `rm` when removing local files.
- When in doubt, ask.

## Coding Discipline

### 1. Think Before Coding

- State assumptions explicitly.
- If multiple interpretations exist, surface them instead of picking one silently.
- Ask clarifying questions when uncertainty matters.
- Push back when a simpler or safer approach is better than the requested path.
- If confused, stop and name what is unclear.

### 2. Simplicity First

- Write the minimum code that solves the problem.
- Do not add speculative abstractions, flexibility, configuration, or features.
- Do not build for imaginary future requirements.
- If a 200-line solution can be 50 lines without losing correctness, simplify it.

### 3. Surgical Changes

- Touch only what is required for the task.
- Do not refactor or clean up unrelated code unless asked.
- Match the existing local style, even if you would normally do it differently.
- Remove only the dead code created by your own changes.
- If you spot unrelated issues, mention them separately instead of changing them opportunistically.

### 4. Goal-Driven Execution

- Turn vague instructions into verifiable success criteria.
- Prefer checks, tests, or observable outcomes over "it should work now".
- For multi-step work, state a short plan with a verification step for each part.
- Keep iterating until the result is verified or the blocker is explicit.

## External vs Internal

Safe to do freely:
- Read files, explore, organize, learn.
- Search the web when current or source-backed information is needed.
- Work within this workspace.

Ask first:
- Sending emails, tweets, public posts, or chat messages.
- Anything that leaves the machine on behalf of the user.
- Anything you're uncertain about.

## Tools

Skills provide specialized workflows. When a task clearly matches an available
skill, read that skill's `SKILL.md` and follow it.

Use `rtk` for verbose shell commands when practical, especially git, package
managers, builds, tests, search, and directory listings.


<claude-mem-context>
# Memory Context

# [CC-sesh-master] recent context, 2026-04-26 9:54pm UTC

Legend: 🎯session 🔴bugfix 🟣feature 🔄refactor ✅change 🔵discovery ⚖️decision 🚨security_alert 🔐security_note
Format: ID TIME TYPE TITLE
Fetch details: get_observations([IDs]) | Search: mem-search skill

### Feb 23, 2026
15296 1:45a 🔵 Found Prior Plan File for Codex CLI Integration (Already Shipped)
15298 " ⚖️ Implementation Plan Written: Full-Text Content Search via ripgrep with Name/Content Mode Toggle
15847 9:51p 🔵 cc-sesh-master Dashboard Runs on Port 37778
15851 " 🔵 cc-sesh-master Dashboard Polling and Refresh Intervals
15853 " 🔵 cc-sesh-master Dashboard Poll Interval Constants
15856 9:52p 🔵 cc-sesh-master Dashboard Configuration Constants Block
15859 " ✅ Dashboard Polling Intervals Reduced for Faster Updates
### Feb 24, 2026
16121 4:20a 🔵 Session Viewer Status Oscillating Between Connected/Disconnected
16122 " 🔵 Session Viewer Backend Running on Port 37778 via Python3
S1792 Fix session viewer status oscillating between connected and disconnected (Feb 24, 4:20 AM)
16125 4:21a 🔴 Session Viewer Dashboard Server Restarted on Port 37778
16126 " 🔴 Session Viewer Dashboard Confirmed Healthy After Restart
S1821 Fix duplicate event display in cc-sesh-master Codex session viewer (Feb 24, 4:21 AM)
16127 " 🔵 Connection Status UI Logic Found in dashboard.html
16128 " 🔵 setConnectionStatus Also Controls Loading Bar Visibility
16129 " 🔵 Three Call Sites for setConnectionStatus Found in dashboard.html
16130 4:22a 🔵 Root Cause of Status Oscillation: Polling fetchData() with No Debounce on Disconnect
16224 5:11a 🔵 Codex Session History Duplication Bug Observed
16225 5:12a 🔵 Codex Session Reading Architecture in serve-dashboard.py
16226 " 🔵 _read_codex_events Has Three Code Paths Based on File Size and Cursor
16227 " 🔵 Two Codex Session JSONL Files Found for 2026-02-24
16228 " 🔵 Recent Codex Session JSONL Contains 16 Raw Lines for a 10-Event Session
16229 5:13a 🔵 Codex JSONL File Confirmed Clean — Duplication Is in Dashboard Layer
16230 " 🔵 _parse_codex_event Skip Logic Reveals Which Codex Events Reach the Dashboard UI
16231 5:14a 🔵 Codex Function Call Events Map to Unified Tool-Use Format in Dashboard
16276 5:38a 🔵 cc-sesh-master localhost port inquiry
S2380 Diagnosticar por que o Session Viewer não mostrava mensagens após interrupção e corrigir a seleção incorreta de arquivo de sessão pai/subagente. (Feb 24, 5:38 AM)
### Feb 27, 2026
17112 9:46a 🔵 Session Viewer Sub-Agent Dropdown Auto-Close Bug
### Mar 11, 2026
23894 9:29p 🔵 Problema reportado entre Codex e Session Viewer após interrupção
S2384 Verificar bug no fork local e preparar avanço; mudanças de correção Codex + suporte Kimi K2 foram commitadas e publicadas no master (Mar 11, 9:49 PM)
### Mar 12, 2026
S2385 Confirmar se o fix deveria ir no PR #1230 e concluir a aplicação do bugfix de session-registry nos branches relevantes (Mar 12, 12:53 AM)
### Mar 22, 2026
28720 3:56p 🔵 Bug: Load More Sessions Reverts to Same 50 Results
### Mar 23, 2026
28800 3:14a 🔵 Live Event Feed Empty Due to Missing Log Files
### Mar 25, 2026
29000 2:11a 🔵 Intent to Track Droid Sessions in cc-sesh-master
### Apr 26, 2026
33122 9:52p 🔵 CC-sesh-master Project Structure Confirmed
</claude-mem-context>