export type Id = number | string;
export type Role = 'reader' | 'researcher' | 'reviewer' | 'admin';
export interface User { id: Id; username: string; role: Role; kind: string }
export interface DatasetItem {
  id: Id; source_key: string; name: string; author: string; url: string;
  license: string; license_class: string; modalities: string[]; modality_source: string;
  task_categories: string[]; languages: string[]; size_rows: number | null;
  downloads: number; likes: number; missing: string[]; dataset_version_id?: Id;
}
export interface EvidenceItem { id: Id; dataset_version_id?: Id; kind: string; location: string; excerpt: string }
export interface VersionItem { id: Id; revision: string; provenance_status?: string; metadata?: Partial<DatasetItem> }
export interface EnrichmentItem {
  id: Id; dataset?: string; dataset_id: Id; dataset_version_id?: Id; kind: string; producer: string;
  status: string; value: Record<string, unknown>; evidence_ids: Id[];
}
export interface PairingItem {
  id: Id; idea: string; dataset: string; dataset_version_id: Id; status: string;
  rationale: string; warnings: string[]; proposed_by: string; evidence_ids: Id[];
}
export interface IdeaItem {
  id: Id; external_id: string; title: string; statement: string;
  requirements: Record<string, unknown>; source_refs: (string | { kind?: string; id?: Id; url?: string })[];
  status: { pairings: { id: Id; status: string }[]; snapshots: SnapshotItem[] };
}
export interface SnapshotItem { id: Id; version: number; status: string; pairing_ids: Id[]; bundle_available?: boolean; approved_at: string | null }
export interface RunItem { id: Id; kind: string; adapter: string; status: string; stats: Record<string, unknown>; error_kind: string; started_at: string | null; finished_at: string | null }
export interface AuditItem { id: Id; actor: string; action: string; detail: string; created_at: string | null }
export interface OverviewStats {
  datasets: number; evidence: number; runs: number; by_license_class: Record<string, number>;
  by_modality: Record<string, number>; enrichment_candidates: number; pairings_pending: number;
  snapshots: Record<string, number>; ideas: number;
}
export interface DashboardCount { key: string; count: number }
export interface DashboardTrendPoint { date: string; datasets: number; versions: number }
export interface CatalogDashboard {
  generated_at: string; timezone: string;
  totals: { datasets: number; versions: number; evidence: number; enrichment_pending: number; pairings_pending: number; downloads_pending: number; assets: number; asset_bytes: number; snapshots_approved: number; bundles: number };
  modalities: DashboardCount[]; licenses: DashboardCount[]; review: { status: string; count: number }[];
  trend: { days: number; start_date: string; end_date: string; points: DashboardTrendPoint[] };
  collection: { jobs_by_status: Record<string, number>; latest_job: Pick<CollectionJob, 'id' | 'status' | 'stage' | 'trigger' | 'created_at' | 'started_at' | 'finished_at' | 'stats' | 'current_source_key' | 'error'> | null; enabled_schedules: number; next_run_at: string | null; next_schedule_name: string | null };
}
export type ReviewDecision = 'confirmed' | 'rejected' | 'needs-info' | 'corrected';
export interface ResearchRequest {
  goal: string; hard_constraints: { modalities?: string[]; task_categories?: string[]; languages?: string[]; license_class?: string[]; min_rows?: number; max_rows?: number };
  preferences: Record<string, unknown>; sources: string[]; max_candidates: number; max_tool_calls: number;
}
export interface AgentRun {
  id: Id; goal: string; status: string; stage?: string; step?: string; actor?: string; model?: string; prompt_version?: string; payload?: ResearchRequest; started_at?: string; finished_at?: string; error?: string; error_kind?: string;
  created_at?: string; updated_at?: string; tool_calls?: number; max_tool_calls?: number;
  candidate_count?: number; usage?: Record<string, unknown>; result?: { candidates?: ResearchCandidate[]; [key: string]: unknown };
}
export interface ResearchCandidate { dataset_version_id: Id; dataset: Partial<DatasetItem>; hard_constraint_gaps: string[]; evidence: { id: Id; location: string; excerpt: string }[]; assessment?: { rationale: string; claims?: { text: string; evidence_ids: Id[]; quotes: string[] }[]; uncertainty?: string[] }; eligible?: boolean; evidence_validation?: string; evidence_errors?: string[] }
export interface AgentEvent { id: Id; kind?: string; step?: string; status?: string; message?: string; detail?: unknown; created_at?: string }
export interface DownloadRequest {
  id: Id; dataset_version_id: Id; status: string; files: string[]; max_bytes: number;
  estimated_bytes?: number; actual_bytes?: number; revision?: string; note?: string; error?: string; created_at?: string;
  license_basis?: unknown; dataset?: string;
}
export interface Asset { id?: Id; path: string; size?: number; size_bytes?: number; sha256: string; format?: string }
export interface AssetReport { id?: Id; kind?: string; report?: Record<string, unknown>; value?: Record<string, unknown>; [key: string]: unknown }
export interface ManualDataset { source_key: string; name: string; url: string; revision: string; license: string; modalities: string[]; task_categories: string[]; languages: string[]; card_text: string; access_conditions: string }
export type AssetPreview = { kind: 'image'; data_url: string; width: number; height: number; sampled: boolean } | { kind: 'table'; rows: Record<string, unknown>[]; sampled: boolean; limit: number } | { kind: 'text'; text: string; sampled: boolean; limit: number } | { kind: '3d' | 'file'; report: Record<string, unknown>; note: string };
export interface CollectionScope { search: string; author: string; languages: string[]; task_categories: string[]; limit: number }
export interface CollectionFailure { source_key?: string; key?: string; error_kind?: string; kind?: string; error?: string; message?: string; [key: string]: unknown }
export interface CollectionStats { found?: number; new?: number; updated?: number; duplicates?: number; skipped_stale?: number; failed?: number; processed?: number; failures?: CollectionFailure[] }
export interface CollectionJob { id: Id; status: 'queued' | 'running' | 'completed' | 'partial' | 'failed' | 'cancelled'; stage: string; scope: CollectionScope; trigger: string; schedule_id?: Id | null; run_id?: Id | null; stats: CollectionStats; cancel_requested?: boolean; current_source_key?: string; error?: string; error_kind?: string; analysis_status?: string; dataset_version_ids: Id[]; attempts: number; created_by?: string; actor?: string; created_at?: string; started_at?: string; finished_at?: string; updated_at?: string }
export interface CollectionScheduleInput { name: string; scope: CollectionScope; interval_minutes: number; enabled: boolean }
export interface CollectionSchedule extends CollectionScheduleInput { id: Id; next_run_at: string | null; last_run_at: string | null; last_job_id: Id | null; created_by: string; created_at?: string; updated_at?: string }
export interface CollectionEvent { id: Id; kind?: string; stage?: string; message?: string; detail?: Record<string, unknown>; created_at?: string }
export interface QaCapabilities { configured: boolean; model: string | null; reason: string; limits: { max_model_calls: number; max_tool_calls: number }; tools: string[] }
export interface QaConversation { id: Id; title: string; actor: string; created_at: string; updated_at: string }
export interface QaSource { id: string; kind: string; title: string; locator: string; content: string; captured_at: string; truncated?: boolean; dataset_id?: Id; dataset_version_id?: Id }
export interface QaCitation { source_id: string; quote: string }
export interface QaTurn { id: Id; conversation_id: Id; question: string; status: 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'; stage: string; answer: { claims: { text: string; citations: QaCitation[] }[]; uncertainty: string[] } | null; sources: QaSource[]; model: string | null; usage: Record<string, number>; model_calls: number; tool_calls: number; error: string | null; created_at: string; finished_at: string | null }
export interface QaConversationDetail extends QaConversation { turns: QaTurn[] }
export class ApiError extends Error {
  constructor(readonly status: number, message: string) { super(message); this.name = 'ApiError'; }
}
export function resolveCatalogBase(search: string, envBase: string | undefined): string | null {
  const params = new URLSearchParams(search);
  if (params.get('mode') === 'demo' || params.get('embed') === '1') return null;
  return params.get('api') || envBase || 'http://127.0.0.1:5280';
}
// crypto.randomUUID 仅在安全上下文（HTTPS/localhost）可用，内网 HTTP 访问需退化到 getRandomValues。
export function requestKey(): string {
  const cryptoApi = globalThis.crypto;
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof cryptoApi?.getRandomValues === 'function') cryptoApi.getRandomValues(bytes);
  else for (let index = 0; index < bytes.length; index += 1) bytes[index] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 0x0f) | 0x40; bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
const pathId = (id: Id) => encodeURIComponent(String(id));
export class CatalogApi {
  constructor(readonly baseUrl: string) { }
  private async response(path: string, init?: RequestInit): Promise<Response> {
    let response: Response;
    try { response = await fetch(this.baseUrl.replace(/\/$/, '') + path, { ...init, credentials: 'include' }); }
    catch { throw new ApiError(0, `无法连接目录服务（${this.baseUrl}），请确认服务已启动。`); }
    if (!response.ok) {
      const detail = await response.json().catch(() => null);
      const message = typeof detail?.detail === 'string' ? detail.detail : Array.isArray(detail?.detail) ? detail.detail.map((item: { msg?: string }) => item.msg).join('；') : `请求失败（${response.status}）`;
      throw new ApiError(response.status, message);
    }
    return response;
  }
  private async request<T>(path: string, init?: RequestInit): Promise<T> { const response = await this.response(path, init); return response.status === 204 ? undefined as T : response.json() as Promise<T>; }
  private post<T>(path: string, body: unknown = {}): Promise<T> { return this.request<T>(path, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }); }
  me() { return this.request<User>('/api/auth/me'); }
  login(username: string, password: string) { return this.post<User>('/api/auth/login', { username, password }); }
  logout() { return this.post<void>('/api/auth/logout'); }
  overview() { return this.request<OverviewStats>('/api/overview'); }
  dashboard(days: 14 | 30 = 14) { return this.request<CatalogDashboard>(`/api/overview/dashboard?days=${days}`); }
  qaCapabilities() { return this.request<QaCapabilities>('/api/qa-capabilities'); }
  qaConversations() { return this.request<{ items: QaConversation[] }>('/api/qa-conversations'); }
  createQaConversation(title?: string) { return this.post<QaConversation>('/api/qa-conversations', title ? { title } : {}); }
  qaConversation(id: Id) { return this.request<QaConversationDetail>(`/api/qa-conversations/${pathId(id)}`); }
  askQa(id: Id, question: string, requestKey: string) { return this.post<QaTurn>(`/api/qa-conversations/${pathId(id)}/turns`, { question, request_key: requestKey }); }
  cancelQaTurn(id: Id) { return this.post<QaTurn>(`/api/qa-turns/${pathId(id)}/cancel`); }
  retryQaTurn(id: Id) { return this.post<QaTurn>(`/api/qa-turns/${pathId(id)}/retry`); }
  datasets(filter: { modality?: string; licenseClass?: string; q?: string; page?: number; size?: number }) {
    const params = new URLSearchParams();
    if (filter.modality) params.set('modality', filter.modality);
    if (filter.licenseClass) params.set('license_class', filter.licenseClass);
    if (filter.q) params.set('q', filter.q);
    params.set('page', String(filter.page ?? 1)); params.set('size', String(filter.size ?? 20));
    return this.request<{ total: number; page: number; size: number; items: DatasetItem[] }>(`/api/datasets?${params}`);
  }
  dataset(id: Id) { return this.request<{ dataset: DatasetItem; evidence: EvidenceItem[]; enrichments: EnrichmentItem[]; versions: VersionItem[] }>(`/api/datasets/${pathId(id)}`); }
  registerDataset(data: ManualDataset) { return this.post<{ id: Id; dataset_version_id?: Id }>('/api/datasets/register', data); }
  enrichmentQueue(status = 'candidate', kind = '') { return this.request<{ items: EnrichmentItem[] }>(`/api/review/enrichments?status=${encodeURIComponent(status)}${kind ? `&kind=${encodeURIComponent(kind)}` : ''}`); }
  reviewEnrichment(id: Id, decision: ReviewDecision, note: string, value?: Record<string, unknown>) { return this.post<{ id: Id; status: string }>(`/api/review/enrichments/${pathId(id)}`, { decision, note, ...(value ? { value } : {}) }); }
  pairings(status = '') { return this.request<{ items: PairingItem[] }>(`/api/pairings${status ? `?status=${encodeURIComponent(status)}` : ''}`); }
  reviewPairing(id: Id, decision: 'confirmed' | 'rejected', note: string) { return this.post<{ id: Id; status: string }>(`/api/pairings/${pathId(id)}/review`, { decision, note }); }
  ideas() { return this.request<{ items: IdeaItem[] }>('/api/ideas'); }
  freeze(ideaExternalId: string, note: string) { return this.post<SnapshotItem>('/api/snapshots/freeze', { idea_external_id: ideaExternalId, note }); }
  approve(snapshotId: Id) { return this.post<SnapshotItem>(`/api/snapshots/${pathId(snapshotId)}/approve`); }
  exportSnapshot(snapshotId: Id) { return this.request<Record<string, unknown>>(`/api/snapshots/${pathId(snapshotId)}/export`); }
  async bundle(snapshotId: Id) { return (await this.response(`/api/snapshots/${pathId(snapshotId)}/bundle`)).blob(); }
  runs() { return this.request<{ items: RunItem[] }>('/api/runs'); }
  audit() { return this.request<{ items: AuditItem[] }>('/api/audit'); }
  agentRuns() { return this.request<{ items: AgentRun[] }>('/api/agent-runs'); }
  createAgentRun(data: ResearchRequest) { return this.post<AgentRun>('/api/agent-runs', data); }
  agentEvents(id: Id) { return this.request<{ items: AgentEvent[] }>(`/api/agent-runs/${pathId(id)}/events`); }
  cancelAgentRun(id: Id) { return this.post<AgentRun>(`/api/agent-runs/${pathId(id)}/cancel`); }
  retryAgentRun(id: Id) { return this.post<AgentRun>(`/api/agent-runs/${pathId(id)}/retry`); }
  collectionJobs() { return this.request<{ items: CollectionJob[] }>('/api/collection-jobs'); }
  createCollectionJob(scope: CollectionScope) { return this.post<CollectionJob>('/api/collection-jobs', scope); }
  collectionJob(id: Id) { return this.request<CollectionJob>(`/api/collection-jobs/${pathId(id)}`); }
  collectionEvents(id: Id, afterId: Id = 0) { return this.request<{ items: CollectionEvent[] }>(`/api/collection-jobs/${pathId(id)}/events?after_id=${pathId(afterId)}`); }
  cancelCollectionJob(id: Id) { return this.post<CollectionJob>(`/api/collection-jobs/${pathId(id)}/cancel`); }
  retryCollectionJob(id: Id) { return this.post<CollectionJob>(`/api/collection-jobs/${pathId(id)}/retry`); }
  collectionSchedules() { return this.request<{ items: CollectionSchedule[] }>('/api/collection-schedules'); }
  saveCollectionSchedule(value: CollectionScheduleInput, id?: Id) { return this.post<CollectionSchedule>(id == null ? '/api/collection-schedules' : `/api/collection-schedules/${pathId(id)}`, value); }
  versionEvidence(id: Id) { return this.request<{ dataset_id: Id; dataset_version_id: Id; revision: string; provenance_status: string; items: EvidenceItem[] }>(`/api/dataset-versions/${pathId(id)}/evidence`); }
  agentSummary(datasetVersionId: Id) { return this.post<EnrichmentItem>(`/api/dataset-versions/${pathId(datasetVersionId)}/agent-summary`); }
  downloadRequests() { return this.request<{ items: DownloadRequest[] }>('/api/download-requests'); }
  requestDownload(datasetVersionId: Id, files: string[], maxBytes: number) { return this.post<DownloadRequest>('/api/download-requests', { dataset_version_id: datasetVersionId, files, max_bytes: maxBytes }); }
  decideDownload(id: Id, decision: 'approved' | 'rejected', note: string) { return this.post<DownloadRequest>(`/api/download-requests/${pathId(id)}/decision`, { decision, note }); }
  versionFiles(id: Id) { return this.request<{ items: { path: string; size: number | null }[]; revision: string }>(`/api/dataset-versions/${pathId(id)}/files`); }
  previewAsset(id: Id) { return this.request<AssetPreview>(`/api/assets/${pathId(id)}/preview`); }
  assets(id: Id) { return this.request<{ items: Asset[]; reports: AssetReport[]; overlap?: unknown[] }>(`/api/dataset-versions/${pathId(id)}/assets`); }
}
