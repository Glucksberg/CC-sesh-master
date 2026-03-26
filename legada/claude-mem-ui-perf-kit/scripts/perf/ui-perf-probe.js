/* eslint-disable no-console */
(() => {
  if (typeof window === 'undefined') return;
  if (window.__uiPerfProbe) return;

  const state = {
    running: false,
    label: '',
    startedAt: 0,
    frames: 0,
    rafId: 0,
    longTasks: [],
    interactionLatencies: [],
    marks: [],
    observer: null,
    clickHandler: null,
    keyHandler: null,
  };

  function percentile(values, p) {
    if (!values.length) return 0;
    const sorted = [...values].sort((a, b) => a - b);
    const idx = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
    return sorted[Math.max(0, idx)];
  }

  function trackFrames() {
    if (!state.running) return;
    state.frames += 1;
    state.rafId = window.requestAnimationFrame(trackFrames);
  }

  function trackInteractionLatency(startTs) {
    window.requestAnimationFrame(() => {
      if (!state.running) return;
      const latency = performance.now() - startTs;
      state.interactionLatencies.push(latency);
      if (state.interactionLatencies.length > 500) {
        state.interactionLatencies.shift();
      }
    });
  }

  function start(label = 'run') {
    if (state.running) {
      stop();
    }

    state.running = true;
    state.label = label;
    state.startedAt = performance.now();
    state.frames = 0;
    state.longTasks = [];
    state.interactionLatencies = [];
    state.marks = [];

    if (typeof PerformanceObserver !== 'undefined') {
      try {
        state.observer = new PerformanceObserver((list) => {
          const entries = list.getEntries();
          for (const entry of entries) {
            state.longTasks.push(entry.duration);
            if (state.longTasks.length > 1000) {
              state.longTasks.shift();
            }
          }
        });
        state.observer.observe({ type: 'longtask', buffered: true });
      } catch {
        state.observer = null;
      }
    }

    state.clickHandler = () => trackInteractionLatency(performance.now());
    state.keyHandler = () => trackInteractionLatency(performance.now());
    window.addEventListener('pointerdown', state.clickHandler, { passive: true });
    window.addEventListener('keydown', state.keyHandler, { passive: true });

    state.rafId = window.requestAnimationFrame(trackFrames);
    return true;
  }

  function mark(name) {
    if (!state.running) return false;
    state.marks.push({
      name,
      msFromStart: performance.now() - state.startedAt,
    });
    return true;
  }

  function stop() {
    if (!state.running) return null;
    state.running = false;

    if (state.rafId) {
      window.cancelAnimationFrame(state.rafId);
    }

    if (state.observer) {
      state.observer.disconnect();
      state.observer = null;
    }

    if (state.clickHandler) {
      window.removeEventListener('pointerdown', state.clickHandler);
    }
    if (state.keyHandler) {
      window.removeEventListener('keydown', state.keyHandler);
    }

    const durationMs = performance.now() - state.startedAt;
    const durationSec = durationMs / 1000;
    const longTaskTotalMs = state.longTasks.reduce((acc, val) => acc + val, 0);

    const memory = performance.memory
      ? {
          usedJSHeapSize: performance.memory.usedJSHeapSize,
          totalJSHeapSize: performance.memory.totalJSHeapSize,
          jsHeapSizeLimit: performance.memory.jsHeapSizeLimit,
        }
      : null;

    const result = {
      label: state.label,
      durationMs: Number(durationMs.toFixed(2)),
      fpsAvg: Number((state.frames / Math.max(durationSec, 0.001)).toFixed(2)),
      longTask: {
        count: state.longTasks.length,
        totalMs: Number(longTaskTotalMs.toFixed(2)),
        p95Ms: Number(percentile(state.longTasks, 95).toFixed(2)),
        maxMs: Number((Math.max(0, ...state.longTasks)).toFixed(2)),
      },
      interactionLatency: {
        sampleCount: state.interactionLatencies.length,
        p50Ms: Number(percentile(state.interactionLatencies, 50).toFixed(2)),
        p95Ms: Number(percentile(state.interactionLatencies, 95).toFixed(2)),
        maxMs: Number((Math.max(0, ...state.interactionLatencies)).toFixed(2)),
      },
      memory,
      marks: state.marks,
      finishedAt: new Date().toISOString(),
    };

    console.log('[ui-perf-probe] result');
    console.log(result);
    return result;
  }

  function status() {
    return {
      running: state.running,
      label: state.label,
      elapsedMs: state.running ? Number((performance.now() - state.startedAt).toFixed(2)) : 0,
      longTaskCount: state.longTasks.length,
      interactionSamples: state.interactionLatencies.length,
    };
  }

  window.__uiPerfProbe = {
    start,
    stop,
    mark,
    status,
  };

  console.log('[ui-perf-probe] ready: window.__uiPerfProbe.start("scenario-name")');
})();
