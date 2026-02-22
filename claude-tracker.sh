#!/bin/bash
# Claude Code Process Lifecycle Tracker
# Monitors process birth/death events in real-time
#
# Usage:
#   ./claude-tracker.sh              # Start interactive tracking (Ctrl+C to stop)
#   ./claude-tracker.sh --status     # Quick status snapshot
#   ./claude-tracker.sh --cleanup    # Kill orphaned processes
#   ./claude-tracker.sh --daemon     # Run as background daemon (60s poll)
#   ./claude-tracker.sh --continuous # Continuous monitoring with auto-reports (for PM2)
#   ./claude-tracker.sh --benchmark  # 4-hour timed run with full report (background)
#   ./claude-tracker.sh --benchmark [T]  # Custom duration (e.g., 2h, 30m, 1h30m, 30s)
#   ./claude-tracker.sh --benchmark-fg [T]  # Foreground benchmark (legacy)

set -u  # Only fail on undefined variables, not on command errors

# Detect script location for portable LOG_DIR
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
SESSION_ID=$(date +%Y%m%d_%H%M%S)

# Date-based log files (rotate daily)
CURRENT_LOG_DATE=$(date +%Y%m%d)
EVENT_LOG="$LOG_DIR/events_${CURRENT_LOG_DATE}.jsonl"
TIMELINE_LOG="$LOG_DIR/timeline_${CURRENT_LOG_DATE}.log"

POLL_INTERVAL=2  # seconds between checks (overridden by modes)
START_TIME=""    # Set by benchmark/interactive/continuous modes

mkdir -p "$LOG_DIR"

# Validate jq dependency (required for JSONL event logging)
if ! command -v jq &>/dev/null; then
    echo "ERROR: jq is required but not installed" >&2
    exit 1
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m' # No Color
BOLD='\033[1m'

# State tracking
declare -A PREV_PROCS=()       # PID -> "type:mem:cpu:state:ppid:start_time"
declare -A PROC_BIRTH=()       # PID -> timestamp when first seen
declare -A MCP_PARENT=()       # MCP PID -> Claude parent PID
PREV_SNAPSHOT=""
EVENT_COUNT=0
ANOMALY_COUNT=0

# Basic anomaly dedup
SEEN_HIGH_COUNT=""
SEEN_HIGH_MEM=""
SEEN_ZOMBIES=""

# === Advanced anomaly detection state ===

# Restart loop detection: recent spawn/exit timestamps per process type
declare -A RECENT_SPAWNS=()   # "type" -> "ts1 ts2 ts3 ..."
declare -A RECENT_EXITS=()    # "type" -> "ts1 ts2 ts3 ..."
SEEN_RESTART_LOOP=""

# Memory leak detection: circular buffer of total_mem readings (~6 min at 10s)
MEM_READINGS=()
MEM_READINGS_COUNT=0

# Swap growth detection: circular buffer, compare current vs ~30 min ago
SWAP_READINGS=()
SWAP_READINGS_COUNT=0

# Stuck process detection (D state > 60s)
declare -A D_STATE_SINCE=()   # PID -> epoch when first seen in D state
SEEN_STUCK=""

# Memory leak / swap growth dedup
SEEN_MEM_LEAK=""
SEEN_SWAP_GROWTH=""

# Orphan candidate tracking (grace period before killing)
declare -A ORPHAN_CANDIDATES=()   # PID -> epoch when first seen as orphan

# Periodic task timing
LAST_CLEANUP_EPOCH=0
LAST_ORPHAN_CHECK=0
LAST_REPORT_TIME=0
LAST_SNAPSHOT_TIME=0
REPORT_INTERVAL=14400   # 4 hours in seconds
SNAPSHOT_INTERVAL=300    # 5 minutes

# Telegram notification config (set env vars or PM2 ecosystem config)
TELEGRAM_BOT_TOKEN="${TRACKER_TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TRACKER_TELEGRAM_CHAT_ID:-}"
TELEGRAM_ENABLED="${TRACKER_TELEGRAM_ENABLED:-1}"

# AI narrative analysis config (uses OpenClaw devops agent)
AI_NARRATIVE_ENABLED="${TRACKER_AI_NARRATIVE:-1}"
AI_NARRATIVE_AGENT="${TRACKER_AI_AGENT:-devops}"
AI_NARRATIVE_TIMEOUT="${TRACKER_AI_TIMEOUT:-120}"

# ═══════════════════════════════════════
# Utility Functions
# ═══════════════════════════════════════

get_system_mem() {
    local available=$(grep MemAvailable /proc/meminfo 2>/dev/null | awk '{print int($2/1024)}')
    local total=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print int($2/1024)}')
    local swap_used=$(free -m 2>/dev/null | grep Swap | awk '{print $3}')
    echo "${available:-0}:${total:-0}:${swap_used:-0}"
}

log_event() {
    local event_type="$1"
    local pid="${2:-}"
    local details="${3:-}"
    local ts=$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')
    local mem_total=$(get_total_mem)
    local proc_counts=$(get_proc_counts)
    local sys_mem=$(get_system_mem)
    local sys_avail=$(echo "$sys_mem" | cut -d: -f1)
    local sys_total=$(echo "$sys_mem" | cut -d: -f2)
    local sys_swap=$(echo "$sys_mem" | cut -d: -f3)

    ((EVENT_COUNT++))

    local json=$(jq -c -n \
        --arg ts "$ts" \
        --arg event "$event_type" \
        --arg pid "$pid" \
        --arg details "$details" \
        --arg mem "$mem_total" \
        --arg counts "$proc_counts" \
        --arg event_num "$EVENT_COUNT" \
        --arg sys_avail "$sys_avail" \
        --arg sys_total "$sys_total" \
        --arg sys_swap "$sys_swap" \
        '{ts: $ts, event_num: ($event_num|tonumber), event: $event, pid: $pid, details: $details, total_mem_mb: ($mem|tonumber), counts: $counts, system: {available_mb: ($sys_avail|tonumber), total_mb: ($sys_total|tonumber), swap_used_mb: ($sys_swap|tonumber)}}'
    )

    echo "$json" >> "$EVENT_LOG"

    # Also log to timeline in human readable format
    echo "[$ts] #$EVENT_COUNT $event_type ${pid:+PID:$pid} $details [mem:${mem_total}MB, $proc_counts, sys:${sys_avail}MB avail, swap:${sys_swap}MB]" >> "$TIMELINE_LOG"
}

get_total_mem() {
    ps aux --no-headers 2>/dev/null | grep -E "claude|claude-mem" | grep -v "claude-tracker\|claude-monitor" | awk '{sum+=$6} END {print int(sum/1024)}'
}

get_proc_counts() {
    local claude_count=$(pgrep -c -f "claude" 2>/dev/null || echo 0)
    # Subtract monitor scripts from count
    local monitor_count=$(pgrep -c -f "claude-tracker|claude-monitor" 2>/dev/null || echo 0)
    claude_count=$((claude_count - monitor_count))
    [ "$claude_count" -lt 0 ] && claude_count=0

    local mcp_count=$(pgrep -c -f "claude-mem.*mcp-server" 2>/dev/null || echo 0)
    local worker_count=$(pgrep -c -f "claude-mem.*worker-service" 2>/dev/null || echo 0)
    local chroma_count=$(pgrep -c -f "chroma-mcp" 2>/dev/null || echo 0)
    echo "claude:$claude_count,mcp:$mcp_count,worker:$worker_count,chroma:$chroma_count"
}

get_process_info() {
    local pid=$1
    if [ -d "/proc/$pid" ]; then
        local stat=$(cat /proc/$pid/stat 2>/dev/null || echo "")
        local cmdline=$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | head -c 200 || echo "")
        local mem_kb=$(awk '/VmRSS/{print $2}' /proc/$pid/status 2>/dev/null || echo 0)
        local state=$(echo "$stat" | awk '{print $3}')
        local ppid=$(echo "$stat" | awk '{print $4}')
        local start_time=$(echo "$stat" | awk '{print $22}')
        # Get CPU from ps (more reliable than calculating from /proc)
        local cpu=$(ps -o %cpu= -p "$pid" 2>/dev/null | tr -d ' ' || echo "0.0")
        # FD count and thread count
        local fd_count=$(ls /proc/$pid/fd 2>/dev/null | wc -l || echo 0)
        local threads=$(awk '/Threads/{print $2}' /proc/$pid/status 2>/dev/null || echo 1)
        echo "$mem_kb:$state:$ppid:$start_time:$cpu:$fd_count:$threads:$cmdline"
    else
        echo ""
    fi
}

detect_process_type() {
    local cmdline="$1"
    if [[ "$cmdline" =~ "mcp-server.cjs" ]]; then
        echo "mcp-server"
    elif [[ "$cmdline" =~ "worker-service.cjs" ]]; then
        echo "worker"
    elif [[ "$cmdline" =~ "chroma-mcp" ]] || [[ "$cmdline" =~ "chroma" ]]; then
        echo "chroma"
    elif [[ "$cmdline" =~ claude ]] && [[ ! "$cmdline" =~ "mcp-server" ]] && [[ ! "$cmdline" =~ "worker-service" ]]; then
        echo "claude-code"
    else
        echo "other"
    fi
}

# ═══════════════════════════════════════
# Log Rotation & Cleanup
# ═══════════════════════════════════════

rotate_logs_if_needed() {
    local today=$(date +%Y%m%d)
    if [ "$today" != "$CURRENT_LOG_DATE" ]; then
        log_event "LOG_ROTATE" "" "Day change: $CURRENT_LOG_DATE -> $today"
        CURRENT_LOG_DATE="$today"
        EVENT_LOG="$LOG_DIR/events_${CURRENT_LOG_DATE}.jsonl"
        TIMELINE_LOG="$LOG_DIR/timeline_${CURRENT_LOG_DATE}.log"
        log_event "LOG_ROTATE" "" "Rotated to new day: $today"
    fi
}

cleanup_old_logs() {
    local now=$(date +%s)
    # Run at most once per hour
    if [ $((now - LAST_CLEANUP_EPOCH)) -lt 3600 ]; then
        return
    fi
    LAST_CLEANUP_EPOCH=$now

    local cleaned=0

    # Delete date-based logs older than 7 days
    local count=$(find "$LOG_DIR" -maxdepth 1 \( -name "events_*.jsonl" -o -name "timeline_*.log" -o -name "report_*.md" \) -mtime +7 2>/dev/null | wc -l)
    if [ "$count" -gt 0 ]; then
        find "$LOG_DIR" -maxdepth 1 \( -name "events_*.jsonl" -o -name "timeline_*.log" -o -name "report_*.md" \) -mtime +7 -delete 2>/dev/null
        cleaned=$((cleaned + count))
    fi

    # Delete legacy session files older than 7 days
    local legacy_count=$(find "$LOG_DIR" -maxdepth 1 \( -name "session_*" -o -name "benchmark_*_report.md" -o -name "benchmark_*_stdout.log" \) -mtime +7 2>/dev/null | wc -l)
    if [ "$legacy_count" -gt 0 ]; then
        find "$LOG_DIR" -maxdepth 1 \( -name "session_*" -o -name "benchmark_*_report.md" -o -name "benchmark_*_stdout.log" \) -mtime +7 -delete 2>/dev/null
        cleaned=$((cleaned + legacy_count))
    fi

    # Truncate PM2 logs if > 10MB (keep last 1MB)
    # Use cp (not mv) to preserve the inode so PM2's open file descriptor stays valid
    for pm2log in "$LOG_DIR"/pm2-*.log; do
        [ -f "$pm2log" ] || continue
        local size=$(stat -c %s "$pm2log" 2>/dev/null || echo 0)
        if [ "$size" -gt 10485760 ]; then
            tail -c 1048576 "$pm2log" > "${pm2log}.tmp" 2>/dev/null
            cp "${pm2log}.tmp" "$pm2log" 2>/dev/null
            rm -f "${pm2log}.tmp"
            log_event "LOG_CLEANUP" "" "Truncated $(basename "$pm2log") from $((size / 1048576))MB to ~1MB"
            ((cleaned++))
        fi
    done

    if [ "$cleaned" -gt 0 ]; then
        log_event "LOG_CLEANUP" "" "Cleaned up $cleaned old files/logs"
    fi
}

# ═══════════════════════════════════════
# Anomaly Detection (Enhanced)
# ═══════════════════════════════════════

check_for_anomalies() {
    local claude_count=$1
    local mcp_count=$2
    local mem_mb=$3

    # --- Original checks ---

    # Check for high instance count
    if [ "$claude_count" -ge 5 ]; then
        if [ "$SEEN_HIGH_COUNT" != "$claude_count" ]; then
            SEEN_HIGH_COUNT="$claude_count"
            ((ANOMALY_COUNT++))
            log_event "ANOMALY" "" "High Claude instance count: $claude_count (threshold: 5)"
            echo -e "${YELLOW}⚠️  ANOMALY #$ANOMALY_COUNT: High instance count ($claude_count)${NC}"
        fi
    else
        SEEN_HIGH_COUNT=""
    fi

    # Check memory threshold (alert at each 2GB increment)
    local mem_tier=$((mem_mb / 2000))
    if [ "$mem_mb" -gt 6000 ]; then
        if [ "$SEEN_HIGH_MEM" != "$mem_tier" ]; then
            SEEN_HIGH_MEM="$mem_tier"
            ((ANOMALY_COUNT++))
            log_event "ANOMALY" "" "High memory usage: ${mem_mb}MB (threshold: 6000MB)"
            echo -e "${YELLOW}⚠️  ANOMALY #$ANOMALY_COUNT: High memory (${mem_mb}MB)${NC}"
        fi
    fi

    # Check for zombie processes
    local zombie_count=$(ps aux 2>/dev/null | grep -E "[c]laude" | grep " Z " | wc -l || echo 0)
    if [ "$zombie_count" -gt 0 ]; then
        if [ "$SEEN_ZOMBIES" != "$zombie_count" ]; then
            SEEN_ZOMBIES="$zombie_count"
            ((ANOMALY_COUNT++))
            log_event "ANOMALY" "" "Zombie processes detected: $zombie_count"
            echo -e "${RED}⚠️  ANOMALY #$ANOMALY_COUNT: $zombie_count zombie process(es)${NC}"
        fi
    else
        SEEN_ZOMBIES=""
    fi

    # --- Advanced checks ---

    local now=$(date +%s)

    # (a) Restart loop detection
    # Check each process type for >3 spawn+exit cycles in 5 min
    for ptype in "${!RECENT_SPAWNS[@]}"; do
        # Prune spawns older than 5 min
        local spawn_count=0
        local new_spawns=""
        for ts in ${RECENT_SPAWNS[$ptype]}; do
            if [ $((now - ts)) -lt 300 ]; then
                ((spawn_count++))
                new_spawns="$new_spawns $ts"
            fi
        done
        RECENT_SPAWNS[$ptype]="${new_spawns# }"

        # Prune exits older than 5 min
        local exit_count=0
        local new_exits=""
        if [ -n "${RECENT_EXITS[$ptype]:-}" ]; then
            for ts in ${RECENT_EXITS[$ptype]}; do
                if [ $((now - ts)) -lt 300 ]; then
                    ((exit_count++))
                    new_exits="$new_exits $ts"
                fi
            done
            RECENT_EXITS[$ptype]="${new_exits# }"
        fi

        if [ "$spawn_count" -gt 3 ] && [ "$exit_count" -gt 3 ]; then
            if [ "$SEEN_RESTART_LOOP" != "$ptype" ]; then
                SEEN_RESTART_LOOP="$ptype"
                ((ANOMALY_COUNT++))
                log_event "ANOMALY_RESTART_LOOP" "" "$ptype restart loop: $spawn_count spawns, $exit_count exits in 5min"
                echo -e "${RED}⚠️  ANOMALY #$ANOMALY_COUNT: Restart loop detected for $ptype${NC}"
            fi
        elif [ "$SEEN_RESTART_LOOP" = "$ptype" ]; then
            SEEN_RESTART_LOOP=""
        fi
    done

    # (b) Memory leak detection
    # Store reading in circular buffer (36 slots = ~6 min at 10s)
    MEM_READINGS[$((MEM_READINGS_COUNT % 36))]=$mem_mb
    ((MEM_READINGS_COUNT++))

    if [ "$MEM_READINGS_COUNT" -ge 36 ]; then
        local first_idx=$(( (MEM_READINGS_COUNT - 36) % 36 ))
        local first_val=${MEM_READINGS[$first_idx]}
        local last_val=$mem_mb
        local delta=$((last_val - first_val))

        if [ "$delta" -gt 200 ]; then
            # Count non-decreasing transitions
            local increasing=0
            local prev=${MEM_READINGS[$first_idx]}
            for ((i=1; i<36; i++)); do
                local idx=$(( (first_idx + i) % 36 ))
                local val=${MEM_READINGS[$idx]}
                if [ "$val" -ge "$prev" ]; then
                    ((increasing++))
                fi
                prev=$val
            done

            # If >80% of readings are non-decreasing, flag as leak
            if [ "$increasing" -gt 28 ]; then
                if [ -z "$SEEN_MEM_LEAK" ]; then
                    SEEN_MEM_LEAK=1
                    ((ANOMALY_COUNT++))
                    log_event "ANOMALY_MEM_LEAK" "" "Memory growing consistently: +${delta}MB over ~6min ($increasing/35 non-decreasing)"
                    echo -e "${RED}⚠️  ANOMALY #$ANOMALY_COUNT: Memory leak detected (+${delta}MB in ~6min)${NC}"
                fi
            else
                SEEN_MEM_LEAK=""
            fi
        else
            SEEN_MEM_LEAK=""
        fi
    fi

    # (c) Swap growth detection
    # Store swap in circular buffer (180 slots = ~30 min at 10s)
    local sys_mem=$(get_system_mem)
    local current_swap=$(echo "$sys_mem" | cut -d: -f3)
    SWAP_READINGS[$((SWAP_READINGS_COUNT % 180))]=$current_swap
    ((SWAP_READINGS_COUNT++))

    if [ "$SWAP_READINGS_COUNT" -ge 180 ]; then
        local old_idx=$(( (SWAP_READINGS_COUNT - 180) % 180 ))
        local old_swap=${SWAP_READINGS[$old_idx]}
        local swap_delta=$((current_swap - old_swap))

        if [ "$swap_delta" -gt 500 ]; then
            if [ -z "$SEEN_SWAP_GROWTH" ]; then
                SEEN_SWAP_GROWTH=1
                ((ANOMALY_COUNT++))
                log_event "ANOMALY_SWAP_GROWTH" "" "Swap grew +${swap_delta}MB in ~30min (${old_swap}MB -> ${current_swap}MB)"
                echo -e "${RED}⚠️  ANOMALY #$ANOMALY_COUNT: Swap growth +${swap_delta}MB in ~30min${NC}"
            fi
        else
            SEEN_SWAP_GROWTH=""
        fi
    fi

    # (d) Stuck process detection (D state > 60s)
    local current_d_pids=""
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local pid=$(echo "$line" | awk '{print $2}')
        local state=$(echo "$line" | awk '{print $8}')
        if [[ "$state" == D* ]]; then
            current_d_pids="$current_d_pids $pid"
            if [ -z "${D_STATE_SINCE[$pid]:-}" ]; then
                D_STATE_SINCE[$pid]=$now
            elif [ $((now - ${D_STATE_SINCE[$pid]})) -gt 60 ]; then
                if [ "$SEEN_STUCK" != "$pid" ]; then
                    SEEN_STUCK="$pid"
                    ((ANOMALY_COUNT++))
                    log_event "ANOMALY_STUCK" "$pid" "Process in D (uninterruptible sleep) for >60s"
                    echo -e "${RED}⚠️  ANOMALY #$ANOMALY_COUNT: Stuck process PID:$pid (D state >60s)${NC}"
                fi
            fi
        fi
    done <<< "$(ps aux --no-headers 2>/dev/null | grep -E "claude" | grep -v "grep\|claude-tracker\|claude-monitor")"

    # Clear D state tracking for PIDs no longer in D state
    for pid in "${!D_STATE_SINCE[@]}"; do
        if [[ ! " $current_d_pids " =~ " $pid " ]]; then
            unset D_STATE_SINCE[$pid]
            if [ "$SEEN_STUCK" = "$pid" ]; then
                SEEN_STUCK=""
            fi
        fi
    done
}

# ═══════════════════════════════════════
# Process Scanning
# ═══════════════════════════════════════

scan_processes() {
    declare -A CURRENT_PROCS=()

    # Get all Claude-related processes
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local pid=$(echo "$line" | awk '{print $2}')

        # Skip self
        [[ "$pid" == "$$" ]] && continue
        [[ "$line" =~ "claude-tracker" ]] && continue
        [[ "$line" =~ "claude-monitor" ]] && continue

        local info=$(get_process_info "$pid")
        [ -z "$info" ] && continue

        local mem_kb=$(echo "$info" | cut -d: -f1)
        local state=$(echo "$info" | cut -d: -f2)
        local ppid=$(echo "$info" | cut -d: -f3)
        local start_time=$(echo "$info" | cut -d: -f4)
        local cpu=$(echo "$info" | cut -d: -f5)
        local fd_count=$(echo "$info" | cut -d: -f6)
        local threads=$(echo "$info" | cut -d: -f7)
        local cmdline=$(echo "$info" | cut -d: -f8-)
        local proc_type=$(detect_process_type "$cmdline")
        local mem_mb=$((mem_kb / 1024))

        CURRENT_PROCS[$pid]="$proc_type:$mem_mb:$cpu:$state:$ppid:$start_time:$fd_count:$threads"

        # Check for new processes
        if [ -z "${PREV_PROCS[$pid]:-}" ]; then
            PROC_BIRTH[$pid]=$(date +%s)

            local parent_info=""
            if [ "$proc_type" = "mcp-server" ] && [ -n "$ppid" ]; then
                local parent_cmd=$(ps -o comm= -p "$ppid" 2>/dev/null || echo "unknown")
                parent_info="parent=$ppid($parent_cmd)"
                if [[ "$parent_cmd" =~ "claude" ]]; then
                    MCP_PARENT[$pid]=$ppid
                fi
            fi

            log_event "SPAWN" "$pid" "$proc_type ${mem_mb}MB cpu=${cpu}% state=$state $parent_info"
            echo -e "${GREEN}▲ SPAWN${NC} PID:$pid ${CYAN}$proc_type${NC} ${mem_mb}MB ${cpu}% $parent_info"

            # Record spawn time for restart loop detection
            RECENT_SPAWNS[$proc_type]="${RECENT_SPAWNS[$proc_type]:-} $(date +%s)"
        else
            # Check for state changes
            local prev_state=$(echo "${PREV_PROCS[$pid]}" | cut -d: -f4)
            local prev_mem=$(echo "${PREV_PROCS[$pid]}" | cut -d: -f2)

            if [ "$state" != "$prev_state" ]; then
                log_event "STATE_CHANGE" "$pid" "$proc_type $prev_state->$state"
                echo -e "${YELLOW}◆ STATE${NC} PID:$pid ${CYAN}$proc_type${NC} $prev_state → $state"
            fi

            # Check for significant memory change (>50MB)
            local mem_diff=$((mem_mb - prev_mem))
            if [ "${mem_diff#-}" -gt 50 ]; then
                local sign=""
                [ "$mem_diff" -gt 0 ] && sign="+"
                [ "$mem_diff" -lt 0 ] && sign="-"
                local abs_diff=${mem_diff#-}
                log_event "MEM_CHANGE" "$pid" "$proc_type ${prev_mem}MB->${mem_mb}MB (${sign}${abs_diff}MB)"
                echo -e "${MAGENTA}◇ MEM${NC} PID:$pid ${CYAN}$proc_type${NC} ${prev_mem}→${mem_mb}MB"
            fi
        fi
    done <<< "$(ps aux --no-headers | grep -E "claude" | grep -v grep)"

    # Check for dead processes
    for pid in "${!PREV_PROCS[@]}"; do
        if [ -z "${CURRENT_PROCS[$pid]:-}" ]; then
            local prev_info="${PREV_PROCS[$pid]}"
            local proc_type=$(echo "$prev_info" | cut -d: -f1)
            local mem_mb=$(echo "$prev_info" | cut -d: -f2)
            local lifetime=""

            if [ -n "${PROC_BIRTH[$pid]:-}" ]; then
                local birth=${PROC_BIRTH[$pid]}
                local now=$(date +%s)
                local age=$((now - birth))
                lifetime="lived ${age}s"
                unset PROC_BIRTH[$pid]
            fi

            # Check if this was a parent of an MCP
            local orphaned_mcp=""
            for mcp_pid in "${!MCP_PARENT[@]}"; do
                if [ "${MCP_PARENT[$mcp_pid]}" = "$pid" ]; then
                    orphaned_mcp="orphaned MCP:$mcp_pid"
                    log_event "ORPHAN" "$mcp_pid" "mcp-server orphaned by death of $pid"
                    echo -e "${RED}☠ ORPHAN${NC} MCP PID:$mcp_pid (parent $pid died)"
                fi
            done

            log_event "EXIT" "$pid" "$proc_type ${mem_mb}MB $lifetime $orphaned_mcp"
            echo -e "${RED}▼ EXIT${NC} PID:$pid ${CYAN}$proc_type${NC} ${mem_mb}MB $lifetime"

            # Record exit time for restart loop detection
            RECENT_EXITS[$proc_type]="${RECENT_EXITS[$proc_type]:-} $(date +%s)"
        fi
    done

    # Update previous state
    PREV_PROCS=()
    for pid in "${!CURRENT_PROCS[@]}"; do
        PREV_PROCS[$pid]="${CURRENT_PROCS[$pid]}"
    done

    # Count and check for anomalies
    local claude_count=0
    local mcp_count=0
    local total_mem=0
    for pid in "${!CURRENT_PROCS[@]}"; do
        local info="${CURRENT_PROCS[$pid]}"
        local ptype=$(echo "$info" | cut -d: -f1)
        local pmem=$(echo "$info" | cut -d: -f2)
        total_mem=$((total_mem + pmem))
        [[ "$ptype" == "claude-code" ]] && ((claude_count++))
        [[ "$ptype" == "mcp-server" ]] && ((mcp_count++))
    done

    check_for_anomalies "$claude_count" "$mcp_count" "$total_mem"
}

# ═══════════════════════════════════════
# Per-PID Snapshot (time series data)
# ═══════════════════════════════════════

take_snapshot() {
    # Emits a SNAPSHOT event with per-PID details for time series analysis
    # Called every SNAPSHOT_INTERVAL (5 min) from the main loop
    local now=$(date +%s)
    if [ $((now - LAST_SNAPSHOT_TIME)) -lt $SNAPSHOT_INTERVAL ]; then
        return
    fi
    LAST_SNAPSHOT_TIME=$now

    local ts=$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')
    local sys_mem=$(get_system_mem)
    local sys_avail=$(echo "$sys_mem" | cut -d: -f1)
    local sys_total=$(echo "$sys_mem" | cut -d: -f2)
    local sys_swap=$(echo "$sys_mem" | cut -d: -f3)

    # Build JSON array of per-PID data from PREV_PROCS
    local procs_json="["
    local first=1
    for pid in "${!PREV_PROCS[@]}"; do
        local info="${PREV_PROCS[$pid]}"
        local ptype=$(echo "$info" | cut -d: -f1)
        local pmem=$(echo "$info" | cut -d: -f2)
        local pcpu=$(echo "$info" | cut -d: -f3)
        local pstate=$(echo "$info" | cut -d: -f4)
        local pppid=$(echo "$info" | cut -d: -f5)
        local pfd=$(echo "$info" | cut -d: -f7)
        local pthreads=$(echo "$info" | cut -d: -f8)
        # Age in seconds
        local age=0
        if [ -n "${PROC_BIRTH[$pid]:-}" ]; then
            age=$((now - PROC_BIRTH[$pid]))
        fi

        [ "$first" -eq 0 ] && procs_json+=","
        first=0
        procs_json+="{\"pid\":$pid,\"type\":\"$ptype\",\"mem_mb\":$pmem,\"cpu\":$pcpu,\"state\":\"$pstate\",\"ppid\":$pppid,\"fd\":${pfd:-0},\"threads\":${pthreads:-1},\"age\":$age}"
    done
    procs_json+="]"

    local total_mem=$(get_total_mem)
    local proc_counts=$(get_proc_counts)

    ((EVENT_COUNT++))

    local json=$(jq -c -n \
        --arg ts "$ts" \
        --arg event "SNAPSHOT" \
        --arg event_num "$EVENT_COUNT" \
        --arg mem "$total_mem" \
        --arg counts "$proc_counts" \
        --arg sys_avail "$sys_avail" \
        --arg sys_total "$sys_total" \
        --arg sys_swap "$sys_swap" \
        --argjson procs "$procs_json" \
        '{ts: $ts, event_num: ($event_num|tonumber), event: $event, pid: "", details: "", total_mem_mb: ($mem|tonumber), counts: $counts, system: {available_mb: ($sys_avail|tonumber), total_mb: ($sys_total|tonumber), swap_used_mb: ($sys_swap|tonumber)}, procs: $procs}'
    )

    echo "$json" >> "$EVENT_LOG"
    echo "[$ts] #$EVENT_COUNT SNAPSHOT procs=$(echo "$procs_json" | jq 'length') total_mem=${total_mem}MB avail=${sys_avail}MB swap=${sys_swap}MB" >> "$TIMELINE_LOG"
}

# ═══════════════════════════════════════
# Orphan Management
# ═══════════════════════════════════════

cleanup_orphans() {
    echo -e "${BOLD}Scanning for orphaned processes...${NC}"
    local cleaned=0

    # Find mcp-server processes without claude parent
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local mcp_pid=$(echo "$line" | awk '{print $2}')
        local mcp_ppid=$(ps -o ppid= -p "$mcp_pid" 2>/dev/null | tr -d ' ')
        if [ -n "$mcp_ppid" ]; then
            local parent_cmd=$(ps -o comm= -p "$mcp_ppid" 2>/dev/null)
            if [[ ! "$parent_cmd" =~ claude ]]; then
                echo -e "${YELLOW}Killing orphaned mcp-server PID $mcp_pid (parent: $mcp_ppid = $parent_cmd)${NC}"
                kill "$mcp_pid" 2>/dev/null && ((cleaned++))
            fi
        fi
    done <<< "$(ps aux --no-headers | grep -E "claude-mem.*mcp-server" | grep -v grep)"

    # Find chroma processes with PPID=1 (truly orphaned)
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local chroma_pid=$(echo "$line" | awk '{print $2}')
        local chroma_ppid=$(ps -o ppid= -p "$chroma_pid" 2>/dev/null | tr -d ' ')
        if [ "$chroma_ppid" = "1" ]; then
            echo -e "${YELLOW}Killing orphaned chroma PID $chroma_pid (PPID=1)${NC}"
            kill "$chroma_pid" 2>/dev/null && ((cleaned++))
        fi
    done <<< "$(ps aux --no-headers | grep -E "chroma-mcp" | grep -v grep)"

    if [ "$cleaned" -eq 0 ]; then
        echo -e "${GREEN}No orphaned processes found.${NC}"
    else
        echo -e "${GREEN}Cleaned up $cleaned orphaned process(es).${NC}"
    fi
}

auto_cleanup_orphans() {
    local now=$(date +%s)
    # Run every 5 minutes
    if [ $((now - LAST_ORPHAN_CHECK)) -lt 300 ]; then
        return
    fi
    LAST_ORPHAN_CHECK=$now

    local cleaned=0
    declare -A seen_this_check=()

    # Find orphaned MCP servers (parent is not claude)
    # Uses a grace period: only kill after being seen as orphan in 2+ consecutive checks
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local mcp_pid=$(echo "$line" | awk '{print $2}')
        local mcp_ppid=$(ps -o ppid= -p "$mcp_pid" 2>/dev/null | tr -d ' ')
        if [ -n "$mcp_ppid" ]; then
            local parent_cmd=$(ps -o comm= -p "$mcp_ppid" 2>/dev/null)
            if [[ ! "$parent_cmd" =~ claude ]]; then
                seen_this_check[$mcp_pid]=1
                if [ -z "${ORPHAN_CANDIDATES[$mcp_pid]:-}" ]; then
                    # First sighting: record as candidate, don't kill yet
                    ORPHAN_CANDIDATES[$mcp_pid]=$now
                    log_event "ORPHAN_CANDIDATE" "$mcp_pid" "Suspected orphan mcp-server (parent=$mcp_ppid:$parent_cmd), will recheck"
                elif [ $((now - ${ORPHAN_CANDIDATES[$mcp_pid]})) -ge 300 ]; then
                    # Confirmed orphan: seen as orphan for 5+ minutes
                    log_event "AUTO_CLEANUP" "$mcp_pid" "Confirmed orphan mcp-server (parent=$mcp_ppid:$parent_cmd)"
                    # Graceful SIGTERM first
                    kill "$mcp_pid" 2>/dev/null
                    # Wait up to 5s for exit
                    local waited=0
                    while [ "$waited" -lt 5 ] && kill -0 "$mcp_pid" 2>/dev/null; do
                        sleep 1
                        ((waited++))
                    done
                    # Force kill if still alive
                    if kill -0 "$mcp_pid" 2>/dev/null; then
                        kill -9 "$mcp_pid" 2>/dev/null
                        log_event "AUTO_CLEANUP" "$mcp_pid" "Force-killed orphan (SIGKILL after ${waited}s)"
                    fi
                    unset ORPHAN_CANDIDATES[$mcp_pid]
                    ((cleaned++))
                fi
            fi
        fi
    done <<< "$(ps aux --no-headers 2>/dev/null | grep -E "claude-mem.*mcp-server" | grep -v grep)"

    # Find orphaned chroma processes (PPID=1)
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local chroma_pid=$(echo "$line" | awk '{print $2}')
        local chroma_ppid=$(ps -o ppid= -p "$chroma_pid" 2>/dev/null | tr -d ' ')
        if [ "$chroma_ppid" = "1" ]; then
            seen_this_check[$chroma_pid]=1
            if [ -z "${ORPHAN_CANDIDATES[$chroma_pid]:-}" ]; then
                ORPHAN_CANDIDATES[$chroma_pid]=$now
                log_event "ORPHAN_CANDIDATE" "$chroma_pid" "Suspected orphan chroma (PPID=1), will recheck"
            elif [ $((now - ${ORPHAN_CANDIDATES[$chroma_pid]})) -ge 300 ]; then
                log_event "AUTO_CLEANUP" "$chroma_pid" "Confirmed orphan chroma (PPID=1)"
                kill "$chroma_pid" 2>/dev/null
                local waited=0
                while [ "$waited" -lt 5 ] && kill -0 "$chroma_pid" 2>/dev/null; do
                    sleep 1
                    ((waited++))
                done
                if kill -0 "$chroma_pid" 2>/dev/null; then
                    kill -9 "$chroma_pid" 2>/dev/null
                fi
                unset ORPHAN_CANDIDATES[$chroma_pid]
                ((cleaned++))
            fi
        fi
    done <<< "$(ps aux --no-headers 2>/dev/null | grep -E "chroma-mcp" | grep -v grep)"

    # Clear candidates that are no longer seen as orphaned
    for pid in "${!ORPHAN_CANDIDATES[@]}"; do
        if [ -z "${seen_this_check[$pid]:-}" ]; then
            unset ORPHAN_CANDIDATES[$pid]
        fi
    done

    if [ "$cleaned" -gt 0 ]; then
        log_event "AUTO_CLEANUP" "" "Auto-cleaned $cleaned orphaned process(es)"
    fi
}

# ═══════════════════════════════════════
# Display Functions
# ═══════════════════════════════════════

print_status_bar() {
    local counts=$(get_proc_counts)
    local claude_count=$(echo "$counts" | grep -oP 'claude:\K[0-9]+')
    local mcp_count=$(echo "$counts" | grep -oP 'mcp:\K[0-9]+')
    local worker_count=$(echo "$counts" | grep -oP 'worker:\K[0-9]+')
    local chroma_count=$(echo "$counts" | grep -oP 'chroma:\K[0-9]+')
    local mem_mb=$(get_total_mem)
    local sys_mem=$(get_system_mem)
    local sys_swap=$(echo "$sys_mem" | cut -d: -f3)
    local uptime=$(($(date +%s) - START_TIME))
    local uptime_str=$(printf '%02d:%02d:%02d' $((uptime/3600)) $((uptime%3600/60)) $((uptime%60)))

    echo -ne "\r${BOLD}[${uptime_str}]${NC} Claude:${CYAN}$claude_count${NC} MCP:${CYAN}$mcp_count${NC} W:${CYAN}$worker_count${NC} Ch:${CYAN}$chroma_count${NC} Mem:${CYAN}${mem_mb}MB${NC} Swap:${MAGENTA}${sys_swap}MB${NC} Ev:${GREEN}$EVENT_COUNT${NC} Anom:${RED}$ANOMALY_COUNT${NC}    "
}

show_summary() {
    echo ""
    echo -e "${BOLD}═══════════════════════════════════════${NC}"
    echo -e "${BOLD}Session Summary${NC}"
    echo -e "${BOLD}═══════════════════════════════════════${NC}"
    echo "Session ID: $SESSION_ID"
    echo "Duration: $(($(date +%s) - START_TIME)) seconds"
    echo "Total events: $EVENT_COUNT"
    echo "Anomalies detected: $ANOMALY_COUNT"
    echo ""
    echo "Event log: $EVENT_LOG"
    echo "Timeline:  $TIMELINE_LOG"
    echo ""

    if [ -f "$EVENT_LOG" ] && [ -s "$EVENT_LOG" ]; then
        echo -e "${BOLD}Event breakdown:${NC}"
        jq -r '.event' "$EVENT_LOG" | sort | uniq -c | sort -rn

        echo ""
        echo -e "${BOLD}Anomalies:${NC}"
        grep "ANOMALY" "$EVENT_LOG" | jq -r '"\(.ts): \(.details)"' 2>/dev/null || echo "None"
    fi
}

# === STATUS MODE ===
show_status() {
    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║   Claude Code Process Status                         ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""

    # System memory
    local sys_mem=$(get_system_mem)
    local sys_avail=$(echo "$sys_mem" | cut -d: -f1)
    local sys_total=$(echo "$sys_mem" | cut -d: -f2)
    local sys_swap=$(echo "$sys_mem" | cut -d: -f3)
    echo -e "${BOLD}System:${NC} ${sys_avail}MB available / ${sys_total}MB total, Swap: ${sys_swap}MB used"
    echo ""

    echo -e "${BOLD}📊 Claude Processes:${NC}"
    ps aux --no-headers | grep -E "[c]laude" | grep -v "claude-tracker\|claude-monitor" | awk '
        BEGIN { count=0; mem=0 }
        {
            count++
            mem+=$6/1024
            type="claude-code"
            if ($0 ~ /mcp-server/) type="mcp-server"
            else if ($0 ~ /worker-service/) type="worker"
            else if ($0 ~ /chroma/) type="chroma"
            printf "  PID %-8s %5.1f%% CPU  %6dMB  state=%-2s  %s\n", $2, $3, $6/1024, $8, type
        }
        END { printf "\n  Total: %d processes, %dMB RAM\n", count, mem }
    '

    echo ""
    echo -e "${BOLD}🔌 MCP Servers:${NC}"
    ps aux --no-headers | grep -E "claude-mem.*mcp-server" | grep -v grep | awk '
        { printf "  PID %-8s %dMB RAM\n", $2, $6/1024 }
    ' || echo "  None running"

    echo ""
    echo -e "${BOLD}⚙️  Worker Service:${NC}"
    ps aux --no-headers | grep -E "claude-mem.*worker-service" | grep -v grep | awk '
        { printf "  PID %-8s %dMB RAM\n", $2, $6/1024 }
    ' || echo "  None running"

    echo ""
    echo -e "${BOLD}🔍 Chroma:${NC}"
    ps aux --no-headers | grep -E "chroma-mcp" | grep -v grep | awk '
        { printf "  PID %-8s %dMB RAM\n", $2, $6/1024 }
    ' || echo "  None running"

    # Check for zombies
    local zombie_count=$(ps aux 2>/dev/null | grep -E "[c]laude" | grep " Z " | wc -l || echo 0)
    if [ "$zombie_count" -gt 0 ]; then
        echo ""
        echo -e "${RED}⚠️  Zombie processes: $zombie_count${NC}"
    fi

    # Check for orphans
    local orphan_count=0
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local mcp_pid=$(echo "$line" | awk '{print $2}')
        local mcp_ppid=$(ps -o ppid= -p "$mcp_pid" 2>/dev/null | tr -d ' ')
        if [ -n "$mcp_ppid" ]; then
            local parent_cmd=$(ps -o comm= -p "$mcp_ppid" 2>/dev/null)
            if [[ ! "$parent_cmd" =~ claude ]]; then
                ((orphan_count++))
            fi
        fi
    done <<< "$(ps aux --no-headers | grep -E "claude-mem.*mcp-server" | grep -v grep)"

    if [ "$orphan_count" -gt 0 ]; then
        echo ""
        echo -e "${YELLOW}⚠️  Orphaned MCP servers: $orphan_count (run --cleanup to fix)${NC}"
    fi

    echo ""
    echo -e "${BOLD}📈 Recent Log Files:${NC}"
    ls -1t "$LOG_DIR"/events_*.jsonl 2>/dev/null | head -5 | while read -r f; do
        local datestr=$(basename "$f" | sed 's/events_\(.*\)\.jsonl/\1/')
        local events=$(wc -l < "$f")
        local anomalies=$(grep -c '"ANOMALY' "$f" 2>/dev/null || echo 0)
        echo "  $datestr: $events events, $anomalies anomalies"
    done || echo "  No logs recorded"
}

# ═══════════════════════════════════════
# Running Modes
# ═══════════════════════════════════════

cleanup_handler() {
    echo ""
    echo -e "${YELLOW}Stopping tracker...${NC}"
    log_event "SESSION_END" "" "Tracker stopped by user"
    show_summary
    exit 0
}

run_interactive() {
    trap cleanup_handler SIGINT SIGTERM

    START_TIME=$(date +%s)

    echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
    echo -e "${BOLD}║   Claude Code Process Lifecycle Tracker              ║${NC}"
    echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo -e "Session: ${CYAN}$SESSION_ID${NC}"
    echo -e "Logs:    ${CYAN}$LOG_DIR${NC}"
    echo -e "Press ${YELLOW}Ctrl+C${NC} to stop and see summary"
    echo ""
    echo -e "${BOLD}Legend:${NC} ${GREEN}▲ SPAWN${NC} | ${RED}▼ EXIT${NC} | ${YELLOW}◆ STATE${NC} | ${MAGENTA}◇ MEM${NC} | ${RED}☠ ORPHAN${NC}"
    echo ""

    log_event "SESSION_START" "" "Tracker started, poll interval: ${POLL_INTERVAL}s"

    # Initial scan
    echo -e "${BOLD}Initial scan...${NC}"
    scan_processes
    echo ""

    # Main loop
    while true; do
        sleep "$POLL_INTERVAL"
        scan_processes
        print_status_bar
    done
}

run_daemon() {
    POLL_INTERVAL=60
    START_TIME=$(date +%s)
    echo "Starting daemon mode (60s intervals)..."
    echo "Logs: $LOG_DIR"

    log_event "SESSION_START" "" "Daemon started, poll interval: ${POLL_INTERVAL}s"

    while true; do
        scan_processes >/dev/null 2>&1
        sleep "$POLL_INTERVAL"
    done
}

# ═══════════════════════════════════════
# Continuous Mode (replaces benchmark for PM2)
# ═══════════════════════════════════════

run_continuous() {
    POLL_INTERVAL=10
    START_TIME=$(date +%s)
    LAST_REPORT_TIME=$START_TIME
    LAST_CLEANUP_EPOCH=$START_TIME
    LAST_ORPHAN_CHECK=$START_TIME
    LAST_SNAPSHOT_TIME=0   # Take first snapshot immediately

    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Claude Code Process Monitor (continuous)           ║"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "Session:  $SESSION_ID"
    echo "Mode:     Continuous (no duration limit)"
    echo "Interval: ${POLL_INTERVAL}s"
    echo "Reports:  Every $((REPORT_INTERVAL / 3600))h"
    echo "Logs:     $LOG_DIR"
    echo ""
    echo "Features:"
    echo "  - Date-based log rotation (daily)"
    echo "  - Auto-cleanup old logs (>7 days)"
    echo "  - Auto-cleanup orphaned processes (every 5min)"
    echo "  - Advanced anomaly detection"
    echo ""

    # Restore event count from current day's log to avoid duplicate event_num on PM2 restart
    EVENT_COUNT=0
    if [ -f "$EVENT_LOG" ]; then
        local last_num=$(tail -1 "$EVENT_LOG" 2>/dev/null | jq -r '.event_num // 0' 2>/dev/null || echo 0)
        [ -n "$last_num" ] && [ "$last_num" -gt 0 ] 2>/dev/null && EVENT_COUNT=$last_num
    fi
    ANOMALY_COUNT=0
    CONTINUOUS_RUNNING=1

    continuous_shutdown_handler() {
        CONTINUOUS_RUNNING=0
        echo ""
        echo "Shutting down continuous monitor..."
        log_event "SESSION_END" "" "Continuous monitor stopped after $(format_duration $(($(date +%s) - START_TIME)))"
        generate_periodic_report
        echo ""
        echo "═══════════════════════════════════════════════════════"
        echo "Continuous monitor stopped"
        echo "Uptime:    $(format_duration $(($(date +%s) - START_TIME)))"
        echo "Events:    $EVENT_COUNT"
        echo "Anomalies: $ANOMALY_COUNT"
        echo "═══════════════════════════════════════════════════════"
        exit 0
    }
    trap continuous_shutdown_handler SIGINT SIGTERM SIGHUP

    # SIGUSR1 = force immediate periodic report (with reentrance guard)
    REPORT_IN_PROGRESS=0
    trap 'if [ "$REPORT_IN_PROGRESS" -eq 0 ]; then REPORT_IN_PROGRESS=1; echo "SIGUSR1: Forcing periodic report..."; generate_periodic_report; LAST_REPORT_TIME=$(date +%s); REPORT_IN_PROGRESS=0; else echo "SIGUSR1: Report already in progress, skipping"; fi' SIGUSR1

    log_event "SESSION_START" "" "Continuous monitor started, poll interval: ${POLL_INTERVAL}s"

    # Initial scan
    scan_processes >/dev/null 2>&1

    local last_progress=0

    while [ "$CONTINUOUS_RUNNING" -eq 1 ]; do
        # Interruptible sleep
        sleep "$POLL_INTERVAL" &
        wait $! 2>/dev/null || true

        [ "$CONTINUOUS_RUNNING" -eq 0 ] && break

        # Rotate logs if day changed
        rotate_logs_if_needed

        # Main scan
        scan_processes >/dev/null 2>&1

        # Periodic tasks
        cleanup_old_logs
        auto_cleanup_orphans
        take_snapshot

        # Generate periodic report every REPORT_INTERVAL
        local now=$(date +%s)
        if [ $((now - LAST_REPORT_TIME)) -ge $REPORT_INTERVAL ]; then
            LAST_REPORT_TIME=$now
            generate_periodic_report
        fi

        # Log progress every 5 minutes
        local elapsed=$((now - START_TIME))
        if [ $((elapsed - last_progress)) -ge 300 ]; then
            last_progress=$elapsed
            log_event "PROGRESS" "" "Uptime: $(format_duration $elapsed), Events: $EVENT_COUNT, Anomalies: $ANOMALY_COUNT"
        fi
    done
}

# ═══════════════════════════════════════
# Benchmark Mode (Legacy)
# ═══════════════════════════════════════

# Parse duration string like "4h", "30m", "2h30m", "30s" to seconds
parse_duration() {
    local input="$1"
    local seconds=0
    local matched=0

    # Reject empty or negative
    if [[ -z "$input" || "$input" =~ ^- ]]; then
        echo "0"
        return 1
    fi

    # Extract hours
    if [[ "$input" =~ ([0-9]+)h ]]; then
        seconds=$((seconds + ${BASH_REMATCH[1]} * 3600))
        matched=1
    fi

    # Extract minutes
    if [[ "$input" =~ ([0-9]+)m ]]; then
        seconds=$((seconds + ${BASH_REMATCH[1]} * 60))
        matched=1
    fi

    # Extract seconds
    if [[ "$input" =~ ([0-9]+)s ]]; then
        seconds=$((seconds + ${BASH_REMATCH[1]}))
        matched=1
    fi

    # If just a number, treat as hours (for convenience: "4" = "4h")
    if [[ "$input" =~ ^[0-9]+$ ]]; then
        seconds=$((input * 3600))
        matched=1
    fi

    # Validate we parsed something
    if [ "$matched" -eq 0 ]; then
        echo "0"
        return 1
    fi

    echo "$seconds"
}

format_duration() {
    local seconds=$1
    local hours=$((seconds / 3600))
    local minutes=$(((seconds % 3600) / 60))
    local secs=$((seconds % 60))
    printf '%02d:%02d:%02d' $hours $minutes $secs
}

# ═══════════════════════════════════════
# Telegram Integration
# ═══════════════════════════════════════

send_telegram() {
    # Send a message to the configured Telegram group
    # $1 = text (HTML parse_mode for reliable formatting)
    [ "$TELEGRAM_ENABLED" != "1" ] && return 0
    [ -z "$TELEGRAM_BOT_TOKEN" ] || [ -z "$TELEGRAM_CHAT_ID" ] && return 0

    local text="$1"
    local max_len=4096

    # Telegram has a 4096 char limit; truncate if needed
    if [ ${#text} -gt $max_len ]; then
        text="${text:0:$((max_len - 30))}

... (truncated)"
    fi

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg chat_id "$TELEGRAM_CHAT_ID" --arg text "$text" \
            '{chat_id: $chat_id, text: $text, parse_mode: "HTML", disable_web_page_preview: true}')" \
        >/dev/null 2>&1 &

    # Fire and forget (don't block the monitor loop)
    disown 2>/dev/null
}

generate_ai_narrative() {
    # Generate AI-powered narrative analysis with persistent history
    # Runs in background, sends result to Telegram as follow-up message
    # Maintains narrative_history.jsonl for story continuity across reports
    [ "$AI_NARRATIVE_ENABLED" != "1" ] && return 0
    [ "$TELEGRAM_ENABLED" != "1" ] && return 0
    [ -z "${START_TIME:-}" ] && return 0
    command -v openclaw &>/dev/null || return 0
    command -v jq &>/dev/null || return 0

    local narrative_history="$LOG_DIR/narrative_history.jsonl"
    local lockfile="$LOG_DIR/.narrative.lock"

    (
        # Acquire exclusive lock to prevent concurrent narrative generation
        exec 200>"$lockfile"
        if ! flock -xn 200; then
            echo "AI narrative: another instance running, skipping" >&2
            return 0
        fi

        local now_ts=$(date '+%Y-%m-%d %H:%M:%S')
        local duration=$(($(date +%s) - ${START_TIME:-$(date +%s)}))
        local uptime_str=$(format_duration $duration 2>/dev/null || echo "${duration}s")
        local report_number=1
        [ -f "$narrative_history" ] && report_number=$(($(wc -l < "$narrative_history") + 1))

        # Snapshot of event log for consistent reads (avoid jq -s on growing file)
        local event_snapshot="/tmp/tracker_ai_events_$$.jsonl"
        tail -10000 "$EVENT_LOG" > "$event_snapshot" 2>/dev/null
        trap 'rm -f "$event_snapshot"' EXIT

        # ─── Standardized Metrics (consistent across all reports) ───

        # M1: System memory
        local sys_mem
        sys_mem=$(get_system_mem 2>/dev/null || echo "0:0:0")
        local mem_available=$(echo "$sys_mem" | cut -d: -f1)
        local mem_total=$(echo "$sys_mem" | cut -d: -f2)
        local swap_used=$(echo "$sys_mem" | cut -d: -f3)
        local mem_used=$((${mem_total:-0} - ${mem_available:-0}))
        local mem_pct=0
        [ "${mem_total:-0}" -gt 0 ] && mem_pct=$((mem_used * 100 / mem_total))

        # M2: Process counts by type
        local proc_counts
        proc_counts=$(jq -r 'select(.event=="SNAPSHOT") | .procs[]?.type' "$event_snapshot" 2>/dev/null | \
            tail -50 | sort | uniq -c | sort -rn | awk '{printf "%s:%s ", $2, $1}')

        # M3: Total claude process memory (from latest snapshot)
        local total_claude_mem
        total_claude_mem=$(jq -s '[.[] | select(.event=="SNAPSHOT")] | if length == 0 then 0 else last.total_mem_mb // 0 end' "$event_snapshot" 2>/dev/null)
        total_claude_mem=${total_claude_mem:-0}

        # M4: Memory trend (first snapshot vs last snapshot)
        local mem_trend_data
        mem_trend_data=$(jq -s '
            [.[] | select(.event=="SNAPSHOT")] |
            if length < 2 then {snapshots: 0, delta: 0} else
            {
                snapshots: length,
                first_ts: first.ts,
                last_ts: last.ts,
                first_mem: first.total_mem_mb,
                last_mem: last.total_mem_mb,
                delta: (last.total_mem_mb - first.total_mem_mb),
                first_procs: (first.procs | length),
                last_procs: (last.procs | length)
            } end
        ' "$event_snapshot" 2>/dev/null)

        # M5: Process lifecycle counters (strip whitespace from wc -l)
        local spawns_count exits_count orphan_count cleanup_count
        spawns_count=$(jq -r 'select(.event=="SPAWN")' "$event_snapshot" 2>/dev/null | wc -l | tr -d ' ')
        exits_count=$(jq -r 'select(.event=="EXIT")' "$event_snapshot" 2>/dev/null | wc -l | tr -d ' ')
        orphan_count=$(jq -r 'select(.event=="ORPHAN")' "$event_snapshot" 2>/dev/null | wc -l | tr -d ' ')
        cleanup_count=$(jq -r 'select(.event=="AUTO_CLEANUP")' "$event_snapshot" 2>/dev/null | wc -l | tr -d ' ')
        spawns_count=${spawns_count:-0}; exits_count=${exits_count:-0}
        orphan_count=${orphan_count:-0}; cleanup_count=${cleanup_count:-0}

        # M6: Top 5 memory growers (PIDs with biggest delta across snapshots)
        local top_growers
        top_growers=$(jq -s '
            [.[] | select(.event=="SNAPSHOT")] |
            if length < 2 then [] else
            (first.procs // []) as $first |
            (last.procs // []) as $last |
            [$last[] as $l | ($first[] | select(.pid == $l.pid)) as $f |
             {pid: $l.pid, type: $l.type, from_mb: $f.mem_mb, to_mb: $l.mem_mb, delta: ($l.mem_mb - $f.mem_mb),
              fd: $l.fd, threads: $l.threads, age_min: (($l.age // 0) / 60 | floor)}] |
            sort_by(-.delta) | limit(5; .[])
            end
        ' "$event_snapshot" 2>/dev/null)

        # M7: Anomaly details
        local anomaly_details
        anomaly_details=$(jq -r 'select(.event | startswith("ANOMALY")) | "\(.ts) \(.event): \(.details)"' \
            "$event_snapshot" 2>/dev/null | tail -10)

        # M8: Significant MEM_CHANGE events
        local mem_changes
        mem_changes=$(jq -r 'select(.event=="MEM_CHANGE") | "\(.ts) \(.proc_type) pid:\(.pid) \(.details)"' \
            "$event_snapshot" 2>/dev/null | tail -10)

        # M9: Process spawns/exits timeline (last 15)
        local lifecycle_timeline
        lifecycle_timeline=$(jq -r 'select(.event=="SPAWN" or .event=="EXIT") | "\(.ts) \(.event) \(.proc_type // "") pid:\(.pid // "")"' \
            "$event_snapshot" 2>/dev/null | tail -15)

        # M10: Orphan and auto-cleanup details
        local orphan_details cleanup_details
        orphan_details=$(jq -r 'select(.event=="ORPHAN") | "\(.ts) pid:\(.pid) \(.details)"' "$event_snapshot" 2>/dev/null)
        cleanup_details=$(jq -r 'select(.event=="AUTO_CLEANUP") | "\(.ts) pid:\(.pid) \(.details)"' "$event_snapshot" 2>/dev/null)

        # M11: Event type distribution
        local event_distribution
        event_distribution=$(jq -r '.event' "$event_snapshot" 2>/dev/null | sort | uniq -c | sort -rn | head -12)

        # M12: FD and thread peaks (from latest snapshot)
        local fd_thread_peaks
        fd_thread_peaks=$(jq -s '
            [.[] | select(.event=="SNAPSHOT")] |
            if length == 0 then "N/A" else
            last.procs // [] |
            sort_by(-.fd) | limit(3; .[]) |
            "\(.type):\(.pid) fd:\(.fd) threads:\(.threads) mem:\(.mem_mb)MB"
            end
        ' "$event_snapshot" 2>/dev/null)

        # ─── Build standardized metrics block (always same format) ───
        local metrics_block="METRICAS PADRONIZADAS (Report #${report_number})
Timestamp: ${now_ts}
Monitor uptime: ${uptime_str}
---
M1-RAM: ${mem_used}/${mem_total}MB (${mem_pct}%) | Swap: ${swap_used}MB
M2-Processos: ${proc_counts:-nenhum}
M3-Memoria Claude total: ${total_claude_mem}MB
M4-Trend: $(echo "$mem_trend_data" | jq -r 'if .snapshots < 2 then "dados insuficientes" else "delta \(.delta)MB em \(.snapshots) snapshots (\(.first_ts) → \(.last_ts))" end' 2>/dev/null)
M5-Lifecycle: spawns=${spawns_count} exits=${exits_count} orphans=${orphan_count} cleanups=${cleanup_count}
M6-Top growers:
$(echo "$top_growers" | jq -r 'if type=="array" then .[] | "  \(.type):\(.pid) \(.from_mb)→\(.to_mb)MB (delta:\(.delta)) fd:\(.fd) t:\(.threads) age:\(.age_min)min" else . end' 2>/dev/null || echo "  N/A")
M7-Anomalias (${ANOMALY_COUNT:-0}):
${anomaly_details:-  Nenhuma}
M8-MEM_CHANGE:
${mem_changes:-  Nenhum}
M9-Lifecycle timeline:
${lifecycle_timeline:-  Vazio}
M10-Orfaos: ${orphan_details:-Nenhum} | Cleanups: ${cleanup_details:-Nenhum}
M11-Distribuicao eventos (${EVENT_COUNT:-0} total):
${event_distribution}
M12-FD/Thread peaks:
${fd_thread_peaks:-  N/A}"

        # ─── Load previous narrative for continuity ───
        local previous_context=""
        if [ -f "$narrative_history" ] && [ -s "$narrative_history" ]; then
            # Get last 3 narratives for context (summary only, not full text)
            previous_context=$(tail -3 "$narrative_history" | jq -r '
                "Report #\(.report_number) (\(.timestamp)):
Mem: \(.metrics.mem_used)/\(.metrics.mem_total)MB (\(.metrics.mem_pct)%) Swap: \(.metrics.swap_used)MB
Processos Claude: \(.metrics.total_claude_mem)MB | Spawns: \(.metrics.spawns) Exits: \(.metrics.exits) Orphans: \(.metrics.orphans) Cleanups: \(.metrics.cleanups)
Anomalias: \(.metrics.anomaly_count)
Resumo da narrativa: \(.narrative_summary)"
            ' 2>/dev/null)
        fi

        # ─── Build prompt ───
        local continuity_section=""
        if [ -n "$previous_context" ]; then
            continuity_section="
HISTORICO DE REPORTS ANTERIORES (use para dar continuidade a historia):
${previous_context}
---
IMPORTANTE: Continue a narrativa de onde parou. Faca referencias ao que aconteceu antes. Se padrao melhorou ou piorou, mencione. A historia deve ser cumulativa e consistente."
        fi

        local prompt="Voce é um analista de infraestrutura que monitora processos Claude Code. Seus reports sao praticos e diretos, com no maximo um toque sutil de estilo literario (uma metafora, uma frase com personalidade) — mas o foco é ser util e acionavel.

Report #${report_number}.
${continuity_section}

DADOS:
${metrics_block}

FORMATO DO REPORT:

<b>Status: [emoji] [classificacao]</b>
Uma frase direta sobre o estado geral.

<b>Destaques</b>
- Bullet points curtos sobre o que importa: mudancas de memoria, processos problematicos, anomalias
- Compare com reports anteriores se houver (ex: 'mem subiu 200MB desde #2')
- Mencione PIDs e tipos especificos quando relevante

<b>Riscos</b>
- So liste se houver algo concreto. Se tudo estiver ok, diga 'Nenhum risco identificado.'

<b>Acao</b>
- Recomendacoes concretas se necessario. Se nao, 'Nenhuma acao necessaria.'

No final, em linha separada:
RESUMO: uma frase tecnica resumindo o periodo (para contexto em reports futuros)

Regras:
- Portugues
- Max 2000 chars
- Emojis ok, mas com moderacao
- HTML tags para Telegram: <b> <code> (NAO use markdown)
- NAO repita dados crus — interprete e destaque o que importa
- Classificacao: 🟢 Saudavel | 🟡 Atencao | 🔴 Critico"

        # ─── Invoke AI agent ───
        local rc=0
        local response
        response=$(openclaw agent \
            --agent "$AI_NARRATIVE_AGENT" \
            --channel last \
            -m "$prompt" \
            --json \
            --timeout "$AI_NARRATIVE_TIMEOUT" 2>/dev/null) || rc=$?

        if [ "$rc" -ne 0 ] || [ -z "$response" ]; then
            echo "{\"ts\":\"$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\",\"event\":\"AI_NARRATIVE_FAIL\",\"details\":\"OpenClaw agent invocation failed (rc=$rc)\"}" >> "$EVENT_LOG"
            return 1
        fi

        # Extract the text
        local narrative
        narrative=$(echo "$response" | jq -r '.result.payloads[0].text // empty' 2>/dev/null)

        if [ -z "$narrative" ]; then
            echo "{\"ts\":\"$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\",\"event\":\"AI_NARRATIVE_FAIL\",\"details\":\"Empty response from AI agent\"}" >> "$EVENT_LOG"
            return 1
        fi

        # Extract the one-line summary (strip HTML tags, then look for RESUMO:)
        local narrative_summary
        narrative_summary=$(echo "$narrative" | sed 's/<[^>]*>//g' | grep -i "^RESUMO:" | head -1 | sed 's/^[Rr][Ee][Ss][Uu][Mm][Oo]:\s*//')
        [ -z "$narrative_summary" ] && narrative_summary=$(echo "$narrative" | sed 's/<[^>]*>//g' | tail -1 | head -c 200)

        # ─── Save to narrative history (compact single-line JSONL for wc -l counting) ───
        jq -cn \
            --arg ts "$now_ts" \
            --argjson rn "$report_number" \
            --argjson mu "${mem_used:-0}" \
            --argjson mt "${mem_total:-0}" \
            --argjson mp "${mem_pct:-0}" \
            --argjson su "${swap_used:-0}" \
            --argjson tcm "${total_claude_mem:-0}" \
            --argjson sc "${spawns_count:-0}" \
            --argjson ec "${exits_count:-0}" \
            --argjson oc "${orphan_count:-0}" \
            --argjson cc "${cleanup_count:-0}" \
            --argjson ac "${ANOMALY_COUNT:-0}" \
            --argjson te "${EVENT_COUNT:-0}" \
            --arg ns "$narrative_summary" \
            '{
                timestamp: $ts,
                report_number: $rn,
                metrics: {
                    mem_used: $mu, mem_total: $mt, mem_pct: $mp,
                    swap_used: $su, total_claude_mem: $tcm,
                    spawns: $sc, exits: $ec, orphans: $oc, cleanups: $cc,
                    anomaly_count: $ac, total_events: $te
                },
                narrative_summary: $ns
            }' >> "$narrative_history"

        # ─── Send to Telegram ───
        local header="🤖 <b>AI Cronista - Report #${report_number}</b>
━━━━━━━━━━━━━━━━━

"
        # Remove the RESUMO line from the message (it's for internal use)
        local clean_narrative
        clean_narrative=$(echo "$narrative" | sed 's/<[^>]*>//g' | grep -iv "^RESUMO:" | head -c 3500)
        # Re-read the original (with HTML) but skip RESUMO lines
        clean_narrative=$(echo "$narrative" | grep -iv "RESUMO:")

        send_telegram "${header}${clean_narrative}"
        echo "{\"ts\":\"$(date -u '+%Y-%m-%dT%H:%M:%S.%3NZ')\",\"event\":\"AI_NARRATIVE_SENT\",\"details\":\"Report #${report_number} sent (${#narrative} chars, summary: ${narrative_summary:0:80})\"}" >> "$EVENT_LOG"

    ) &
    disown 2>/dev/null
}

format_telegram_report() {
    # Generate a compact Telegram-friendly report
    local duration=$(($(date +%s) - START_TIME))
    local uptime_str=$(format_duration $duration)

    # Current system state
    local sys_mem
    sys_mem=$(get_system_mem)
    local mem_available=$(echo "$sys_mem" | cut -d: -f1)
    local mem_total=$(echo "$sys_mem" | cut -d: -f2)
    local swap_used=$(echo "$sys_mem" | cut -d: -f3)
    local mem_used=$((mem_total - mem_available))
    local mem_pct=0
    [ "$mem_total" -gt 0 ] && mem_pct=$((mem_used * 100 / mem_total))

    # Build RAM bar (20 chars)
    local bar_len=20
    local filled=$((mem_pct * bar_len / 100))
    local empty=$((bar_len - filled))
    local ram_bar=""
    for ((i=0; i<filled; i++)); do ram_bar+="█"; done
    for ((i=0; i<empty; i++)); do ram_bar+="░"; done

    # Swap bar
    local swap_total
    swap_total=$(free -m 2>/dev/null | grep Swap | awk '{print $2}')
    swap_total=${swap_total:-0}
    local swap_pct=0
    [ "$swap_total" -gt 0 ] && swap_pct=$((swap_used * 100 / swap_total))
    local swap_filled=$((swap_pct * bar_len / 100))
    local swap_empty=$((bar_len - swap_filled))
    local swap_bar=""
    for ((i=0; i<swap_filled; i++)); do swap_bar+="█"; done
    for ((i=0; i<swap_empty; i++)); do swap_bar+="░"; done

    # Process counts
    local claude_count=0 mcp_count=0
    claude_count=$(ps aux --no-headers 2>/dev/null | grep -c "[c]laude" || echo 0)
    mcp_count=$(ps aux --no-headers 2>/dev/null | grep -c "mcp-server" || echo 0)

    # Peak values from event log
    local peak_mem="N/A" peak_swap="N/A" min_avail="N/A"
    if [ -f "$EVENT_LOG" ] && command -v jq &>/dev/null; then
        local peaks
        peaks=$(jq -s '{
            peak_mem: ([.[].total_mem_mb // 0] | max),
            peak_swap: ([.[].system.swap_used_mb // 0] | max),
            min_avail: ([.[].system.available_mb // 99999] | min)
        }' "$EVENT_LOG" 2>/dev/null || echo '{}')
        peak_mem=$(echo "$peaks" | jq -r '.peak_mem // "N/A"' 2>/dev/null)
        peak_swap=$(echo "$peaks" | jq -r '.peak_swap // "N/A"' 2>/dev/null)
        min_avail=$(echo "$peaks" | jq -r '.min_avail // "N/A"' 2>/dev/null)
    fi

    # Event summary
    local event_summary=""
    if [ -f "$EVENT_LOG" ]; then
        event_summary=$(jq -r '.event' "$EVENT_LOG" 2>/dev/null | sort | uniq -c | sort -rn | head -8 | \
            awk '{printf "  %s × %s\n", $1, $2}')
    fi

    # Anomaly flag
    local anomaly_emoji=""
    if [ "$ANOMALY_COUNT" -gt 0 ]; then
        anomaly_emoji="⚠️ "
    fi

    # Trend analysis from snapshots (compare oldest vs newest)
    local trend_section=""
    if [ -f "$EVENT_LOG" ] && command -v jq &>/dev/null; then
        # Get first and last snapshot total_mem for delta
        local first_snap_mem last_snap_mem mem_trend_str=""
        first_snap_mem=$(jq -r 'select(.event=="SNAPSHOT") | .total_mem_mb' "$EVENT_LOG" 2>/dev/null | head -1)
        last_snap_mem=$(jq -r 'select(.event=="SNAPSHOT") | .total_mem_mb' "$EVENT_LOG" 2>/dev/null | tail -1)
        if [ -n "$first_snap_mem" ] && [ -n "$last_snap_mem" ] && [ "$first_snap_mem" != "null" ] && [ "$last_snap_mem" != "null" ]; then
            local mem_delta=$((last_snap_mem - first_snap_mem))
            local sign=""
            [ "$mem_delta" -gt 0 ] && sign="+"
            mem_trend_str="  Mem trend: ${sign}${mem_delta}MB"
        fi

        # Top memory growers: PIDs that appear in both first and last snapshot with biggest growth
        local top_growers=""
        top_growers=$(jq -s '
            [.[] | select(.event=="SNAPSHOT") | {ts: .ts, procs: .procs}] |
            if length < 2 then empty else
            (first.procs // []) as $first |
            (last.procs // []) as $last |
            [$last[] as $l | ($first[] | select(.pid == $l.pid)) as $f |
             {pid: $l.pid, type: $l.type, from: $f.mem_mb, to: $l.mem_mb, delta: ($l.mem_mb - $f.mem_mb), fd: $l.fd, threads: $l.threads}] |
            sort_by(-.delta) | [limit(3; .[] | select(.delta > 20))] |
            if length == 0 then empty else
            .[] | "  \(.type):\(.pid) \(.from)→\(.to)MB (\(if .delta > 0 then "+" else "" end)\(.delta)) fd:\(.fd) t:\(.threads)"
            end end
        ' "$EVENT_LOG" 2>/dev/null)

        # Orphan/cleanup events count
        local orphan_events cleanup_events spawn_events exit_events
        orphan_events=$(jq -r 'select(.event=="ORPHAN") | .pid' "$EVENT_LOG" 2>/dev/null | wc -l)
        cleanup_events=$(jq -r 'select(.event=="AUTO_CLEANUP") | .pid' "$EVENT_LOG" 2>/dev/null | wc -l)
        spawn_events=$(jq -r 'select(.event=="SPAWN") | .pid' "$EVENT_LOG" 2>/dev/null | wc -l)
        exit_events=$(jq -r 'select(.event=="EXIT") | .pid' "$EVENT_LOG" 2>/dev/null | wc -l)

        # Build lifecycle line
        local lifecycle="  Born: ${spawn_events} | Died: ${exit_events} | Orphaned: ${orphan_events} | Cleaned: ${cleanup_events}"

        trend_section="
<b>Trends</b>
${mem_trend_str}
${lifecycle}"

        if [ -n "$top_growers" ]; then
            trend_section+="

<b>Top growers</b>
<pre>${top_growers}</pre>"
        fi
    fi

    # Build message (HTML format for Telegram)
    cat << EOF
📊 <b>Claude Tracker Report</b>
━━━━━━━━━━━━━━━━━

⏱ Uptime: <code>${uptime_str}</code>
📅 Log: <code>${CURRENT_LOG_DATE}</code>

<b>RAM</b> ${mem_pct}%
<code>${ram_bar}</code> ${mem_used}/${mem_total}MB

<b>Swap</b> ${swap_pct}%
<code>${swap_bar}</code> ${swap_used}/${swap_total}MB

<b>Processes</b>
  Claude: ${claude_count} | MCP: ${mcp_count}

<b>Peaks (today)</b>
  Mem: ${peak_mem}MB | Swap: ${peak_swap}MB
  Min avail: ${min_avail}MB
${trend_section}

<b>Events</b>: ${EVENT_COUNT} total
<pre>${event_summary}</pre>
${anomaly_emoji}<b>Anomalies</b>: ${ANOMALY_COUNT}
EOF
}

# ═══════════════════════════════════════
# Report Generation
# ═══════════════════════════════════════

generate_periodic_report() {
    local report_file="$LOG_DIR/report_$(date +%Y%m%d_%H%M).md"
    local duration=$(($(date +%s) - START_TIME))

    cat > "$report_file" << EOF
# Claude Code Process Monitor - Periodic Report

## Report Metadata

| Field | Value |
|-------|-------|
| Generated | $(date '+%Y-%m-%d %H:%M:%S %Z') |
| Uptime | $(format_duration $duration) |
| Log Date | $CURRENT_LOG_DATE |
| Total Events | $EVENT_COUNT |
| Anomalies | $ANOMALY_COUNT |

## Event Summary

\`\`\`
$(jq -r '.event' "$EVENT_LOG" 2>/dev/null | sort | uniq -c | sort -rn || echo "No events")
\`\`\`

## Peak Values

\`\`\`json
$(jq -s '{
  peak_total_mem_mb: ([.[].total_mem_mb] | max),
  peak_swap_mb: ([.[].system.swap_used_mb] | max),
  min_available_mb: ([.[].system.available_mb] | min)
}' "$EVENT_LOG" 2>/dev/null || echo '{"error": "Unable to calculate"}')
\`\`\`

## Anomalies

\`\`\`
$(jq -r 'select(.event | startswith("ANOMALY")) | "\(.ts) | \(.event) | \(.details)"' "$EVENT_LOG" 2>/dev/null | tail -50 || echo "None")
\`\`\`

## Auto-Cleanup Events

\`\`\`
$(jq -r 'select(.event == "AUTO_CLEANUP") | "\(.ts) | PID \(.pid) | \(.details)"' "$EVENT_LOG" 2>/dev/null | tail -20 || echo "None")
\`\`\`

## Recent Timeline (last 30 entries)

\`\`\`
$(tail -30 "$TIMELINE_LOG" 2>/dev/null || echo "No timeline data")
\`\`\`

---

_Periodic report from continuous monitor, generated at $(date '+%Y-%m-%d %H:%M:%S %Z')_
EOF

    log_event "REPORT_GENERATED" "" "Periodic report: $(basename "$report_file")"

    # Send to Telegram
    local telegram_msg
    telegram_msg=$(format_telegram_report)
    send_telegram "$telegram_msg"

    # Generate AI narrative analysis (runs in background)
    generate_ai_narrative
}

generate_benchmark_report() {
    # Guard: ensure START_TIME is set
    if [ -z "${START_TIME:-}" ]; then
        echo "ERROR: START_TIME not set" >&2
        return 1
    fi

    # Check jq availability
    if ! command -v jq &>/dev/null; then
        echo "WARNING: jq not found, report will have limited statistics" >&2
    fi

    local report_file="$LOG_DIR/benchmark_${SESSION_ID}_report.md"
    local duration=$(($(date +%s) - START_TIME))

    echo "Generating benchmark report..."

    cat > "$report_file" << 'REPORTHEADER'
# Claude Code Process Benchmark Report

REPORTHEADER

    cat >> "$report_file" << EOF
## Session Metadata

| Field | Value |
|-------|-------|
| Session ID | \`$SESSION_ID\` |
| Start Time | $(date -d "@$START_TIME" '+%Y-%m-%d %H:%M:%S %Z') |
| End Time | $(date '+%Y-%m-%d %H:%M:%S %Z') |
| Duration | $(format_duration $duration) ($duration seconds) |
| Poll Interval | ${POLL_INTERVAL}s |
| Total Events | $EVENT_COUNT |
| Anomalies | $ANOMALY_COUNT |

## Raw Data Files

- **Event Log (JSONL):** \`$EVENT_LOG\`
- **Timeline Log:** \`$TIMELINE_LOG\`
- **This Report:** \`$report_file\`

---

## Event Summary

\`\`\`
$(jq -r '.event' "$EVENT_LOG" 2>/dev/null | sort | uniq -c | sort -rn || echo "No events or jq unavailable")
\`\`\`

## Peak Values

\`\`\`json
$(jq -s '
{
  peak_total_mem_mb: ([.[].total_mem_mb] | max),
  peak_swap_mb: ([.[].system.swap_used_mb] | max),
  min_available_mb: ([.[].system.available_mb] | min),
  peak_claude_count: ([.[].counts | capture("claude:(?<n>[0-9]+)") | .n | tonumber] | max),
  peak_mcp_count: ([.[].counts | capture("mcp:(?<n>[0-9]+)") | .n | tonumber] | max)
}
' "$EVENT_LOG" 2>/dev/null || echo '{"error": "Unable to calculate peaks"}')
\`\`\`

---

## All Anomalies

EOF

    if grep -q '"event":"ANOMALY' "$EVENT_LOG" 2>/dev/null; then
        echo '```' >> "$report_file"
        jq -r 'select(.event | startswith("ANOMALY")) | "\(.ts) | \(.event) | \(.details)"' "$EVENT_LOG" >> "$report_file"
        echo '```' >> "$report_file"
    else
        echo "_No anomalies detected during this benchmark._" >> "$report_file"
    fi

    cat >> "$report_file" << EOF

---

## Process Spawns (All)

\`\`\`
$(jq -r 'select(.event == "SPAWN") | "\(.ts) | PID \(.pid) | \(.details)"' "$EVENT_LOG" 2>/dev/null | head -200 || echo "No spawns")
\`\`\`

EOF

    local spawn_count=$(grep -c '"event":"SPAWN"' "$EVENT_LOG" 2>/dev/null || echo 0)
    if [ "$spawn_count" -gt 200 ]; then
        echo "_... truncated ($spawn_count total spawns, see raw JSONL for complete list)_" >> "$report_file"
    fi

    cat >> "$report_file" << EOF

---

## Process Exits (All)

\`\`\`
$(jq -r 'select(.event == "EXIT") | "\(.ts) | PID \(.pid) | \(.details)"' "$EVENT_LOG" 2>/dev/null | head -200 || echo "No exits")
\`\`\`

EOF

    local exit_count=$(grep -c '"event":"EXIT"' "$EVENT_LOG" 2>/dev/null || echo 0)
    if [ "$exit_count" -gt 200 ]; then
        echo "_... truncated ($exit_count total exits, see raw JSONL for complete list)_" >> "$report_file"
    fi

    cat >> "$report_file" << EOF

---

## Orphan Events

EOF

    if grep -q '"event":"ORPHAN"' "$EVENT_LOG" 2>/dev/null; then
        echo '```' >> "$report_file"
        jq -r 'select(.event == "ORPHAN") | "\(.ts) | PID \(.pid) | \(.details)"' "$EVENT_LOG" >> "$report_file"
        echo '```' >> "$report_file"
    else
        echo "_No orphan events detected._" >> "$report_file"
    fi

    cat >> "$report_file" << EOF

---

## Memory Changes (>100MB)

EOF

    if grep -q '"event":"MEM_CHANGE"' "$EVENT_LOG" 2>/dev/null; then
        echo '```' >> "$report_file"
        jq -r 'select(.event == "MEM_CHANGE") | "\(.ts) | PID \(.pid) | \(.details)"' "$EVENT_LOG" 2>/dev/null | head -100 >> "$report_file"
        echo '```' >> "$report_file"
    else
        echo "_No significant memory changes detected._" >> "$report_file"
    fi

    cat >> "$report_file" << EOF

---

## State Changes

EOF

    if grep -q '"event":"STATE_CHANGE"' "$EVENT_LOG" 2>/dev/null; then
        echo '```' >> "$report_file"
        jq -r 'select(.event == "STATE_CHANGE") | "\(.ts) | PID \(.pid) | \(.details)"' "$EVENT_LOG" 2>/dev/null | head -100 >> "$report_file"
        echo '```' >> "$report_file"
    else
        echo "_No state changes detected._" >> "$report_file"
    fi

    cat >> "$report_file" << EOF

---

## Memory Trajectory (sampled every 10 events)

\`\`\`
$(jq -r 'select(.event_num % 10 == 0 or .event == "ANOMALY") | "\(.ts) | total:\(.total_mem_mb)MB | swap:\(.system.swap_used_mb)MB | avail:\(.system.available_mb)MB | \(.counts)"' "$EVENT_LOG" 2>/dev/null | head -150 || echo "No data")
\`\`\`

---

## Timeline (First 50 events)

\`\`\`
$(head -50 "$TIMELINE_LOG" 2>/dev/null || echo "No timeline data")
\`\`\`

---

## Timeline (Last 50 events)

\`\`\`
$(tail -50 "$TIMELINE_LOG" 2>/dev/null || echo "No timeline data")
\`\`\`

---

## Instructions for LLM Analysis

To analyze this data with an LLM, you can:

1. **Share this report** for a high-level overview
2. **Query the JSONL file** for detailed analysis:

\`\`\`bash
# Get all anomalies with context (5 events before each)
jq -r 'select(.event | startswith("ANOMALY")) | .event_num' $EVENT_LOG | while read n; do
  jq "select(.event_num >= \$((\$n-5)) and .event_num <= \$n)" $EVENT_LOG
done

# Get memory over time
jq -r '[.ts, .total_mem_mb, .system.swap_used_mb] | @csv' $EVENT_LOG

# Get all events for a specific PID
jq 'select(.pid == "TARGET_PID")' $EVENT_LOG

# Find processes that lived less than 60 seconds
jq -r 'select(.event == "EXIT" and (.details | test("lived [0-5]?[0-9]s")))' $EVENT_LOG
\`\`\`

---

_Report generated at $(date '+%Y-%m-%d %H:%M:%S %Z')_
EOF

    echo ""
    echo "Benchmark report saved to: $report_file"
}

# ═══════════════════════════════════════
# Benchmark Mode Functions
# ═══════════════════════════════════════

run_benchmark() {
    local duration_str="${1:-4h}"
    local duration_secs=$(parse_duration "$duration_str")

    if [ "$duration_secs" -eq 0 ]; then
        echo "Invalid duration: $duration_str"
        echo "Examples: 4h, 30m, 2h30m, 1h30m, 30s"
        exit 1
    fi

    local poll_interval=10  # 10 seconds for benchmark mode
    POLL_INTERVAL=$poll_interval
    START_TIME=$(date +%s)
    local end_time=$((START_TIME + duration_secs))

    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Claude Code Process Benchmark                      ║"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "Session:  $SESSION_ID"
    echo "Duration: $(format_duration $duration_secs) ($duration_str)"
    echo "Interval: ${poll_interval}s"
    echo "End time: $(date -d "@$end_time" '+%Y-%m-%d %H:%M:%S')"
    echo ""
    echo "Logs:"
    echo "  Events:   $EVENT_LOG"
    echo "  Timeline: $TIMELINE_LOG"
    echo ""
    echo "Running in background..."
    echo ""

    # Detach and run in background
    (
        # Export variables for subshell
        export START_TIME POLL_INTERVAL SESSION_ID EVENT_LOG TIMELINE_LOG LOG_DIR
        export EVENT_COUNT=0 ANOMALY_COUNT=0

        # Flag for clean shutdown
        BENCHMARK_RUNNING=1

        # Trap for early termination - still generate report
        benchmark_abort_handler() {
            BENCHMARK_RUNNING=0
            log_event "SESSION_END" "" "Benchmark ABORTED by user after $(format_duration $(($(date +%s) - START_TIME)))"
            generate_benchmark_report
            echo ""
            echo "═══════════════════════════════════════════════════════"
            echo "Benchmark ABORTED (report still generated)"
            echo "Duration: $(format_duration $(($(date +%s) - START_TIME)))"
            echo "Events:   $EVENT_COUNT"
            echo "Anomalies: $ANOMALY_COUNT"
            echo ""
            echo "Report: $LOG_DIR/benchmark_${SESSION_ID}_report.md"
            echo "═══════════════════════════════════════════════════════"
            # Clean up PID file
            rm -f "$LOG_DIR/benchmark_${SESSION_ID}.pid"
            exit 0
        }
        trap benchmark_abort_handler SIGINT SIGTERM SIGHUP

        log_event "SESSION_START" "" "Benchmark started, duration: $duration_str ($duration_secs s), poll interval: ${POLL_INTERVAL}s"

        # Initial scan
        scan_processes >/dev/null 2>&1

        # Progress tracking
        local last_progress=0

        # Main loop until duration expires or aborted
        while [ "$BENCHMARK_RUNNING" -eq 1 ] && [ $(date +%s) -lt $end_time ]; do
            # Interruptible sleep
            sleep "$POLL_INTERVAL" &
            wait $! 2>/dev/null || true

            [ "$BENCHMARK_RUNNING" -eq 0 ] && break

            scan_processes >/dev/null 2>&1

            # Log progress every 5 minutes
            local elapsed=$(($(date +%s) - START_TIME))
            if [ $((elapsed - last_progress)) -ge 300 ]; then
                last_progress=$elapsed
                local remaining=$((end_time - $(date +%s)))
                log_event "PROGRESS" "" "Elapsed: $(format_duration $elapsed), Remaining: $(format_duration $remaining), Events: $EVENT_COUNT"
            fi
        done

        # Normal completion (if we get here, wasn't aborted)
        if [ "$BENCHMARK_RUNNING" -eq 1 ]; then
            log_event "SESSION_END" "" "Benchmark completed after $(format_duration $(($(date +%s) - START_TIME)))"

            # Generate the report
            generate_benchmark_report

            echo ""
            echo "═══════════════════════════════════════════════════════"
            echo "Benchmark complete!"
            echo "Duration: $(format_duration $(($(date +%s) - START_TIME)))"
            echo "Events:   $EVENT_COUNT"
            echo "Anomalies: $ANOMALY_COUNT"
            echo ""
            echo "Report: $LOG_DIR/benchmark_${SESSION_ID}_report.md"
            echo "═══════════════════════════════════════════════════════"
        fi

        # Clean up PID file
        rm -f "$LOG_DIR/benchmark_${SESSION_ID}.pid"

    ) >> "$LOG_DIR/benchmark_${SESSION_ID}_stdout.log" 2>&1 &

    local bg_pid=$!

    # Write PID file with error checking
    if ! echo "$bg_pid" > "$LOG_DIR/benchmark_${SESSION_ID}.pid"; then
        echo "ERROR: Failed to write PID file" >&2
        echo "Background process running as PID: $bg_pid"
    fi

    # Verify process actually started
    sleep 0.2
    if ! kill -0 "$bg_pid" 2>/dev/null; then
        echo "WARNING: Background process may have failed to start" >&2
        echo "Check: $LOG_DIR/benchmark_${SESSION_ID}_stdout.log"
        exit 1
    fi

    echo "Background PID: $bg_pid"
    echo "PID file: $LOG_DIR/benchmark_${SESSION_ID}.pid"
    echo ""
    echo "To stop early (report will still be generated):"
    echo "  kill $bg_pid"
    echo "  OR: kill \$(cat $LOG_DIR/benchmark_${SESSION_ID}.pid)"
    echo ""
    echo "To monitor:"
    echo "  tail -f $TIMELINE_LOG"
    echo "  tail -f $LOG_DIR/benchmark_${SESSION_ID}_stdout.log"
}

run_benchmark_fg() {
    local duration_str="${1:-4h}"
    local duration_secs=$(parse_duration "$duration_str")

    if [ "$duration_secs" -eq 0 ]; then
        echo "Invalid duration: $duration_str"
        echo "Examples: 4h, 30m, 2h30m, 1h30m, 30s"
        exit 1
    fi

    local poll_interval=10
    POLL_INTERVAL=$poll_interval
    START_TIME=$(date +%s)
    local end_time=$((START_TIME + duration_secs))

    echo "╔══════════════════════════════════════════════════════╗"
    echo "║   Claude Code Process Benchmark (foreground)         ║"
    echo "╚══════════════════════════════════════════════════════╝"
    echo ""
    echo "Session:  $SESSION_ID"
    echo "Duration: $(format_duration $duration_secs) ($duration_str)"
    echo "Interval: ${poll_interval}s"
    echo "End time: $(date -d "@$end_time" '+%Y-%m-%d %H:%M:%S')"
    echo ""
    echo "Logs:"
    echo "  Events:   $EVENT_LOG"
    echo "  Timeline: $TIMELINE_LOG"
    echo ""

    EVENT_COUNT=0
    ANOMALY_COUNT=0
    BENCHMARK_RUNNING=1

    benchmark_abort_handler() {
        BENCHMARK_RUNNING=0
        log_event "SESSION_END" "" "Benchmark ABORTED by user after $(format_duration $(($(date +%s) - START_TIME)))"
        generate_benchmark_report
        echo ""
        echo "═══════════════════════════════════════════════════════"
        echo "Benchmark ABORTED (report still generated)"
        echo "Duration: $(format_duration $(($(date +%s) - START_TIME)))"
        echo "Events:   $EVENT_COUNT"
        echo "Anomalies: $ANOMALY_COUNT"
        echo ""
        echo "Report: $LOG_DIR/benchmark_${SESSION_ID}_report.md"
        echo "═══════════════════════════════════════════════════════"
        exit 0
    }
    trap benchmark_abort_handler SIGINT SIGTERM SIGHUP

    log_event "SESSION_START" "" "Benchmark started (fg), duration: $duration_str ($duration_secs s), poll interval: ${POLL_INTERVAL}s"

    scan_processes >/dev/null 2>&1

    local last_progress=0

    while [ "$BENCHMARK_RUNNING" -eq 1 ] && [ $(date +%s) -lt $end_time ]; do
        sleep "$POLL_INTERVAL" &
        wait $! 2>/dev/null || true

        [ "$BENCHMARK_RUNNING" -eq 0 ] && break

        scan_processes >/dev/null 2>&1

        local elapsed=$(($(date +%s) - START_TIME))
        if [ $((elapsed - last_progress)) -ge 300 ]; then
            last_progress=$elapsed
            local remaining=$((end_time - $(date +%s)))
            log_event "PROGRESS" "" "Elapsed: $(format_duration $elapsed), Remaining: $(format_duration $remaining), Events: $EVENT_COUNT"
        fi
    done

    if [ "$BENCHMARK_RUNNING" -eq 1 ]; then
        log_event "SESSION_END" "" "Benchmark completed after $(format_duration $(($(date +%s) - START_TIME)))"
        generate_benchmark_report
        echo ""
        echo "═══════════════════════════════════════════════════════"
        echo "Benchmark complete!"
        echo "Duration: $(format_duration $(($(date +%s) - START_TIME)))"
        echo "Events:   $EVENT_COUNT"
        echo "Anomalies: $ANOMALY_COUNT"
        echo ""
        echo "Report: $LOG_DIR/benchmark_${SESSION_ID}_report.md"
        echo "═══════════════════════════════════════════════════════"
    fi
}

# ═══════════════════════════════════════
# Usage & Main
# ═══════════════════════════════════════

usage() {
    echo "Claude Code Process Lifecycle Tracker"
    echo ""
    echo "Usage: $0 [mode]"
    echo ""
    echo "Modes:"
    echo "  (none)           Interactive real-time tracking (2s poll, Ctrl+C to stop)"
    echo "  --status         Quick status snapshot of all processes"
    echo "  --cleanup        Find and kill orphaned processes"
    echo "  --daemon         Run as background daemon (60s poll)"
    echo "  --continuous     Continuous monitoring for PM2 (10s poll, auto-reports,"
    echo "                   log rotation, orphan cleanup, advanced anomaly detection)"
    echo "  --benchmark [T]  Timed benchmark run (default: 4h) with full report"
    echo "                   Examples: --benchmark 4h, --benchmark 30m, --benchmark 2h30m"
    echo "                   Runs in background, generates report at end or on abort"
    echo "  --benchmark-fg [T]  Same as --benchmark but runs in foreground (legacy)"
    echo ""
    echo "Log directory: $LOG_DIR"
}

# Main
case "${1:-}" in
    --status)
        show_status
        ;;
    --cleanup)
        cleanup_orphans
        ;;
    --daemon)
        run_daemon
        ;;
    --continuous)
        run_continuous
        ;;
    --benchmark)
        run_benchmark "${2:-4h}"
        ;;
    --benchmark-fg)
        run_benchmark_fg "${2:-4h}"
        ;;
    --help|-h)
        usage
        ;;
    "")
        run_interactive
        ;;
    *)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
esac
