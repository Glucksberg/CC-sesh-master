# UI Parallel Performance Playbook (React vs HTML)

Use this to compare the current Session Registry UI against the HTML/CSS version with the same scenarios and the same metrics.

## Goal

- Detect where each implementation is slow.
- Prioritize fixes by impact, not by framework preference.
- Run the same script and the same flow on both UIs.

## Probe Setup

1. Open Chrome on the target UI.
2. Open DevTools Console.
3. Paste the contents of `scripts/perf/ui-perf-probe.js`.
4. You should see: `[ui-perf-probe] ready`.

## Standard Scenarios

Run each scenario 5 times per UI (React and HTML).

### Scenario A - Sessions First Paint

1. Reload page.
2. Run: `window.__uiPerfProbe.start('sessions-first-paint')`
3. Wait until sessions list and right pane are visible.
4. Run: `window.__uiPerfProbe.stop()`

### Scenario B - Session Selection

1. Run: `window.__uiPerfProbe.start('session-selection')`
2. Click one session.
3. Switch tabs: `Events -> Stats -> Subagents -> Events`.
4. Run: `window.__uiPerfProbe.stop()`

### Scenario C - Search + Filter

1. Run: `window.__uiPerfProbe.start('search-filter')`
2. Type 12-20 chars in search input.
3. Change status filter and source filter.
4. Run: `window.__uiPerfProbe.stop()`

### Scenario D - Long Scroll

1. Run: `window.__uiPerfProbe.start('long-scroll')`
2. Scroll the sessions list aggressively for 8-10 seconds.
3. Run: `window.__uiPerfProbe.stop()`

## What To Compare

For each run, record:

- `fpsAvg` (higher is better)
- `longTask.count` and `longTask.totalMs` (lower is better)
- `interactionLatency.p95Ms` (lower is better)
- `memory.usedJSHeapSize` (lower is better for same workload)

## Scorecard Template

Copy this table and fill with median values from 5 runs:

| Scenario | React fpsAvg | HTML fpsAvg | React longTask totalMs | HTML longTask totalMs | React interaction p95 | HTML interaction p95 | Winner |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| A - first paint |  |  |  |  |  |  |  |
| B - select/tabs |  |  |  |  |  |  |  |
| C - search/filter |  |  |  |  |  |  |  |
| D - long scroll |  |  |  |  |  |  |  |

## Optimization Backlog - React UI

- Virtualize sessions/events lists (avoid rendering all rows at once).
- Split `SessionRegistryPage` into memoized leaf components (`React.memo`).
- Replace hot-path inline style objects with class-based styles.
- Debounce search and avoid full list state churn on every keystroke.
- Cache expensive formatters (`Intl.DateTimeFormat`, size/time formatting).
- Keep selected detail pane mounted if useful, swap data only.

## Optimization Backlog - HTML UI

- Avoid per-row event listeners; use event delegation on list container.
- Avoid `innerHTML` rewrites for large sections; patch only changed nodes.
- Batch DOM writes with `requestAnimationFrame`.
- Minimize layout thrash (separate reads from writes).
- Add list windowing manually if row count grows.
- Avoid heavy paint effects in dense lists (blur/shadow on many items).

## Shared Improvements

- Paginate and prefetch detail data progressively.
- Render lightweight row skeletons first, hydrate details after.
- Avoid repeated JSON parse/stringify on hot interactions.
- Add explicit performance budget per scenario (e.g. interaction p95 < 80ms).
- Re-run this playbook after every significant UI change.

## Decision Rule

- Keep React if it meets budget and productivity is better.
- Keep HTML approach for specific hotspots where direct DOM is measurably faster.
- Hybrid is allowed: React shell + highly optimized virtualized list module.
