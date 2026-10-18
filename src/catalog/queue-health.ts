import type { QueueMetrics } from './api';

export const QUEUE_LABELS = { interactive: '交互任务', io: '下载与导入', cpu: '构建与计算' } as const;
export type QueueName = keyof typeof QUEUE_LABELS;
export const QUEUE_NAMES = Object.keys(QUEUE_LABELS) as QueueName[];
export const QUEUE_STATES = [
  { key: 'waiting', label: '待执行' },
  { key: 'delayed', label: '延后' },
  { key: 'running', label: '运行' },
] as const;

export function knownCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}

export function queueScale(queues: Partial<Record<QueueName, QueueMetrics>>) {
  let maximum = 0;
  for (const name of QUEUE_NAMES) {
    for (const { key } of QUEUE_STATES) maximum = Math.max(maximum, knownCount(queues[name]?.[key]) ?? 0);
  }
  return { maximum: Math.max(1, maximum), hasWork: maximum > 0 };
}

export function countWidth(value: unknown, maximum: number): number {
  const count = knownCount(value);
  return count === null || !Number.isFinite(maximum) || maximum <= 0 ? 0 : Math.min(100, count / maximum * 100);
}

export function failureSummary(queue?: QueueMetrics) {
  const failed = knownCount(queue?.failed);
  const succeeded = knownCount(queue?.succeeded);
  const finished = failed === null || succeeded === null ? null : knownCount(failed + succeeded);
  return { failed, succeeded, finished,
    rate: finished === null ? '未返回完整数据' : finished === 0 ? '暂无完成任务' : `${(failed! / finished * 100).toFixed(1)}%` };
}

export function heartbeatSummary(queue?: QueueMetrics) {
  const heartbeat = queue?.heartbeat;
  const workers = knownCount(heartbeat?.active_workers);
  const hasRecordedTime = typeof heartbeat?.last_seen_at === 'string' && Number.isFinite(Date.parse(heartbeat.last_seen_at));
  if (heartbeat?.healthy === true && workers !== null && workers > 0 && hasRecordedTime) return { tone: 'good', text: `${workers} 个在线` };
  if (heartbeat?.healthy === false) return { tone: 'bad', text: '暂无有效心跳' };
  return { tone: 'unknown', text: '心跳信息未知' };
}

export function waitTime(seconds: number | null | undefined, waiting?: number) {
  if (seconds === null) return waiting === 0 ? '无等待' : '等待时间未返回';
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '等待时间未知';
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${(seconds / 3600).toFixed(1)} 小时`;
}

export function snapshotFreshness(asOf?: string, now = Date.now(), readFailed = false) {
  if (readFailed) return { stale: true, notice: '当前显示上次成功读取的结果，心跳状态以该次快照为准。' };
  const timestamp = asOf ? Date.parse(asOf) : NaN;
  if (!Number.isFinite(timestamp)) return { stale: true, notice: '数据时间未知，无法确认这次快照是否仍有效。' };
  if (timestamp > now + 60_000) return { stale: true, notice: '数据时间超前，请检查服务器时钟；心跳状态以该次快照为准。' };
  if (now - timestamp > 45_000) return { stale: true, notice: '快照已超过 45 秒未更新，心跳状态以该次快照为准。' };
  return { stale: false, notice: '' };
}
