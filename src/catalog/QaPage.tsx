import { useEffect, useRef, useState, type FormEvent } from 'react';
import { ArrowUp, Database, MessageSquare, Plus, RotateCcw, Square, X } from 'lucide-react';
import { ApiError, CatalogApi, type Id, type QaCitation, type QaConversationDetail, type QaSource, type QaTurn } from './api';
import { dateTime, errorText, LoadState, StatusBadge, useApiData } from './ui';
import './qa.css';

const suggestions = ['目前收集了多少数据集？', '最近的采集有哪些失败，原因是什么？', '目录里有哪些图像数据集？', '哪些数据集的许可还需要核验？'];
const stages: Record<string, string> = { queued: '等待后台执行', planning: '分析问题', retrieving: '查询本地目录', cancelling: '正在取消回答', querying: '查询本地目录', tools: '查询本地目录', answering: '整理回答与证据', validating: '核对引用', completed: '回答完成', failed: '回答未完成', cancelled: '已取消', finished: '处理结束' };
const isActive = (turn: QaTurn) => turn.status === 'queued' || turn.status === 'running';
function timestamp(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }); }
function SourceDetail({ source, quote, onClose }: { source: QaSource; quote?: string; onClose: () => void }) {
  return <section className="qa-source" aria-label="引用来源详情" tabIndex={-1}>
    <div className="qa-source-head"><h3>{source.title}</h3><button type="button" className="btn icon" aria-label="关闭来源详情" onClick={onClose}><X size={15} /></button></div>
    <dl><div><dt>记录位置</dt><dd>{source.locator}</dd></div><div><dt>取证时间</dt><dd>{timestamp(source.captured_at)}（上海时间）</dd></div>{source.dataset_version_id != null && <div><dt>数据版本</dt><dd>#{source.dataset_version_id}</dd></div>}<div><dt>来源类型</dt><dd>{source.kind}</dd></div></dl>
    {quote && <><h4>回答引用的原文</h4><blockquote>{quote}</blockquote></>}
    <details><summary>展开本次保存的来源记录</summary><pre>{source.content}</pre></details>
    {source.truncated && <p className="pp-footnote">记录较长，当前快照仅保存了截取内容，不能视为完整来源。</p>}<p className="pp-footnote">这是本次回答保存的证据快照，当前目录后续更新不会改写此记录。</p>
  </section>;
}
function Turn({ turn, busy, configured, onAction }: { turn: QaTurn; busy: boolean; configured: boolean; onAction: (turn: QaTurn, retry: boolean) => void }) {
  const [citation, setCitation] = useState<QaCitation | null>(null);
  const source = citation ? turn.sources.find(item => item.id === citation.source_id) : undefined;
  return <article className="qa-turn" data-testid={`qa-turn-${turn.id}`}>
    <div className="qa-question"><span>我的问题</span><p>{turn.question}</p></div>
    <div className="qa-answer-head"><strong><MessageSquare size={15} />目录助手</strong><StatusBadge status={turn.status} /><time dateTime={turn.created_at}>{dateTime(turn.created_at)}</time></div>
    {isActive(turn) && <div className="qa-progress" role="status"><span className="qa-pulse" /><div><strong>{stages[turn.stage] || turn.stage || '等待后台执行'}</strong><p>{turn.stage === 'cancelling' ? '已请求取消，等待当前模型或查询执行结束后安全停止。' : turn.status === 'queued' ? '问题已保存。后台启动后将读取目录记录并回答。' : '正在读取真实记录并整理可核对的回答。'}</p></div><button type="button" className="btn small" disabled={busy || turn.stage === 'cancelling'} onClick={() => onAction(turn, false)}><Square size={11} />{turn.stage === 'cancelling' ? '取消中…' : '取消回答'}</button></div>}
    {turn.status === 'failed' && <div className="qa-failure" role="alert"><strong>本次回答未完成</strong><p>{turn.error || '后台未提供进一步原因，请重试。'}</p><button type="button" className="btn small" disabled={busy || !configured} onClick={() => onAction(turn, true)}><RotateCcw size={13} />重试回答</button></div>}
    {turn.status === 'cancelled' && <div className="qa-failure"><p>已取消本次回答，问题和执行记录仍保留。</p><button type="button" className="btn small" disabled={busy || !configured} onClick={() => onAction(turn, true)}>重新回答</button></div>}
    {turn.status === 'completed' && turn.answer && <div className="qa-answer">
      <p className="qa-answer-note">模型归纳，引用可供核对；未经人工确认。</p>
      {turn.answer.claims.map((claim, index) => <div className="qa-claim" key={index}><p>{claim.text}</p><div className="qa-citations">{claim.citations.map((item, number) => { const record = turn.sources.find(entry => entry.id === item.source_id); return <button type="button" key={`${item.source_id}-${number}`} className="qa-citation" aria-expanded={citation === item} disabled={!record} onClick={() => setCitation(citation === item ? null : item)}><Database size={12} />{record?.title || '来源记录不可用'}</button>; })}</div></div>)}
      {turn.answer.uncertainty.length > 0 && <section className="qa-uncertainty" aria-label="待确认与限制"><h3>待确认与限制</h3><ul>{turn.answer.uncertainty.map((text, index) => <li key={index}>{text}</li>)}</ul></section>}
      {source && <SourceDetail source={source} quote={citation?.quote} onClose={() => setCitation(null)} />}
    </div>}
    {turn.status === 'completed' && !turn.answer && <p className="cat-error" role="alert">回答内容不可用，请刷新会话查看。</p>}
    <details className="qa-run-detail"><summary>执行详情{turn.model ? ` · ${turn.model}` : ''}</summary><dl><div><dt>模型</dt><dd>{turn.model || '尚未调用'}</dd></div><div><dt>模型调用</dt><dd>{turn.model_calls} 次</dd></div><div><dt>只读工具调用</dt><dd>{turn.tool_calls} 次</dd></div><div><dt>结束时间</dt><dd>{turn.finished_at ? timestamp(turn.finished_at) : '尚未结束'}</dd></div>{Object.entries(turn.usage).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{value.toLocaleString()}</dd></div>)}</dl>{turn.sources.length > 0 && <details><summary>本次读取的 {turn.sources.length} 条来源记录</summary>{turn.sources.map(item => <details key={item.id}><summary>{item.title}</summary><p className="pp-footnote">{item.truncated ? '已截取部分内容 · ' : ''}{item.locator} · {timestamp(item.captured_at)}{item.dataset_version_id != null ? ` · 版本 #${item.dataset_version_id}` : ''}</p><pre>{item.content}</pre></details>)}</details>}</details>
  </article>;
}

export function QaPage({ api, onCollection, onDirectory }: { api: CatalogApi; onCollection: () => void; onDirectory: () => void }) {
  const capabilities = useApiData(client => client.qaCapabilities(), api, []);
  const conversations = useApiData(client => client.qaConversations(), api, []);
  const overview = useApiData(client => client.dashboard(14), api, []);
  const [selected, setSelected] = useState<Id | null>(null);
  const [detail, setDetail] = useState<QaConversationDetail | null>(null);
  const [loading, setLoading] = useState(false); const [loadError, setLoadError] = useState(''); const [epoch, setEpoch] = useState(0);
  const [drafts, setDrafts] = useState<Record<string, string>>({}); const [busy, setBusy] = useState(false); const [error, setError] = useState('');
  const pending = useRef<{ conversationId: Id; question: string; requestKey: string } | null>(null);
  const locked = useRef(false); const composer = useRef<HTMLTextAreaElement>(null);
  const draftKey = selected == null ? 'new' : String(selected); const draft = drafts[draftKey] || '';
  const setDraft = (value: string) => setDrafts(previous => ({ ...previous, [draftKey]: value }));
  const active = detail?.turns.some(isActive) ?? false;
  const configured = capabilities.data?.configured === true && !capabilities.loading && !capabilities.error;
  useEffect(() => {
    if (selected == null) { setDetail(null); setLoading(false); setLoadError(''); return; }
    let disposed = false; let timer: number | undefined;
    setLoading(true); setLoadError('');
    const load = async (poll = false) => {
      if (disposed) return;
      if (poll && document.visibilityState !== 'visible') { timer = window.setTimeout(() => void load(true), 2000); return; }
      try { const result = await api.qaConversation(selected); if (disposed) return; setDetail(result); setLoadError(''); if (result.turns.some(isActive)) timer = window.setTimeout(() => void load(true), 2000); }
      catch (reason) { if (!disposed) { setLoadError(errorText(reason)); if (poll) timer = window.setTimeout(() => void load(true), 4000); } }
      finally { if (!disposed) setLoading(false); }
    };
    void load(); return () => { disposed = true; window.clearTimeout(timer); };
  }, [api, selected, epoch]);
  const choose = (id: Id | null) => { if (locked.current) return; setSelected(id); setError(''); setDetail(null); };
  const submit = async (event?: FormEvent) => {
    event?.preventDefault(); const question = draft.trim();
    if (!question || locked.current || !configured || active || loading) return;
    locked.current = true; setBusy(true); setError('');
    let id = selected;
    try {
      if (id == null) { const conversation = await api.createQaConversation(question.slice(0, 70)); id = conversation.id; setDrafts(previous => ({ ...previous, [String(id)]: draft, new: '' })); setSelected(id); conversations.reload(); }
      if (!pending.current || pending.current.conversationId !== id || pending.current.question !== question) pending.current = { conversationId: id, question, requestKey: crypto.randomUUID() };
      await api.askQa(id, question, pending.current.requestKey);
      pending.current = null; setDrafts(previous => ({ ...previous, [String(id)]: '' })); setEpoch(value => value + 1); conversations.reload();
    } catch (reason) { setError(errorText(reason)); if (reason instanceof ApiError && reason.status === 503) capabilities.reload(); }
    finally { locked.current = false; setBusy(false); }
  };
  const action = async (turn: QaTurn, retry: boolean) => {
    if (locked.current) return; locked.current = true; setBusy(true); setError('');
    try { await (retry ? api.retryQaTurn(turn.id) : api.cancelQaTurn(turn.id)); setEpoch(value => value + 1); conversations.reload(); }
    catch (reason) { setError(errorText(reason)); if (reason instanceof ApiError && reason.status === 503) capabilities.reload(); }
    finally { locked.current = false; setBusy(false); }
  };
  return <div className="qa-page"><header className="pp-heading"><div><h1>数据问答</h1><p className="muted pp-subtitle">询问已收集的数据、采集情况与待核验信息，回答附带目录证据。</p></div><div className="cat-actions"><button className="btn" onClick={onCollection}>查看采集</button><button className="btn" onClick={onDirectory}>查看目录</button></div></header>
    <div className="qa-layout panel"><aside className="qa-history" aria-label="问答历史"><button type="button" className="btn qa-new" disabled={busy} onClick={() => { choose(null); composer.current?.focus(); }}><Plus size={15} />新建对话</button><div className="qa-history-title"><h2>我的对话</h2><button type="button" className="btn icon" aria-label="刷新对话列表" onClick={conversations.reload}><RotateCcw size={13} /></button></div><LoadState loading={conversations.loading} error={conversations.error} onRetry={conversations.reload} />{!conversations.loading && conversations.data && <ul>{conversations.data.items.map(item => <li key={item.id}><button type="button" disabled={busy} aria-current={selected === item.id ? 'true' : undefined} onClick={() => choose(item.id)}><span>{item.title || '未命名对话'}</span><time>{dateTime(item.updated_at)}</time></button></li>)}</ul>}{!conversations.loading && !conversations.error && conversations.data?.items.length === 0 && <p className="qa-history-empty">提出问题后，对话会保存在这里。</p>}<p className="qa-history-note">仅显示当前账号的对话</p></aside>
      <section className="qa-main" aria-label="数据问答会话"><div className="qa-session-head"><h2>{selected == null ? '了解你的数据目录' : detail?.title || '正在读取对话'}</h2><span className="badge"><Database size={12} />只读查询</span></div>
        <LoadState loading={capabilities.loading} error={capabilities.error} onRetry={capabilities.reload} />
        {!capabilities.loading && !capabilities.error && capabilities.data && !configured && <section className="qa-config" aria-label="模型配置状态"><h3>配置模型后即可开始问答</h3><p>{capabilities.data.reason || '服务器尚未配置模型接口。草稿会保留，可以先浏览目录与采集记录。'}</p><details><summary>服务端配置项</summary><p>由部署管理员配置模型地址、模型名称与密钥，再重启 API 和 worker。请勿在问题中粘贴密钥。</p><code>XIAOYI_LLM_BASE_URL<br />XIAOYI_LLM_MODEL<br />XIAOYI_LLM_API_KEY</code></details><button type="button" className="btn small" onClick={capabilities.reload}><RotateCcw size={13} />重新检查配置</button></section>}
        {loadError && <div className="qa-load-error" role="alert"><p>对话刷新失败：{loadError}</p><button type="button" className="btn small" onClick={() => setEpoch(value => value + 1)}>刷新对话</button></div>}
        {loading && <LoadState loading error="" onRetry={() => setEpoch(value => value + 1)} />}
        {!loading && selected == null && <div className="qa-welcome"><MessageSquare size={26} /><h3>从一个具体问题开始</h3><p>可查询本地目录、版本证据、采集记录和核验状态。每条回答保留取证时间，便于后续复核。</p><div className="qa-suggestions">{suggestions.map(question => <button type="button" disabled={busy} key={question} onClick={() => { setDraft(question); composer.current?.focus(); }}>{question}<span aria-hidden="true">↗</span></button>)}</div>{!configured && overview.data && !overview.loading && !overview.error && <section className="qa-local-facts"><h3>当前目录记录</h3><p>已收集 <strong>{overview.data.totals.datasets.toLocaleString()}</strong> 个数据集，保存 <strong>{overview.data.totals.evidence.toLocaleString()}</strong> 条证据，<strong>{overview.data.totals.enrichment_pending.toLocaleString()}</strong> 条整理结果待核验。</p><small>直接读取目录统计，非模型回答。</small></section>}</div>}
        {!loading && selected != null && detail && <div className="qa-turns" aria-label="对话记录">{detail.turns.map(turn => <Turn key={turn.id} turn={turn} busy={busy} configured={configured} onAction={(item, retry) => void action(item, retry)} />)}{detail.turns.length === 0 && <p className="qa-empty-thread">这段对话还没有问题，可以在下方开始。</p>}</div>}
        <form className="qa-composer" onSubmit={event => void submit(event)} aria-label="提问"><label htmlFor="qa-question">{selected == null ? '你的问题' : '继续提问'}</label><textarea ref={composer} id="qa-question" value={draft} maxLength={4000} rows={3} onChange={event => setDraft(event.target.value)} placeholder="例如：这周收集的图像数据中，哪些已确认许可？" aria-describedby="qa-composer-help" onKeyDown={event => { if (event.key === 'Enter' && event.ctrlKey && !event.nativeEvent.isComposing) { event.preventDefault(); void submit(); } }} />{error && <p className="cat-error" role="alert">{error}</p>}<div className="qa-compose-actions"><p id="qa-composer-help">{active ? '当前回答结束后可继续提问，草稿可先编辑。' : 'Enter 换行，Ctrl + Enter 发送'}<span>仅查询已有记录，采集、下载与审批请使用对应工作台。</span></p><button type="submit" className="btn primary" disabled={!configured || busy || loading || active || !draft.trim()}><ArrowUp size={15} />{busy ? '提交中…' : '发送问题'}</button></div>{configured && <p className="qa-budget">模型 {capabilities.data?.model} · 每轮最多 {capabilities.data?.limits.max_model_calls} 次模型调用、{capabilities.data?.limits.max_tool_calls} 次只读工具调用</p>}</form>
      </section></div></div>;
}
