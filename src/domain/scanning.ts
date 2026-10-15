import type { Asset, ScanDirectoryId, ScanEntry } from './types';

export const SCAN_DIRECTORIES: { id: ScanDirectoryId; label: string; path: string }[] = [
  { id: 'inbox', label: '科研资料收件目录', path: '\\\\demo-server\\research\\inbox' },
  { id: 'archive', label: '历史资料归档目录', path: '\\\\demo-server\\research\\archive' },
];
export const SCAN_ASSETS: Asset[] = [
  { id: 'F-013', patientId: 'P-DEMO-001', name: '补充记录_001.txt', kind: 'text', format: 'TXT', size: '1 份', date: '2026-03-18', status: 'indexed', detail: '目录扫描新增合成文本；尚未进行模拟处理，不增加结论' },
  { id: 'F-014', patientId: 'P-DEMO-002', name: '调查表_002.csv', kind: 'table', format: 'CSV', size: '1 行', date: '2026-04-15', status: 'indexed', detail: '目录扫描新增合成表格；对象脱敏检查状态仍保留' },
  { id: 'F-015', patientId: 'P-DEMO-003', name: '样本C_三维模型02.glb', kind: '3d', format: 'GLB', size: '1 个', date: '2026-02-18', status: 'indexed', detail: '目录扫描新增三维模型目录；无真实文件，仅演示基础建档' },
  { id: 'F-016', patientId: 'P-DEMO-004', name: '图像集E004_目录', kind: 'image', format: '图像序列', size: '1 批 / 2 组', date: '2026-05-16', status: 'indexed', detail: '目录扫描新增图像集目录；无真实文件，不进行图像识别' },
];
/** Fixed synthetic entries; this function never touches a filesystem or server. */
export function getScanCandidates(directoryId: ScanDirectoryId, recursive: boolean): ScanEntry[] {
  const directory = SCAN_DIRECTORIES.find(item => item.id === directoryId);
  if (!directory || typeof recursive !== 'boolean') throw new Error('不支持的模拟扫描目录配置。');
  const assets = directoryId === 'archive' ? SCAN_ASSETS.slice(2) : recursive ? SCAN_ASSETS : SCAN_ASSETS.slice(0, 2);
  const entries: ScanEntry[] = assets.map(asset => ({
    path: `${directory.path}\\${directoryId === 'inbox' && ['3d', 'image'].includes(asset.kind) ? 'media\\' : ''}${asset.name}`,
    status: 'new', assetId: asset.id, reason: '支持的合成资料，等待确认导入。',
  }));
  entries.push(
    { path: `${directory.path}\\分析报告_001.txt`, status: 'duplicate', assetId: 'F-001', reason: '演示资料库中已有同一资料标识，跳过重复导入。' },
    { path: `${directory.path}\\研究备注.tmp`, status: 'unsupported', reason: '演示支持列表不包含 TMP 格式，保留问题记录。' },
    { path: `${directory.path}\\受限报告.pdf`, status: 'unreadable', reason: '模拟目录访问受限，未读取文件内容。' },
  );
  return entries;
}
