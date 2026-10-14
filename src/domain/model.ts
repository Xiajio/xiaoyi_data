import { PAGES, type DemoState, type DemoAction, type DemoSummary, type Field, type Role, type InventoryCounts, type InventorySummary, type AssetKind, type AssetProcessingStatus, type ScanRun } from './types';

import { getScanCandidates, SCAN_ASSETS, SCAN_DIRECTORIES } from './scanning';

const roles: Role[] = ['reviewer', 'manager', 'lead', 'operator'];
const processingStatuses: AssetProcessingStatus[] = ['pending', 'processing', 'processed', 'failed', 'restricted'];
const reviewedStatuses = new Set(['confirmed', 'corrected', 'missing']);
export function isReviewed(field: Field): boolean { return reviewedStatuses.has(field.status); }
function requireRole(state: DemoState, allowed: Role[]) {
  if (!allowed.includes(state.role)) throw new Error('当前演示角色不能执行此操作，请切换相应角色。');
}
function identify(prefix: string) { return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`; }
function log(state: DemoState, action: string, detail: string) {
  state.audit.unshift({ id: identify('AUDIT'), at: new Date().toISOString(), role: state.role, action, detail });
}
function fieldOf(state: DemoState, fieldId: string): Field {
  const field = state.fields.find(item => item.id === fieldId);
  if (!field) throw new Error('未找到对应的演示字段。');
  return field;
}
export function getFieldBlockers(state: DemoState, fieldId: string): string[] {
  const field = fieldOf(state, fieldId);
  const patient = state.patients.find(item => item.id === field.patientId);
  if (!patient) return ['研究对象关联不存在'];
  const blockers: string[] = [];
  if (!isReviewed(field)) blockers.push(field.status === 'needs-info' ? '字段仍待补充资料' : field.status === 'rejected' ? '字段候选已驳回，待重新核验' : '字段未完成人工核验');
  if (!patient.associationPassed) blockers.push('患者与事件关联检查未通过');
  if (!patient.privacyPassed) blockers.push('患者隐私检查未完成');
  if (state.fields.some(item => item.patientId === patient.id && item.critical && !isReviewed(item))) blockers.push('同一患者的关键问题尚未闭环');
  return blockers;
}
export function getSummary(state: DemoState): DemoSummary {
  return { pending: state.fields.filter(field => !isReviewed(field)).length, critical: state.fields.filter(field => field.critical && !isReviewed(field)).length, reviewed: state.fields.filter(isReviewed).length, publishable: state.fields.filter(field => getFieldBlockers(state, field.id).length === 0).length, approved: state.snapshots.filter(snapshot => snapshot.status === 'approved').length, running: state.runs.some(run => run.status === 'running'), inventory: getInventorySummary(state) };
}
function emptyCounts(): InventoryCounts { return { total: 0, pending: 0, processing: 0, processed: 0, failed: 0, restricted: 0 }; }
export function getInventorySummary(state: DemoState): InventorySummary {
  const counts = emptyCounts();
  const byKind = Object.fromEntries(['text', 'table', 'wsi', 'image'].map(kind => [kind, emptyCounts()])) as Record<AssetKind, InventoryCounts>;
  state.assets.forEach(asset => {
    const status = state.assetProcessing[asset.id];
    if (!processingStatuses.includes(status)) throw new Error('资料处理状态不完整。');
    counts.total += 1; counts[status] += 1;
    byKind[asset.kind].total += 1; byKind[asset.kind][status] += 1;
  });
  return { ...counts, byKind };
}
function scheduleNextScan(state: DemoState) {
  if (state.scanConfig.autoEnabled && !state.scans.some(scan => scan.status === 'running')) state.scanConfig.nextScanAt = new Date(Date.now() + state.scanConfig.intervalSeconds * 1000).toISOString();
  else delete state.scanConfig.nextScanAt;
}
export function exportSnapshot(state: DemoState, snapshotId: string): object {
  const snapshot = state.snapshots.find(item => item.id === snapshotId);
  if (!snapshot || snapshot.status !== 'approved') throw new Error('只能导出已批准的演示快照。');
  return structuredClone({ schemaVersion: 1, type: 'synthetic_field_subset', disclaimer: '全部为虚构演示数据，不可用于真实科研或诊疗。', sessionId: state.sessionId, snapshotId: snapshot.id, version: snapshot.version, createdAt: snapshot.createdAt, approvedAt: snapshot.approvedAt, note: snapshot.note, fieldIds: snapshot.fieldIds, fields: snapshot.fields });
}
export function transition(state: DemoState, action: DemoAction): DemoState {
  const next = structuredClone(state);
  switch (action.type) {
    case 'NAVIGATE': {
      if (!PAGES.includes(action.page)) throw new Error('不支持的演示页面。');
      if (action.fieldId) {
        const field = fieldOf(next, action.fieldId);
        next.selectedFieldId = field.id;
        next.selectedPatientId = field.patientId;
      }
      if (action.patientId) {
        if (!next.patients.some(patient => patient.id === action.patientId)) throw new Error('未找到对应的演示研究对象。');
        next.selectedPatientId = action.patientId;
      }
      if (action.assetStatus !== undefined) {
        if (action.assetStatus !== 'all' && !processingStatuses.includes(action.assetStatus)) throw new Error('不支持的资料处理状态筛选。');
        next.assetFilter = action.assetStatus;
      }
      next.page = action.page;
      return next;
    }
    case 'SET_ROLE':
      if (!roles.includes(action.role)) throw new Error('不支持的演示角色。');
      next.role = action.role;
      return next;
    case 'SET_THEME':
      if (action.theme !== 'light' && action.theme !== 'dark') throw new Error('不支持的主题。');
      next.theme = action.theme;
      return next;
    case 'REVIEW': {
      requireRole(next, ['reviewer']);
      const field = fieldOf(next, action.fieldId);
      if (!['confirm', 'correct', 'missing', 'needs-info', 'reject'].includes(action.decision)) throw new Error('不支持的核验操作。');
      const previousValue = field.value;
      const note = action.note?.trim() || '';
      if (action.decision !== 'confirm' && !note) throw new Error('请填写核验说明与原文依据。');
      if (note.length > 2000 || (action.value?.length || 0) > 1000) throw new Error('核验内容过长，请简要记录字段值与依据。');
      if (action.decision === 'confirm') {
        if (field.critical) throw new Error('关键问题不能直接确认，请按原文修正或保留待补资料。');
        if (isReviewed(field)) throw new Error('此字段已完成核验，后续变更请使用修正并留痕。');
        field.status = 'confirmed';
        field.reviewNote = note || '已对照虚构原文，确认候选一致。';
      } else if (action.decision === 'correct') {
        const value = action.value?.trim();
        if (!value) throw new Error('请填写修正后的值。');
        if (field.id === 'R-02' && value !== '2026-03-05') throw new Error('此演示原文的报告日期为 2026-03-05，请按原文修正。');
        if (field.id === 'R-03' && !['计划', '计划／未见实施记录', '计划，未提供实施记录'].includes(value)) throw new Error('此演示仅能确认“计划”，没有治疗实施依据。');
        if (field.id === 'R-04' && !['未记录', '资料不足', '未记录（已核查本批次资料）'].includes(value)) throw new Error('此演示缺少复发状态依据，请保留“未记录”或“资料不足”。');
        field.value = value;
        field.status = field.id === 'R-04' ? 'missing' : 'corrected';
        field.reviewNote = note;
      } else if (action.decision === 'missing') {
        if (field.id !== 'R-04') throw new Error('此演示字段已有明确原文；无法确认时请保留待补资料。');
        field.value = '未记录';
        field.status = 'missing';
        field.reviewNote = note;
      } else {
        field.status = action.decision === 'needs-info' ? 'needs-info' : 'rejected';
        field.reviewNote = note;
      }
      field.history.push({ id: identify('REVIEW'), at: new Date().toISOString(), role: next.role, decision: action.decision, previousValue, value: field.value, note: field.reviewNote });
      next.revision += 1;
      let invalidated = 0;
      next.snapshots.forEach(snapshot => {
        if (snapshot.status === 'pending' && (snapshot.fieldIds.includes(field.id) || (field.critical && snapshot.fields.some(item => item.patientId === field.patientId)))) {
          snapshot.status = 'invalidated';
          invalidated += 1;
        }
      });
      log(next, '字段人工核验', `${field.id} / ${field.label}：${previousValue} → ${field.value}；${field.reviewNote}${invalidated ? `；${invalidated} 个待审批快照已失效` : ''}`);
      return next;
    }
    case 'SELECT_FIELDS': {
      if (!Array.isArray(action.ids) || new Set(action.ids).size !== action.ids.length) throw new Error('字段选择包含重复记录。');
      action.ids.forEach(id => fieldOf(next, id));
      const changed = action.ids.length !== next.selectedFieldIds.length || action.ids.some(id => !next.selectedFieldIds.includes(id));
      next.selectedFieldIds = [...action.ids];
      if (changed) {
        next.snapshots.forEach(snapshot => { if (snapshot.status === 'pending') snapshot.status = 'invalidated'; });
        log(next, '调整发布选择', `${action.ids.length} 项演示字段；待审批快照失效，已批准版本保持不变。`);
      }
      return next;
    }
    case 'FREEZE': {
      requireRole(next, ['manager', 'reviewer']);
      if (!next.selectedFieldIds.length) throw new Error('请至少选择一项可发布字段。');
      const blocked = next.selectedFieldIds.filter(id => getFieldBlockers(next, id).length > 0);
      if (blocked.length) throw new Error(`所选 ${blocked.length} 项字段未通过发布检查，请先核验或调整选择。`);
      if (next.snapshots.some(snapshot => snapshot.status === 'pending')) throw new Error('已有待审批快照，请先完成审批或变更后重新申请。');
      const version = Math.max(0, ...next.snapshots.map(snapshot => snapshot.version)) + 1;
      const snapshot = { id: identify('DEMO-SNAPSHOT'), version, status: 'pending' as const, createdAt: new Date().toISOString(), fieldIds: [...next.selectedFieldIds], fields: next.selectedFieldIds.map(id => structuredClone(fieldOf(next, id))), note: '虚构字段子集；冻结来源、原始候选与核验记录，不构成真实科研数据集。' };
      next.snapshots.unshift(snapshot);
      log(next, '冻结快照并申请演示审批', `${snapshot.id} / V${version} / ${snapshot.fields.length} 项字段`);
      return next;
    }
    case 'APPROVE': {
      requireRole(next, ['lead']);
      const snapshot = next.snapshots.find(item => item.id === action.snapshotId);
      if (!snapshot || snapshot.status !== 'pending') throw new Error('此快照当前不能审批，请检查是否已失效或已批准。');
      if (snapshot.fieldIds.some(id => getFieldBlockers(next, id).length > 0)) throw new Error('当前字段存在发布阻断，不能批准此快照。');
      snapshot.status = 'approved';
      snapshot.approvedAt = new Date().toISOString();
      log(next, '演示发布审批通过', `${snapshot.id} / V${snapshot.version}；仅为模拟审批。`);
      return next;
    }
    case 'EXPORT_SNAPSHOT':
      exportSnapshot(next, action.snapshotId);
      log(next, 'EXPORT', `导出虚构 JSON：${action.snapshotId}；内容来自已批准的冻结版本。`);
      return next;
    case 'INTAKE':
      requireRole(next, ['manager']);
      next.intakeDone = true;
      log(next, '确认合成资料接入', `演示资料库 / ${next.assets.length} 项目录；未读取或上传任何真实文件。`);
      return next;
    case 'START_RUN': {
      requireRole(next, ['manager']);
      if (!next.intakeDone) throw new Error('请先在数据接入页确认合成批次。');
      if (next.runs.some(run => run.status === 'running')) throw new Error('已有演示任务运行中，请等待完成。');
      const prior = action.retryOf ? next.runs.find(run => run.id === action.retryOf) : undefined;
      if (action.retryOf && (!prior || !['failed', 'interrupted'].includes(prior.status))) throw new Error('只有失败或中断的任务可以重试。');
      const assetIds = next.assets.filter(asset => asset.status === 'indexed' && ['pending', 'failed'].includes(next.assetProcessing[asset.id]) && (!prior || prior.legacy || prior.assetIds.includes(asset.id))).map(asset => asset.id);
      if (!assetIds.length) throw new Error('当前没有待处理或可重试的资料，已处理和受限资料不会重复执行。');
      const run = { id: identify('DEMO-RUN'), status: 'running' as const, step: 0, fail: Boolean(action.fail), assetIds, startedAt: new Date().toISOString(), ...(action.retryOf ? { retryOf: action.retryOf } : {}) };
      assetIds.forEach(id => { next.assetProcessing[id] = 'processing'; });
      next.runs.unshift(run);
      log(next, action.retryOf ? '重试演示任务' : '确认处理计划并模拟执行', `${run.id} / ${assetIds.length} 项资料${action.retryOf ? ` / 原任务 ${action.retryOf}` : ''}；输入范围已冻结，不覆盖人工核验结果。`);
      return next;
    }
    case 'ADVANCE_RUN': {
      const run = next.runs.find(item => item.id === action.runId);
      if (!run || run.status !== 'running') throw new Error('此演示任务已结束或不存在。');
      run.step += 1;
      if (run.fail && run.step === 2) {
        run.status = 'failed';
        run.finishedAt = new Date().toISOString();
        run.assetIds.forEach(id => { if (next.assetProcessing[id] === 'processing') next.assetProcessing[id] = 'failed'; });
        log(next, '演示任务失败', `${run.id} / ${run.assetIds.length} 项资料模拟字段整理失败，可重试；人工核验保持不变。`);
      } else if (run.step === 4) {
        run.status = 'completed';
        run.finishedAt = new Date().toISOString();
        run.assetIds.forEach(id => { if (next.assetProcessing[id] === 'processing') next.assetProcessing[id] = 'processed'; });
        log(next, '演示任务完成', `${run.id} / ${run.assetIds.length} 项资料已完成模拟处理；非真实模型或 GPU 执行结果。`);
      }
      return next;
    }
    case 'CONFIGURE_SCAN': {
      requireRole(next, ['manager']);
      const config = action.config;
      if (!config || typeof config !== 'object' || Object.keys(config).some(key => !['directoryId', 'recursive', 'autoEnabled', 'intervalSeconds'].includes(key))) throw new Error('不支持的扫描配置。');
      if (config.directoryId !== undefined && !SCAN_DIRECTORIES.some(directory => directory.id === config.directoryId)) throw new Error('不支持的模拟扫描目录。');
      if (config.recursive !== undefined && typeof config.recursive !== 'boolean') throw new Error('递归扫描配置错误。');
      if (config.autoEnabled !== undefined && typeof config.autoEnabled !== 'boolean') throw new Error('自动扫描配置错误。');
      if (config.intervalSeconds !== undefined && ![15, 30, 60].includes(config.intervalSeconds)) throw new Error('扫描间隔只能选择 15、30 或 60 秒。');
      const active = next.scans.some(scan => scan.status === 'running');
      if (active && (['directoryId', 'recursive', 'intervalSeconds'] as const).some(key => config[key] !== undefined && config[key] !== next.scanConfig[key])) throw new Error('扫描运行中不能修改目录、递归方式或间隔，请先停止扫描。');
      next.scanConfig = { ...next.scanConfig, ...config };
      scheduleNextScan(next);
      log(next, '调整模拟扫描配置', `${next.scanConfig.directoryId} / ${next.scanConfig.recursive ? '含子目录' : '当前目录'} / 自动扫描${next.scanConfig.autoEnabled ? `每 ${next.scanConfig.intervalSeconds} 秒` : '关闭'}；无真实服务器访问。`);
      return next;
    }
    case 'START_SCAN': {
      const trigger = action.trigger ?? 'manual';
      if (!['manual', 'automatic'].includes(trigger)) throw new Error('不支持的扫描触发方式。');
      if (trigger === 'manual') requireRole(next, ['manager']);
      else if (!next.scanConfig.autoEnabled || !next.scanConfig.nextScanAt || Date.parse(next.scanConfig.nextScanAt) > Date.now()) throw new Error('自动扫描尚未启用或尚未到达计划时间。');
      if (next.scans.some(scan => scan.status === 'running')) throw new Error('已有目录扫描运行中，请等待完成或停止。');
      const scan: ScanRun = { id: identify('DEMO-SCAN'), status: 'running', step: 0, trigger, directoryId: next.scanConfig.directoryId, recursive: next.scanConfig.recursive, startedAt: new Date().toISOString(), entries: [], importedAssetIds: [] };
      next.scans.unshift(scan);
      delete next.scanConfig.nextScanAt;
      log(next, trigger === 'automatic' ? '定时模拟目录扫描' : '开始模拟目录扫描', `${scan.id} / ${scan.directoryId}；仅遍历固定合成清单，不访问实际文件系统。`);
      return next;
    }
    case 'ADVANCE_SCAN': {
      const scan = next.scans.find(item => item.id === action.scanId);
      if (!scan || scan.status !== 'running') throw new Error('此目录扫描已结束或不存在。');
      scan.step = (scan.step + 1) as ScanRun['step'];
      if (scan.step === 4) {
        scan.status = 'completed';
        scan.finishedAt = new Date().toISOString();
        scan.entries = getScanCandidates(scan.directoryId, scan.recursive).map(entry => entry.assetId && next.assets.some(asset => asset.id === entry.assetId) ? { ...entry, status: 'duplicate' as const, reason: '演示资料库中已有同一资料标识，跳过重复导入。' } : entry);
        scheduleNextScan(next);
        log(next, '模拟目录扫描完成', `${scan.id} / 扫描 ${scan.entries.length} 项，新增候选 ${scan.entries.filter(entry => entry.status === 'new').length} 项；等待用户确认导入。`);
      }
      return next;
    }
    case 'IMPORT_SCAN': {
      requireRole(next, ['manager']);
      const scan = next.scans.find(item => item.id === action.scanId);
      if (!scan || scan.status !== 'completed') throw new Error('只能导入已完成扫描中的合成资料。');
      const allowedIds = new Set(getScanCandidates(scan.directoryId, scan.recursive).filter(entry => entry.status === 'new').map(entry => entry.assetId));
      const ids = [...new Set(scan.entries.filter(entry => entry.status === 'new' && entry.assetId && allowedIds.has(entry.assetId) && !next.assets.some(asset => asset.id === entry.assetId)).map(entry => entry.assetId!))];
      if (!ids.length) return next;
      ids.forEach(id => {
        const asset = SCAN_ASSETS.find(item => item.id === id);
        if (!asset) throw new Error('扫描结果包含不支持的资料引用。');
        next.assets.push(structuredClone(asset));
        next.assetProcessing[id] = 'pending';
      });
      scan.importedAssetIds = [...new Set([...scan.importedAssetIds, ...ids])];
      next.intakeDone = true;
      log(next, '确认导入扫描资料', `${scan.id} / ${ids.length} 项新增合成资料进入待处理队列；原有核验和已批准版本保持不变。`);
      return next;
    }
    case 'STOP_SCAN': {
      requireRole(next, ['manager']);
      const scan = next.scans.find(item => item.id === action.scanId);
      if (!scan || scan.status !== 'running') throw new Error('此目录扫描当前不在运行。');
      scan.status = 'interrupted'; scan.finishedAt = new Date().toISOString();
      scheduleNextScan(next);
      log(next, '停止模拟目录扫描', `${scan.id} / 未导入任何未完成的扫描结果。`);
      return next;
    }
    case 'INTERRUPT_RUNS': {
      next.runs.filter(run => run.status === 'running').forEach(run => {
        run.status = 'interrupted'; run.finishedAt = new Date().toISOString();
        run.assetIds.forEach(id => { if (next.assetProcessing[id] === 'processing') next.assetProcessing[id] = 'pending'; });
        log(next, '演示任务中断', `${run.id} / 页面关闭或重新载入后终止旧模拟，可手动重试。`);
      });
      next.scans.filter(scan => scan.status === 'running').forEach(scan => {
        scan.status = 'interrupted'; scan.finishedAt = new Date().toISOString();
        log(next, '模拟目录扫描中断', `${scan.id} / 页面关闭、切换会话或刷新后停止。`);
      });
      if (next.scanConfig.autoEnabled) log(next, '暂停自动目录扫描', '会话离开或重新载入后暂停定时扫描，需数据管理员重新启用。');
      next.scanConfig.autoEnabled = false;
      delete next.scanConfig.nextScanAt;
      return next;
    }
    default:
      throw new Error('不支持的演示操作。');
  }
}
