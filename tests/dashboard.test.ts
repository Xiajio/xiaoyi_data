import { describe, expect, it } from 'vitest';
import { ASSET_KINDS, FIELD_STATUSES, PROCESSING_STATUSES, getResearchDashboard } from '../src/domain/dashboard';
import { transition } from '../src/domain/model';
import { createInitialState } from '../src/domain/seed';
import type { DemoAction, DemoState } from '../src/domain/types';

const apply = (state: DemoState, ...actions: DemoAction[]) => actions.reduce(transition, state);
const initial = () => createInitialState('dashboard');
const manager = () => transition(initial(), { type: 'SET_ROLE', role: 'manager' });
function completeScan(state: DemoState) {
  let next = transition(state, { type: 'START_SCAN' });
  const scanId = next.scans[0].id;
  for (let step = 0; step < 4; step++) next = transition(next, { type: 'ADVANCE_SCAN', scanId });
  return next;
}
function deepFreeze(value: unknown): void {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return;
  Object.values(value).forEach(deepFreeze);
  Object.freeze(value);
}

describe('research dashboard derived statistics', () => {
  it('uses patients, asset records and candidate fields as separate denominators', () => {
    const data = getResearchDashboard(initial());
    expect(data.scope).toEqual({ patients: 4, assets: 12, fields: 6 });
    expect(data.inventory).toMatchObject({ total: 12, pending: 10, processing: 0, processed: 0, failed: 0, restricted: 2 });
    expect(data.review).toMatchObject({ total: 6, reviewed: 1, pending: 5, critical: 3, missing: 0 });
    expect(data.review.byStatus).toEqual({ pending: 5, confirmed: 1, corrected: 0, missing: 0, 'needs-info': 0, rejected: 0 });
    expect(PROCESSING_STATUSES.reduce((sum, status) => sum + data.inventory[status], 0)).toBe(data.scope.assets);
    expect(FIELD_STATUSES.reduce((sum, status) => sum + data.review.byStatus[status], 0)).toBe(data.scope.fields);
  });

  it('counts one covered patient per type even with multiple records, including separately flagged restricted assets', () => {
    const data = getResearchDashboard(initial());
    expect(data.typeCoverage.map(row => row.kind)).toEqual(ASSET_KINDS);
    expect(data.typeCoverage.map(row => row.patients)).toEqual([4, 3, 2, 2]);
    expect(data.typeCoverage.map(row => row.totalPatients)).toEqual([4, 4, 4, 4]);
    expect(data.typeCoverage[0].assetIds).toHaveLength(5);
    const first = data.coverage.find(row => row.patientId === 'P-DEMO-001')!;
    expect(first.cells.map(cell => cell.total)).toEqual([2, 1, 1, 1]);
    const restricted = data.coverage.find(row => row.patientId === 'P-DEMO-002')!;
    expect(restricted).toMatchObject({ privacyPassed: false, associationPassed: true });
    expect(restricted.cells.find(cell => cell.kind === 'table')).toEqual({ kind: 'table', assetIds: ['F-007'], total: 1, restricted: 1 });
    expect(restricted.cells.find(cell => cell.kind === 'image')).toEqual({ kind: 'image', assetIds: ['F-008'], total: 1, restricted: 1 });
    expect(data.coverage.find(row => row.patientId === 'P-DEMO-004')!.cells.find(cell => cell.kind === 'image')).toEqual({ kind: 'image', assetIds: [], total: 0, restricted: 0 });
  });

  it('groups by source asset month in calendar order without interpreting patient events or field timestamps', () => {
    const state = initial();
    state.assets.reverse();
    state.patients[0].events[0].date = '2099-01-01';
    state.fields[0].eventTime = '2099-01-01';
    const months = getResearchDashboard(state).months;
    expect(months.map(row => [row.month, row.total])).toEqual([['2026-02', 2], ['2026-03', 5], ['2026-04', 3], ['2026-05', 2]]);
    expect(months[0].byKind).toEqual({ text: 1, table: 0, wsi: 1, image: 0 });
    expect(months[1].byKind).toEqual({ text: 2, table: 1, wsi: 1, image: 1 });
    for (const month of months) {
      expect(month.assetIds).toHaveLength(month.total);
      expect(Object.values(month.byKind).reduce((sum, count) => sum + count, 0)).toBe(month.total);
    }
  });

  it('does not count discovered candidates until explicit import and adds coverage without inventing patients or fields', () => {
    let state = completeScan(manager());
    expect(getResearchDashboard(state).scope).toEqual({ patients: 4, assets: 12, fields: 6 });
    state = transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id });
    const data = getResearchDashboard(state);
    expect(data.scope).toEqual({ patients: 4, assets: 16, fields: 6 });
    expect(data.inventory).toMatchObject({ pending: 14, restricted: 2 });
    expect(data.typeCoverage.map(row => row.patients)).toEqual([4, 3, 2, 3]);
    expect(data.months.map(row => row.total)).toEqual([3, 6, 4, 3]);
    expect(data.coverage.find(row => row.patientId === 'P-DEMO-004')!.cells.find(cell => cell.kind === 'image')).toEqual({ kind: 'image', assetIds: ['F-016'], total: 1, restricted: 0 });
    expect(getResearchDashboard(state, 'P-DEMO-004').scope).toEqual({ patients: 1, assets: 3, fields: 1 });
    expect(getResearchDashboard(transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id }))).toEqual(data);
  });

  it('treats documented missingness as reviewed while retaining its own count and closes only the reviewed critical issues', () => {
    const state = apply(initial(),
      { type: 'REVIEW', fieldId: 'R-04', decision: 'missing', note: '随访记录未提供复发信息，保留缺失。' },
      { type: 'REVIEW', fieldId: 'R-02', decision: 'correct', value: '2026-03-05', note: '对照报告签发日期修正科研表原值。' },
      { type: 'REVIEW', fieldId: 'R-01', decision: 'confirm' },
    );
    const data = getResearchDashboard(state);
    expect(data.review).toMatchObject({ total: 6, reviewed: 4, pending: 2, critical: 1, missing: 1 });
    expect(data.review.byStatus).toEqual({ pending: 2, confirmed: 2, corrected: 1, missing: 1, 'needs-info': 0, rejected: 0 });
    expect(data.review.byPatient).toEqual([
      { patientId: 'P-DEMO-001', total: 3, reviewed: 2, critical: 0 },
      { patientId: 'P-DEMO-002', total: 1, reviewed: 0, critical: 1 },
      { patientId: 'P-DEMO-003', total: 1, reviewed: 1, critical: 0 },
      { patientId: 'P-DEMO-004', total: 1, reviewed: 1, critical: 0 },
    ]);
  });

  it('includes needs-info and rejected in incomplete review and unresolved critical totals', () => {
    const state = apply(initial(),
      { type: 'REVIEW', fieldId: 'R-02', decision: 'needs-info', note: '等待核对报告与科研表日期。' },
      { type: 'REVIEW', fieldId: 'R-03', decision: 'reject', note: '现有记录仅有计划，没有治疗实施依据。' },
    );
    expect(getResearchDashboard(state).review).toMatchObject({ reviewed: 1, pending: 5, critical: 3, byStatus: { pending: 3, 'needs-info': 1, rejected: 1 } });
  });

  it('derives processing progress from per-asset execution without inflating review completion', () => {
    let state = apply(manager(), { type: 'INTAKE' }, { type: 'START_RUN' });
    const runId = state.runs[0].id;
    expect(getResearchDashboard(state).inventory).toMatchObject({ pending: 0, processing: 10, processed: 0, restricted: 2 });
    expect(getResearchDashboard(state, 'P-DEMO-002').inventory).toMatchObject({ total: 3, processing: 1, restricted: 2 });
    for (let step = 0; step < 4; step++) state = transition(state, { type: 'ADVANCE_RUN', runId });
    const data = getResearchDashboard(state);
    expect(data.inventory).toMatchObject({ pending: 0, processing: 0, processed: 10, restricted: 2 });
    expect(data.inventory.byKind.text.processed).toBe(5);
    expect(data.review).toEqual(getResearchDashboard(initial()).review);
  });

  it('updates failed and interrupted inputs without counting them as processed', () => {
    let state = apply(manager(), { type: 'INTAKE' }, { type: 'START_RUN', fail: true });
    const runId = state.runs[0].id;
    state = apply(state, { type: 'ADVANCE_RUN', runId }, { type: 'ADVANCE_RUN', runId });
    expect(getResearchDashboard(state).inventory).toMatchObject({ failed: 10, processing: 0, processed: 0 });
    state = apply(state, { type: 'START_RUN', retryOf: runId }, { type: 'INTERRUPT_RUNS' });
    expect(getResearchDashboard(state).inventory).toMatchObject({ pending: 10, failed: 0, processing: 0, processed: 0 });
  });

  it('filters every chart and drill-down reference to the chosen patient', () => {
    const data = getResearchDashboard(initial(), 'P-DEMO-002');
    expect(data.scope).toEqual({ patients: 1, assets: 3, fields: 1 });
    expect(data.patients.map(patient => patient.id)).toEqual(['P-DEMO-002']);
    expect(data.assets.map(asset => asset.id)).toEqual(['F-006', 'F-007', 'F-008']);
    expect(data.fields.map(field => field.id)).toEqual(['R-03']);
    expect(data.inventory).toMatchObject({ total: 3, pending: 1, restricted: 2 });
    expect(data.coverage.map(row => row.patientId)).toEqual(['P-DEMO-002']);
    expect(data.typeCoverage.map(row => row.patients)).toEqual([1, 1, 0, 1]);
    expect(data.typeCoverage.map(row => row.totalPatients)).toEqual([1, 1, 1, 1]);
    expect(data.months).toEqual([{ month: '2026-04', total: 3, assetIds: ['F-006', 'F-007', 'F-008'], byKind: { text: 1, table: 1, wsi: 0, image: 1 } }]);
    expect(data.review).toMatchObject({ total: 1, reviewed: 0, pending: 1, critical: 1, missing: 0, byPatient: [{ patientId: 'P-DEMO-002', total: 1, reviewed: 0, critical: 1 }] });
    const assetIds = new Set(data.assets.map(asset => asset.id));
    for (const row of [...data.coverage.flatMap(row => row.cells), ...data.typeCoverage, ...data.months]) expect(row.assetIds.every(id => assetIds.has(id))).toBe(true);
  });

  it.each(['unknown-patient', ''])('returns a defined empty scope for an unknown explicit patient %s', patientId => {
    const data = getResearchDashboard(initial(), patientId);
    expect(data.scope).toEqual({ patients: 0, assets: 0, fields: 0 });
    expect(data.patients).toEqual([]);
    expect(data.assets).toEqual([]);
    expect(data.fields).toEqual([]);
    expect(data.coverage).toEqual([]);
    expect(data.months).toEqual([]);
    expect(data.review).toMatchObject({ total: 0, reviewed: 0, pending: 0, critical: 0, missing: 0, byPatient: [] });
    expect(data.typeCoverage).toEqual(ASSET_KINDS.map(kind => ({ kind, patients: 0, totalPatients: 0, assetIds: [] })));
    expect(Object.values(data.review.byStatus)).toEqual(FIELD_STATUSES.map(() => 0));
    expect(PROCESSING_STATUSES.every(status => data.inventory[status] === 0)).toBe(true);
    expect(Object.values(data.inventory.byKind).every(counts => Object.values(counts).every(count => count === 0))).toBe(true);
  });

  it('ignores navigation, role and release selection when deriving an explicit scope', () => {
    const before = initial();
    const after = apply(before, { type: 'NAVIGATE', page: 'processing', patientId: 'P-DEMO-002', assetStatus: 'restricted' }, { type: 'SET_ROLE', role: 'lead' }, { type: 'SELECT_FIELDS', ids: ['R-06'] });
    expect(getResearchDashboard(after)).toEqual(getResearchDashboard(before));
    expect(getResearchDashboard(after, 'P-DEMO-001')).toEqual(getResearchDashboard(before, 'P-DEMO-001'));
  });

  it('is deterministic and never mutates the input state, evidence, or nested review history', () => {
    let state = completeScan(manager());
    state = transition(state, { type: 'IMPORT_SCAN', scanId: state.scans[0].id });
    state = apply(state, { type: 'SET_ROLE', role: 'reviewer' }, { type: 'REVIEW', fieldId: 'R-04', decision: 'missing', note: '已查本批资料，没有对应随访信息。' });
    const before = structuredClone(state);
    deepFreeze(state);
    const data = getResearchDashboard(state);
    expect(getResearchDashboard(state)).toEqual(data);
    expect(getResearchDashboard(state, 'P-DEMO-004').fields[0]).toEqual(before.fields.find(field => field.id === 'R-04'));
    expect(state).toEqual(before);
    expect(data.fields.find(field => field.id === 'R-04')!.relatedAssetIds).toEqual(['F-012']);
    expect(data.fields.find(field => field.id === 'R-04')!.history).toHaveLength(1);
  });
});
