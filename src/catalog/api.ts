// 策展模式 API 客户端：与 platform/src/xiaoyi_catalog/service.py 的端点一一对应。
// 模式判定：?mode=catalog 或构建期 VITE_CATALOG_API；地址可用 ?api= 覆盖，默认本机 5280。

export interface DatasetItem {
  id: number; source_key: string; name: string; author: string; url: string;
  license: string; license_class: string; modalities: string[]; modality_source: string;
  task_categories: string[]; languages: string[]; size_rows: number | null;
  downloads: number; likes: number; missing: string[];
}
export interface EvidenceItem { id: number; kind: string; location: string; excerpt: string }
export interface EnrichmentItem {
  id: number; dataset?: string; dataset_id: number; kind: string; producer: string;
  status: string; value: Record<string, unknown>; evidence_ids: number[];
}
export interface PairingItem {
  id: number; idea: string; dataset: string; dataset_version_id: number; status: string;
  rationale: string; warnings: string[]; proposed_by: string; evidence_ids: number[];
}
export interface IdeaItem {
  id: number; external_id: string; title: string; statement: string;
  requirements: Record<string, unknown>; source_refs: string[];
  status: { pairings: { id: number; status: string }[]; snapshots: SnapshotItem[] };
}
export interface SnapshotItem { id: number; version: number; status: string; pairing_ids: number[]; approved_at: string | null }
export interface RunItem { id: number; kind: string; adapter: string; status: string; stats: Record<string, unknown>; error_kind: string; started_at: string | null; finished_at: string | null }
export interface AuditItem { id: number; actor: string; action: string; detail: string; created_at: string | null }
export interface OverviewStats {
  datasets: number; evidence: number; runs: number; by_license_class: Record<string, number>;
  by_modality: Record<string, number>; enrichment_candidates: number; pairings_pending: number;
  snapshots: Record<string, number>; ideas: number;
}
export type ReviewDecision = 'confirmed' | 'rejected';

export class ApiError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = 'ApiError'; }
}

export function resolveCatalogBase(search: string, envBase: string | undefined): string | null {
  const params = new URLSearchParams(search);
  const paramApi = params.get('api');
  const modeCatalog = params.get('mode') === 'catalog';
  if (!modeCatalog && !envBase) return null; // 缺省仍是演示模式
  return (paramApi ?? '') || envBase || 'http://127.0.0.1:5280';
}

export class CatalogApi {
  constructor(readonly baseUrl: string) { }

  private async request<T>(path: string, init?: RequestInit): Promise<T> {
    let response: Response;
    try {
      response = await fetch(this.baseUrl + path, init);
    } catch {
      throw new ApiError(0, `无法连接目录服务（${this.baseUrl}），请确认服务已启动。`);
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      const message = typeof detail?.detail === 'string' ? detail.detail : `请求失败（${response.status}）`;
      throw new ApiError(response.status, message);
    }
    return response.json() as Promise<T>;
  }

  private post<T>(path: string, body: unknown): Promise<T> {
    return this.request<T>(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  }

  overview() { return this.request<OverviewStats>('/api/overview'); }
  datasets(filter: { modality?: string; licenseClass?: string; q?: string; page?: number; size?: number }) {
    const params = new URLSearchParams();
    if (filter.modality) params.set('modality', filter.modality);
    if (filter.licenseClass) params.set('license_class', filter.licenseClass);
    if (filter.q) params.set('q', filter.q);
    params.set('page', String(filter.page ?? 1));
    params.set('size', String(filter.size ?? 20));
    return this.request<{ total: number; page: number; size: number; items: DatasetItem[] }>(`/api/datasets?${params}`);
  }
  dataset(id: number) {
    return this.request<{ dataset: DatasetItem; evidence: EvidenceItem[]; enrichments: EnrichmentItem[]; versions: { id: number; revision: string }[] }>(`/api/datasets/${id}`);
  }
  enrichmentQueue(status = 'candidate', kind = '') {
    return this.request<{ items: EnrichmentItem[] }>(`/api/review/enrichments?status=${status}${kind ? `&kind=${kind}` : ''}`);
  }
  reviewEnrichment(id: number, decision: ReviewDecision, note: string, role: string) {
    return this.post<{ id: number; status: string }>(`/api/review/enrichments/${id}`, { decision, note, role });
  }
  pairings(status = '') { return this.request<{ items: PairingItem[] }>(`/api/pairings${status ? `?status=${status}` : ''}`); }
  reviewPairing(id: number, decision: ReviewDecision, note: string, role: string) {
    return this.post<{ id: number; status: string }>(`/api/pairings/${id}/review`, { decision, note, role });
  }
  ideas() { return this.request<{ items: IdeaItem[] }>('/api/ideas'); }
  freeze(ideaExternalId: string, note: string, role: string) {
    return this.post<{ id: number; version: number; status: string }>('/api/snapshots/freeze', { idea_external_id: ideaExternalId, note, role });
  }
  approve(snapshotId: number, role: string) {
    return this.post<{ id: number; status: string; approved_at: string }>(`/api/snapshots/${snapshotId}/approve`, { role });
  }
  exportSnapshot(snapshotId: number) { return this.request<Record<string, unknown>>(`/api/snapshots/${snapshotId}/export`); }
  runs() { return this.request<{ items: RunItem[] }>('/api/runs'); }
  audit() { return this.request<{ items: AuditItem[] }>('/api/audit'); }
}
