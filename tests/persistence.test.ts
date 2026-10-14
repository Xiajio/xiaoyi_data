import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createInitialState } from '../src/domain/seed';
import { transition } from '../src/domain/model';
import { loadSession, resetSession, saveSession } from '../src/domain/persistence';

let stored: Map<string, string>;
let storage: Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;
const storageKey = (id: string) => 'xiaoyi-data-demo:v1:' + encodeURIComponent(id);
beforeEach(() => {
  stored = new Map();
  storage = { getItem: vi.fn((key: string) => stored.get(key) ?? null), setItem: vi.fn((key: string, value: string) => { stored.set(key, value); }), removeItem: vi.fn((key: string) => { stored.delete(key); }) };
  vi.stubGlobal('localStorage', storage);
});
afterEach(() => { vi.unstubAllGlobals(); });

describe('session persistence', () => {
  it('isolates sessions and restores a complete state including role and selection', () => {
    const state = transition(createInitialState('isolated-a'), { type: 'SET_ROLE', role: 'lead' });
    expect(saveSession(state)).toBeNull();
    expect(loadSession('isolated-a')).toEqual({ state, notice: null, blocked: false });
    expect(loadSession('isolated-b').state.role).toBe('reviewer');
    expect(loadSession('isolated-b').state.sessionId).toBe('isolated-b');
  });
  it('marks restored running work interrupted and retains the reviewed values', () => {
    let state = createInitialState('resume');
    state = transition(state, { type: 'SET_ROLE', role: 'manager' });
    state = transition(state, { type: 'INTAKE' });
    state = transition(state, { type: 'START_RUN' });
    expect(saveSession(state)).toBeNull();
    const loaded = loadSession('resume');
    expect(loaded.blocked).toBe(false);
    expect(loaded.notice).toContain('中断');
    expect(loaded.state.runs[0].status).toBe('interrupted');
    expect(loaded.state.fields).toEqual(state.fields);
    expect(saveSession(loaded.state)).toBeNull();
    expect(loadSession('resume').notice).toBeNull();
  });
  it('does not overwrite corrupt storage until explicit reset', () => {
    stored.set(storageKey('corrupt-json'), '{not-json');
    const loaded = loadSession('corrupt-json');
    expect(loaded.blocked).toBe(true);
    expect(loaded.notice).toContain('暂停写入');
    expect(saveSession(loaded.state)).toContain('暂停写入');
    expect(stored.get(storageKey('corrupt-json'))).toBe('{not-json');
    expect(resetSession('corrupt-json')).toBeNull();
    expect(loadSession('corrupt-json').blocked).toBe(false);
    expect(saveSession(createInitialState('corrupt-json'))).toBeNull();
  });
  it.each([
    ['version', (state: Record<string, any>) => { state.schemaVersion = 99; }],
    ['wrong-session', (state: Record<string, any>) => { state.sessionId = 'other'; }],
    ['reference', (state: Record<string, any>) => { state.fields[0].assetId = 'F-UNKNOWN'; }],
    ['selection', (state: Record<string, any>) => { state.selectedFieldIds = ['R-UNKNOWN']; }],
    ['status', (state: Record<string, any>) => { state.fields[0].status = 'approved'; }],
    ['candidate', (state: Record<string, any>) => { state.fields[0].originalValue = 'altered'; }],
    ['related-source-reference', (state: Record<string, any>) => { state.fields[1].relatedAssetIds = ['F-012']; }],
    ['related-source-shape', (state: Record<string, any>) => { state.fields[1].relatedAssetIds = 'F-003'; }],
    ['missing-related-source', (state: Record<string, any>) => { delete state.fields[1].relatedAssetIds; }],
    ['duplicate-related-source', (state: Record<string, any>) => { state.fields[1].relatedAssetIds = ['F-003', 'F-003']; }],
    ['privacy', (state: Record<string, any>) => { state.patients[1].privacyPassed = true; }],
    ['duplicate-field', (state: Record<string, any>) => { state.fields[1] = state.fields[0]; }],
    ['review-evidence', (state: Record<string, any>) => { state.fields[5].history = []; }],
  ])('blocks malformed %s without replacing it', (name, mutate) => {
    const id = 'malformed-' + name;
    const state = createInitialState(id);
    mutate(state);
    const raw = JSON.stringify(state);
    stored.set(storageKey(id), raw);
    expect(loadSession(id).blocked).toBe(true);
    expect(stored.get(storageKey(id))).toBe(raw);
  });
  it('validates and restores approved snapshot history independently from later revisions', () => {
    let state = transition(createInitialState('versions'), { type: 'SELECT_FIELDS', ids: ['R-06'] });
    state = transition(state, { type: 'FREEZE' });
    state = transition(state, { type: 'SET_ROLE', role: 'lead' });
    state = transition(state, { type: 'APPROVE', snapshotId: state.snapshots[0].id });
    state = transition(state, { type: 'SET_ROLE', role: 'reviewer' });
    state = transition(state, { type: 'REVIEW', fieldId: 'R-06', decision: 'correct', value: '直肠（原文）', note: '补充原文标识。' });
    expect(saveSession(state)).toBeNull();
    expect(loadSession('versions').state.snapshots[0].fields[0].value).toBe('直肠');
    expect(loadSession('versions').state.fields[5].value).toBe('直肠（原文）');
    const broken = structuredClone(state);
    broken.snapshots[0].fieldIds = ['R-01'];
    stored.set(storageKey('versions'), JSON.stringify(broken));
    expect(loadSession('versions').blocked).toBe(true);
  });
  it('continues in memory when storage is unavailable or full', () => {
    const state = transition(createInitialState('memory-only'), { type: 'SET_ROLE', role: 'manager' });
    vi.stubGlobal('localStorage', { getItem() { throw new Error('denied'); }, setItem() { throw new Error('quota'); }, removeItem() { throw new Error('denied'); } });
    expect(saveSession(state)).toContain('内存');
    const loaded = loadSession('memory-only');
    expect(loaded.blocked).toBe(false);
    expect(loaded.state.role).toBe('manager');
    expect(loaded.notice).toContain('内存');
    expect(resetSession('memory-only')).toContain('内存');
    expect(loadSession('memory-only').state.role).toBe('reviewer');
  });
  it('keeps the latest memory state when only writes fail due to quota', () => {
    let state = createInitialState('quota-only');
    expect(saveSession(state)).toBeNull();
    storage.setItem = vi.fn(() => { throw new Error('quota exceeded'); });
    state = transition(state, { type: 'SET_ROLE', role: 'lead' });
    expect(saveSession(state)).toContain('内存');
    expect(loadSession('quota-only').state.role).toBe('lead');
    expect(JSON.parse(stored.get(storageKey('quota-only'))!).role).toBe('reviewer');
  });
  it('explicit reset resumes in memory when corrupt storage cannot be removed', () => {
    stored.set(storageKey('reset-blocked'), '{bad-json');
    expect(loadSession('reset-blocked').blocked).toBe(true);
    storage.removeItem = vi.fn(() => { throw new Error('denied'); });
    expect(resetSession('reset-blocked')).toContain('内存');
    expect(loadSession('reset-blocked').blocked).toBe(false);
    expect(loadSession('reset-blocked').state).toEqual(createInitialState('reset-blocked'));
  });
  it('resets only the requested session', () => {
    saveSession(createInitialState('reset-one'));
    saveSession(createInitialState('reset-two'));
    resetSession('reset-one');
    expect(stored.has(storageKey('reset-one'))).toBe(false);
    expect(stored.has(storageKey('reset-two'))).toBe(true);
  });
});
