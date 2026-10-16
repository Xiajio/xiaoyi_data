import type { ReactNode } from 'react';
import { CircleAlert, Download, FileText, Hash, Heart, Layers, Quote, ShieldCheck, Table2 } from 'lucide-react';
import type { DatasetItem, EnrichmentItem, Id } from './api';
import { formatBytes } from './ui';

export type LevelTone = 'good' | 'warn' | 'bad' | '';
const LEVEL_MAP: Record<string, [string, LevelTone]> = {
  high: ['高', 'good'], complete: ['完整', 'good'],
  medium: ['中', 'warn'], partial: ['部分', 'warn'],
  low: ['低', 'bad'], incomplete: ['不完整', 'bad'],
};
export function normalizeLevel(level: unknown): { label: string; tone: LevelTone } | null {
  if (typeof level !== 'string' || !level.trim()) return null;
  const key = level.trim().toLowerCase();
  const mapped = LEVEL_MAP[key];
  return mapped ? { label: mapped[0], tone: mapped[1] } : { label: level.trim(), tone: '' };
}

const RISK_REASON = /未知|缺少|缺失|较少|未提供|无法|没有|超过\s*\d+\s*个月未更新/;
export function reasonTone(reason: unknown): 'good' | 'warn' {
  return typeof reason === 'string' && RISK_REASON.test(reason) ? 'warn' : 'good';
}

const VECTOR_PREVIEW = 8;
export function truncateVectorValue(value: Record<string, unknown>): Record<string, unknown> {
  const vector = value.vector;
  if (!Array.isArray(vector) || vector.length <= VECTOR_PREVIEW) return value;
  return { ...value, vector: [...vector.slice(0, VECTOR_PREVIEW), `… 共 ${vector.length} 维`] };
}

const BUILTIN_PATTERNS = ['\\d{4}-\\d{2}-\\d{2}', '\\d[\\d,]{2,}', '(?i:(?:mit|apache-?\\d(?:\\.\\d)?|lgpl|gpl-?[\\d.]*|bsd-?\\d-clause|cc0|cc-by(?:-[a-z]+)*-?\\d(?:\\.\\d)?|odc-by|odbl))'];
const escapeReg = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
export function buildHighlightTokens(value: Record<string, unknown>, dataset?: DatasetItem | null): string[] {
  const tokens: string[] = [];
  const push = (token: unknown, min = 2) => { if (typeof token === 'string' && token.trim().length >= min) tokens.push(token.trim()); };
  if (Array.isArray(value.reasons)) for (const reason of value.reasons) {
    if (typeof reason !== 'string') continue;
    for (const match of reason.match(/\d[\d,]{1,}/g) ?? []) push(match, 3);
    for (const match of reason.match(/（([^（）]{2,20})）/g) ?? []) push(match.replace(/[（）]/g, ''), 2);
  }
  if (Array.isArray(value.splits)) for (const split of value.splits) {
    if (split && typeof split === 'object') push((split as Record<string, unknown>).name);
  }
  if (Array.isArray(value.claims)) for (const claim of value.claims) {
    if (!claim || typeof claim !== 'object') continue;
    const quotes = (claim as Record<string, unknown>).quotes;
    if (Array.isArray(quotes)) for (const quote of quotes) push(typeof quote === 'string' && quote.length > 80 ? quote.slice(0, 80) : quote, 4);
  }
  if (dataset) {
    push(dataset.license, 2);
    if (dataset.downloads >= 100) push(String(dataset.downloads), 3);
    if (typeof dataset.size_rows === 'number' && dataset.size_rows >= 100) push(String(dataset.size_rows), 3);
  }
  return [...new Set(tokens)].sort((a, b) => b.length - a.length).slice(0, 40);
}
export function highlightText(text: string, tokens: string[]): ReactNode[] {
  const parts = [...tokens.map(escapeReg), ...BUILTIN_PATTERNS].filter(Boolean);
  if (!parts.length || !text) return [text];
  const regex = new RegExp(`(${parts.join('|')})`, 'g');
  return text.split(regex).map((part, index) =>
    index % 2 === 1 && part ? <mark key={index} className="rv-mark">{part}</mark> : part);
}

interface SchemaSummary {
  present: boolean;
  features: { name: string; dtype: string }[];
  splits: { name: string; num_examples: number | null }[];
  totalExamples: number | null;
  datasetSize: number | null;
  downloadSize: number | null;
}
export function summarizeSchema(value: Record<string, unknown>): SchemaSummary {
  const record = (item: unknown): Record<string, unknown> => (item && typeof item === 'object' ? item as Record<string, unknown> : {});
  const numberOrNull = (item: unknown) => (typeof item === 'number' && Number.isFinite(item) ? item : null);
  const features = (Array.isArray(value.features) ? value.features : []).map(item => ({ name: String(record(item).name ?? ''), dtype: String(record(item).dtype ?? '') })).filter(item => item.name);
  const splits = (Array.isArray(value.splits) ? value.splits : []).map(item => ({ name: String(record(item).name ?? ''), num_examples: numberOrNull(record(item).num_examples) })).filter(item => item.name);
  const explicitTotal = numberOrNull(value.total_examples);
  const summed = splits.reduce((sum, item) => sum + (item.num_examples ?? 0), 0);
  return {
    present: value.present !== false,
    features, splits,
    totalExamples: explicitTotal ?? (summed > 0 ? summed : null),
    datasetSize: numberOrNull(value.dataset_size),
    downloadSize: numberOrNull(value.download_size),
  };
}

const LICENSE_LABELS: Record<string, string> = { permissive: '宽松许可', copyleft: '著佐权', 'research-only': '仅限研究', unknown: '许可未知' };
export function DatasetFacts({ dataset }: { dataset: DatasetItem }) {
  const licenseTone = dataset.license_class === 'permissive' ? 'good' : dataset.license_class === 'unknown' ? 'warn' : '';
  return <div className="rv-facts" data-testid="review-facts">
    <p className="rv-facts-title"><ShieldCheck size={13} />目录事实 · 来自采集版本，供核对候选理由</p>
    <dl>
      <div><dt>许可</dt><dd><span className={`badge ${licenseTone}`}>{LICENSE_LABELS[dataset.license_class] ?? dataset.license_class}{dataset.license ? ` · ${dataset.license}` : ''}</span></dd></div>
      <div><dt><Download size={11} />下载</dt><dd>{dataset.downloads.toLocaleString('zh-CN')}</dd></div>
      <div><dt><Heart size={11} />点赞</dt><dd>{dataset.likes.toLocaleString('zh-CN')}</dd></div>
      <div><dt><Table2 size={11} />规模</dt><dd>{dataset.size_rows == null ? '未知' : `${dataset.size_rows.toLocaleString('zh-CN')} 行`}</dd></div>
      <div><dt><Layers size={11} />模态</dt><dd>{dataset.modalities.length ? dataset.modalities.join('、') : '未标注'}</dd></div>
      <div><dt><CircleAlert size={11} />缺失项</dt><dd>{dataset.missing.length ? <span className="badge warn">{dataset.missing.length} 项</span> : <span className="badge good">齐</span>}</dd></div>
      {dataset.task_categories.length ? <div><dt><FileText size={11} />任务</dt><dd>{dataset.task_categories.slice(0, 3).join('、')}</dd></div> : null}
    </dl>
  </div>;
}

function ScoreDots({ score, max = 6 }: { score: number; max?: number }) {
  const clamped = Math.max(0, Math.min(max, Math.round(score)));
  return <span className="rv-score" role="img" aria-label={`分数 ${clamped}/${max}`} title={`分数 ${clamped}/${max}`}>
    {Array.from({ length: max }, (_, index) => <i key={index} className={index < clamped ? 'is-on' : ''} />)}
    <b>{clamped}/{max}</b>
  </span>;
}

function RawJson({ value, truncate = false }: { value: Record<string, unknown>; truncate?: boolean }) {
  return <details className="rv-raw"><summary>查看原始 JSON</summary><pre className="cat-json">{JSON.stringify(truncate ? truncateVectorValue(value) : value, null, 2)}</pre></details>;
}

function QualityView({ value }: { value: Record<string, unknown> }) {
  const level = normalizeLevel(value.level);
  const score = typeof value.score === 'number' && Number.isFinite(value.score) ? value.score : null;
  const reasons = Array.isArray(value.reasons) ? value.reasons.filter((item): item is string => typeof item === 'string') : [];
  return <>
    <div className="rv-summary">
      {level && <span className={`badge ${level.tone}`} data-testid="review-level">{level.label}</span>}
      {score !== null && <ScoreDots score={score} />}
      {typeof value.measurement_kind === 'string' && value.measurement_kind && <span className="badge">{value.measurement_kind}</span>}
    </div>
    {reasons.length ? <ul className="rv-reasons">{reasons.map((reason, index) => {
      const tone = reasonTone(reason);
      return <li key={index} className={`rv-reason is-${tone}`}><i aria-hidden="true" />{highlightText(reason, [])}</li>;
    })}</ul> : <p className="muted">没有逐条依据，建议结合证据与目录事实判断。</p>}
    <RawJson value={value} />
  </>;
}

function SchemaView({ value }: { value: Record<string, unknown> }) {
  const summary = summarizeSchema(value);
  if (!summary.present) return <>
    <p className="pp-note pp-note-warning rv-note"><CircleAlert size={15} /><span>卡片缺少 dataset_info 结构信息，无法提取特征列与切片；可标记待补资料。</span></p>
    <RawJson value={value} />
  </>;
  const maxSplit = Math.max(1, ...summary.splits.map(item => item.num_examples ?? 0));
  return <>
    <div className="rv-summary">
      <span className="badge good">含结构信息</span>
      {summary.features.length > 0 && <span className="badge">{summary.features.length} 个特征列</span>}
      {summary.splits.length > 0 && <span className="badge">{summary.splits.length} 个切片</span>}
      {summary.totalExamples !== null && <span className="badge good">共 {summary.totalExamples.toLocaleString('zh-CN')} 行</span>}
    </div>
    {summary.splits.length > 0 && <div className="rv-splits">{summary.splits.map(item => <div key={item.name} className="rv-split-row">
      <span className="rv-split-name">{item.name}</span>
      <span className="rv-split-track" aria-hidden="true"><span style={{ width: `${Math.max(2, (item.num_examples ?? 0) / maxSplit * 100)}%` }} /></span>
      <strong>{item.num_examples == null ? '未知' : item.num_examples.toLocaleString('zh-CN')}</strong>
    </div>)}</div>}
    {summary.features.length > 0 && (summary.features.length > 10 ?
      <details className="rv-features"><summary>展开 {summary.features.length} 个特征列</summary><FeatureTable features={summary.features} /></details> :
      <FeatureTable features={summary.features} />)}
    {(summary.datasetSize !== null || summary.downloadSize !== null) && <p className="rv-sizes">
      {summary.datasetSize !== null && <span>数据集体积 {formatBytes(summary.datasetSize)}</span>}
      {summary.downloadSize !== null && <span>下载体积 {formatBytes(summary.downloadSize)}</span>}
    </p>}
    <RawJson value={value} />
  </>;
}
function FeatureTable({ features }: { features: { name: string; dtype: string }[] }) {
  return <div className="table-wrap rv-feature-table"><table><thead><tr><th>特征列</th><th>类型</th></tr></thead><tbody>
    {features.map(item => <tr key={item.name}><td>{item.name}</td><td className="pp-mono">{item.dtype || '未标注'}</td></tr>)}
  </tbody></table></div>;
}

interface LlmClaim { text: string; evidence_ids: Id[]; quotes: string[] }
function LlmSummaryView({ value, onEvidenceJump }: { value: Record<string, unknown>; onEvidenceJump?: (id: Id) => void }) {
  const relevant = typeof value.relevant === 'boolean' ? value.relevant : null;
  const rankScore = typeof value.rank_score === 'number' ? value.rank_score : null;
  const rationale = typeof value.rationale === 'string' ? value.rationale : '';
  const claims: LlmClaim[] = (Array.isArray(value.claims) ? value.claims : []).map(item => {
    const record = item && typeof item === 'object' ? item as Record<string, unknown> : {};
    return {
      text: typeof record.text === 'string' ? record.text : '',
      evidence_ids: Array.isArray(record.evidence_ids) ? record.evidence_ids.filter((id): id is Id => typeof id === 'number' || typeof id === 'string') : [],
      quotes: Array.isArray(record.quotes) ? record.quotes.filter((quote): quote is string => typeof quote === 'string') : [],
    };
  }).filter(item => item.text);
  const uncertainty = Array.isArray(value.uncertainty) ? value.uncertainty.filter((item): item is string => typeof item === 'string') : [];
  return <>
    <div className="rv-summary">
      {relevant !== null && <span className={`badge ${relevant ? 'good' : 'warn'}`}>{relevant ? '模型判断：相关' : '模型判断：可能不相关'}</span>}
      {rankScore !== null && <span className="badge">rank_score {rankScore}</span>}
      <span className="badge">模型推断 · 需人工核验</span>
    </div>
    {rationale && <p className="rv-rationale">{rationale}</p>}
    {claims.length > 0 && <div className="rv-claims">{claims.map((claim, index) => <article key={index} className="rv-claim">
      <p>{claim.text}</p>
      {claim.quotes.length > 0 && <div className="rv-quotes">{claim.quotes.slice(0, 4).map((quote, quoteIndex) => <span key={quoteIndex} className="rv-quote"><Quote size={10} />{quote.length > 80 ? `${quote.slice(0, 80)}…` : quote}</span>)}</div>}
      {claim.evidence_ids.length > 0 && <div className="rv-claim-links">{[...new Set(claim.evidence_ids)].map(id =>
        <button key={String(id)} type="button" className="pp-link" onClick={() => onEvidenceJump?.(id)}><Hash size={11} />证据 {String(id)}</button>)}</div>}
    </article>)}</div>}
    {uncertainty.length > 0 && <ul className="rv-reasons">{uncertainty.map((item, index) => <li key={index} className="rv-reason is-warn"><i aria-hidden="true" />{item}</li>)}</ul>}
    <RawJson value={value} />
  </>;
}

function EmbeddingView({ value, producer }: { value: Record<string, unknown>; producer: string }) {
  const dim = typeof value.dim === 'number' ? value.dim : null;
  const hash = typeof value.text_hash === 'string' ? value.text_hash : '';
  return <>
    <div className="rv-summary">
      {dim !== null && <span className="badge good">{dim} 维向量</span>}
      {producer && <span className="badge">{producer}</span>}
      {hash && <span className="badge pp-mono">hash {hash.slice(0, 12)}…</span>}
    </div>
    <p className="rv-note muted">嵌入向量用于相似度检索，数值内容不在此展示；核验重点是模型与文本哈希是否对应来源卡片。</p>
    <RawJson value={value} truncate />
  </>;
}

function GenericView({ value }: { value: Record<string, unknown> }) {
  return <>
    <div className="rv-summary"><span className="badge">未结构化类型</span></div>
    <RawJson value={value} />
  </>;
}

export function ReviewValue({ item, onEvidenceJump }: { item: EnrichmentItem; onEvidenceJump?: (id: Id) => void }) {
  const value = item.value && typeof item.value === 'object' && !Array.isArray(item.value) ? item.value : {};
  switch (item.kind) {
    case 'quality': return <QualityView value={value} />;
    case 'schema': return <SchemaView value={value} />;
    case 'llm_summary': return <LlmSummaryView value={value} onEvidenceJump={onEvidenceJump} />;
    case 'embedding': return <EmbeddingView value={value} producer={item.producer} />;
    default: return <GenericView value={value} />;
  }
}
