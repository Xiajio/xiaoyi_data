import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ShieldCheck, Sparkles, X } from 'lucide-react';
import { CatalogApi, type EnrichmentItem, type Id, type ReviewDecision, type Role } from './api';
import { canResearch, canReview, errorText, LoadState, StatusBadge, useApiData } from './ui';
import { buildHighlightTokens, DatasetFacts, highlightText, reviewVersionMetadata, ReviewValue } from './reviewValue';

const KIND_LABELS: Record<string, string> = { quality: '信息完整度', schema: '结构摘要', embedding: '嵌入', llm_summary: '模型摘要' };
const EVIDENCE_KINDS: Record<string, string> = { card: '卡片', metadata: '元数据', derivation: '派生' };
const AGENT_POLL_MS = 3000;
const AGENT_WAIT_MS = 120_000;

function ReviewCard({ api, item, role, agentEnabled, onReviewed, onAgentCandidate, onToast }: { api: CatalogApi; item: EnrichmentItem; role: Role; agentEnabled: boolean; onReviewed: () => void; onAgentCandidate: (id: Id) => void; onToast: (text: string) => void }) {
  const detail = useApiData(client => client.dataset(item.dataset_id), api, [item.dataset_id]); const [note, setNote] = useState(''); const [acting, setActing] = useState(false); const [correcting, setCorrecting] = useState(false); const [value, setValue] = useState(JSON.stringify(item.value, null, 2)); const [error, setError] = useState('');
  const [flashId, setFlashId] = useState<Id | null>(null);
  const [agentBusy, setAgentBusy] = useState<Id | null>(null);
  const agentStarted = useRef(0);
  const jumpToEvidence = useCallback((id: Id) => {
    const node = document.getElementById(`evidence-${String(id)}`);
    if (!node) return;
    node.scrollIntoView({ behavior: 'smooth', block: 'center' });
    setFlashId(id);
    window.setTimeout(() => setFlashId(current => (String(current) === String(id) ? null : current)), 1600);
  }, []);
  const review = async (decision: ReviewDecision) => { setError(''); let corrected: Record<string, unknown> | undefined;
    if ((decision === 'corrected' || decision === 'needs-info' || decision === 'rejected') && !note.trim()) { setError('请填写核验说明，记录更正、驳回或待补资料的依据。'); return; }
    if (decision === 'corrected') { try { const parsed: unknown = JSON.parse(value); if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error(); corrected = parsed as Record<string, unknown>; } catch { setError('更正内容必须是有效的 JSON 对象。'); return; } }
    setActing(true); try { await api.reviewEnrichment(item.id, decision, note, corrected); onToast(`${({ confirmed: '已确认', rejected: '已驳回', 'needs-info': '已标记待补资料', corrected: '已更正' })[decision]} ${item.dataset || ''} 的整理候选`); onReviewed(); } catch (reason) { setError(errorText(reason)); } finally { setActing(false); }
  };
  const askAgent = async () => {
    setError('');
    if (item.dataset_version_id == null) { setError('该候选缺少版本信息，无法生成智能体总结。'); return; }
    setActing(true);
    try {
      const created = await api.agentSummary(item.dataset_version_id);
      if (created.status === 'generating') { agentStarted.current = Date.now(); setAgentBusy(created.id); }
      else { onToast(`${item.dataset || ''} 的智能体总结已在核验队列中`); onAgentCandidate(created.id); }
    } catch (reason) { setError(errorText(reason)); } finally { setActing(false); }
  };
  const agentTrack = useApiData(client => client.dataset(item.dataset_id), api, [item.dataset_id, agentBusy], agentBusy === null ? 0 : AGENT_POLL_MS);
  useEffect(() => {
    if (agentBusy === null || !agentTrack.data) return;
    if (Date.now() - agentStarted.current > AGENT_WAIT_MS) { setAgentBusy(null); setError('后台仍在生成，稍后在整理类型「模型摘要」中查看。'); return; }
    const found = agentTrack.data.enrichments.find(entry => String(entry.id) === String(agentBusy));
    if (!found) return;
    if (found.status === 'candidate') { setAgentBusy(null); onToast(`${item.dataset || ''} 的智能体总结已生成，请人工核验`); onAgentCandidate(found.id); }
    else if (found.status === 'failed') { setAgentBusy(null); const message = found.value && typeof found.value.error === 'string' ? found.value.error : '智能体总结失败，请重试。'; setError(message); }
  }, [agentTrack.data, agentBusy, item.dataset, onAgentCandidate, onToast]);
  const evidence = detail.data?.evidence.filter(e => item.evidence_ids.map(String).includes(String(e.id)) && (item.dataset_version_id != null && e.dataset_version_id != null && String(item.dataset_version_id) === String(e.dataset_version_id))) ?? [];
  const disabled = !canReview(role) || acting;
  const versionMetadata = reviewVersionMetadata(detail.data?.versions, item.dataset_version_id);
  const tokens = useMemo(() => buildHighlightTokens(item.value, versionMetadata), [item.value, versionMetadata]);
  const correction = useMemo(() => {
    if (!correcting) return null;
    try { const parsed: unknown = JSON.parse(value); if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return { ok: false as const, message: '更正内容必须是有效的 JSON 对象。' }; return { ok: true as const, value: parsed as Record<string, unknown> }; }
    catch (reason) { return { ok: false as const, message: reason instanceof Error ? `JSON 解析失败：${reason.message}` : 'JSON 解析失败。' }; }
  }, [correcting, value]);
  return <article className="panel cat-review-card" data-testid={`enrichment-${item.id}`}><div className="cat-review-head"><div><h2>{item.dataset || `数据集 #${item.dataset_id}`}</h2><p className="pp-footnote">版本 #{item.dataset_version_id ?? '待核对'} · {item.producer}</p></div><div className="rv-head-actions"><button className="btn" disabled={!canResearch(role) || acting || agentBusy !== null || !agentEnabled} title={!agentEnabled ? '未配置模型服务，无法生成智能体总结' : !canResearch(role) ? '需要研究员及以上角色' : '读取该版本留存的元数据与证据，由智能体生成带引用的数据集总结，完成后进入核验队列'} onClick={() => void askAgent()} data-testid="agent-summary"><Sparkles size={14} />{agentBusy !== null ? '智能体总结中…' : '智能体核验总结'}</button><StatusBadge status={item.status} /></div></div>
    {agentBusy !== null && <p className="pp-note rv-note" data-testid="agent-summary-progress"><Sparkles size={15} /><span>智能体正在读取版本证据并生成总结，通常需要几十秒；完成后会自动打开生成的「模型摘要」候选，无需等待。</span></p>}
    {detail.data && <DatasetFacts dataset={versionMetadata} versionId={item.dataset_version_id} />}
    <div className="cat-evidence-layout"><section><h3>{item.kind === 'quality' ? '目录信息完整度' : KIND_LABELS[item.kind] ?? item.kind} · 候选值</h3><p className="pp-footnote">自动整理结果，需依据右侧版本证据核验。</p>{correcting ? <><label className="pp-form-field">更正候选内容<textarea className="cat-code-input" rows={12} value={value} onChange={e => setValue(e.target.value)} /></label>{correction && (correction.ok ? <p className="rv-correction-ok" aria-live="polite">✓ 有效 JSON · {Object.keys(correction.value).length} 个键，保存前可对照下方预览</p> : <p className="cat-error" role="alert">{correction.message}</p>)}{correction?.ok && <div className="rv-correction-preview"><ReviewValue item={{ ...item, value: correction.value }} onEvidenceJump={jumpToEvidence} /></div>}</> : <ReviewValue item={item} onEvidenceJump={jumpToEvidence} />}</section>
    <section><h3>关联证据</h3><LoadState loading={detail.loading} error={detail.error} onRetry={detail.reload} />{!detail.loading && evidence.map(e => <blockquote key={e.id} id={`evidence-${String(e.id)}`} className={`cat-evidence ${flashId !== null && String(flashId) === String(e.id) ? 'rv-flash' : ''}`}><p className="pp-footnote"><span className="badge">{EVIDENCE_KINDS[e.kind] ?? e.kind}</span> #{e.id} · <span className="pp-mono">{e.location}</span></p><p>{e.excerpt ? highlightText(e.excerpt, tokens) : '未提供摘录'}</p></blockquote>)}{!detail.loading && !evidence.length && <p className="muted">没有可定位的关联证据。可标记待补资料，避免将未验证信息作为事实。</p>}</section></div>
    <label className="pp-form-field">核验说明<textarea rows={2} value={note} onChange={e => setNote(e.target.value)} placeholder="记录依据、修正原因或需要补充的资料" /></label>{error && <p className="cat-error" role="alert">{error}</p>}
    <div className="pp-inline-actions">{!canReview(role) ? <p className="pp-permission">需要审核员或管理员账号完成核验。</p> : <span />}<div className="cat-actions"><button className="btn" disabled={disabled} onClick={() => void review('needs-info')}>待补资料</button><button className="btn" disabled={disabled} onClick={() => void review('rejected')}><X size={14} />驳回</button>{correcting ? <button className="btn primary" disabled={disabled} onClick={() => void review('corrected')}>保存更正</button> : <button className="btn" disabled={disabled} onClick={() => setCorrecting(true)}>更正候选</button>}<button className="btn primary" disabled={disabled || correcting} onClick={() => void review('confirmed')}><Check size={14} />确认</button></div></div>
  </article>;
}
export function ReviewPage({ api, role, onToast }: { api: CatalogApi; role: Role; onToast: (text: string) => void }) {
  const [kind, setKind] = useState(''); const [status, setStatus] = useState('candidate'); const [selected, setSelected] = useState(''); const queue = useApiData(client => client.enrichmentQueue(status, kind), api, [kind, status]);
  const capabilities = useApiData(client => client.qaCapabilities(), api, []);
  const active = queue.data?.items.find(item => String(item.id) === selected) ?? queue.data?.items[0];
  const openAgentCandidate = useCallback((id: Id) => { setKind(''); setStatus('candidate'); setSelected(String(id)); queue.reload(); }, [queue]);
  return <><header className="pp-heading"><div><h1>整理核验</h1><p className="muted pp-subtitle">对照版本证据确认、更正候选，或记录需要补充的资料。</p></div></header><div className="pp-toolbar cat-toolbar"><label>整理类型 <select aria-label="整理类型" value={kind} onChange={e => { setKind(e.target.value); setSelected(''); }}><option value="">全部类型</option><option value="schema">结构摘要</option><option value="quality">目录信息完整度</option><option value="embedding">嵌入</option><option value="llm_summary">模型摘要</option></select></label><label>核验状态 <select aria-label="核验状态" value={status} onChange={e => { setStatus(e.target.value); setSelected(''); }}><option value="candidate">待核验</option><option value="needs-info">待补资料</option><option value="confirmed">已确认</option><option value="rejected">已驳回</option></select></label></div>
    <LoadState loading={queue.loading} error={queue.error} onRetry={queue.reload} />{queue.data && !queue.loading && (queue.data.items.length ? <><p className="rv-queue-count">共 {queue.data.items.length} 项{status === 'candidate' ? '待核验' : '该状态'}候选</p><div className="cat-review-selector" aria-label="候选列表">{queue.data.items.map(item => <button key={item.id} className={`btn ${item.id === active?.id ? 'primary' : ''}`} onClick={() => setSelected(String(item.id))}><i className={`rv-kind-dot rv-kind-${item.kind}`} aria-hidden="true" />{item.dataset || `#${item.dataset_id}`} · {KIND_LABELS[item.kind] ?? item.kind}</button>)}</div>{active && <ReviewCard key={active.id} api={api} item={active} role={role} agentEnabled={capabilities.data?.configured === true} onReviewed={queue.reload} onAgentCandidate={openAgentCandidate} onToast={onToast} />}</> : <div className="empty"><ShieldCheck size={26} /><strong>当前没有{status === 'candidate' ? '待核验' : '该状态的'}候选</strong><p>新任务产生的整理候选会进入这里。</p></div>)}
  </>;
}
