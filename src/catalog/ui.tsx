import { useCallback, useEffect, useState } from 'react';
import { CircleAlert } from 'lucide-react';
import { CatalogApi, type Role } from './api';
export const canReview = (role: Role) => role === 'reviewer' || role === 'admin';
export const canResearch = (role: Role) => role !== 'reader';
export const errorText = (reason: unknown) => reason instanceof Error ? reason.message : '操作未完成，请重试。';
export const dateTime = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '未记录';
export const stageLabel = (value: string) => ({ queued: '等待执行', planning: '规划检索', parsing: '拆解目标', discovering: '发现来源', collecting: '采集版本', enriching: '整理目录', matching: '核对证据与约束', proposing: '生成提案', completed: '已完成', cancelled: '已取消' } as Record<string, string>)[value] || value;
export const splitList = (value: string) => value.split(/[,，\n]/).map(v => v.trim()).filter(Boolean);
export const formatBytes = (value?: number | null) => value == null ? '体积未知' : value < 1024 ? `${value} B` : value < 1024 ** 2 ? `${(value / 1024).toFixed(1)} KB` : value < 1024 ** 3 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${(value / 1024 ** 3).toFixed(1)} GB`;
const STATUS: Record<string, [string, string]> = {
  confirmed: ['已确认', 'good'], approved: ['已批准', 'good'], rejected: ['已驳回', 'bad'], invalidated: ['已失效', 'bad'],
  pending: ['待审批', 'warn'], candidate: ['待核验', 'warn'], completed: ['完成', 'good'], failed: ['失败', 'bad'], running: ['运行中', 'warn'],
  interrupted: ['已中断', 'warn'], queued: ['排队中', ''], cancelled: ['已取消', ''], 'needs-info': ['待补资料', 'warn'], corrected: ['已更正', 'good'],
  waiting_approval: ['等待人工审批', 'warn'], awaiting_approval: ['等待人工审批', 'warn'], historical_unverified: ['历史版本未验证', 'warn'],
};
export function StatusBadge({ status }: { status: string }) { const [label, tone] = STATUS[status] ?? [status, '']; return <span className={`badge ${tone}`}>{label}</span>; }
export function useApiData<T>(loader: (api: CatalogApi) => Promise<T>, api: CatalogApi, deps: unknown[], pollMs = 0) {
  const [data, setData] = useState<T | null>(null); const [error, setError] = useState(''); const [loading, setLoading] = useState(true); const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    let cancelled = false; let inFlight = false;
    const load = async () => { if (inFlight) return; inFlight = true; try { const value = await loader(api); if (!cancelled) { setData(value); setError(''); } } catch (reason) { if (!cancelled) setError(errorText(reason)); } finally { inFlight = false; if (!cancelled) setLoading(false); } };
    setLoading(true); void load(); const timer = pollMs ? window.setInterval(() => { if (document.visibilityState === 'visible') void load(); }, pollMs) : undefined;
    return () => { cancelled = true; window.clearInterval(timer); };
    // Loaders are deliberately represented by the explicit dependency list.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, epoch, pollMs, ...deps]);
  return { data, error, loading, reload: useCallback(() => setEpoch(value => value + 1), []) };
}
export function LoadState({ loading, error, onRetry }: { loading: boolean; error: string; onRetry: () => void }) {
  if (loading) return <div className="cat-loading" aria-label="正在加载"><span /><span /><span /><p className="muted">正在加载…</p></div>;
  if (error) return <div className="empty" role="alert"><CircleAlert size={24} /><strong>加载失败</strong><p>{error}</p><button className="btn" onClick={onRetry}>重试加载</button></div>;
  return null;
}
export function saveBlob(blob: Blob, filename: string) { const url = URL.createObjectURL(blob); const link = document.createElement('a'); link.href = url; link.download = filename; link.click(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); }
export function JsonValue({ value }: { value: unknown }) { return <pre className="cat-json">{JSON.stringify(value, null, 2)}</pre>; }
