import { describe, expect, it } from 'vitest';
import { createInitialState } from '../src/domain/seed';
import { exportSnapshot, getFieldBlockers, getSummary, isReviewed, transition } from '../src/domain/model';
import type { DemoAction, DemoState } from '../src/domain/types';

function apply(state: DemoState, ...actions: DemoAction[]) { return actions.reduce(transition, state); }
function ready() {
  return apply(createInitialState('domain-test'),
    { type: 'REVIEW', fieldId: 'R-01', decision: 'confirm' },
    { type: 'REVIEW', fieldId: 'R-02', decision: 'correct', value: '2026-03-05', note: '依据 F-001 第 5 行报告日期，保留取材日期。' },
    { type: 'REVIEW', fieldId: 'R-04', decision: 'missing', note: '核查 F-011 与 F-012，本批资料未明确记载。' },
    { type: 'SELECT_FIELDS', ids: ['R-01', 'R-02', 'R-04', 'R-06'] });
}

describe('synthetic seed and evidence', () => {
  it('creates independent four-patient, twelve-asset, six-field sessions', () => {
    const first = createInitialState('first');
    const second = createInitialState('second');
    expect(first.patients).toHaveLength(4);
    expect(first.assets).toHaveLength(12);
    expect(first.fields).toHaveLength(6);
    expect(getSummary(first)).toMatchObject({ pending: 5, critical: 3, reviewed: 1, publishable: 1, approved: 0, running: false });
    first.fields[0].value = 'changed';
    expect(second.fields[0].value).toBe('乙状结肠');
    expect(second.fields[1].evidence).toContain('2026年03月05日');
  });
  it('links conflicting tables as related sources and preserves them in frozen exports', () => {
    const seed = createInitialState('sources');
    expect(seed.fields.find(field => field.id === 'R-02')?.relatedAssetIds).toEqual(['F-003']);
    expect(seed.fields.find(field => field.id === 'R-04')?.relatedAssetIds).toEqual(['F-012']);
    expect(seed.fields.find(field => field.id === 'R-04')?.evidence).toContain('E2「随访复发信息状态」原值为“无复发”');
    let state = transition(ready(), { type: 'FREEZE' });
    state = transition(state, { type: 'SET_ROLE', role: 'lead' });
    state = transition(state, { type: 'APPROVE', snapshotId: state.snapshots[0].id });
    const exported = exportSnapshot(state, state.snapshots[0].id) as { fields: DemoState['fields'] };
    expect(exported.fields.find(field => field.id === 'R-02')?.relatedAssetIds).toEqual(['F-003']);
    expect(exported.fields.find(field => field.id === 'R-04')).toMatchObject({ relatedAssetIds: ['F-012'], value: '未记录' });
    expect(exported.fields.find(field => field.id === 'R-04')?.evidence).toContain('本批随访原文没有支持这一阴性结局的明确记载');
    exported.fields.find(field => field.id === 'R-02')!.relatedAssetIds!.push('F-001');
    expect(state.snapshots[0].fields.find(field => field.id === 'R-02')?.relatedAssetIds).toEqual(['F-003']);
  });
  it('does not mutate the input and keeps original evidence and candidate', () => {
    const before = createInitialState('first');
    const next = transition(before, { type: 'REVIEW', fieldId: 'R-02', decision: 'correct', value: '2026-03-05', note: '依据第 5 行。' });
    expect(before.fields[1].status).toBe('pending');
    expect(next.fields[1]).toMatchObject({ status: 'corrected', value: '2026-03-05', originalValue: '2026-03-03', evidence: before.fields[1].evidence });
    expect(next.fields[1].history[0]).toMatchObject({ previousValue: '2026-03-03', value: '2026-03-05', role: 'reviewer' });
  });
});

describe('review and publication quality gates', () => {
  it('requires reviewer, requires an explanation, and blocks direct critical confirmation', () => {
    const initial = createInitialState('permissions');
    expect(() => transition(initial, { type: 'REVIEW', fieldId: 'R-02', decision: 'confirm' })).toThrow('关键问题');
    expect(() => transition(initial, { type: 'REVIEW', fieldId: 'R-04', decision: 'missing' })).toThrow('说明');
    expect(() => transition({ ...initial, role: 'manager' }, { type: 'REVIEW', fieldId: 'R-01', decision: 'confirm' })).toThrow('角色');
    expect(() => transition(initial, { type: 'REVIEW', fieldId: 'R-03', decision: 'correct', value: '已实施', note: '猜测' })).toThrow('没有治疗实施依据');
    expect(() => transition(initial, { type: 'REVIEW', fieldId: 'R-04', decision: 'correct', value: '无复发', note: '猜测' })).toThrow('缺少复发状态依据');
  });
  it('keeps needs-info and rejected unresolved while reviewed missing can be published', () => {
    const state = ready();
    expect(isReviewed(state.fields[3])).toBe(true);
    expect(getFieldBlockers(state, 'R-04')).toEqual([]);
    const deferred = transition(state, { type: 'REVIEW', fieldId: 'R-04', decision: 'needs-info', note: '请求补充随访资料。' });
    expect(getFieldBlockers(deferred, 'R-04')).toContain('字段仍待补充资料');
    const rejected = transition(state, { type: 'REVIEW', fieldId: 'R-04', decision: 'reject', note: '拒绝无依据候选。' });
    expect(isReviewed(rejected.fields[3])).toBe(false);
  });
  it('blocks a reviewed field if another critical field for that patient remains open', () => {
    const state = transition(createInitialState('critical'), { type: 'REVIEW', fieldId: 'R-01', decision: 'confirm' });
    expect(getFieldBlockers(state, 'R-01')).toContain('同一患者的关键问题尚未闭环');
    expect(() => transition({ ...state, selectedFieldIds: ['R-01'] }, { type: 'FREEZE' })).toThrow('发布检查');
  });
  it('keeps privacy and association as distinct blockers', () => {
    const state = transition(createInitialState('privacy'), { type: 'REVIEW', fieldId: 'R-03', decision: 'correct', value: '计划', note: '未见实施依据。' });
    expect(getFieldBlockers(state, 'R-03')).toEqual(['患者隐私检查未完成']);
    state.patients[2].associationPassed = false;
    expect(getFieldBlockers(state, 'R-06')).toEqual(['患者与事件关联检查未通过']);
  });
  it('restricts freezing and approval roles and blocks empty selections', () => {
    const state = ready();
    expect(() => transition({ ...state, role: 'operator' }, { type: 'FREEZE' })).toThrow('角色');
    expect(() => transition({ ...state, selectedFieldIds: [] }, { type: 'FREEZE' })).toThrow('至少');
    const frozen = transition(state, { type: 'FREEZE' });
    expect(() => transition(frozen, { type: 'APPROVE', snapshotId: frozen.snapshots[0].id })).toThrow('角色');
    expect(() => exportSnapshot(frozen, frozen.snapshots[0].id)).toThrow('已批准');
  });
});

describe('frozen versions', () => {
  it('invalidates a pending snapshot when selected fields change but not on unrelated review', () => {
    const frozen = transition(ready(), { type: 'FREEZE' });
    const unrelated = transition(frozen, { type: 'REVIEW', fieldId: 'R-03', decision: 'correct', value: '计划', note: '本批资料没有实施依据。' });
    expect(unrelated.snapshots[0].status).toBe('pending');
    const modified = transition(unrelated, { type: 'REVIEW', fieldId: 'R-01', decision: 'correct', value: '乙状结肠（原文）', note: '保留原文范围。' });
    expect(modified.snapshots[0].status).toBe('invalidated');
    expect(frozen.snapshots[0].status).toBe('pending');
    expect(transition(frozen, { type: 'SELECT_FIELDS', ids: ['R-06'] }).snapshots[0].status).toBe('invalidated');
    expect(transition(frozen, { type: 'SELECT_FIELDS', ids: [...frozen.selectedFieldIds].reverse() }).snapshots[0].status).toBe('pending');
  });
  it('invalidates a snapshot when a related patient critical field reopens', () => {
    const initial = apply(ready(), { type: 'SELECT_FIELDS', ids: ['R-01'] }, { type: 'FREEZE' });
    const reopened = transition(initial, { type: 'REVIEW', fieldId: 'R-02', decision: 'needs-info', note: '请求重新核对报告。' });
    expect(reopened.snapshots[0].status).toBe('invalidated');
  });
  it('keeps approved snapshot immutable, exports its exact content, and allows a later version', () => {
    const frozen = transition(ready(), { type: 'FREEZE' });
    const id = frozen.snapshots[0].id;
    const approved = apply(frozen, { type: 'SET_ROLE', role: 'lead' }, { type: 'APPROVE', snapshotId: id });
    const exported = exportSnapshot(approved, id);
    const changed = apply(approved,
      { type: 'SET_ROLE', role: 'reviewer' },
      { type: 'REVIEW', fieldId: 'R-01', decision: 'correct', value: '乙状结肠（原文）', note: '补充原文标识。' },
      { type: 'FREEZE' });
    expect(changed.snapshots[0].version).toBe(2);
    expect(changed.snapshots[1]).toEqual(approved.snapshots[0]);
    expect(exportSnapshot(changed, id)).toEqual(exported);
    expect(changed.snapshots[1].fields[0].value).toBe('乙状结肠');
    const logged = transition(changed, { type: 'EXPORT_SNAPSHOT', snapshotId: id });
    expect(logged.audit[0].action).toBe('EXPORT');
    (exported as { fields: { value: string }[] }).fields[0].value = 'external mutation';
    expect(approved.snapshots[0].fields[0].value).toBe('乙状结肠');
  });
});

describe('simulated runs', () => {
  it('requires manager and intake, fails at stage two, retries with a new id, and preserves review', () => {
    const initial = ready();
    expect(() => transition(initial, { type: 'INTAKE' })).toThrow('角色');
    expect(() => transition({ ...initial, role: 'manager' }, { type: 'START_RUN' })).toThrow('接入');
    let state = apply(initial, { type: 'SET_ROLE', role: 'manager' }, { type: 'INTAKE' }, { type: 'START_RUN', fail: true });
    const first = state.runs[0].id;
    expect(() => transition(state, { type: 'START_RUN' })).toThrow('运行中');
    state = apply(state, { type: 'ADVANCE_RUN', runId: first }, { type: 'ADVANCE_RUN', runId: first });
    expect(state.runs[0]).toMatchObject({ status: 'failed', step: 2 });
    state = transition(state, { type: 'START_RUN', retryOf: first });
    const second = state.runs[0].id;
    expect(second).not.toBe(first);
    for (let index = 0; index < 4; index++) state = transition(state, { type: 'ADVANCE_RUN', runId: second });
    expect(state.runs[0]).toMatchObject({ status: 'completed', step: 4, retryOf: first });
    expect(state.fields).toEqual(initial.fields);
    expect(() => transition(state, { type: 'START_RUN', retryOf: second })).toThrow('只有失败或中断');
  });
  it('marks running work interrupted without modifying reviewed data', () => {
    const state = apply(ready(), { type: 'SET_ROLE', role: 'manager' }, { type: 'INTAKE' }, { type: 'START_RUN' });
    const interrupted = transition(state, { type: 'INTERRUPT_RUNS' });
    expect(interrupted.runs[0].status).toBe('interrupted');
    expect(interrupted.fields).toEqual(state.fields);
    expect(transition(interrupted, { type: 'INTERRUPT_RUNS' }).audit).toEqual(interrupted.audit);
  });
});
