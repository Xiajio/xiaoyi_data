import { describe, expect, it } from 'vitest';
import type { QueueMetrics } from '../src/catalog/api';
import { countWidth, failureSummary, heartbeatSummary, knownCount, queueScale, snapshotFreshness, waitTime } from '../src/catalog/queue-health';

const queue = (values: Partial<QueueMetrics> = {}): QueueMetrics => ({
  queued: 0, waiting: 0, delayed: 0, running: 0, failed: 0, succeeded: 0, cancelled: 0, expired_running: 0,
  oldest_wait_seconds: null, heartbeat: { healthy: false, active_workers: 0, last_seen_at: null }, ...values,
});

describe('queue snapshot visual semantics', () => {
  it('shares one count scale across queues and all three current states', () => {
    const scale = queueScale({ interactive: queue({ waiting: 4 }), io: queue({ delayed: 20 }), cpu: queue({ running: 10 }) });
    expect(scale).toEqual({ maximum: 20, hasWork: true });
    expect(countWidth(4, scale.maximum)).toBe(20);
    expect(countWidth(10, scale.maximum)).toBe(50);
    expect(countWidth(0, scale.maximum)).toBe(0);
  });
  it('keeps an empty snapshot empty and never substitutes missing values with zero', () => {
    expect(queueScale({ interactive: queue(), io: queue(), cpu: queue() })).toEqual({ maximum: 1, hasWork: false });
    expect(knownCount(0)).toBe(0);
    for (const value of [undefined, null, -1, NaN, Infinity, 1.5, '2']) expect(knownCount(value)).toBeNull();
    expect(countWidth(undefined, 10)).toBe(0);
  });
  it('excludes cancelled, queued and running work from the historical failure denominator', () => {
    const result = failureSummary(queue({ failed: 2, succeeded: 6, cancelled: 100, waiting: 300, running: 50 }));
    expect(result).toEqual({ failed: 2, succeeded: 6, finished: 8, rate: '25.0%' });
  });
  it('distinguishes no finished work from unknown failure totals', () => {
    expect(failureSummary(queue()).rate).toBe('暂无完成任务');
    expect(failureSummary().rate).toBe('未返回完整数据');
    expect(failureSummary(queue({ failed: NaN })).finished).toBeNull();
  });
  it('does not equate missing or inconsistent heartbeat data with an online worker', () => {
    expect(heartbeatSummary().text).toBe('心跳信息未知');
    expect(heartbeatSummary(queue()).text).toBe('暂无有效心跳');
    expect(heartbeatSummary(queue({ heartbeat: { healthy: true, active_workers: 2, last_seen_at: '2026-10-18T03:00:00Z' } })).text).toBe('2 个在线');
    expect(heartbeatSummary(queue({ heartbeat: { healthy: true, active_workers: 2, last_seen_at: null } })).tone).toBe('unknown');
    expect(heartbeatSummary(queue({ heartbeat: { healthy: true, active_workers: 0, last_seen_at: null } })).tone).toBe('unknown');
  });
  it('shows no wait only when the API confirms no waiting tasks', () => {
    expect(waitTime(null, 0)).toBe('无等待');
    expect(waitTime(null, 3)).toBe('等待时间未返回');
    expect(waitTime(undefined)).toBe('等待时间未知');
    expect(waitTime(61, 1)).toBe('1 分钟');
    expect(waitTime(5400, 1)).toBe('1.5 小时');
  });
  it('labels stale, failed and unknown snapshots without changing their counts', () => {
    const now = Date.parse('2026-10-18T03:00:00Z');
    expect(snapshotFreshness('2026-10-18T02:59:30Z', now).stale).toBe(false);
    expect(snapshotFreshness('2026-10-18T02:59:14Z', now).notice).toContain('45 秒');
    expect(snapshotFreshness('2026-10-18T03:00:00Z', now, true).notice).toContain('上次成功读取');
    expect(snapshotFreshness(undefined, now).notice).toContain('数据时间未知');
    expect(snapshotFreshness('2026-10-18T03:02:00Z', now).stale).toBe(true);
  });
});
