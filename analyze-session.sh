#!/bin/bash
# Claude Tracker Session Analyzer
# Generates a comprehensive X-ray from JSONL event logs
#
# Usage:
#   ./analyze-session.sh                    # Analyze today's events
#   ./analyze-session.sh 20260215           # Analyze specific date
#   ./analyze-session.sh /path/to/events.jsonl  # Analyze specific file
#   ./analyze-session.sh --telegram         # Analyze today + send to Telegram
#   ./analyze-session.sh 20260215 --telegram

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"

# Telegram config (same as tracker)
TELEGRAM_BOT_TOKEN="${TRACKER_TELEGRAM_BOT_TOKEN:-7307131738:AAHi8UUh7DaPbxRGmNMXJMH47S-YptUvrUM}"
TELEGRAM_CHAT_ID="${TRACKER_TELEGRAM_CHAT_ID:--1002912787560}"

SEND_TELEGRAM=0

# Validate jq
if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required" >&2
    exit 1
fi

# Parse args
EVENT_FILE=""
for arg in "$@"; do
    case "$arg" in
        --telegram) SEND_TELEGRAM=1 ;;
        /*)  EVENT_FILE="$arg" ;;
        *)
            if [[ "$arg" =~ ^[0-9]{8}$ ]]; then
                EVENT_FILE="$LOG_DIR/events_${arg}.jsonl"
            fi
            ;;
    esac
done

# Default to today
if [ -z "$EVENT_FILE" ]; then
    EVENT_FILE="$LOG_DIR/events_$(date +%Y%m%d).jsonl"
fi

if [ ! -f "$EVENT_FILE" ]; then
    echo "ERROR: Event file not found: $EVENT_FILE" >&2
    echo ""
    echo "Available event files:"
    ls -1 "$LOG_DIR"/events_*.jsonl 2>/dev/null | sed 's|.*/||' || echo "  (none)"
    exit 1
fi

EVENT_COUNT=$(wc -l < "$EVENT_FILE")
echo "═══════════════════════════════════════════════════════"
echo "  Claude Tracker - Session Analysis"
echo "  File: $(basename "$EVENT_FILE")"
echo "  Events: $EVENT_COUNT"
echo "═══════════════════════════════════════════════════════"
echo ""

# ── 1. Time Range ──
echo "┌─ TIME RANGE ────────────────────────────────────────"
FIRST_TS=$(jq -r '.ts' "$EVENT_FILE" | head -1)
LAST_TS=$(jq -r '.ts' "$EVENT_FILE" | tail -1)
echo "│ Start: $FIRST_TS"
echo "│ End:   $LAST_TS"
echo "└────────────────────────────────────────────────────"
echo ""

# ── 2. Event Summary ──
echo "┌─ EVENT SUMMARY ──────────────────────────────────────"
jq -r '.event' "$EVENT_FILE" | sort | uniq -c | sort -rn | awk '{printf "│ %5s × %s\n", $1, $2}'
echo "└────────────────────────────────────────────────────"
echo ""

# ── 3. Process Lifecycle ──
echo "┌─ PROCESS LIFECYCLE ──────────────────────────────────"
SPAWN_COUNT=$(jq -r 'select(.event=="SPAWN") | .pid' "$EVENT_FILE" | wc -l)
EXIT_COUNT=$(jq -r 'select(.event=="EXIT") | .pid' "$EVENT_FILE" | wc -l)
ORPHAN_COUNT=$(jq -r 'select(.event=="ORPHAN") | .pid' "$EVENT_FILE" | wc -l)
CLEANUP_COUNT=$(jq -r 'select(.event=="AUTO_CLEANUP") | .pid' "$EVENT_FILE" | wc -l)
echo "│ Spawned:  $SPAWN_COUNT"
echo "│ Exited:   $EXIT_COUNT"
echo "│ Orphaned: $ORPHAN_COUNT"
echo "│ Cleaned:  $CLEANUP_COUNT"
echo "│ Leaked:   $((SPAWN_COUNT - EXIT_COUNT - CLEANUP_COUNT))  (spawn - exit - cleanup)"
echo "│"

# Lifetime distribution from EXIT events
echo "│ Lifetime Distribution:"
jq -r 'select(.event=="EXIT") | .details' "$EVENT_FILE" | \
    grep -oP 'lived \K[0-9]+' | \
    awk '
    {
        lifetimes[NR] = $1
        total += $1
        if ($1 < 60) short++
        else if ($1 < 300) medium++
        else if ($1 < 3600) long_++
        else vlong++
    }
    END {
        if (NR == 0) { print "│   No exit data with lifetime"; exit }
        printf "│   <1min: %d | 1-5min: %d | 5-60min: %d | >1h: %d\n", short+0, medium+0, long_+0, vlong+0
        printf "│   Avg lifetime: %ds | Total: %d processes\n", total/NR, NR
    }'
echo "│"

# Process type breakdown
echo "│ By Type:"
jq -r 'select(.event=="SPAWN") | .details' "$EVENT_FILE" | \
    awk '{print $1}' | sort | uniq -c | sort -rn | awk '{printf "│   %s × %s spawned\n", $1, $2}'
echo "└────────────────────────────────────────────────────"
echo ""

# ── 4. Memory Analysis ──
echo "┌─ MEMORY ANALYSIS ────────────────────────────────────"
jq -s '{
    peak_total_mb: ([.[].total_mem_mb] | max),
    min_total_mb: ([.[].total_mem_mb] | min),
    avg_total_mb: ([.[].total_mem_mb] | add / length | floor),
    peak_swap_mb: ([.[].system.swap_used_mb] | max),
    min_available_mb: ([.[].system.available_mb] | min),
    max_available_mb: ([.[].system.available_mb] | max)
}' "$EVENT_FILE" 2>/dev/null | jq -r '
    "│ Claude processes memory:",
    "│   Peak: \(.peak_total_mb)MB | Min: \(.min_total_mb)MB | Avg: \(.avg_total_mb)MB",
    "│ System:",
    "│   Peak swap: \(.peak_swap_mb)MB",
    "│   Avail range: \(.min_available_mb)MB - \(.max_available_mb)MB"
'
echo "│"

# Memory trend from snapshots
SNAP_COUNT=$(jq -r 'select(.event=="SNAPSHOT") | .ts' "$EVENT_FILE" 2>/dev/null | wc -l)
echo "│ Snapshots collected: $SNAP_COUNT"
if [ "$SNAP_COUNT" -ge 2 ]; then
    FIRST_MEM=$(jq -r 'select(.event=="SNAPSHOT") | .total_mem_mb' "$EVENT_FILE" | head -1)
    LAST_MEM=$(jq -r 'select(.event=="SNAPSHOT") | .total_mem_mb' "$EVENT_FILE" | tail -1)
    MEM_DELTA=$((LAST_MEM - FIRST_MEM))
    SIGN=""
    [ "$MEM_DELTA" -gt 0 ] && SIGN="+"
    echo "│ Memory trend: ${FIRST_MEM}MB → ${LAST_MEM}MB (${SIGN}${MEM_DELTA}MB)"

    # Memory over time from snapshots
    echo "│"
    echo "│ Memory timeline (snapshots):"
    jq -r 'select(.event=="SNAPSHOT") | "│   [\(.ts | split("T")[1] | split(".")[0])] \(.total_mem_mb)MB (\(.procs | length) procs) avail:\(.system.available_mb)MB swap:\(.system.swap_used_mb)MB"' "$EVENT_FILE" 2>/dev/null
fi

# MEM_CHANGE events
MEM_CHANGE_COUNT=$(jq -r 'select(.event=="MEM_CHANGE") | .pid' "$EVENT_FILE" 2>/dev/null | wc -l)
if [ "$MEM_CHANGE_COUNT" -gt 0 ]; then
    echo "│"
    echo "│ Memory jumps (>50MB): $MEM_CHANGE_COUNT events"
    jq -r 'select(.event=="MEM_CHANGE") | "│   [\(.ts | split("T")[1] | split(".")[0])] PID:\(.pid) \(.details)"' "$EVENT_FILE" | tail -15
fi
echo "└────────────────────────────────────────────────────"
echo ""

# ── 5. Per-PID Memory Growth (from snapshots) ──
if [ "$SNAP_COUNT" -ge 2 ]; then
    echo "┌─ PER-PID MEMORY GROWTH ──────────────────────────────"
    echo "│ (first snapshot vs last snapshot, sorted by delta)"
    jq -s '
        [.[] | select(.event=="SNAPSHOT")] |
        if length < 2 then empty else
        (first.procs // []) as $first |
        (last.procs // []) as $last |
        # PIDs in both snapshots
        ([$last[] as $l | ($first[] | select(.pid == $l.pid)) as $f |
         {pid: $l.pid, type: $l.type, from: $f.mem_mb, to: $l.mem_mb, delta: ($l.mem_mb - $f.mem_mb),
          fd_from: ($f.fd // 0), fd_to: ($l.fd // 0), fd_delta: (($l.fd // 0) - ($f.fd // 0)),
          t_from: ($f.threads // 1), t_to: ($l.threads // 1),
          age: ($l.age // 0)}] | sort_by(-.delta)),
        # PIDs only in last snapshot (new processes)
        ([$last[] | select(.pid as $p | $first | map(.pid) | index($p) | not) |
         {pid: .pid, type: .type, from: 0, to: .mem_mb, delta: .mem_mb, new: true,
          fd_to: (.fd // 0), t_to: (.threads // 1), age: (.age // 0)}])
        | flatten | sort_by(-.delta) | .[] |
        if .new then
        "│ \(.type):\(.pid) NEW \(.to)MB fd:\(.fd_to) threads:\(.t_to) age:\(
            if .age > 3600 then "\(.age / 3600 | floor)h\((.age % 3600) / 60 | floor)m"
            elif .age > 60 then "\(.age / 60 | floor)m\(.age % 60)s"
            else "\(.age)s" end)"
        else
        "│ \(.type):\(.pid) \(.from)→\(.to)MB (\(if .delta > 0 then "+" else "" end)\(.delta)) fd:\(.fd_from)→\(.fd_to) (\(if .fd_delta > 0 then "+" else "" end)\(.fd_delta)) threads:\(.t_from)→\(.t_to) age:\(
            if .age > 3600 then "\(.age / 3600 | floor)h\((.age % 3600) / 60 | floor)m"
            elif .age > 60 then "\(.age / 60 | floor)m\(.age % 60)s"
            else "\(.age)s" end)"
        end
        end
    ' "$EVENT_FILE" 2>/dev/null | head -20
    echo "└────────────────────────────────────────────────────"
    echo ""
fi

# ── 6. Leak Candidates ──
if [ "$SNAP_COUNT" -ge 3 ]; then
    echo "┌─ LEAK CANDIDATES ────────────────────────────────────"
    echo "│ (PIDs with consistently growing memory across snapshots)"
    LEAK_OUTPUT=$(jq -s '
        [.[] | select(.event=="SNAPSHOT") | .procs // []] |
        if length < 3 then "│ Need 3+ snapshots" else
        [.[][] | .pid] | unique | .[] as $pid |
        {pid: $pid,
         readings: [.[][] | select(.pid == $pid) | .mem_mb],
         fd_readings: [.[][] | select(.pid == $pid) | (.fd // 0)],
         type: ([.[][] | select(.pid == $pid) | .type] | first)} |
        select(.readings | length >= 3) |
        {pid, type, readings, fd_readings,
         diffs: [range(1; .readings | length) as $i | .readings[$i] - .readings[$i-1]],
         total_delta: (.readings | last - first),
         fd_delta: (.fd_readings | last - first)} |
        select(.total_delta > 20) |
        select([.diffs[] | . >= 0] | all) |
        "│ ⚠️  \(.type):\(.pid) \(.readings | first)→\(.readings | last)MB (+\(.total_delta)MB) fd:\(.fd_readings | first)→\(.fd_readings | last) (\(if .fd_delta > 0 then "+" else "" end)\(.fd_delta)) readings: \(.readings)"
        end
    ' "$EVENT_FILE" 2>/dev/null)

    if [ -n "$LEAK_OUTPUT" ]; then
        echo "$LEAK_OUTPUT"
    else
        echo "│ No leak candidates detected (consistent growth >20MB)"
    fi
    echo "└────────────────────────────────────────────────────"
    echo ""
fi

# ── 7. FD Growth ──
if [ "$SNAP_COUNT" -ge 2 ]; then
    echo "┌─ FILE DESCRIPTOR CHANGES ────────────────────────────"
    FD_OUTPUT=$(jq -s '
        [.[] | select(.event=="SNAPSHOT")] |
        if length < 2 then empty else
        (first.procs // []) as $first |
        (last.procs // []) as $last |
        [$last[] as $l | ($first[] | select(.pid == $l.pid)) as $f |
         {pid: $l.pid, type: $l.type, fd_from: ($f.fd // 0), fd_to: ($l.fd // 0),
          fd_delta: (($l.fd // 0) - ($f.fd // 0))}] |
        sort_by(-.fd_delta) |
        [.[] | select(.fd_delta != 0)] |
        if length == 0 then "│ No FD changes detected" else
        .[] | "│ \(.type):\(.pid) fd:\(.fd_from)→\(.fd_to) (\(if .fd_delta > 0 then "+" else "" end)\(.fd_delta))"
        end end
    ' "$EVENT_FILE" 2>/dev/null)
    echo "${FD_OUTPUT:-│ No FD data available}" | head -10
    echo "└────────────────────────────────────────────────────"
    echo ""
fi

# ── 8. Thread Changes ──
if [ "$SNAP_COUNT" -ge 2 ]; then
    THREAD_OUTPUT=$(jq -s '
        [.[] | select(.event=="SNAPSHOT")] |
        if length < 2 then empty else
        (first.procs // []) as $first |
        (last.procs // []) as $last |
        [$last[] as $l | ($first[] | select(.pid == $l.pid)) as $f |
         {pid: $l.pid, type: $l.type, t_from: ($f.threads // 1), t_to: ($l.threads // 1),
          t_delta: (($l.threads // 1) - ($f.threads // 1))}] |
        sort_by(-.t_delta) |
        [.[] | select(.t_delta != 0)]
        | if length == 0 then empty else .[] |
        "│ \(.type):\(.pid) threads:\(.t_from)→\(.t_to) (\(if .t_delta > 0 then "+" else "" end)\(.t_delta))"
        end end
    ' "$EVENT_FILE" 2>/dev/null)
    if [ -n "$THREAD_OUTPUT" ]; then
        echo "┌─ THREAD CHANGES ──────────────────────────────────"
        echo "$THREAD_OUTPUT" | head -10
        echo "└────────────────────────────────────────────────────"
        echo ""
    fi
fi

# ── 9. Anomalies ──
ANOMALY_COUNT=$(jq -r 'select(.event | startswith("ANOMALY")) | .event' "$EVENT_FILE" 2>/dev/null | wc -l)
if [ "$ANOMALY_COUNT" -gt 0 ]; then
    echo "┌─ ANOMALIES ($ANOMALY_COUNT) ──────────────────────────────"
    # Group by type
    echo "│ By type:"
    jq -r 'select(.event | startswith("ANOMALY")) | .event' "$EVENT_FILE" | sort | uniq -c | sort -rn | awk '{printf "│   %s × %s\n", $1, $2}'
    echo "│"
    echo "│ Timeline:"
    jq -r 'select(.event | startswith("ANOMALY")) | "│   [\(.ts | split("T")[1] | split(".")[0])] \(.event): \(.details)"' "$EVENT_FILE" | tail -20
    echo "│"

    # Context: 3 events before each anomaly
    echo "│ Context (3 events before first 5 anomalies):"
    ANOMALY_NUMS=$(jq -r 'select(.event | startswith("ANOMALY")) | .event_num' "$EVENT_FILE" | head -5)
    while read -r anum; do
        [ -z "$anum" ] && continue
        local_start=$((anum - 3))
        [ "$local_start" -lt 1 ] && local_start=1
        echo "│   ─── Before anomaly #$anum ───"
        jq -r "select(.event_num >= $local_start and .event_num < $anum) | \"│   #\(.event_num) \(.event) \(.details[:80])\"" "$EVENT_FILE"
    done <<< "$ANOMALY_NUMS"
    echo "└────────────────────────────────────────────────────"
    echo ""
fi

# ── 10. Orphan Timeline ──
ORPHAN_TOTAL=$(jq -r 'select(.event=="ORPHAN" or .event=="ORPHAN_CANDIDATE" or .event=="AUTO_CLEANUP") | .event' "$EVENT_FILE" 2>/dev/null | wc -l)
if [ "$ORPHAN_TOTAL" -gt 0 ]; then
    echo "┌─ ORPHAN TIMELINE ($ORPHAN_TOTAL events) ────────────────"
    jq -r 'select(.event=="ORPHAN" or .event=="ORPHAN_CANDIDATE" or .event=="AUTO_CLEANUP") |
        "│ [\(.ts | split("T")[1] | split(".")[0])] \(.event) PID:\(.pid) \(.details[:80])"' "$EVENT_FILE" | tail -20
    echo "└────────────────────────────────────────────────────"
    echo ""
fi

# ── 11. Current Survivors (from last snapshot) ──
if [ "$SNAP_COUNT" -ge 1 ]; then
    echo "┌─ LAST SNAPSHOT PROCESSES ────────────────────────────"
    jq -r '[.[] | select(.event=="SNAPSHOT")] | last |
        "│ Time: \(.ts)",
        "│ Total mem: \(.total_mem_mb)MB | Sys avail: \(.system.available_mb)MB | Swap: \(.system.swap_used_mb)MB",
        "│",
        (.procs // [] | sort_by(-.mem_mb) | .[] |
        "│ \(.type):\(.pid) \(.mem_mb)MB cpu:\(.cpu)% fd:\(.fd // "?") threads:\(.threads // "?") state:\(.state) age:\(
            if .age > 86400 then "\(.age / 86400 | floor)d\((.age % 86400) / 3600 | floor)h"
            elif .age > 3600 then "\(.age / 3600 | floor)h\((.age % 3600) / 60 | floor)m"
            elif .age > 60 then "\(.age / 60 | floor)m\(.age % 60)s"
            else "\(.age)s" end)")' "$EVENT_FILE" 2>/dev/null
    echo "└────────────────────────────────────────────────────"
    echo ""
fi

# ── 12. Rapid Spawn Detection ──
echo "┌─ RAPID SPAWN DETECTION ────────────────────────────"
# Find spawns that happened within 3 seconds of each other
RAPID=$(jq -r 'select(.event=="SPAWN") | "\(.ts) \(.pid) \(.details)"' "$EVENT_FILE" | \
    awk '
    {
        gsub(/[TZ]/, " ", $1); gsub(/-/, " ", $1); gsub(/\..*/, "", $1)
        cmd = "date -d \"" $1 "\" +%s 2>/dev/null"
        cmd | getline ts
        close(cmd)
        if (ts == "") next
        if (prev > 0 && ts - prev <= 3) {
            count++
            printf "│ %s +%ds PID:%s %s\n", $1, ts-prev, $2, $3
        }
        prev = ts
    }
    END { if (count+0 == 0) print "│ No rapid spawns detected (<3s apart)" }
    ' 2>/dev/null)
echo "$RAPID"
echo "└────────────────────────────────────────────────────"
echo ""

echo "═══════════════════════════════════════════════════════"
echo "  Analysis complete"
echo "═══════════════════════════════════════════════════════"

# ── Telegram Summary ──
if [ "$SEND_TELEGRAM" -eq 1 ]; then
    echo ""
    echo "Sending summary to Telegram..."

    # Build compact telegram report
    MEM_DELTA_TG="N/A"
    if [ "$SNAP_COUNT" -ge 2 ]; then
        FIRST_MEM_TG=$(jq -r 'select(.event=="SNAPSHOT") | .total_mem_mb' "$EVENT_FILE" | head -1)
        LAST_MEM_TG=$(jq -r 'select(.event=="SNAPSHOT") | .total_mem_mb' "$EVENT_FILE" | tail -1)
        if [ -n "$FIRST_MEM_TG" ] && [ -n "$LAST_MEM_TG" ]; then
            local_delta=$((LAST_MEM_TG - FIRST_MEM_TG))
            [ "$local_delta" -gt 0 ] && MEM_DELTA_TG="+${local_delta}MB" || MEM_DELTA_TG="${local_delta}MB"
        fi
    fi

    PEAKS_TG=$(jq -s '{peak: ([.[].total_mem_mb] | max), peak_swap: ([.[].system.swap_used_mb] | max)}' "$EVENT_FILE" 2>/dev/null)
    PEAK_MEM_TG=$(echo "$PEAKS_TG" | jq -r '.peak // "?"')
    PEAK_SWAP_TG=$(echo "$PEAKS_TG" | jq -r '.peak_swap // "?"')

    # Top growers
    TOP_GROWERS_TG=""
    if [ "$SNAP_COUNT" -ge 2 ]; then
        TOP_GROWERS_TG=$(jq -s '
            [.[] | select(.event=="SNAPSHOT")] |
            if length < 2 then empty else
            (first.procs // []) as $first |
            (last.procs // []) as $last |
            [$last[] as $l | ($first[] | select(.pid == $l.pid)) as $f |
             {pid: $l.pid, type: $l.type, delta: ($l.mem_mb - $f.mem_mb), to: $l.mem_mb,
              fd: ($l.fd // 0), threads: ($l.threads // 1)}] |
            sort_by(-.delta) | [limit(5; .[] | select(.delta > 10))] |
            if length == 0 then empty else
            .[] | "  \(.type):\(.pid) \(if .delta > 0 then "+" else "" end)\(.delta)MB =\(.to)MB fd:\(.fd) t:\(.threads)"
            end end
        ' "$EVENT_FILE" 2>/dev/null)
    fi

    # Leak candidates
    LEAK_TG=""
    if [ "$SNAP_COUNT" -ge 3 ]; then
        LEAK_TG=$(jq -s '
            [.[] | select(.event=="SNAPSHOT") | .procs // []] |
            if length < 3 then empty else
            [.[][] | .pid] | unique | .[] as $pid |
            {pid: $pid,
             readings: [.[][] | select(.pid == $pid) | .mem_mb],
             type: ([.[][] | select(.pid == $pid) | .type] | first)} |
            select(.readings | length >= 3) |
            {pid, type, total_delta: (.readings | last - first),
             diffs: [range(1; .readings | length) as $i | .readings[$i] - .readings[$i-1]]} |
            select(.total_delta > 20) |
            select([.diffs[] | . >= 0] | all) |
            "  ⚠️ \(.type):\(.pid) +\(.total_delta)MB"
            end
        ' "$EVENT_FILE" 2>/dev/null)
    fi

    LEAKED=$((SPAWN_COUNT - EXIT_COUNT - CLEANUP_COUNT))

    TG_MSG="🔬 <b>Session Analysis</b>
━━━━━━━━━━━━━━━━━
📅 <code>$(basename "$EVENT_FILE" .jsonl | sed 's/events_//')</code>
📊 Events: ${EVENT_COUNT} | Snapshots: ${SNAP_COUNT}

<b>Lifecycle</b>
  Born: ${SPAWN_COUNT} | Died: ${EXIT_COUNT}
  Orphaned: ${ORPHAN_COUNT} | Cleaned: ${CLEANUP_COUNT}
  Leaked: ${LEAKED}

<b>Memory</b>
  Peak: ${PEAK_MEM_TG}MB | Swap: ${PEAK_SWAP_TG}MB
  Trend: ${MEM_DELTA_TG}

<b>Anomalies</b>: ${ANOMALY_COUNT}"

    if [ -n "$TOP_GROWERS_TG" ]; then
        TG_MSG+="

<b>Top growers</b>
<pre>${TOP_GROWERS_TG}</pre>"
    fi

    if [ -n "$LEAK_TG" ]; then
        TG_MSG+="

<b>Leak suspects</b>
<pre>${LEAK_TG}</pre>"
    fi

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg chat_id "$TELEGRAM_CHAT_ID" --arg text "$TG_MSG" \
            '{chat_id: $chat_id, text: $text, parse_mode: "HTML", disable_web_page_preview: true}')" | \
        jq -r 'if .ok then "Sent to Telegram!" else "Failed: \(.description)" end'
fi
