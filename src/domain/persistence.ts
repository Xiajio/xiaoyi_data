import { createInitialState } from './seed';
import { isReviewed, transition } from './model';
import { PAGES, type DemoState, type Field, type Role } from './types';

const prefix = 'xiaoyi-data-demo:v1:';
const blockedSessions = new Set<string>();
const memorySessions = new Map<string, DemoState>();
const unsavedSessions = new Set<string>();
const roles: Role[] = ['reviewer', 'manager', 'lead', 'operator'];
const statuses = ['pending', 'confirmed', 'corrected', 'missing', 'needs-info', 'rejected'];
const decisions = ['confirm', 'correct', 'missing', 'needs-info', 'reject'];
const key = (sessionId: string) => prefix + encodeURIComponent(sessionId);
const unavailable = '浏览器存储不可用，本次演示在内存中继续；关闭页面后可能无法恢复。';
const corrupt = '此演示会话的本地记录已损坏或版本不兼容，已暂停写入。当前显示初始示例，请点击“一键重置”后继续。';
function ensure(condition: unknown, message: string): asserts condition { if (!condition) throw new Error(message); }
function record(value: unknown): Record<string, unknown> {
  ensure(value !== null && typeof value === 'object' && !Array.isArray(value), '记录格式错误');
  return value as Record<string, unknown>;
}
function text(value: unknown, limit = 12000): value is string { return typeof value === 'string' && value.length <= limit; }
function timestamp(value: unknown): boolean { return text(value, 80) && Number.isFinite(Date.parse(value)); }
function integer(value: unknown, min: number, max = Number.MAX_SAFE_INTEGER): boolean { return Number.isSafeInteger(value) && Number(value) >= min && Number(value) <= max; }
function list(value: unknown, max = 20000): unknown[] { ensure(Array.isArray(value) && value.length <= max, '列表格式错误'); return value; }
function unique(values: unknown[]): boolean { return new Set(values).size === values.length; }
function validRole(value: unknown): boolean { return roles.includes(value as Role); }
function sessionValid(value: unknown): value is string { return text(value, 128) && value.trim().length > 0 && !/[\u0000-\u001f]/.test(value); }
function assertState(value: unknown, sessionId: string): asserts value is DemoState {
  const state = record(value);
  ensure(state.schemaVersion === 1 && state.sessionId === sessionId && sessionValid(sessionId), '会话或格式版本错误');
  ensure(integer(state.revision, 1) && PAGES.includes(state.page as DemoState['page']) && ['light', 'dark'].includes(String(state.theme)) && validRole(state.role), '视图状态错误');
  ensure(typeof state.intakeDone === 'boolean', '接入状态错误');
  const seed = createInitialState(sessionId);
  // These are fixed synthetic sources. No action in this demo can change them.
  ensure(JSON.stringify(state.patients) === JSON.stringify(seed.patients), '研究对象资料不匹配');
  ensure(JSON.stringify(state.assets) === JSON.stringify(seed.assets), '资料目录不匹配');
  const seedFields = new Map(seed.fields.map(field => [field.id, field]));
  const validateField = (value: unknown): Field => {
    const field = record(value);
    ensure(text(field.id), '字段标识错误');
    const original = seedFields.get(field.id);
    ensure(original, '未知字段');
    const fixedKeys = ['patientId', 'assetId', 'label', 'originalValue', 'critical', 'issue', 'location', 'evidence', 'eventTime', 'recordTime'] as const;
    ensure(fixedKeys.every(name => field[name] === original[name]), '原始证据或字段引用不匹配');
    ensure(JSON.stringify(field.relatedAssetIds) === JSON.stringify(original.relatedAssetIds), '辅助来源与固定证据不匹配');
    const relatedIds = field.relatedAssetIds === undefined ? [] : list(field.relatedAssetIds, 12);
    ensure(unique(relatedIds) && relatedIds.every(id => typeof id === 'string' && id !== field.assetId && seed.assets.some(asset => asset.id === id && asset.patientId === field.patientId)), '辅助来源引用错误');
    ensure(text(field.value, 1000) && field.value.trim().length > 0 && text(field.reviewNote, 2000) && statuses.includes(String(field.status)), '字段值或审核状态错误');
    const history = list(field.history);
    ensure(unique(history.map(item => record(item).id)), '审核记录标识重复');
    history.forEach(item => {
      const entry = record(item);
      ensure(text(entry.id, 128) && timestamp(entry.at) && entry.role === 'reviewer' && decisions.includes(String(entry.decision)), '审核记录格式错误');
      ensure(text(entry.previousValue, 1000) && text(entry.value, 1000) && text(entry.note, 2000) && entry.note.trim().length > 0, '审核依据错误');
    });
    if (field.status === 'pending') ensure(!history.length && field.value === original.originalValue && field.reviewNote === '', '待核验状态不匹配');
    else {
      ensure(history.length > 0, '缺少审核记录');
      const last = record(history.at(-1));
      ensure(last.value === field.value && last.note === field.reviewNote, '当前字段与审核记录不一致');
      const expected: Record<string, string> = { confirm: 'confirmed', correct: field.id === 'R-04' ? 'missing' : 'corrected', missing: 'missing', 'needs-info': 'needs-info', reject: 'rejected' };
      ensure(field.status === expected[String(last.decision)], '审核决策与状态不匹配');
    }
    if (field.critical && field.status === 'confirmed') throw new Error('关键问题不可直接确认');
    if (field.status === 'missing') ensure(field.id === 'R-04' && ['未记录', '资料不足', '未记录（已核查本批次资料）'].includes(field.value), '缺失值错误');
    if (field.id === 'R-02' && field.status === 'corrected') ensure(field.value === '2026-03-05', '演示报告日期错误');
    if (field.id === 'R-03' && field.status === 'corrected') ensure(['计划', '计划／未见实施记录', '计划，未提供实施记录'].includes(field.value), '演示治疗状态错误');
    return field as unknown as Field;
  };
  const fields = list(state.fields, 6).map(validateField);
  ensure(fields.length === 6 && unique(fields.map(field => field.id)), '字段集不完整');
  ensure(fields.some(field => field.id === state.selectedFieldId) && seed.patients.some(patient => patient.id === state.selectedPatientId), '当前选择引用错误');
  const selectedIds = list(state.selectedFieldIds, 6);
  ensure(unique(selectedIds) && selectedIds.every(id => typeof id === 'string' && seedFields.has(id)), '发布选择错误');
  const snapshots = list(state.snapshots);
  ensure(unique(snapshots.map(item => record(item).id)) && unique(snapshots.map(item => record(item).version)), '快照标识或版本重复');
  snapshots.forEach(item => {
    const snapshot = record(item);
    ensure(text(snapshot.id, 128) && integer(snapshot.version, 1) && ['pending', 'approved', 'invalidated'].includes(String(snapshot.status)) && timestamp(snapshot.createdAt) && text(snapshot.note), '快照格式错误');
    ensure(snapshot.status === 'approved' ? timestamp(snapshot.approvedAt) : snapshot.approvedAt === undefined, '快照审批时间错误');
    const ids = list(snapshot.fieldIds, 6);
    const frozen = list(snapshot.fields, 6).map(validateField);
    ensure(ids.length > 0 && ids.length === frozen.length && unique(ids) && frozen.every((field, index) => field.id === ids[index] && isReviewed(field)), '冻结字段或引用错误');
    frozen.forEach(field => {
      const patient = seed.patients.find(patient => patient.id === field.patientId);
      ensure(patient?.privacyPassed && patient.associationPassed, '冻结字段隐私或关联不通过');
    });
    if (snapshot.status === 'pending') {
      ensure(ids.length === selectedIds.length && ids.every(id => selectedIds.includes(id)), '待审批选择范围不一致');
      ensure(frozen.every(field => JSON.stringify(field) === JSON.stringify(fields.find(current => current.id === field.id))), '待审批字段已变化');
      ensure(frozen.every(field => !fields.some(current => current.patientId === field.patientId && current.critical && !isReviewed(current))), '待审批质量门未通过');
    }
  });
  ensure(snapshots.filter(item => record(item).status === 'pending').length <= 1, '存在多个待审批快照');
  const runs = list(state.runs);
  ensure(unique(runs.map(item => record(item).id)), '任务标识重复');
  ensure(runs.filter(item => record(item).status === 'running').length <= 1, '存在多个运行任务');
  runs.forEach(item => {
    const run = record(item);
    ensure(text(run.id, 128) && ['running', 'completed', 'failed', 'interrupted'].includes(String(run.status)) && integer(run.step, 0, 4) && typeof run.fail === 'boolean' && timestamp(run.startedAt), '任务状态错误');
    ensure(run.status === 'running' ? run.finishedAt === undefined && Number(run.step) < 4 : timestamp(run.finishedAt), '任务时间错误');
    if (run.status === 'completed') ensure(run.step === 4, '任务完成阶段错误');
    if (run.status === 'failed') ensure(run.fail === true && run.step === 2, '任务失败阶段错误');
    if (run.status === 'running' && run.fail) ensure(Number(run.step) < 2, '失败任务不可继续运行');
    if (run.status === 'interrupted') ensure(Number(run.step) < 4, '任务中断阶段错误');
    if (run.retryOf !== undefined) ensure(text(run.retryOf, 128) && run.retryOf !== run.id && runs.some(prior => record(prior).id === run.retryOf && ['failed', 'interrupted'].includes(String(record(prior).status))), '重试任务引用错误');
  });
  ensure(!runs.length || state.intakeDone, '任务缺少演示接入记录');
  const audit = list(state.audit, 100000);
  ensure(unique(audit.map(item => record(item).id)), '审计记录标识重复');
  audit.forEach(item => {
    const entry = record(item);
    ensure(text(entry.id, 128) && timestamp(entry.at) && validRole(entry.role) && text(entry.action, 1000) && text(entry.detail), '审计记录错误');
  });
}

export function loadSession(sessionId: string): { state: DemoState; notice: string | null; blocked: boolean } {
  const initial = createInitialState(sessionId);
  if (!sessionValid(sessionId)) {
    blockedSessions.add(sessionId);
    return { state: initial, notice: corrupt, blocked: true };
  }
  if (blockedSessions.has(sessionId)) return { state: initial, notice: corrupt, blocked: true };
  const inMemory = memorySessions.get(sessionId);
  if (unsavedSessions.has(sessionId) && inMemory) {
    const state = transition(inMemory, { type: 'INTERRUPT_RUNS' });
    memorySessions.set(sessionId, structuredClone(state));
    return { state, notice: unavailable, blocked: false };
  }
  let raw: string | null;
  try { raw = localStorage.getItem(key(sessionId)); }
  catch {
    const cached = memorySessions.get(sessionId);
    const state = cached ? transition(cached, { type: 'INTERRUPT_RUNS' }) : initial;
    memorySessions.set(sessionId, structuredClone(state));
    return { state, notice: unavailable, blocked: false };
  }
  if (raw === null) return { state: initial, notice: null, blocked: false };
  try {
    ensure(raw.length <= 10000000, '记录过大');
    const parsed: unknown = JSON.parse(raw);
    assertState(parsed, sessionId);
    const interrupted = parsed.runs.some(run => run.status === 'running');
    const state = interrupted ? transition(parsed, { type: 'INTERRUPT_RUNS' }) : parsed;
    memorySessions.set(sessionId, structuredClone(state));
    return { state, notice: interrupted ? '上次未结束的模拟任务已标记为中断，可在数据处理页手动重试。' : null, blocked: false };
  } catch {
    blockedSessions.add(sessionId);
    return { state: initial, notice: corrupt, blocked: true };
  }
}
export function saveSession(state: DemoState): string | null {
  if (blockedSessions.has(state.sessionId)) return corrupt;
  try { assertState(state, state.sessionId); }
  catch { return '演示状态校验未通过，未覆盖本地记录。请重置当前会话。'; }
  memorySessions.set(state.sessionId, structuredClone(state));
  try {
    localStorage.setItem(key(state.sessionId), JSON.stringify(state));
    unsavedSessions.delete(state.sessionId);
    return null;
  } catch {
    unsavedSessions.add(state.sessionId);
    return unavailable;
  }
}
export function resetSession(sessionId: string): string | null {
  blockedSessions.delete(sessionId);
  memorySessions.delete(sessionId);
  unsavedSessions.delete(sessionId);
  try { localStorage.removeItem(key(sessionId)); return null; }
  catch {
    memorySessions.set(sessionId, createInitialState(sessionId));
    unsavedSessions.add(sessionId);
    return unavailable;
  }
}
