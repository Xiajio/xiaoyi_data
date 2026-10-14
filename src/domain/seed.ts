import type { DemoState, Field, Patient, Asset } from './types';

export function createInitialState(sessionId: string): DemoState {
  const patients: Patient[] = [
    { id: 'P-DEMO-001', label: '研究对象 001', description: '部位与报告日期核验', privacyPassed: true, associationPassed: true, events: [
      { date: '2026-03-03', label: '标本 SP-DEMO-A 取材', detail: '取材时间单独保留，不等于报告签发时间。' },
      { date: '2026-03-05', label: '病理报告签发', detail: '关联 F-001 与登记表 F-003，日期存在冲突。' },
      { date: '2026-03-12', label: '出院资料建档', detail: '虚构出院小结与科研登记表。' },
    ] },
    { id: 'P-DEMO-002', label: '研究对象 002', description: '治疗计划与实施状态区分；隐私检查待完成', privacyPassed: false, associationPassed: true, events: [
      { date: '2026-04-02', label: '影像检查 E002', detail: '文字叠加隐私核查未完成，仅展示目录。' },
      { date: '2026-04-08', label: '病程记录', detail: '记录计划于 4 月 10 日开始治疗，没有实施依据。' },
    ] },
    { id: 'P-DEMO-003', label: '研究对象 003', description: '数字切片与标本关联示例', privacyPassed: true, associationPassed: true, events: [
      { date: '2026-02-17', label: '标本 SP-DEMO-C 报告与切片建档', detail: '取材部位原文已预置核验；切片不包含真实图像。' },
    ] },
    { id: 'P-DEMO-004', label: '研究对象 004', description: '随访资料范围核查；缺失不能推断为阴性', privacyPassed: true, associationPassed: true, events: [
      { date: '2026-05-12', label: '随访联系 EV-DEMO-04', detail: '本批次记录未明确给出可用于判定复发状态的结果。' },
      { date: '2026-05-15', label: '随访表格登记', detail: '表格中的“无复发”缺少原文支持，待核验。' },
    ] },
  ];
  const assets: Asset[] = [
    { id: 'F-001', patientId: 'P-DEMO-001', name: '病理报告_001.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-03-05', status: 'indexed', detail: '合成文本已索引，报告日期待核验' },
    { id: 'F-002', patientId: 'P-DEMO-001', name: '出院小结_001.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-03-12', status: 'indexed', detail: '仅展示合成资料目录' },
    { id: 'F-003', patientId: 'P-DEMO-001', name: '科研登记表_001.csv', kind: 'table', format: 'CSV', size: '1 行', date: '2026-03-12', status: 'indexed', detail: 'Sheet1 / D2 将取材日期填入报告日期' },
    { id: 'F-004', patientId: 'P-DEMO-001', name: '标本A_切片01.svs', kind: 'wsi', format: 'SVS', size: '1 张', date: '2026-03-05', status: 'indexed', detail: 'SP-DEMO-A 切片建档；预览占位，无真实图像' },
    { id: 'F-005', patientId: 'P-DEMO-001', name: '检查E001_影像目录', kind: 'image', format: 'DICOM', size: '1 检查 / 2 序列', date: '2026-03-01', status: 'indexed', detail: '检查 E001 的虚构序列目录' },
    { id: 'F-006', patientId: 'P-DEMO-002', name: '病程记录_002.docx', kind: 'text', format: 'DOCX', size: '1 份', date: '2026-04-08', status: 'indexed', detail: '计划与实施状态待区分；仅虚构文本' },
    { id: 'F-007', patientId: 'P-DEMO-002', name: '研究登记表_002.xlsx', kind: 'table', format: 'XLSX', size: '1 行', date: '2026-04-08', status: 'restricted', detail: '隐藏内容隐私核查未完成' },
    { id: 'F-008', patientId: 'P-DEMO-002', name: '检查E002_影像目录', kind: 'image', format: 'DICOM', size: '1 检查', date: '2026-04-02', status: 'restricted', detail: '图像文字叠加隐私核查未完成，预览受限' },
    { id: 'F-009', patientId: 'P-DEMO-003', name: '病理报告_003.pdf', kind: 'text', format: 'PDF', size: '1 份', date: '2026-02-17', status: 'indexed', detail: '虚构文本，取材部位已预置复核' },
    { id: 'F-010', patientId: 'P-DEMO-003', name: '标本C_切片01.svs', kind: 'wsi', format: 'SVS', size: '1 张', date: '2026-02-17', status: 'indexed', detail: 'SP-DEMO-C 切片建档，无真实图像' },
    { id: 'F-011', patientId: 'P-DEMO-004', name: '随访记录_004.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-05-12', status: 'indexed', detail: '之后随访资料未提供' },
    { id: 'F-012', patientId: 'P-DEMO-004', name: '随访登记表_004.xlsx', kind: 'table', format: 'XLSX', size: '1 行', date: '2026-05-15', status: 'indexed', detail: '原表不等于已确认真值，缺失状态待核验' },
  ];
  const pathology = '研究编号：P-DEMO-001；标本：SP-DEMO-A。\n取材日期：2026年03月03日。\n送检信息：原发部位记载为乙状结肠。\n病理诊断记载：腺癌。\n报告签发日期：2026年03月05日。\n本示例不包含分期、治疗建议或疗效判定。';
  const fields: Field[] = [
    { id: 'R-01', patientId: 'P-DEMO-001', assetId: 'F-001', label: '原发部位原文', originalValue: '乙状结肠', value: '乙状结肠', status: 'pending', critical: false, issue: '原文明确，待人工确认', location: '正文 · 第 3 行', evidence: pathology, eventTime: '取材 2026-03-03', recordTime: '报告 2026-03-05', reviewNote: '', history: [] },
    { id: 'R-02', patientId: 'P-DEMO-001', assetId: 'F-001', relatedAssetIds: ['F-003'], label: '病理报告日期', originalValue: '2026-03-03', value: '2026-03-03', status: 'pending', critical: true, issue: '登记表把取材日期填入报告日期', location: '正文 · 第 5 行；对照 F-003 / Sheet1 / D2', evidence: pathology + '\n\n对照 F-003：Sheet1 / D2「病理报告日期」为 2026-03-03。原表存在冲突，不是已确认真值。', eventTime: '取材 2026-03-03', recordTime: '报告 2026-03-05', reviewNote: '', history: [] },
    { id: 'R-03', patientId: 'P-DEMO-002', assetId: 'F-006', label: '治疗事件状态', originalValue: '已实施', value: '已实施', status: 'pending', critical: true, issue: '候选结果把计划误作已实施', location: '正文 · 第 2—3 行', evidence: '研究编号：P-DEMO-002；事件：EV-DEMO-02。\n本次记录：计划于2026年04月10日开始治疗。\n本批次资料未提供相应治疗实施记录。\n记录日期：2026年04月08日。\n不能据计划文本生成已实施治疗事件。', eventTime: '计划 2026-04-10', recordTime: '记录 2026-04-08', reviewNote: '', history: [] },
    { id: 'R-04', patientId: 'P-DEMO-004', assetId: 'F-011', relatedAssetIds: ['F-012'], label: '随访复发信息状态', originalValue: '无复发', value: '无复发', status: 'pending', critical: true, issue: '缺失信息不能变为阴性结局', location: '本批次 F-011；对照登记表 F-012', evidence: '研究编号：P-DEMO-004。\n本次联系日期：2026年05月12日。\n未提供此次联系之后的随访资料。\n本记录未明确记载可用于判定复发状态的结果。\n缺失不能转换为“无复发”。\n\n对照 F-012 随访登记表：Sheet1 / E2「随访复发信息状态」原值为“无复发”。\n该表为待核验来源，本批随访原文没有支持这一阴性结局的明确记载。保留表格原值与缺失说明，不能将其作为已确认真值。', eventTime: '联系 2026-05-12', recordTime: '记录 2026-05-12', reviewNote: '', history: [] },
    { id: 'R-05', patientId: 'P-DEMO-001', assetId: 'F-001', label: '病理诊断原文', originalValue: '腺癌', value: '腺癌', status: 'pending', critical: false, issue: '仅确认原文，不生成分期', location: '正文 · 第 4 行', evidence: pathology, eventTime: '取材 2026-03-03', recordTime: '报告 2026-03-05', reviewNote: '', history: [] },
    { id: 'R-06', patientId: 'P-DEMO-003', assetId: 'F-009', label: '取材部位原文', originalValue: '直肠', value: '直肠', status: 'confirmed', critical: false, issue: '演示预置：已核验原文', location: '第 1 页 · 第 2 行', evidence: '研究编号：P-DEMO-003；标本：SP-DEMO-C。\n报告明确记载：取材部位为直肠。\n报告日期：2026年02月17日。\n本演示只核查原文记载，不补充诊断或分期。', eventTime: '标本 SP-DEMO-C', recordTime: '报告 2026-02-17', reviewNote: '演示预置：已对照虚构报告确认', history: [{ id: 'REVIEW-SEED-01', at: '2026-10-13T01:00:00.000Z', role: 'reviewer', decision: 'confirm', previousValue: '直肠', value: '直肠', note: '演示预置：已对照虚构报告确认' }] },
  ];
  return { schemaVersion: 2, sessionId, revision: 1, page: 'processing', theme: 'light', role: 'reviewer', selectedFieldId: 'R-02', selectedPatientId: 'P-DEMO-001', patients, assets, assetProcessing: Object.fromEntries(assets.map(asset => [asset.id, asset.status === 'restricted' ? 'restricted' as const : 'pending' as const])), assetFilter: 'all', scanConfig: { directoryId: 'inbox', recursive: true, autoEnabled: false, intervalSeconds: 30 }, scans: [], fields, selectedFieldIds: fields.map(field => field.id), snapshots: [], runs: [], audit: [
    { id: 'AUDIT-SEED-02', at: '2026-10-13T01:00:00.000Z', role: 'manager', action: '预置候选字段', detail: '6 项合成候选，非模型运行结果；其中 1 项已预置人工核验。' },
    { id: 'AUDIT-SEED-01', at: '2026-10-13T01:00:00.000Z', role: 'manager', action: '载入合成资料目录', detail: '4 名虚构研究对象 / 12 项资料；未读取真实文件。' },
  ], intakeDone: false };
}
