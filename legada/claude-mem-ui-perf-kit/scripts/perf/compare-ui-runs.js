#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';

function usage() {
  console.log('Usage: node scripts/perf/compare-ui-runs.js --react <react.json> --html <html.json>');
  console.log('Each file should contain an array of probe results from window.__uiPerfProbe.stop()');
  process.exit(1);
}

function parseArgs(argv) {
  const args = { react: '', html: '' };
  for (let i = 2; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = argv[i + 1];
    if (arg === '--react') {
      args.react = next || '';
      i += 1;
    } else if (arg === '--html') {
      args.html = next || '';
      i += 1;
    }
  }
  if (!args.react || !args.html) usage();
  return args;
}

function loadJson(filePath) {
  const abs = path.resolve(filePath);
  const raw = fs.readFileSync(abs, 'utf8');
  const data = JSON.parse(raw);
  if (!Array.isArray(data)) {
    throw new Error(`${filePath} must be a JSON array`);
  }
  return data;
}

function median(values) {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 0) return (sorted[mid - 1] + sorted[mid]) / 2;
  return sorted[mid];
}

function toFixed(n) {
  return Number.isFinite(n) ? n.toFixed(2) : '0.00';
}

function groupByLabel(runs) {
  const byLabel = new Map();
  for (const run of runs) {
    const label = run.label || 'unknown';
    if (!byLabel.has(label)) byLabel.set(label, []);
    byLabel.get(label).push(run);
  }
  return byLabel;
}

function metricOf(run, key) {
  if (key === 'fpsAvg') return Number(run.fpsAvg || 0);
  if (key === 'longTask.totalMs') return Number(run.longTask?.totalMs || 0);
  if (key === 'interactionLatency.p95Ms') return Number(run.interactionLatency?.p95Ms || 0);
  if (key === 'memory.usedJSHeapSize') return Number(run.memory?.usedJSHeapSize || 0);
  return 0;
}

function winner(lowerIsBetter, reactVal, htmlVal) {
  if (reactVal === htmlVal) return 'tie';
  if (lowerIsBetter) return reactVal < htmlVal ? 'react' : 'html';
  return reactVal > htmlVal ? 'react' : 'html';
}

function formatBytes(bytes) {
  if (!bytes) return '0';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function main() {
  const { react, html } = parseArgs(process.argv);
  const reactRuns = loadJson(react);
  const htmlRuns = loadJson(html);

  const reactByLabel = groupByLabel(reactRuns);
  const htmlByLabel = groupByLabel(htmlRuns);
  const labels = [...new Set([...reactByLabel.keys(), ...htmlByLabel.keys()])].sort();

  const scenarios = labels.map((label) => {
    const rRuns = reactByLabel.get(label) || [];
    const hRuns = htmlByLabel.get(label) || [];

    const rFps = median(rRuns.map((r) => metricOf(r, 'fpsAvg')));
    const hFps = median(hRuns.map((r) => metricOf(r, 'fpsAvg')));
    const rLong = median(rRuns.map((r) => metricOf(r, 'longTask.totalMs')));
    const hLong = median(hRuns.map((r) => metricOf(r, 'longTask.totalMs')));
    const rP95 = median(rRuns.map((r) => metricOf(r, 'interactionLatency.p95Ms')));
    const hP95 = median(hRuns.map((r) => metricOf(r, 'interactionLatency.p95Ms')));
    const rMem = median(rRuns.map((r) => metricOf(r, 'memory.usedJSHeapSize')));
    const hMem = median(hRuns.map((r) => metricOf(r, 'memory.usedJSHeapSize')));

    const wins = [
      winner(false, rFps, hFps),
      winner(true, rLong, hLong),
      winner(true, rP95, hP95),
      winner(true, rMem, hMem),
    ];
    const reactPoints = wins.filter((w) => w === 'react').length;
    const htmlPoints = wins.filter((w) => w === 'html').length;
    const overall = reactPoints === htmlPoints ? 'tie' : reactPoints > htmlPoints ? 'react' : 'html';

    return {
      label,
      rFps,
      hFps,
      rLong,
      hLong,
      rP95,
      hP95,
      rMem,
      hMem,
      overall,
      reactSamples: rRuns.length,
      htmlSamples: hRuns.length,
    };
  });

  console.log('| Scenario | React fps | HTML fps | React longTask ms | HTML longTask ms | React p95 ms | HTML p95 ms | React heap | HTML heap | Winner |');
  console.log('| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |');
  for (const s of scenarios) {
    console.log(`| ${s.label} (${s.reactSamples}/${s.htmlSamples}) | ${toFixed(s.rFps)} | ${toFixed(s.hFps)} | ${toFixed(s.rLong)} | ${toFixed(s.hLong)} | ${toFixed(s.rP95)} | ${toFixed(s.hP95)} | ${formatBytes(s.rMem)} | ${formatBytes(s.hMem)} | ${s.overall} |`);
  }
}

main();
