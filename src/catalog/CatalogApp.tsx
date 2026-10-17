import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { Activity, ArrowRight, Box, Check, ChevronLeft, ChevronRight, Database, Download, FileText, FlaskConical, Image, LayoutDashboard, MessageSquare, Menu, Moon, RotateCcw, ShieldCheck, SquareStack, Sun, Table2, X, CircleAlert } from 'lucide-react';
import { ApiError, CatalogApi, type AuditItem, type Id, type Role, type User, type RunItem } from './api';
import { canReview, canResearch, dateTime, errorText, LoadState, saveBlob, StatusBadge, useApiData } from './ui';
import { DatasetDetail } from './DatasetDetail';
import { ReviewPage } from './ReviewPage';
import { ResearchPage } from './ResearchPage';
import { QaPanel } from './QaPanel';
import { HfCollectionPage } from './HfCollectionPage';
import { OverviewPage, type DatasetFilter } from './OverviewPage';
import { DownloadsPage } from './Assets';
import { ManualRegister } from './ManualRegister';
import '../components/pages.css';
import './catalog.css';

type PageId = 'overview' | 'collection' | 'research' | 'datasets' | 'review' | 'downloads' | 'publish' | 'ops';
const ROLE_LABELS: Record<Role, string> = { reader: '只读成员', researcher: '研究员', reviewer: '审核员', admin: '管理员' };
const LICENSE_LABELS: Record<string, string> = { permissive: '宽松许可', copyleft: '著佐权', 'research-only': '仅限研究', unknown: '许可未知' };
const KIND_ICON = { text: FileText, table: Table2, image: Image, '3d': Box } as Record<string, typeof FileText>;

function Modalities({ values }: { values: string[] }) {
  if (!values.length) return <span className="badge warn">模态未知</span>;
  return <span className="cat-modalities">{values.map(value => { const Icon = KIND_ICON[value] ?? Database; return <span key={value} className="badge"><Icon size={12} />{value}</span>; })}</span>;
}
function LicenseBadge({ value }: { value: string }) {
  return <span className={`badge ${value === 'permissive' ? 'good' : value === 'unknown' ? 'warn' : ''}`}>{LICENSE_LABELS[value] ?? value}</span>;
}
function DatasetsPage({ api, role, onToast, initialFilter }: { api: CatalogApi; role: Role; onToast: (text: string) => void; initialFilter: DatasetFilter }) {
  const [modality, setModality] = useState(initialFilter.modality || '');
  const [licenseClass, setLicenseClass] = useState(initialFilter.licenseClass || '');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Id | null>(null);
  const { data, error, loading, reload } = useApiData(
    client => client.datasets({ modality, licenseClass, q: query, page }), api, [modality, licenseClass, query, page]);
  const pages = data ? Math.max(1, Math.ceil(data.total / data.size)) : 1;
  return <>
    <header className="pp-heading"><div><h1>数据目录</h1><p className="muted pp-subtitle">按模态、许可与关键词浏览已采集数据集；点击数据集名称查看证据与整理候选。</p></div></header>
    <>{canResearch(role) && <ManualRegister api={api} onSaved={() => { reload(); onToast('数据集已登记，来源与版本已保留。'); }} />}</>
    <div className="pp-toolbar cat-toolbar">
      <select aria-label="模态筛选" value={modality} onChange={event => { setModality(event.target.value); setPage(1); }}>
        <option value="">全部模态</option>{['text', 'table', 'image', '3d', 'audio', 'video', 'document', 'unknown'].map(value => <option value={value} key={value}>{value === 'unknown' ? '模态未标注' : value}</option>)}{modality && !['text', 'table', 'image', '3d', 'audio', 'video', 'document', 'unknown'].includes(modality) && <option value={modality}>{modality}</option>}
      </select>
      <select aria-label="许可筛选" value={licenseClass} onChange={event => { setLicenseClass(event.target.value); setPage(1); }}>
        <option value="">全部许可</option><option value="permissive">宽松许可</option><option value="copyleft">著佐权</option><option value="research-only">仅限研究</option><option value="unknown">许可分类未知</option>{licenseClass && !['permissive', 'copyleft', 'research-only', 'unknown'].includes(licenseClass) && <option value={licenseClass}>{licenseClass}</option>}
      </select>
      <label className="pp-search"><input aria-label="搜索数据集" placeholder="搜索名称、标识、卡片…" value={query} onChange={event => { setQuery(event.target.value); setPage(1); }} /></label>
      <button className="btn quiet" onClick={reload} aria-label="刷新"><RotateCcw size={15} /></button>
    </div>
    {error ? <LoadState loading={false} error={error} onRetry={reload} /> : loading || !data ? <LoadState loading error="" onRetry={reload} /> : <>
      <div className="table-wrap"><table className="pp-table"><thead><tr><th>数据集</th><th>模态</th><th>许可</th><th>规模</th><th>下载</th><th>缺失</th></tr></thead><tbody>
        {data.items.map(item => <tr key={item.id} className="cat-row" data-testid={`dataset-${item.source_key}`} >
          <td><button className="cat-text-button" onClick={() => setSelected(item.id)}>{item.source_key}</button><small>{item.task_categories.slice(0, 2).join('、')}</small></td>
          <td><Modalities values={item.modalities} /></td><td><LicenseBadge value={item.license_class} /></td>
          <td>{item.size_rows === null ? <span className="muted">未知</span> : item.size_rows}</td><td>{item.downloads}</td>
          <td>{item.missing.length ? <span className="badge warn">{item.missing.length} 项</span> : <span className="badge good">齐</span>}</td>
        </tr>)}
      </tbody></table>{data.items.length === 0 ? <div className="empty">没有匹配的数据集，请调整筛选。</div> : null}</div>
      <div className="pp-table-footer"><span>共 {data.total} 个 · 第 {data.page} / {pages} 页</span><span className="cat-pager"><button className="btn quiet" disabled={page <= 1} onClick={() => setPage(page - 1)} aria-label="上一页"><ChevronLeft size={15} /></button><button className="btn quiet" disabled={page >= pages} onClick={() => setPage(page + 1)} aria-label="下一页"><ChevronRight size={15} /></button></span></div>
    </>}
    {selected !== null && <DatasetDetail key={selected} api={api} id={selected} role={role} onToast={onToast} onClose={() => setSelected(null)} />}
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
  const exportSnapshot = async (snapshotId: Id) => {
    const data = await api.exportSnapshot(snapshotId);
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' }));
    const link = document.createElement('a'); link.href = url; link.download = `catalog-snapshot-${snapshotId}.json`; link.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const pendingPairings = (pairings.data?.items ?? []).filter(item => item.status === 'pending');
  return <>
    <header className="pp-heading"><div><h1>配对与发布</h1><p className="muted pp-subtitle">智能体提案只能待核验；人工确认后冻结快照，批准后导出。</p></div></header>
    <section className="panel"><div className="pp-panel-head"><div><h2>配对提案队列</h2><p className="muted">待核验 {pendingPairings.length} 项 / 共 {pairings.data?.items.length ?? 0} 项</p></div><button className="btn quiet" onClick={reloadAll} aria-label="刷新"><RotateCcw size={15} /></button></div>
      {pairings.error ? <LoadState loading={false} error={pairings.error} onRetry={pairings.reload} /> : pendingPairings.length === 0 ? <div className="empty"><FlaskConical size={24} /><strong>没有待核验提案</strong><p>科研任务产生的配对提案会进入这里。</p></div> :
        <div className="pp-body cat-pairing-list">{pendingPairings.map(item => <article key={item.id} className="cat-pairing" data-testid={`pairing-${item.id}`}>
          <div className="cat-review-head"><div><strong>{item.dataset}</strong><span className="muted"> · 版本 #{item.dataset_version_id} · {item.idea}</span></div><span className="badge">{item.proposed_by === 'agent' ? '智能体提案' : '人工提案'}</span></div>
          <p className="pp-prose">{item.rationale}</p>
          {item.warnings.length ? <div className="pp-note pp-note-warning"><CircleAlert size={15} /><div>{item.warnings.join('；')}</div></div> : null}
          <div className="pp-inline-actions">
            {!canReview(role) ? <p className="pp-permission">需审核员或管理员账号核验。</p> : <span className="pp-footnote">证据指针 {item.evidence_ids.length} 条</span>}
            <span className="cat-actions">
              <button className="btn" disabled={!canReview(role) || acting} onClick={() => run(() => api.reviewPairing(item.id, 'rejected', ''), `已驳回配对 #${item.id}`)}><X size={14} />驳回</button>
              <button className="btn primary" disabled={!canReview(role) || acting} onClick={() => run(() => api.reviewPairing(item.id, 'confirmed', ''), `已确认配对 #${item.id}`)}><Check size={14} />确认</button>
            </span>
          </div>
        </article>)}</div>}
    </section>
    <section className="panel pp-space-top"><div className="pp-panel-head"><div><h2>思路卡与快照</h2><p className="muted">研究包需要先批准并完成下载，再冻结快照。仅含目录的历史快照可导出 JSON。</p></div></div>
      {ideas.error ? <LoadState loading={false} error={ideas.error} onRetry={ideas.reload} /> : (ideas.data?.items.length ?? 0) === 0 ? <div className="empty"><SquareStack size={24} /><strong>还没有思路卡</strong><p>创建科研任务后，研究目标及其候选配对会进入这里。</p></div> :
        <div className="pp-body">{ideas.data!.items.map(idea => { const confirmed = idea.status.pairings.filter(p => p.status === 'confirmed').length; return <div key={idea.id} className="cat-idea" data-testid={`idea-${idea.external_id}`}>
          <div className="cat-review-head"><div><strong>{idea.title}</strong><span className="muted"> · {idea.external_id}</span></div><span className="badge">已确认配对 {confirmed}</span></div>
          {idea.statement ? <p className="muted pp-prose">{idea.statement}</p> : null}
          <div className="cat-snapshots">{idea.status.snapshots.map(snapshot => <span key={snapshot.id} className="cat-snapshot"><StatusBadge status={snapshot.status} /><strong>V{snapshot.version}</strong><span className="muted">{dateTime(snapshot.approved_at)}</span>
            {snapshot.status === 'pending' ? <button className="btn" disabled={!canReview(role) || acting} title={!canReview(role) ? '需审核员或管理员批准' : ''} onClick={() => run(() => api.approve(snapshot.id), `快照 V${snapshot.version} 已批准`)}>批准</button> : null}
            {snapshot.status === 'approved' ? <><button className="btn" disabled={acting} onClick={() => run(() => exportSnapshot(snapshot.id), `快照 V${snapshot.version} 已导出`)}><Download size={14} />导出</button><button className="btn" disabled={acting || snapshot.bundle_available === false} title={snapshot.bundle_available === false ? '此快照仅含目录；完成下载后请重新冻结' : '下载固定版本清单、文件与 Python 加载入口'} onClick={() => run(async () => saveBlob(await api.bundle(snapshot.id), `research-bundle-${snapshot.id}.zip`), `快照 V${snapshot.version} 的研究包已下载`)}>下载研究包</button></> : null}
          </span>)}</div>
          <div className="pp-inline-actions pp-space-top"><span className="pp-footnote">{idea.source_refs.length ? `来源：${idea.source_refs.map(ref => typeof ref === 'string' ? ref : ref.url || `${ref.kind === 'agent_run' ? '科研任务' : ref.kind || '引用'} #${ref.id ?? ''}`).join('、')}` : '无来源引用'}</span>
            <button className="btn primary" disabled={!canReview(role) || acting || confirmed === 0 || idea.status.snapshots.some(s => s.status === 'pending')}
              title={!canReview(role) ? '需审核员或管理员冻结' : confirmed === 0 ? '没有已确认配对' : '已有待审批快照'}
              onClick={() => run(() => api.freeze(idea.external_id, ''), `已冻结 ${idea.title} 的快照`)}><SquareStack size={14} />冻结快照</button></div>
        </div>; })}</div>}
    </section>
  </>;
}

function SummaryBoard({ api, role, go, onToast }: { api: CatalogApi; role: Role; go: (page: PageId) => void; onToast: (text: string) => void }) {
  const board = useApiData(client => client.agentSummaries(), api, [], 5000);
  const [retrying, setRetrying] = useState(false);
  const data = board.data;
  const retry = async (versionId: Id) => {
    setRetrying(true);
    try { await api.agentSummary(versionId); onToast('已重新排队，后台将重试生成'); board.reload(); }
    catch (reason) { onToast(errorText(reason)); }
    finally { setRetrying(false); }
  };
  const waited = (value: string | null) => {
    if (!value) return '时长未知';
    const minutes = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 60000));
    return minutes < 1 ? '不到 1 分钟' : minutes < 60 ? `${minutes} 分钟` : `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分钟`;
  };
  if (!data) return <LoadState loading={board.loading} error={board.error} onRetry={board.reload} />;
  const done = data.counts.candidate + data.counts.confirmed + data.counts.corrected;
  const total = done + data.counts.generating + data.counts.failed;
  const running = data.running ?? [];
  const pct = total > 0 ? Math.round(done / total * 100) : 100;
  return <div className="pp-body" data-testid="summary-board">
    {board.error ? <div className="pp-note pp-note-warning" role="alert"><span>刷新失败：{board.error}。下方保留上次成功读取的数据。</span><button className="btn" onClick={board.reload}>重试</button></div> : null}
    <div className="pp-stats ov-stats ops-board-stats">
      <div className="pp-stat"><span className="muted">排队与处理中</span><div className={data.counts.generating > 0 ? 'pp-stat-alert' : ''}><strong data-testid="summary-generating">{data.counts.generating}</strong><span>条</span></div><small>处理 {data.counts.running ?? 0} 条 · 排队 {data.counts.queued ?? data.counts.generating} 条</small></div>
      <div className="pp-stat"><span className="muted">已生成</span><div><strong>{done}</strong><span>条</span></div><small>其中 {data.counts.candidate} 条待人工核验</small></div>
      <div className="pp-stat"><span className="muted">失败</span><div><strong>{data.counts.failed}</strong><span>条</span></div><small>可在下方重试</small></div>
      <div className="pp-stat"><span className="muted">完成率</span><div><strong>{pct}<span>%</span></strong></div><small>共 {total} 条总结任务</small></div>
    </div>
    <div className="ops-progress" aria-label={`完成率 ${pct}%`}><span style={{ width: `${pct}%` }} /></div>
    {running.map(item => <section className="panel ops-running" data-testid="summary-running" key={item.id}>
      <div className="ops-running-head">{!board.error ? <span className="ops-pulse" aria-hidden="true" /> : null}<div><h3>{board.error ? '上次记录处理' : '正在处理'}：{item.dataset}</h3><p className="muted">版本 #{item.dataset_version_id} · 第 {item.attempts} 次尝试 · 开始于 {dateTime(item.started_at)}</p></div></div>
    </section>)}
    {!running.length && !data.queue.length ? <div className="empty"><ShieldCheck size={24} /><strong>没有正在处理或排队的总结</strong><p>新采集的版本整理完成后会自动排队；也可在核验卡上手动发起。</p></div> : null}
    {!running.length && data.queue.length > 0 ? <p className="pp-note" data-testid="summary-waiting">尚无确认执行中的总结，等待后台处理。</p> : null}
    {data.queue.length > 0 ? <section className="ops-queue" data-testid="summary-queued"><h3>排队中 <span className="badge">{data.counts.queued ?? data.queue.length}</span></h3><ol className="ops-queue-list">
      {data.queue.map(item => <li key={item.id}><span className="ops-pos">#{item.position}</span><strong>{item.dataset}</strong><span className="muted">版本 #{item.dataset_version_id}{item.attempts > 0 ? ` · 已尝试 ${item.attempts} 次` : ''} · 已等待 {waited(item.created_at)}{item.error ? ` · 上次失败：${item.error}` : ''}</span></li>)}
    </ol>{data.counts.queued > data.queue.length ? <p className="muted">展示前 {data.queue.length} 条排队任务。</p> : null}</section> : null}
    <section className="ops-recent"><h3>最近完成 <span className="badge">{data.recent.length}</span></h3>
      {data.recent.length ? <div className="table-wrap"><table><thead><tr><th>数据集</th><th>状态</th><th>依据</th><th>模型</th><th>完成时间</th><th></th></tr></thead><tbody>
        {data.recent.map(item => <tr key={item.id} data-testid={`summary-recent-${item.id}`}><td><strong>{item.dataset}</strong><small>版本 #{item.dataset_version_id}</small></td>
          <td><StatusBadge status={item.status} /></td>
          <td><span className="badge good">{item.claims} 条引用</span>{item.demoted > 0 ? <span className="badge warn">{item.demoted} 条降级</span> : null}</td>
          <td><small>{item.model}{item.tokens ? ` · ${item.tokens} tok` : ''}</small></td>
          <td className="pp-nowrap">{dateTime(item.updated_at)}</td>
          <td><button className="btn quiet" onClick={() => go('review')}>去核验</button></td></tr>)}
      </tbody></table></div> : <p className="muted">还没有完成的总结。</p>}
    </section>
    {data.failed.length ? <section className="ops-failed"><h3>失败 <span className="badge bad">{data.failed.length}</span></h3><ol className="ops-queue-list">
      {data.failed.map(item => <li key={item.id} data-testid={`summary-failed-${item.id}`}><span className="ops-pos">✕</span><strong>{item.dataset}</strong><span className="muted">版本 #{item.dataset_version_id} · {item.attempts} 次尝试 · {item.error}</span><button className="btn" disabled={!canResearch(role) || retrying} title={!canResearch(role) ? '需要研究员及以上角色' : '重新排队生成'} onClick={() => void retry(item.dataset_version_id)}><RotateCcw size={13} />重试</button></li>)}
    </ol></section> : null}
    <p className="pp-footnote">状态更新于 {dateTime(data.as_of)}，每 5 秒刷新；仅后台已开始且状态仍有效的任务显示处理中，意外断开约 2 分钟后转回排队。失败最多尝试 3 次；生成结果经人工核验后才生效。</p>
  </div>;
}

function OpsPage({ api, role, go, onToast }: { api: CatalogApi; role: Role; go: (page: PageId) => void; onToast: (text: string) => void }) {
  const [tab, setTab] = useState<'summaries' | 'runs' | 'audit'>('summaries');
  const runs = useApiData(client => client.runs(), api, []);
  const audit = useApiData(client => client.audit(), api, []);
  const active = tab === 'runs' ? runs : audit;
  return <>
    <header className="pp-heading"><div><h1>运行与审计</h1><p className="muted pp-subtitle">智能体总结进度、采集任务与操作留痕；本地记录，不具备不可篡改能力。</p></div></header>
    <div className="pp-tabs cat-tabs">{[['summaries', '智能体总结'], ['runs', '采集任务'], ['audit', '操作审计']].map(([id, label]) => <button key={id} className={tab === id ? 'is-active' : ''} onClick={() => setTab(id as 'summaries' | 'runs' | 'audit')}>{label}</button>)}</div>
    {tab === 'summaries' ? <SummaryBoard api={api} role={role} go={go} onToast={onToast} /> : <>
    <LoadState loading={active.loading} error={active.error} onRetry={active.reload} />
    {tab === 'runs' && runs.data ? <div className="table-wrap"><table><thead><tr><th>任务</th><th>来源</th><th>状态</th><th>统计</th><th>开始</th><th>结束</th></tr></thead><tbody>
      {runs.data.items.map((run: RunItem) => <tr key={run.id}><td className="pp-mono">#{run.id}</td><td>{run.adapter}</td><td><StatusBadge status={run.status} /></td>
        <td><small>{Object.entries(run.stats).filter(([, v]) => typeof v === 'number').map(([k, v]) => `${k}=${v}`).join(' ')}</small></td>
        <td>{dateTime(run.started_at)}</td><td>{dateTime(run.finished_at)}</td></tr>)}
    </tbody></table></div> : null}
    {tab === 'audit' && audit.data ? <div className="table-wrap"><table><thead><tr><th>时间</th><th>角色</th><th>操作</th><th>详情</th></tr></thead><tbody>
      {audit.data.items.map((item: AuditItem) => <tr key={item.id}><td className="pp-nowrap">{dateTime(item.created_at)}</td><td>{item.actor}</td><td>{item.action}</td><td>{item.detail}</td></tr>)}
    </tbody></table></div> : null}
    </>}
  </>;
}

function Login({ api, onLogin }: { api: CatalogApi; onLogin: (user: User) => void }) {
  const [username, setUsername] = useState(''); const [password, setPassword] = useState(''); const [error, setError] = useState(''); const [acting, setActing] = useState(false);
  const submit = async (event: FormEvent) => { event.preventDefault(); setActing(true); setError(''); try { await api.login(username, password); const user = await api.me(); onLogin(user); } catch (reason) { setError(errorText(reason)); } finally { setActing(false); } };
  return <main className="connection-screen"><div className="brand"><span className="brand-emblem"><Database size={22} /></span><strong>小亿数据</strong></div><form className="panel cat-login cat-form" onSubmit={submit}><h1>登录科研工作区</h1><p className="muted">使用团队账号查看目录、发起科研任务与核验数据。</p><label className="pp-form-field">账号<input required autoComplete="username" value={username} onChange={e => setUsername(e.target.value)} /></label><label className="pp-form-field">密码<input required type="password" autoComplete="current-password" value={password} onChange={e => setPassword(e.target.value)} /></label>{error && <p className="cat-error" role="alert">{error}</p>}<button className="btn primary" disabled={acting}>{acting ? '正在登录…' : '登录工作区'}</button></form><a href="?mode=demo">查看合成数据演示</a></main>;
}
export function CatalogApp({ api }: { api: CatalogApi }) {
  const [directoryFilter, setDirectoryFilter] = useState<DatasetFilter>({});
  const [page, setPage] = useState<PageId>('overview'); const [user, setUser] = useState<User | null>(null); const [checking, setChecking] = useState(true); const [authError, setAuthError] = useState(''); const [authEpoch, setAuthEpoch] = useState(0);
  const [theme, setTheme] = useState<'light' | 'dark'>('light'); const [toast, setToast] = useState(''); const [navOpen, setNavOpen] = useState(false);
  const [qaOpen, setQaOpen] = useState(false); const [qaMounted, setQaMounted] = useState(false);
  useEffect(() => { let cancelled = false; setChecking(true); api.me().then(value => { if (!cancelled) { setUser(value); setAuthError(''); } }).catch(reason => { if (!cancelled) { setUser(null); setAuthError(reason instanceof ApiError && reason.status === 401 ? '' : errorText(reason)); } }).finally(() => { if (!cancelled) setChecking(false); }); return () => { cancelled = true; }; }, [api, authEpoch]);
  useEffect(() => { document.documentElement.dataset.theme = theme; document.documentElement.style.colorScheme = theme; }, [theme]);
  useEffect(() => { if (!toast) return; const timer = window.setTimeout(() => setToast(''), 5000); return () => window.clearTimeout(timer); }, [toast]);
  const pages = useMemo(() => [
    { id: 'overview' as PageId, label: '工作概览', icon: LayoutDashboard }, { id: 'research' as PageId, label: '科研任务', icon: FlaskConical },
    { id: 'collection' as PageId, label: 'HF 采集', icon: Download }, { id: 'datasets' as PageId, label: '数据目录', icon: Database }, { id: 'review' as PageId, label: '整理核验', icon: ShieldCheck },
    { id: 'downloads' as PageId, label: '下载管理', icon: Download }, { id: 'publish' as PageId, label: '配对与发布', icon: SquareStack }, { id: 'ops' as PageId, label: '运行与审计', icon: Activity },
  ], []);
  const go = (next: PageId) => { if (next === 'datasets') setDirectoryFilter({}); setPage(next); setNavOpen(false); window.scrollTo(0, 0); };
  const openDirectory = (filter: DatasetFilter) => { setDirectoryFilter(filter); setPage('datasets'); setNavOpen(false); window.scrollTo(0, 0); };
  if (checking) return <main className="connection-screen"><LoadState loading error="" onRetry={() => setAuthEpoch(v => v + 1)} /></main>;
  if (authError) return <main className="connection-screen"><h1>无法连接目录服务</h1><p>{authError}</p><button className="btn primary" onClick={() => setAuthEpoch(v => v + 1)}>重新连接</button><a href="?mode=demo">查看合成数据演示</a></main>;
  if (!user) return <Login api={api} onLogin={setUser} />;
  return <div className="app-shell cat-app" data-testid="catalog-app"><a className="skip-link" href="#catalog-content">跳转到主要内容</a>
    <aside className={`sidebar ${navOpen ? 'is-open' : ''}`} id="catalog-navigation"><div className="brand"><span className="brand-emblem"><Database size={22} /></span><span><strong>小亿数据</strong><small>科研数据工作区</small></span></div><div className="nav-caption">科研工作区</div>
      <nav aria-label="科研工作区">{pages.map(item => { const Icon = item.icon; return <button key={item.id} className={`nav-item ${page === item.id ? 'active' : ''}`} aria-current={page === item.id ? 'page' : undefined} onClick={() => go(item.id)}><Icon size={18} /><span>{item.label}</span></button>; })}</nav>
      <div className="sidebar-bottom"><div className="local-indicator"><i />团队目录服务</div><p>候选有证据<br />数据有版本</p><a href="?mode=demo">打开合成演示 <ArrowRight size={13} /></a></div></aside>
    <div className="main-shell"><header className="topbar"><div className="breadcrumb"><button className="btn icon nav-toggle" aria-label="切换导航" aria-expanded={navOpen} aria-controls="catalog-navigation" onClick={() => setNavOpen(!navOpen)}><Menu size={18} /></button><span>科研数据</span><span className="crumb-slash">/</span><strong>{pages.find(item => item.id === page)?.label}</strong></div><div className="top-actions"><button className="btn quiet cat-qa-toggle" aria-label="数据问答" aria-expanded={qaOpen} aria-controls="catalog-qa-panel" onClick={() => { setQaMounted(true); setQaOpen(value => !value); }}><MessageSquare size={15} /><span>数据问答</span></button><span className="cat-identity">{user.username}<span className="badge">{ROLE_LABELS[user.role]}</span></span><button className="btn quiet" onClick={async () => { try { await api.logout(); setUser(null); setPage('overview'); } catch (reason) { setToast(errorText(reason)); } }}>退出登录</button><button className="btn icon" aria-label={theme === 'light' ? '切换深色主题' : '切换浅色主题'} onClick={() => setTheme(theme === 'light' ? 'dark' : 'light')}>{theme === 'light' ? <Moon size={17} /> : <Sun size={17} />}</button></div></header>
      <div className="demo-strip"><span className="demo-tag">团队目录</span><span>智能整理 · 人工核验 · 版本留存</span><span className="demo-strip-detail">当前权限由团队账号确定</span></div>
      <main className="workspace-content" id="catalog-content" tabIndex={-1}>{page === 'overview' && <OverviewPage api={api} go={go} onDirectory={openDirectory} />}{page === 'collection' && <HfCollectionPage api={api} user={user} onToast={setToast} onDirectory={() => go('datasets')} onReview={() => go('review')} />}{page === 'research' && <ResearchPage api={api} role={user.role} onToast={setToast} />}{page === 'datasets' && <DatasetsPage key={JSON.stringify(directoryFilter)} api={api} role={user.role} onToast={setToast} initialFilter={directoryFilter} />}{page === 'review' && <ReviewPage api={api} role={user.role} onToast={setToast} />}{page === 'downloads' && <DownloadsPage api={api} role={user.role} onToast={setToast} />}{page === 'publish' && <PublishPage api={api} role={user.role} onToast={setToast} />}{page === 'ops' && <OpsPage api={api} role={user.role} go={go} onToast={setToast} />}</main>
      <footer className="workspace-footer"><span>小亿数据 · 科研工作区</span><span>人工核验与审批均在服务端留痕</span></footer></div>{qaMounted && <QaPanel api={api} open={qaOpen} onClose={() => setQaOpen(false)} />}<div className={`toast ${toast ? 'visible' : ''}`} role="status" aria-live="polite">{toast}</div>
  </div>;
}
