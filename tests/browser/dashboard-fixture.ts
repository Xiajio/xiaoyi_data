import type { CatalogDashboard } from '../../src/catalog/api';
export function dashboardFixture(days = 14, datasets = 60): CatalogDashboard {
  const points = Array.from({ length: days }, (_, index) => { const day = new Date(Date.UTC(2026, 9, 17 - days + index)); return { date: day.toISOString().slice(0, 10), datasets: datasets && index === days - 1 ? 50 : 0, versions: datasets && index === days - 1 ? 55 : 0 }; });
  return {
    generated_at: '2026-10-16T01:00:00Z', timezone: 'Asia/Shanghai',
    totals: { datasets, versions: datasets ? 65 : 0, evidence: datasets ? 92 : 0, enrichment_pending: datasets ? 120 : 0, pairings_pending: 0, downloads_pending: 0, assets: 0, asset_bytes: 0, snapshots_approved: 0, bundles: 0 },
    modalities: [{ key: 'text', count: datasets ? 9 : 0 }, { key: 'table', count: datasets ? 5 : 0 }, { key: 'image', count: datasets ? 2 : 0 }, { key: '3d', count: 0 }, { key: 'audio', count: datasets ? 2 : 0 }, { key: 'video', count: datasets ? 4 : 0 }, { key: 'document', count: datasets ? 2 : 0 }, { key: 'unknown', count: datasets ? 44 : 0 }],
    licenses: [{ key: 'permissive', count: datasets ? 11 : 0 }, { key: 'copyleft', count: datasets ? 1 : 0 }, { key: 'research-only', count: datasets ? 3 : 0 }, { key: 'unknown', count: datasets ? 45 : 0 }],
    review: [{ status: 'candidate', count: datasets ? 118 : 0 }, { status: 'needs-info', count: datasets ? 2 : 0 }, { status: 'confirmed', count: 0 }, { status: 'corrected', count: 0 }, { status: 'rejected', count: 0 }],
    trend: { days, start_date: points[0].date, end_date: points.at(-1)!.date, points },
    collection: { jobs_by_status: { queued: 0, running: 0, completed: datasets ? 1 : 0, partial: 0, failed: datasets ? 1 : 0, cancelled: datasets ? 1 : 0 }, latest_job: datasets ? { id: 3, status: 'completed', stage: 'finished', trigger: 'retry', stats: { found: 50, processed: 50, new: 47, updated: 0, duplicates: 3, failed: 0 }, created_at: '2026-10-16T00:44:00Z', finished_at: '2026-10-16T00:45:00Z' } : null, enabled_schedules: datasets ? 1 : 0, next_run_at: datasets ? '2026-10-17T00:34:00Z' : null, next_schedule_name: datasets ? 'HF 每日目录同步' : null },
  };
}
