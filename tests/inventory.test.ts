import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createInitialState } from '../src/domain/seed';
import { getInventorySummary, getSummary, transition } from '../src/domain/model';
import { SCAN_ASSETS, SCAN_DIRECTORIES, getScanCandidates } from '../src/domain/scanning';
import { loadSession, saveSession } from '../src/domain/persistence';
import type { DemoAction, DemoState } from '../src/domain/types';

let cache: Map<string, string>;
const key = (id: string) => 'xiaoyi-data-demo:v1:' + encodeURIComponent(id);
const apply = (state: DemoState, ...actions: DemoAction[]) => actions.reduce(transition, state);
const manager = (id: string) => transition(createInitialState(id), { type: 'SET_ROLE', role: 'manager' });
function scanComplete(state: DemoState) {
  let next = transition(state, { type: 'START_SCAN' });
  const scanId = next.scans[0].id;
  for (let index = 0; index < 4; index++) next = transition(next, { type: 'ADVANCE_SCAN', scanId });
  return next;
}
function runComplete(state: DemoState) {
  let next = transition(state, { type: 'START_RUN' });
  const runId = next.runs[0].id;
  for (let index = 0; index < 4; index++) next = transition(next, { type: 'ADVANCE_RUN', runId });
  return next;
}
function legacyFrom(state: DemoState): Record<string, any> {
  const legacy = structuredClone(state) as unknown as Record<string, any>;
  legacy.schemaVersion = 1;
  for (const field of ['assetProcessing', 'assetFilter', 'scanConfig', 'scans']) delete legacy[field];
  legacy.runs.forEach((run: Record<string, any>) => { delete run.assetIds; delete run.legacy; });
  return legacy;
}
beforeEach(() => {
  vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-14T01:00:00.000Z'));
  cache = new Map();
  vi.stubGlobal('localStorage', { getItem: (key: string) => cache.get(key) ?? null, setItem: (key: string, value: string) => { cache.set(key, value); }, removeItem: (key: string) => { cache.delete(key); } });
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe('inventory counts and per-asset processing', () => {
  it('starts with ten pending and two restricted and counts each kind consistently', () => {
    const state = createInitialState('inventory-start');
    const summary = getInventorySummary(state);
    expect(summary).toMatchObject({ total: 12, pending: 10, processing: 0, processed: 0, failed: 0, restricted: 2 });
    expect(summary.byKind.text).toEqual({ total: 5, pending: 5, processing: 0, processed: 0, failed: 0, restricted: 0 });
    expect(summary.byKind.table).toMatchObject({ total: 3, pending: 2, restricted: 1 });
    expect(Object.values(summary.byKind).reduce((sum, kind) => sum + kind.total, 0)).toBe(summary.total);
    expect(getSummary(state).inventory).toEqual(summary);
    expect(state.assetFilter).toBe('all');
    expect(transition(state, { type: 'NAVIGATE', page: 'processing', assetStatus: 'failed' }).assetFilter).toBe('failed');
  });
  it('runs only ready inputs, preserves review, and refuses another run when no targets remain', () => {
    const before = apply(manager('asset-completion'), { type: 'INTAKE' });
    const after = runComplete(before);
    expect(after.runs[0].assetIds).toHaveLength(10);
    expect(after.runs[0].assetIds).not.toContain('F-007');
    expect(getInventorySummary(after)).toMatchObject({ total: 12, processed: 10, pending: 0, restricted: 2 });
    expect(after.fields).toEqual(before.fields);
    expect(() => transition(after, { type: 'START_RUN' })).toThrow('没有待处理');
    expect(saveSession(after)).toBeNull();
    expect(loadSession('asset-completion').state.assetProcessing).toEqual(after.assetProcessing);
  });
  it('records failed asset counts and resets only in-flight inputs on interruption', () => {
    let state = apply(manager('asset-failure'), { type: 'INTAKE' }, { type: 'START_RUN', fail: true });
    expect(getInventorySummary(state).processing).toBe(10);
    const runId = state.runs[0].id;
    state = apply(state, { type: 'ADVANCE_RUN', runId }, { type: 'ADVANCE_RUN', runId });
    expect(getInventorySummary(state)).toMatchObject({ failed: 10, processing: 0, restricted: 2 });
    state = transition(state, { type: 'START_RUN', retryOf: runId });
    expect(getInventorySummary(state).processing).toBe(10);
    state = transition(state, { type: 'INTERRUPT_RUNS' });
    expect(getInventorySummary(state)).toMatchObject({ pending: 10, processing: 0, failed: 0 });
    expect(saveSession(state)).toBeNull();
  });
});

describe('synthetic directory scan', () => {
  it('offers fixed directories and deterministic candidates without changing returned source fixtures', () => {
    expect(SCAN_DIRECTORIES.map(directory => directory.id)).toEqual(['inbox', 'archive']);
    expect(SCAN_ASSETS.map(asset => asset.id)).toEqual(['F-013', 'F-014', 'F-015', 'F-016']);
    expect(new Set(SCAN_ASSETS.map(asset => asset.kind)).size).toBe(4);
    expect(getScanCandidates('inbox', true)).toHaveLength(7);
    expect(getScanCandidates('inbox', false)).toHaveLength(5);
    expect(getScanCandidates('archive', true)).toHaveLength(5);
    const copy = getScanCandidates('inbox', true); copy[0].reason = 'changed';
    expect(getScanCandidates('inbox', true)[0].reason).not.toBe('changed');
  });
  it('keeps discovery separate from import and deduplicates explicit imports and subsequent scans', () => {
    let state = scanComplete(manager('scan-import'));
    const scanId = state.scans[0].id;
    expect(state.assets).toHaveLength(12);
    expect(state.scans[0].entries.map(entry => entry.status)).toEqual(['new', 'new', 'new', 'new', 'duplicate', 'unsupported', 'unreadable']);
    expect(() => transition({ ...state, role: 'reviewer' }, { type: 'IMPORT_SCAN', scanId })).toThrow('角色');
    state = transition(state, { type: 'IMPORT_SCAN', scanId });
    expect(state.intakeDone).toBe(true);
    expect(getInventorySummary(state)).toMatchObject({ total: 16, pending: 14, restricted: 2 });
    expect(state.scans[0].importedAssetIds).toEqual(['F-013', 'F-014', 'F-015', 'F-016']);
    expect(transition(state, { type: 'IMPORT_SCAN', scanId })).toEqual(state);
    state = scanComplete(state);
    expect(state.scans[0].entries.filter(entry => entry.status === 'new')).toHaveLength(0);
    expect(state.scans[0].entries.filter(entry => entry.status === 'duplicate')).toHaveLength(5);
    expect(state.fields).toHaveLength(6);
    expect(saveSession(state)).toBeNull();
    expect(loadSession('scan-import').state.assets).toHaveLength(16);
  });
  it('respects nonrecursive and archive subsets while sharing imported ids', () => {
    let state = apply(manager('scan-subsets'), { type: 'CONFIGURE_SCAN', config: { recursive: false } });
    state = scanComplete(state);
    state = transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id });
    expect(state.assets).toHaveLength(14);
    expect(state.assets.slice(-2).map(asset => asset.id)).toEqual(['F-013', 'F-014']);
    state = transition(state, { type: 'CONFIGURE_SCAN', config: { directoryId: 'archive' } });
    state = scanComplete(state);
    state = transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id });
    expect(state.assets).toHaveLength(16);
    expect(saveSession(state)).toBeNull();
  });
  it('freezes run inputs so concurrent scan import stays pending until the next run', () => {
    let state = apply(manager('parallel-import'), { type: 'INTAKE' }, { type: 'START_RUN' });
    const oldRunId = state.runs[0].id;
    const oldFields = structuredClone(state.fields);
    state = scanComplete(state);
    state = transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id });
    expect(getInventorySummary(state)).toMatchObject({ total: 16, processing: 10, pending: 4, restricted: 2 });
    for (let index = 0; index < 4; index++) state = transition(state, { type: 'ADVANCE_RUN', runId: oldRunId });
    expect(getInventorySummary(state)).toMatchObject({ processed: 10, pending: 4, restricted: 2 });
    state = runComplete(state);
    expect(state.runs[0].assetIds).toEqual(['F-013', 'F-014', 'F-015', 'F-016']);
    expect(getInventorySummary(state)).toMatchObject({ total: 16, processed: 14, pending: 0, restricted: 2 });
    expect(state.fields).toEqual(oldFields);
    expect(saveSession(state)).toBeNull();
  });
  it('stops scans without partial import and rejects stale callbacks', () => {
    let state = transition(manager('scan-stop'), { type: 'START_SCAN' });
    const scanId = state.scans[0].id;
    state = transition(state, { type: 'ADVANCE_SCAN', scanId });
    expect(() => transition(state, { type: 'CONFIGURE_SCAN', config: { recursive: false } })).toThrow('运行中');
    state = transition(state, { type: 'STOP_SCAN', scanId });
    expect(state.scans[0]).toMatchObject({ status: 'interrupted', entries: [], importedAssetIds: [] });
    expect(state.assets).toHaveLength(12);
    expect(() => transition(state, { type: 'ADVANCE_SCAN', scanId })).toThrow('已结束');
    expect(() => transition(state, { type: 'IMPORT_SCAN', scanId })).toThrow('已完成扫描');
    expect(saveSession(state)).toBeNull();
  });
  it('allows configured automatic scans after role change and permits disabling while a scan runs', () => {
    let state = transition(manager('automatic-scan'), { type: 'CONFIGURE_SCAN', config: { autoEnabled: true, intervalSeconds: 15 } });
    expect(state.scanConfig.nextScanAt).toBe('2026-10-14T01:00:15.000Z');
    state = transition(state, { type: 'SET_ROLE', role: 'reviewer' });
    expect(() => transition(state, { type: 'START_SCAN', trigger: 'automatic' })).toThrow('计划时间');
    vi.advanceTimersByTime(15000);
    state = transition(state, { type: 'START_SCAN', trigger: 'automatic' });
    expect(state.scans[0].trigger).toBe('automatic');
    expect(state.scanConfig.nextScanAt).toBeUndefined();
    state = transition(state, { type: 'SET_ROLE', role: 'manager' });
    state = transition(state, { type: 'CONFIGURE_SCAN', config: { autoEnabled: false } });
    const scanId = state.scans[0].id;
    for (let index = 0; index < 4; index++) state = transition(state, { type: 'ADVANCE_SCAN', scanId });
    expect(state.scanConfig.nextScanAt).toBeUndefined();
    expect(state.scans[0].status).toBe('completed');
    expect(state.assets).toHaveLength(12);
  });
  it('schedules the next automatic scan after completion and pauses automatic work on restore', () => {
    let state = transition(manager('auto-restore'), { type: 'CONFIGURE_SCAN', config: { autoEnabled: true, intervalSeconds: 30 } });
    state = scanComplete(state);
    expect(state.scanConfig.nextScanAt).toBe('2026-10-14T01:00:30.000Z');
    state = transition(state, { type: 'START_SCAN' });
    state = transition(state, { type: 'INTAKE' });
    state = transition(state, { type: 'START_RUN' });
    expect(saveSession(state)).toBeNull();
    const loaded = loadSession('auto-restore');
    expect(loaded.blocked).toBe(false);
    expect(loaded.notice).toContain('中断');
    expect(loaded.state.scans[0].status).toBe('interrupted');
    expect(loaded.state.runs[0].status).toBe('interrupted');
    expect(loaded.state.scanConfig).toMatchObject({ autoEnabled: false });
    expect(loaded.state.scanConfig.nextScanAt).toBeUndefined();
    expect(getInventorySummary(loaded.state)).toMatchObject({ pending: 10, processing: 0, restricted: 2 });
    expect(saveSession(loaded.state)).toBeNull();
  });
});

describe('v1 migration and scan cache validation', () => {
  it('migrates approved history and completed legacy runs while restarting per-asset counts', () => {
    let state = apply(manager('legacy-progress'), { type: 'INTAKE' });
    state = runComplete(state);
    state = apply(state, { type: 'SET_ROLE', role: 'reviewer' }, { type: 'REVIEW', fieldId: 'R-01', decision: 'confirm' }, { type: 'SELECT_FIELDS', ids: ['R-06'] }, { type: 'FREEZE' }, { type: 'SET_ROLE', role: 'lead' });
    state = transition(state, { type: 'APPROVE', snapshotId: state.snapshots[0].id });
    const old = legacyFrom(state); cache.set(key('legacy-progress'), JSON.stringify(old));
    const loaded = loadSession('legacy-progress');
    expect(loaded.blocked).toBe(false);
    expect(loaded.state.schemaVersion).toBe(2);
    expect(loaded.notice).toContain('旧版未记录逐资料处理结果');
    expect(loaded.state.fields).toEqual(state.fields);
    expect(loaded.state.snapshots).toEqual(state.snapshots);
    expect(loaded.state.runs[0]).toMatchObject({ status: 'completed', legacy: true, assetIds: [] });
    expect(getInventorySummary(loaded.state)).toMatchObject({ pending: 10, processed: 0, restricted: 2 });
    expect(saveSession(loaded.state)).toBeNull();
    expect(JSON.parse(cache.get(key('legacy-progress'))!).schemaVersion).toBe(2);
  });
  it('interrupts a legacy active run without inventing completed assets', () => {
    const state = apply(manager('legacy-active'), { type: 'INTAKE' }, { type: 'START_RUN' });
    cache.set(key('legacy-active'), JSON.stringify(legacyFrom(state)));
    const loaded = loadSession('legacy-active');
    expect(loaded.blocked).toBe(false);
    expect(loaded.state.runs[0]).toMatchObject({ status: 'interrupted', legacy: true, assetIds: [] });
    expect(getInventorySummary(loaded.state).processed).toBe(0);
    expect(saveSession(loaded.state)).toBeNull();
  });
  it('does not migrate damaged v1 state or overwrite its storage', () => {
    const legacy = legacyFrom(createInitialState('legacy-damaged'));
    legacy.fields[0].patientId = 'P-DEMO-004';
    const raw = JSON.stringify(legacy); cache.set(key('legacy-damaged'), raw);
    const loaded = loadSession('legacy-damaged');
    expect(loaded.blocked).toBe(true);
    expect(saveSession(loaded.state)).toContain('暂停写入');
    expect(cache.get(key('legacy-damaged'))).toBe(raw);
  });
  it.each([
    ['processed-without-run', (state: Record<string, any>) => { state.assetProcessing['F-001'] = 'processed'; }],
    ['processing-without-run', (state: Record<string, any>) => { state.assetProcessing['F-001'] = 'processing'; }],
    ['failed-without-run', (state: Record<string, any>) => { state.assetProcessing['F-001'] = 'failed'; }],
    ['restricted-bypass', (state: Record<string, any>) => { state.assetProcessing['F-007'] = 'pending'; }],
    ['missing-processing', (state: Record<string, any>) => { delete state.assetProcessing['F-001']; }],
    ['unknown-processing', (state: Record<string, any>) => { state.assetProcessing['F-999'] = 'pending'; }],
    ['unproven-import', (state: Record<string, any>) => { state.assets.push(structuredClone(SCAN_ASSETS[0])); state.assetProcessing['F-013'] = 'pending'; }],
    ['invalid-directory', (state: Record<string, any>) => { state.scanConfig.directoryId = 'real-server'; }],
  ])('blocks inconsistent inventory %s', (name, mutate) => {
    const sessionId = 'inventory-bad-' + name;
    const state = createInitialState(sessionId); mutate(state);
    const raw = JSON.stringify(state); cache.set(key(sessionId), raw);
    expect(loadSession(sessionId).blocked).toBe(true);
    expect(cache.get(key(sessionId))).toBe(raw);
  });
  it('rejects altered scan source paths and imported ids', () => {
    let state = scanComplete(manager('scan-source-damaged'));
    state = transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id });
    state.scans[0].entries[0].path = '\\\\real-server\\private\\file.txt';
    cache.set(key(state.sessionId), JSON.stringify(state));
    expect(loadSession(state.sessionId).blocked).toBe(true);
  });
});
