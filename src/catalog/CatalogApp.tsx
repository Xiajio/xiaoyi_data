import { useCallback, useEffect, useMemo, useState } from 'react';
import { Activity, ArrowRight, Box, Check, ChevronLeft, ChevronRight, CircleAlert, Database, Download, FileText, FlaskConical, Image, LayoutDashboard, Loader2, Moon, RotateCcw, ShieldCheck, SquareStack, Sun, Table2, X } from 'lucide-react';
import { CatalogApi, type AuditItem, type DatasetItem, type EnrichmentItem, type IdeaItem, type OverviewStats, type PairingItem, type ReviewDecision, type RunItem } from './api';
import './catalog.css';

type PageId = 'overview' | 'datasets' | 'review' | 'publish' | 'ops';
type Role = 'reviewer' | 'manager' | 'lead';
const ROLE_LABELS: Record<Role, string> = { reviewer: '数据审核员', manager: '数据管理员', lead: '项目负责人' };
const ROLE_HINT: Record<Role, string> = { reviewer: '可核验整理候选与配对提案', manager: '可冻结快照', lead: '可批准快照' };
const LICENSE_LABELS: Record<string, string> = { permissive: '宽松许可', copyleft: '著佐权', 'research-only': '仅限研究', unknown: '许可未知' };
const KIND_ICON = { text: FileText, table: Table2, image: Image, '3d': Box } as Record<string, typeof FileText>;

function Modalities({ values }: { values: string[] }) {
  if (!values.length) return <span className="badge warn">模态未知</span>;
  return <span className="cat-modalities">{values.map(value => { const Icon = KIND_ICON[value] ?? Database; return <span key={value} className="badge"><Icon size={12} />{value}</span>; })}</span>;
}
function LicenseBadge({ value }: { value: string }) {
  return <span className={`badge ${value === 'permissive' ? 'good' : value === 'unknown' ? 'warn' : ''}`}>{LICENSE_LABELS[value] ?? value}</span>;
}
function StatusBadge({ status }: { status: string }) {
  const tone = { confirmed: 'good', approved: 'good', rejected: 'bad', invalidated: 'bad', pending: 'warn', candidate: 'warn', completed: 'good', failed: 'bad', running: 'warn', interrupted: 'warn' }[status] ?? '';
  const label = { confirmed: '已确认', approved: '已批准', rejected: '已驳回', invalidated: '已失效', pending: '待核验', candidate: '候选', completed: '完成', failed: '失败', running: '运行中', interrupted: '中断' }[status] ?? status;
  return <span className={`badge ${tone}`}>{label}</span>;
}
const dateTime = (value: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false, month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—';

function useApiData<T>(loader: (api: CatalogApi) => Promise<T>, api: CatalogApi, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [epoch, setEpoch] = useState(0);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    loader(api).then(value => { if (!cancelled) { setData(value); setError(''); } })
      .catch((reason: unknown) => { if (!cancelled) setError(reason instanceof Error ? reason.message : '加载失败'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, epoch, ...deps]);
  return { data, error, loading, reload: useCallback(() => setEpoch(value => value + 1), []) };
}

function LoadState({ loading, error, onRetry }: { loading: boolean; error: string; onRetry: () => void }) {
  if (loading) return <div className="empty"><Loader2 size={24} className="cat-spin" /><strong>正在加载…</strong></div>;
  if (error) return <div className="empty"><CircleAlert size={24} /><strong>加载失败</strong><p>{error}</p><button className="btn" onClick={onRetry}>重试</button></div>;
  return null;
}

function OverviewPage({ api, go }: { api: CatalogApi; go: (page: PageId) => void }) {
  const { data, error, loading, reload } = useApiData<OverviewStats>(client => client.overview(), api, []);
  if (loading || error || !data) return <LoadState loading={loading} error={error} onRetry={reload} />;
  const stats = data;
  const cards: { label: string; value: number; hint: string; page?: PageId; alert?: boolean }[] = [
    { label: '数据集', value: stats.datasets, hint: `${stats.evidence} 条证据`, page: 'datasets' },
    { label: '待核验整理候选', value: stats.enrichment_candidates, hint: 'schema / quality / embedding', page: 'review', alert: stats.enrichment_candidates > 0 },
    { label: '待核验配对提案', value: stats.pairings_pending, hint: `${stats.ideas} 张思路卡`, page: 'publish', alert: stats.pairings_pending > 0 },
    { label: '采集任务', value: stats.runs, hint: '运行与审计页可见明细', page: 'ops' },
  ];
  return <>
    <header className="pp-heading"><div><p className="eyebrow">CATALOG / 01</p><h1>工作概览</h1><p className="muted pp-subtitle">数据集目录、整理与配对的实时状态。</p></div></header>
    <div className="pp-stats">{cards.map(card => <button className="pp-stat cat-stat-button" key={card.label} onClick={() => card.page && go(card.page)}><span className="muted">{card.label}</span><div className={card.alert && card.value ? 'pp-stat-alert' : ''}><strong>{card.value}</strong></div><small>{card.hint}</small></button>)}</div>
    <div className="cat-two-col">
      <section className="panel"><div className="pp-panel-head"><div><h2>模态分布</h2><p className="muted">每个数据集按其模态计数，多模态会重复计入</p></div></div><div className="pp-body cat-chip-list">{Object.entries(stats.by_modality).map(([kind, count]) => <span key={kind} className="badge">{kind} · {count}</span>)}</div></section>
      <section className="panel"><div className="pp-panel-head"><div><h2>许可分级</h2><p className="muted">unknown 需要人工核验后才宜进入快照</p></div></div><div className="pp-body cat-chip-list">{Object.entries(stats.by_license_class).map(([kind, count]) => <span key={kind} className="cat-license-row"><LicenseBadge value={kind} /><strong>{count}</strong></span>)}</div></section>
    </div>
    <section className="panel pp-space-top"><div className="pp-panel-head"><div><h2>快照状态</h2></div></div><div className="pp-body cat-chip-list">{Object.keys(stats.snapshots).length ? Object.entries(stats.snapshots).map(([status, count]) => <span key={status} className="cat-license-row"><StatusBadge status={status} /><strong>{count}</strong></span>) : <p className="muted">还没有快照。由智能体提案、人工核验后冻结产生。</p>}</div></section>
  </>;
}

function DatasetDetail({ api, id, onClose }: { api: CatalogApi; id: number; onClose: () => void }) {
  const { data, error, loading, reload } = useApiData(client => client.dataset(id), api, [id]);
  if (loading || error || !data) return <section className="panel pp-space-top"><LoadState loading={loading} error={error} onRetry={reload} /></section>;
  const { dataset, evidence, enrichments, versions } = data;
  return <section className="panel pp-space-top" data-testid="dataset-detail">
    <div className="pp-panel-head"><div><h2>{dataset.source_key}</h2><p className="muted">{dataset.url}</p></div><button className="btn quiet" onClick={onClose} aria-label="关闭详情"><X size={16} /></button></div>
    <div className="pp-body">
      <div className="pp-key-value cat-detail-grid">
        <span>许可</span><strong>{dataset.license || '未标注'}（{LICENSE_LABELS[dataset.license_class]}）</strong>
        <span>规模</span><strong>{dataset.size_rows === null ? '未知' : `${dataset.size_rows} 行`}</strong>
        <span>下载 / 收藏</span><strong>{dataset.downloads} / {dataset.likes}</strong>
        <span>版本</span><strong>{versions.map(v => `#${v.id} ${v.revision.slice(0, 8)}`).join(' · ')}</strong>
        <span>任务类别</span><strong>{dataset.task_categories.join('、') || '未标注'}</strong>
        <span>语言</span><strong>{dataset.languages.join('、') || '未标注'}</strong>
      </div>
      {dataset.missing.length ? <div className="pp-note pp-note-warning pp-space-top"><CircleAlert size={16} /><div>缺失项：{dataset.missing.join('、')}。缺失不代表不满足，核验前请查来源。</div></div> : null}
      <h3 className="pp-space-top">整理候选（{enrichments.length}）</h3>
      {enrichments.map(item => <div key={item.id} className="cat-enrichment"><StatusBadge status={item.status} /><strong>{item.kind}</strong><span className="muted">{item.producer}</span><code>{JSON.stringify(item.value).slice(0, 160)}</code></div>)}
      <h3 className="pp-space-top">证据摘录</h3>
      {evidence.length ? evidence.map(item => <blockquote key={item.id} className="rd-field-evidence cat-evidence"><p className="pp-footnote">{item.kind} · {item.location}</p>{item.excerpt.slice(0, 600) || '（空）'}</blockquote>) : <p className="muted">没有证据记录。</p>}
    </div>
  </section>;
}

function DatasetsPage({ api }: { api: CatalogApi }) {
  const [modality, setModality] = useState('');
  const [licenseClass, setLicenseClass] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<number | null>(null);
  const { data, error, loading, reload } = useApiData(
    client => client.datasets({ modality, licenseClass, q: query, page }), api, [modality, licenseClass, query, page]);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;
  return <>
    <header className="pp-heading"><div><p className="eyebrow">CATALOG / 02</p><h1>数据目录</h1><p className="muted pp-subtitle">按模态、许可与关键词浏览已采集数据集；点击行查看证据与整理候选。</p></div></header>
    <div className="pp-toolbar cat-toolbar">
      <select aria-label="模态筛选" value={modality} onChange={event => { setModality(event.target.value); setPage(1); }}>
        <option value="">全部模态</option><option value="text">text</option><option value="table">table</option><option value="image">image</option><option value="3d">3d</option>
      </select>
      <select aria-label="许可筛选" value={licenseClass} onChange={event => { setLicenseClass(event.target.value); setPage(1); }}>
        <option value="">全部许可</option><option value="permissive">宽松许可</option><option value="copyleft">著佐权</option><option value="research-only">仅限研究</option><option value="unknown">许可未知</option>
      </select>
      <label className="pp-search"><input aria-label="搜索数据集" placeholder="搜索名称、标识、卡片…" value={query} onChange={event => { setQuery(event.target.value); setPage(1); }} /></label>
      <button className="btn quiet" onClick={reload} aria-label="刷新"><RotateCcw size={15} /></button>
    </div>
    {error ? <LoadState loading={false} error={error} onRetry={reload} /> : loading || !data ? <LoadState loading error="" onRetry={reload} /> : <>
      <div className="table-wrap"><table className="pp-table"><thead><tr><th>数据集</th><th>模态</th><th>许可</th><th>规模</th><th>下载</th><th>缺失</th></tr></thead><tbody>
        {data.items.map(item => <tr key={item.id} className="cat-row" data-testid={`dataset-${item.source_key}`} onClick={() => setSelected(item.id)}>
          <td><strong>{item.source_key}</strong><small>{item.task_categories.slice(0, 2).join('、')}</small></td>
          <td><Modalities values={item.modalities} /></td><td><LicenseBadge value={item.license_class} /></td>
          <td>{item.size_rows === null ? <span className="muted">未知</span> : item.size_rows}</td><td>{item.downloads}</td>
          <td>{item.missing.length ? <span className="badge warn">{item.missing.length} 项</span> : <span className="badge good">齐</span>}</td>
        </tr>)}
      </tbody></table>{data.items.length === 0 ? <div className="empty">没有匹配的数据集，请调整筛选。</div> : null}</div>
      <div className="pp-table-footer"><span>共 {data.total} 个 · 第 {data.page} / {pages} 页</span><span className="cat-pager"><button className="btn quiet" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="上一页"><ChevronLeft size={15} /></button><button className="btn quiet" disabled={page >= pages} onClick={() => setPage(page + 1)} aria-label="下一页"><ChevronRight size={15} /></button></span></div>
    </>}
    {selected !== null && <DatasetDetail api={api} id={selected} onClose={() => setSelected(null)} />}
  </>;
}

function ReviewCard({ item, role, acting, onReview }: { item: EnrichmentItem; role: Role; acting: boolean; onReview: (decision: ReviewDecision, note: string) => void }) {
  const [note, setNote] = useState('');
  const value = item.value as { present?: boolean; features?: { name: string }[]; total_examples?: number; level?: string; reasons?: string[] };
  const summary = item.kind === 'schema'
    ? value.present ? `特征 ${value.features?.length ?? 0} 列${value.total_examples ? ` · 共 ${value.total_examples} 行` : ''}` : '卡片未提供结构化 dataset_info'
    : item.kind === 'quality' ? `质量分级 ${value.level} · ${(value.reasons ?? []).join('；')}` : `维度 ${(item.value as { dim?: number }).dim ?? '?'} · 文本哈希已记录`;
  const disabled = role !== 'reviewer' || acting;
  return <article className="panel cat-review-card" data-testid={`enrichment-${item.id}`}>
    <div className="cat-review-head"><div><strong>{item.dataset}</strong><span className="muted"> #{item.dataset_id} · {item.producer}</span></div><span className="badge">{item.kind}</span></div>
    <p className="pp-prose">{summary}</p>
    {item.evidence_ids.length ? <p className="pp-footnote">证据：卡片摘录 #{item.evidence_ids.join('、#')}</p> : <p className="pp-footnote">无证据指针（嵌入派生自卡片）</p>}
    <label className="pp-form-field">核验说明<input value={note} onChange={event => setNote(event.target.value)} placeholder="确认或驳回的依据…" /></label>
    <div className="pp-inline-actions">
      {role !== 'reviewer' ? <p className="pp-permission">请切换「数据审核员」视角核验。</p> : <span />}
      <span className="cat-actions">
        <button className="btn" disabled={disabled} onClick={() => onReview('rejected', note)}><X size={14} />驳回</button>
        <button className="btn primary" disabled={disabled} onClick={() => onReview('confirmed', note)}><Check size={14} />确认</button>
      </span>
    </div>
  </article>;
}

function ReviewPage({ api, role, onToast }: { api: CatalogApi; role: Role; onToast: (text: string) => void }) {
  const [kind, setKind] = useState('');
  const [acting, setActing] = useState(false);
  const { data, error, loading, reload } = useApiData(client => client.enrichmentQueue('candidate', kind), api, [kind]);
  const review = async (item: EnrichmentItem, decision: ReviewDecision, note: string) => {
    setActing(true);
    try {
      await api.reviewEnrichment(item.id, decision, note, role);
      onToast(`已${decision === 'confirmed' ? '确认' : '驳回'} ${item.dataset} 的 ${item.kind} 候选`);
      reload();
    } catch (reason) { onToast(reason instanceof Error ? reason.message : '操作未完成'); } finally { setActing(false); }
  };
  return <>
    <header className="pp-heading"><div><p className="eyebrow">CATALOG / 03</p><h1>整理核验</h1><p className="muted pp-subtitle">自动整理只产出候选；确认或驳回都由人工留痕。</p></div></header>
    <div className="pp-tabs cat-tabs">{[['', '全部'], ['schema', 'schema 摘要'], ['quality', '质量信号'], ['embedding', '嵌入']].map(([id, label]) =>
      <button key={id} className={kind === id ? 'is-active' : ''} onClick={() => setKind(id)}>{label}</button>)}</div>
    <LoadState loading={loading} error={error} onRetry={reload} />
    {data && (data.items.length ? <div className="cat-queue">{data.items.map(item => <ReviewCard key={item.id} item={item} role={role} acting={acting} onReview={(decision, note) => review(item, decision, note)} />)}</div>
      : <div className="empty"><ShieldCheck size={26} /><strong>候选队列已清空</strong><p>新的整理候选会出现在这里。</p></div>)}
  </>;
}

function PublishPage({ api, role, onToast }: { api: CatalogApi; role: Role; onToast: (text: string) => void }) {
  const [acting, setActing] = useState(false);
  const pairings = useApiData(client => client.pairings(), api, []);
  const ideas = useApiData(client => client.ideas(), api, []);
  const reloadAll = () => { pairings.reload(); ideas.reload(); };
  const run = async (action: () => Promise<unknown>, okText: string) => {
    setActing(true);
    try { await action(); onToast(okText); reloadAll(); }
    catch (reason) { onToast(reason instanceof Error ? reason.message : '操作未完成'); }
    finally { setActing(false); }
  };
  const exportSnapshot = async (snapshotId: number) => {
    const data = await api.exportSnapshot(snapshotId);
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `catalog-snapshot-${snapshotId}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const pendingPairings = (pairings.data?.items ?? []).filter(item => item.status === 'pending');
  return <>
    <header className="pp-heading"><div><p className="eyebrow">CATALOG / 04</p><h1>配对与发布</h1><p className="muted pp-subtitle">智能体提案只能待核验；人工确认后冻结快照，批准后导出。</p></div></header>
    <section className="panel"><div className="pp-panel-head"><div><h2>配对提案队列</h2><p className="muted">待核验 {pendingPairings.length} 项 / 共 {pairings.data?.items.length ?? 0} 项</p></div><button className="btn quiet" onClick={reloadAll} aria-label="刷新"><RotateCcw size={15} /></button></div>
      {pairings.error ? <LoadState loading={false} error={pairings.error} onRetry={pairings.reload} /> : pendingPairings.length === 0 ? <div className="empty"><FlaskConical size={24} /><strong>没有待核验提案</strong><p>智能体通过 MCP 的 pairing_propose 提交的提案会进入这里。</p></div> :
        <div className="pp-body cat-pairing-list">{pendingPairings.map(item => <article key={item.id} className="cat-pairing" data-testid={`pairing-${item.id}`}>
          <div className="cat-review-head"><div><strong>{item.dataset}</strong><span className="muted"> · 版本 #{item.dataset_version_id} · {item.idea}</span></div><span className="badge">{item.proposed_by === 'agent' ? '智能体提案' : '人工提案'}</span></div>
          <p className="pp-prose">{item.rationale}</p>
          {item.warnings.length ? <div className="pp-note pp-note-warning"><CircleAlert size={15} /><div>{item.warnings.join('；')}</div></div> : null}
          <div className="pp-inline-actions">
            {role !== 'reviewer' ? <p className="pp-permission">请切换「数据审核员」视角核验。</p> : <span className="pp-footnote">证据指针 {item.evidence_ids.length} 条</span>}
            <span className="cat-actions">
              <button className="btn" disabled={role !== 'reviewer' || acting} onClick={() => run(() => api.reviewPairing(item.id, 'rejected', '', role), `已驳回配对 #${item.id}`)}><X size={14} />驳回</button>
              <button className="btn primary" disabled={role !== 'reviewer' || acting} onClick={() => run(() => api.reviewPairing(item.id, 'confirmed', '', role), `已确认配对 #${item.id}`)}><Check size={14} />确认</button>
            </span>
          </div>
        </article>)}</div>}
    </section>
    <section className="panel pp-space-top"><div className="pp-panel-head"><div><h2>思路卡与快照</h2><p className="muted">冻结取全部已确认配对；已批准快照可导出</p></div></div>
      {ideas.error ? <LoadState loading={false} error={ideas.error} onRetry={ideas.reload} /> : (ideas.data?.items.length ?? 0) === 0 ? <div className="empty"><SquareStack size={24} /><strong>还没有思路卡</strong><p>思路卡由文献挖掘智能体经 pairing_propose 带入。</p></div> :
        <div className="pp-body">{ideas.data!.items.map(idea => { const confirmed = idea.status.pairings.filter(p => p.status === 'confirmed').length; return <div key={idea.id} className="cat-idea" data-testid={`idea-${idea.external_id}`}>
          <div className="cat-review-head"><div><strong>{idea.title}</strong><span className="muted"> · {idea.external_id}</span></div><span className="badge">已确认配对 {confirmed}</span></div>
          {idea.statement ? <p className="muted pp-prose">{idea.statement}</p> : null}
          <div className="cat-snapshots">{idea.status.snapshots.map(snapshot => <span key={snapshot.id} className="cat-snapshot"><StatusBadge status={snapshot.status} /><strong>V{snapshot.version}</strong><span className="muted">{dateTime(snapshot.approved_at)}</span>
            {snapshot.status === 'pending' ? <button className="btn" disabled={role !== 'lead' || acting} title={role !== 'lead' ? '请切换项目负责人视角' : ''} onClick={() => run(() => api.approve(snapshot.id, role), `快照 V${snapshot.version} 已批准`)}>批准</button> : null}
            {snapshot.status === 'approved' ? <button className="btn" disabled={acting} onClick={() => run(() => exportSnapshot(snapshot.id), `快照 V${snapshot.version} 已导出`)}><Download size={14} />导出</button> : null}
          </span>)}</div>
          <div className="pp-inline-actions pp-space-top"><span className="pp-footnote">{idea.source_refs.length ? `来源：${idea.source_refs.join('、')}` : '无来源引用'}</span>
            <button className="btn primary" disabled={role !== 'manager' || acting || confirmed === 0 || idea.status.snapshots.some(s => s.status === 'pending')}
              title={role !== 'manager' ? '请切换数据管理员视角' : confirmed === 0 ? '没有已确认配对' : '已有待审批快照'}
              onClick={() => run(() => api.freeze(idea.external_id, '', role), `已冻结 ${idea.title} 的快照`)}><SquareStack size={14} />冻结快照</button></div>
        </div>; })}</div>}
    </section>
  </>;
}

function OpsPage({ api }: { api: CatalogApi }) {
  const [tab, setTab] = useState<'runs' | 'audit'>('runs');
  const runs = useApiData(client => client.runs(), api, []);
  const audit = useApiData(client => client.audit(), api, []);
  const active = tab === 'runs' ? runs : audit;
  return <>
    <header className="pp-heading"><div><p className="eyebrow">CATALOG / 05</p><h1>运行与审计</h1><p className="muted pp-subtitle">采集任务与操作留痕；本地记录，不具备不可篡改能力。</p></div></header>
    <div className="pp-tabs cat-tabs">{[['runs', '采集任务'], ['audit', '操作审计']].map(([id, label]) => <button key={id} className={tab === id ? 'is-active' : ''} onClick={() => setTab(id as 'runs' | 'audit')}>{label}</button>)}</div>
    <LoadState loading={active.loading} error={active.error} onRetry={active.reload} />
    {tab === 'runs' && runs.data ? <div className="table-wrap"><table><thead><tr><th>任务</th><th>来源</th><th>状态</th><th>统计</th><th>开始</th><th>结束</th></tr></thead><tbody>
      {runs.data.items.map((run: RunItem) => <tr key={run.id}><td className="pp-mono">#{run.id}</td><td>{run.adapter}</td><td><StatusBadge status={run.status} /></td>
        <td><small>{Object.entries(run.stats).filter(([, v]) => typeof v === 'number').map(([k, v]) => `${k}=${v}`).join(' ')}</small></td>
        <td>{dateTime(run.started_at)}</td><td>{dateTime(run.finished_at)}</td></tr>)}
    </tbody></table></div> : null}
    {tab === 'audit' && audit.data ? <div className="table-wrap"><table><thead><tr><th>时间</th><th>角色</th><th>操作</th><th>详情</th></tr></thead><tbody>
      {audit.data.items.map((item: AuditItem) => <tr key={item.id}><td className="pp-nowrap">{dateTime(item.created_at)}</td><td>{item.actor}</td><td>{item.action}</td><td>{item.detail}</td></tr>)}
    </tbody></table></div> : null}
  </>;
}

export function CatalogApp({ api }: { api: CatalogApi }) {
  const [page, setPage] = useState<PageId>('overview');
  const [role, setRole] = useState<Role>('reviewer');
  const [theme, setTheme] = useState<'light' | 'dark'>('light');
  const [toast, setToast] = useState('');
  useEffect(() => { document.documentElement.dataset.theme = theme; document.documentElement.style.colorScheme = theme; }, [theme]);
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(''), 5000); return () => window.clearTimeout(timer); }, [toast]);
  const pages = useMemo(() => [
    { id: 'overview' as PageId, label: '工作概览', icon: LayoutDashboard },
    { id: 'datasets' as PageId, label: '数据目录', icon: Database },
    { id: 'review' as PageId, label: '整理核验', icon: ShieldCheck },
    { id: 'publish' as PageId, label: '配对与发布', icon: SquareStack },
    { id: 'ops' as PageId, label: '运行与审计', icon: Activity },
  ], []);
  return <div className="app-shell" data-testid="catalog-app">
    <aside className="sidebar">
      <div className="brand"><span className="brand-emblem"><Database size={22} /></span><span><strong>小亿数据</strong><small>数据集目录与策展</small></span></div>
      <div className="nav-caption">策展工作区</div>
      <nav>{pages.map(item => { const Icon = item.icon; return <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => setPage(item.id)}><Icon size={18} /><span>{item.label}</span></button>; })}</nav>
      <div className="sidebar-bottom"><div className="local-indicator"><i />目录服务 · {api.baseUrl}</div><p>候选必核验<br/>发布必留痕</p></div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <div className="breadcrumb"><span>数据集目录</span><span className="crumb-slash">/</span><strong>{pages.find(item => item.id === page)?.label}</strong></div>
        <div className="top-actions">
          <label className="role-control"><span>策展角色</span><select aria-label="策展角色" value={role} onChange={event => setRole(event.target.value as Role)}>{Object.entries(ROLE_LABELS).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></label>
          <button className="btn icon" aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'} onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button>
        </div>
      </header>
      <div className="demo-strip"><span className="demo-tag">策展模式</span><span>数据来自目录服务</span><span className="demo-strip-detail">角色仅决定可操作范围：{ROLE_HINT[role]}</span></div>
      <main className="workspace-content">
        {page === 'overview' && <OverviewPage api={api} go={setPage} />}
        {page === 'datasets' && <DatasetsPage api={api} />}
        {page === 'review' && <ReviewPage api={api} role={role} onToast={setToast} />}
        {page === 'publish' && <PublishPage api={api} role={role} onToast={setToast} />}
        {page === 'ops' && <OpsPage api={api} />}
      </main>
      <footer className="workspace-footer"><span>小亿数据 <span className="version">v0.4</span></span><span>catalog-service · REST</span><span>人工核验与审批均在服务端留痕</span></footer>
    </div>
    <div className={`toast ${toast ? 'visible' : ''}`} role="status" aria-live="polite">{toast}</div>
  </div>;
}
