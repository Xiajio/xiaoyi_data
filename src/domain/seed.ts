import type { DemoState, Field, Patient, Asset } from './types';

export function createInitialState(sessionId: string): DemoState {
  const patients: Patient[] = [
    { id: 'P-DEMO-001', label: '研究对象 001', description: '采集日期与报告签发日期核验', privacyPassed: true, associationPassed: true, events: [
      { date: '2026-03-03', label: '样本 SAMPLE-A 数据采集', detail: '采集时间单独保留，不等于报告签发时间。' },
      { date: '2026-03-05', label: '分析报告签发', detail: '关联 F-001 与登记表 F-003，日期存在冲突。' },
      { date: '2026-03-12', label: '资料归档建档', detail: '虚构阶段总结与数据登记表。' },
    ] },
    { id: 'P-DEMO-002', label: '研究对象 002', description: '任务计划与执行状态区分；脱敏检查待完成', privacyPassed: false, associationPassed: true, events: [
      { date: '2026-04-02', label: '图像采集批次 E002', detail: '图像脱敏核查未完成，仅展示目录。' },
      { date: '2026-04-08', label: '实验记录', detail: '记录计划于 4 月 10 日开始训练任务，没有执行依据。' },
    ] },
    { id: 'P-DEMO-003', label: '研究对象 003', description: '三维模型与样本关联示例', privacyPassed: true, associationPassed: true, events: [
      { date: '2026-02-17', label: '样本 SAMPLE-C 报告与三维模型建档', detail: '采集位置原文已预置核验；模型不包含真实文件。' },
    ] },
    { id: 'P-DEMO-004', label: '研究对象 004', description: '评估资料范围核查；缺失不能推断为通过', privacyPassed: true, associationPassed: true, events: [
      { date: '2026-05-12', label: '评估批次 EV-DEMO-04', detail: '本批次记录未明确给出可用于判定质量结论的结果。' },
      { date: '2026-05-15', label: '评估表格登记', detail: '表格中的“无异常”缺少原文支持，待核验。' },
    ] },
  ];
  const assets: Asset[] = [
    { id: 'F-001', patientId: 'P-DEMO-001', name: '分析报告_001.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-03-05', status: 'indexed', detail: '合成文本已索引，报告日期待核验' },
    { id: 'F-002', patientId: 'P-DEMO-001', name: '阶段总结_001.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-03-12', status: 'indexed', detail: '仅展示合成资料目录' },
    { id: 'F-003', patientId: 'P-DEMO-001', name: '数据登记表_001.csv', kind: 'table', format: 'CSV', size: '1 行', date: '2026-03-12', status: 'indexed', detail: 'Sheet1 / D2 将采集日期填入报告签发日期' },
    { id: 'F-004', patientId: 'P-DEMO-001', name: '样本A_三维模型01.glb', kind: '3d', format: 'GLB', size: '1 个', date: '2026-03-05', status: 'indexed', detail: 'SAMPLE-A 三维模型建档；预览占位，无真实模型文件' },
    { id: 'F-005', patientId: 'P-DEMO-001', name: '图像集E001_目录', kind: 'image', format: '图像序列', size: '1 批 / 2 组', date: '2026-03-01', status: 'indexed', detail: '采集批次 E001 的虚构图像目录' },
    { id: 'F-006', patientId: 'P-DEMO-002', name: '实验记录_002.docx', kind: 'text', format: 'DOCX', size: '1 份', date: '2026-04-08', status: 'indexed', detail: '计划与执行状态待区分；仅虚构文本' },
    { id: 'F-007', patientId: 'P-DEMO-002', name: '数据登记表_002.xlsx', kind: 'table', format: 'XLSX', size: '1 行', date: '2026-04-08', status: 'restricted', detail: '隐藏内容脱敏核查未完成' },
    { id: 'F-008', patientId: 'P-DEMO-002', name: '图像集E002_目录', kind: 'image', format: '图像序列', size: '1 批', date: '2026-04-02', status: 'restricted', detail: '图像脱敏核查未完成，预览受限' },
    { id: 'F-009', patientId: 'P-DEMO-003', name: '分析报告_003.pdf', kind: 'text', format: 'PDF', size: '1 份', date: '2026-02-17', status: 'indexed', detail: '虚构文本，采集位置已预置复核' },
    { id: 'F-010', patientId: 'P-DEMO-003', name: '样本C_三维模型01.glb', kind: '3d', format: 'GLB', size: '1 个', date: '2026-02-17', status: 'indexed', detail: 'SAMPLE-C 三维模型建档，无真实模型文件' },
    { id: 'F-011', patientId: 'P-DEMO-004', name: '评估记录_004.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-05-12', status: 'indexed', detail: '之后评估资料未提供' },
    { id: 'F-012', patientId: 'P-DEMO-004', name: '评估登记表_004.xlsx', kind: 'table', format: 'XLSX', size: '1 行', date: '2026-05-15', status: 'indexed', detail: '原表不等于已确认真值，缺失状态待核验' },
  ];
  const report = '研究编号：P-DEMO-001；样本：SAMPLE-A。\n数据采集日期：2026年03月03日。\n数据来源记载为：实验室内部采集。\n数据格式记载：JSON。\n报告签发日期：2026年03月05日。\n本示例不包含质量结论或效果判定。';
  const fields: Field[] = [
    { id: 'R-01', patientId: 'P-DEMO-001', assetId: 'F-001', label: '数据来源原文', originalValue: '实验室内部采集', value: '实验室内部采集', status: 'pending', critical: false, issue: '原文明确，待人工确认', location: '正文 · 第 3 行', evidence: report, eventTime: '采集 2026-03-03', recordTime: '报告 2026-03-05', reviewNote: '', history: [] },
    { id: 'R-02', patientId: 'P-DEMO-001', assetId: 'F-001', relatedAssetIds: ['F-003'], label: '报告签发日期', originalValue: '2026-03-03', value: '2026-03-03', status: 'pending', critical: true, issue: '登记表把采集日期填入报告签发日期', location: '正文 · 第 5 行；对照 F-003 / Sheet1 / D2', evidence: report + '\n\n对照 F-003：Sheet1 / D2「报告签发日期」为 2026-03-03。原表存在冲突，不是已确认真值。', eventTime: '采集 2026-03-03', recordTime: '报告 2026-03-05', reviewNote: '', history: [] },
    { id: 'R-03', patientId: 'P-DEMO-002', assetId: 'F-006', label: '训练任务状态', originalValue: '已完成', value: '已完成', status: 'pending', critical: true, issue: '候选结果把计划误作已完成', location: '正文 · 第 2—3 行', evidence: '研究编号：P-DEMO-002；任务：TASK-DEMO-02。\n本次记录：计划于2026年04月10日开始训练。\n本批次资料未提供相应执行记录。\n记录日期：2026年04月08日。\n不能据计划文本生成已完成任务记录。', eventTime: '计划 2026-04-10', recordTime: '记录 2026-04-08', reviewNote: '', history: [] },
    { id: 'R-04', patientId: 'P-DEMO-004', assetId: 'F-011', relatedAssetIds: ['F-012'], label: '质量评估结论状态', originalValue: '无异常', value: '无异常', status: 'pending', critical: true, issue: '缺失信息不能变为通过结论', location: '本批次 F-011；对照登记表 F-012', evidence: '研究编号：P-DEMO-004。\n本次评估日期：2026年05月12日。\n未提供此次评估之后的资料。\n本记录未明确记载可用于判定质量结论的结果。\n缺失不能转换为“无异常”。\n\n对照 F-012 评估登记表：Sheet1 / E2「质量评估结论状态」原值为“无异常”。\n该表为待核验来源，本批评估原文没有支持这一结论的明确记载。保留表格原值与缺失说明，不能将其作为已确认真值。', eventTime: '评估 2026-05-12', recordTime: '记录 2026-05-12', reviewNote: '', history: [] },
    { id: 'R-05', patientId: 'P-DEMO-001', assetId: 'F-001', label: '数据格式原文', originalValue: 'JSON', value: 'JSON', status: 'pending', critical: false, issue: '仅确认原文，不生成结论', location: '正文 · 第 4 行', evidence: report, eventTime: '采集 2026-03-03', recordTime: '报告 2026-03-05', reviewNote: '', history: [] },
    { id: 'R-06', patientId: 'P-DEMO-003', assetId: 'F-009', label: '采集位置原文', originalValue: '东区站点', value: '东区站点', status: 'confirmed', critical: false, issue: '演示预置：已核验原文', location: '第 1 页 · 第 2 行', evidence: '研究编号：P-DEMO-003；样本：SAMPLE-C。\n报告明确记载：采集位置为东区站点。\n报告日期：2026年02月17日。\n本演示只核查原文记载，不补充结论或推断。', eventTime: '样本 SAMPLE-C', recordTime: '报告 2026-02-17', reviewNote: '演示预置：已对照虚构报告确认', history: [{ id: 'REVIEW-SEED-01', at: '2026-10-13T01:00:00.000Z', role: 'reviewer', decision: 'confirm', previousValue: '东区站点', value: '东区站点', note: '演示预置：已对照虚构报告确认' }] },
  ];
  return { schemaVersion: 2, sessionId, revision: 1, page: 'processing', theme: 'light', role: 'reviewer', selectedFieldId: 'R-02', selectedPatientId: 'P-DEMO-001', patients, assets, assetProcessing: Object.fromEntries(assets.map(asset => [asset.id, asset.status === 'restricted' ? 'restricted' as const : 'pending' as const])), assetFilter: 'all', scanConfig: { directoryId: 'inbox', recursive: true, autoEnabled: false, intervalSeconds: 30 }, scans: [], fields, selectedFieldIds: fields.map(field => field.id), snapshots: [], runs: [], audit: [
    { id: 'AUDIT-SEED-02', at: '2026-10-13T01:00:00.000Z', role: 'manager', action: '预置候选字段', detail: '6 项合成候选，非模型运行结果；其中 1 项已预置人工核验。' },
    { id: 'AUDIT-SEED-01', at: '2026-10-13T01:00:00.000Z', role: 'manager', action: '载入合成资料目录', detail: '4 名虚构研究对象 / 12 项资料；未读取真实文件。' },
  ], intakeDone: false };
}
