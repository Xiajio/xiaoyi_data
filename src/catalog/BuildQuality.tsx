import { useState } from 'react';
import type { CatalogApi, DatasetBuild } from './api';
import { errorText, JsonValue, LoadState, StatusBadge, useApiData } from './ui';
import { asRecord, buildMeasurements, currentMiningProfile, displayCount, labelSlices, measuredCount, proportion } from './buildMeasurements';
import './data-visuals.css';

const percent = (value: number | null) => value === null ? '未提供' : `${value.toFixed(1)}%`;
const MODALITY: Record<string, string> = { text: '文本', table: '表格', image: '图像' };

function QuantityRow({ label, value, total, tone = 'neutral', note }: { label: string; value: number | null; total: number | null; tone?: string; note?: string }) {
  const width = proportion(value, total);
  return <div className={`dv-quantity dv-${tone}`}><span className="dv-quantity-label">{label}</span><span className="dv-track" aria-hidden="true"><span style={{ width: `${width ?? 0}%` }} /></span><strong>{displayCount(value)}</strong>{note ? <small>{note}</small> : null}</div>;
}

export function BuildQuality({ api, build }: { api: CatalogApi; build: DatasetBuild }) {
  const measurements = useApiData(async client => {
    const [response, profiles] = await Promise.all([client.buildManifest(build.id), client.buildProfiles(build.id)]);
    return { response, profiles: profiles.items };
  }, api, [build.id]);
  const [split, setSplit] = useState('all');
  const [showRaw, setShowRaw] = useState(false);
  const [moreProfiles, setMoreProfiles] = useState<Record<string, unknown>[]>([]);
  const [lastPageSize, setLastPageSize] = useState<number | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [pageError, setPageError] = useState('');
  const data = measurements.data;
  const view = data ? buildMeasurements(data.response) : null;
  const profiles = [...(data?.profiles ?? []), ...moreProfiles];
  const moreAvailable = (lastPageSize ?? data?.profiles.length ?? 0) >= 50;
  const mining = currentMiningProfile(profiles);
  const refresh = () => { setMoreProfiles([]); setLastPageSize(null); setPageError(''); measurements.reload(); };
  const loadMore = async () => {
    const lastId = profiles.reduce((maximum, item) => Math.max(maximum, measuredCount(item.id) ?? 0), 0);
    setLoadingMore(true); setPageError('');
    try {
      const page = await api.buildProfiles(build.id, lastId);
      if (page.items.some(item => (measuredCount(item.id) ?? 0) <= lastId)) throw new Error('测量分页未向后推进，请刷新后重试。');
      setMoreProfiles(previous => [...previous, ...page.items]); setLastPageSize(page.items.length);
    } catch (reason) { setPageError(errorText(reason)); } finally { setLoadingMore(false); }
  };
  const miningSummary = asRecord(mining?.summary);
  const labels = labelSlices(mining, split);
  const labelMax = labels[0]?.count ?? 0;
  const recipe = asRecord(view?.manifest.recipe);
  const inputs = Array.isArray(view?.manifest.inputs) ? view.manifest.inputs.map(asRecord) : [];
  const limited = miningSummary.truncated === true || miningSummary.slice_output_truncated === 1 || miningSummary.slice_output_truncated === true;
  return <section id="selected-build-quality" className="dv-quality" aria-labelledby={`quality-${build.id}`} data-testid="build-quality">
    <div className="dv-section-head"><div><h3 tabIndex={-1} id={`quality-${build.id}`}>版本 #{build.id} · 质量与分布</h3><p className="muted">{MODALITY[String(recipe.modality)] ?? '样本数据'} · 固定清单的实际计数</p></div><div className="cat-actions"><StatusBadge status={build.status} /><button className="btn small" disabled={measurements.loading || loadingMore} onClick={refresh}>刷新测量</button></div></div>
    <LoadState loading={measurements.loading} error={measurements.error} onRetry={refresh} />
    {measurements.error && data ? <p className="dv-note">当前保留上次成功读取的测量结果。</p> : null}
    {view && data ? <>
      <div className="dv-quality-grid">
        <figure className="dv-figure"><figcaption><h3>样本处理结果</h3><p>各阶段共用输入样本数量刻度</p></figcaption>
          <QuantityRow label="输入样本" value={view.input} total={view.input} />
          <QuantityRow label="校验后" value={view.valid} total={view.input} note={`排除 ${displayCount(view.invalid)}`} />
          <QuantityRow label="精确去重后" value={view.unique} total={view.input} note={`排除 ${displayCount(view.duplicates)}`} />
          <QuantityRow label="最终保留" value={view.output} total={view.input} tone="retained" note={`占输入 ${percent(view.retainedPercent)}`} />
          <div className="dv-exclusions">{view.reasons.map(item => <span key={item.key}><i className={`dv-dot dv-${item.tone}`} aria-hidden="true" />{item.label}<b>{displayCount(item.value)}</b></span>)}</div>
          {view.input === 0 ? <p className="dv-note">输入为空，没有可计算的保留比例。</p> : !view.balanced ? <p className="dv-note">部分计数缺失或无法对账，请展开原始清单核对。</p> : null}
        </figure>
        <figure className="dv-figure"><figcaption><h3>数据集切分</h3><p>实际样本量；同一实体组保持在同一集合</p></figcaption>
          <div className="dv-split-bar" aria-hidden="true">{view.splitBalanced && view.output !== null && view.output > 0 ? view.splits.map(item => <span key={item.key} className={`dv-split-${item.key}`} style={{ width: `${proportion(item.value, view.output) ?? 0}%` }} />) : null}</div>
          <dl className="dv-split-values">{view.splits.map(item => <div key={item.key}><dt><i className={`dv-dot dv-split-${item.key}`} aria-hidden="true" />{item.label}</dt><dd><strong>{displayCount(item.value)}</strong><span>{percent(item.percentage)}</span></dd></div>)}</dl>
          <p className="dv-note">配方比例按组分配，实际样本比例可能不同。{typeof recipe.seed === 'number' ? `固定种子 ${recipe.seed}。` : ''}</p>
          {!view.splitBalanced ? <p className="dv-note">切分计数尚不完整，暂不绘制占比。</p> : null}
        </figure>
      </div>
      <section className="dv-mining" aria-labelledby={`mining-${build.id}`}>
        <div className="dv-section-head"><div><h3 id={`mining-${build.id}`}>标签分布与数据疑点</h3><p className="muted">{mining ? `测量 #${String(mining.id)} · ${String(mining.producer)}` : '运行「检查数据问题」后查看标签切片与疑点测量'}</p></div>{mining ? <label className="dv-select">查看切分<select aria-label="标签分布切分" value={split} onChange={event => setSplit(event.target.value)}><option value="all">全部切分</option>{view.splits.map(item => <option key={item.key} value={item.key}>{item.label}</option>)}</select></label> : null}</div>
        {mining ? <><p className="dv-findings"><strong>{displayCount(measuredCount(miningSummary.findings))}</strong> 对候选关系<span>可能涉及重复、标签冲突或跨集合相似；候选关系不是问题样本数。</span></p>
          {limited ? <p className="dv-notice" role="status">本次测量存在截断。分布与疑点只覆盖已报告的范围，不能视作完整数据结论。</p> : null}
          {labels.length ? <div className="dv-label-bars" aria-label="标签数量分布">{labels.slice(0, 8).map(item => <QuantityRow key={item.label} label={item.label || '空标签'} value={item.count} total={labelMax} />)}{labels.length > 8 ? <p className="dv-note">图中显示数量最多的 8 个标签；其余标签可在原始测量中查看。</p> : null}</div> : <p className="dv-note">当前切分没有已报告的标签计数。</p>}
          <p className="dv-note">本次测量不自动删除语义相似样本。显示已加载记录中的最近一次疑点检查。</p></> : null}
        {moreAvailable ? <div className="dv-profile-pagination"><p className="dv-note">已加载前 {profiles.length} 条测量，后续可能还有新结果。</p><button className="btn small" disabled={loadingMore || measurements.loading} onClick={() => void loadMore()}>{loadingMore ? '正在加载测量…' : '加载后续测量'}</button></div> : null}
        {pageError ? <p className="cat-error" role="alert">{pageError}</p> : null}
      </section>
      <details className="dv-source"><summary>来源与复现依据 · {inputs.length} 个输入资产</summary><p className="dv-note">算子版本 {String(recipe.operator_version ?? '未提供')} · 清单 SHA-256</p><code className="dv-hash">{String(data.response.sha256 ?? build.manifest_hash)}</code><ul>{inputs.slice(0, 20).map((item, i) => <li key={`${String(item.asset_id)}-${i}`}><strong>资产 #{String(item.asset_id)} · {String(item.path ?? '未提供文件名')}</strong><small>固定来源版本 #{String(item.dataset_version_id ?? '未提供')}</small><code className="dv-hash">{String(item.sha256 ?? '哈希未提供')}</code></li>)}</ul>{inputs.length > 20 ? <p className="dv-note">此处展示前 20 个输入资产；完整清单在下方 JSON 中。</p> : null}</details>
      <details className="dv-source" onToggle={event => setShowRaw(event.currentTarget.open)}><summary>原始清单与测量 JSON</summary>{showRaw ? <JsonValue value={{ ...data.response, profiles }} /> : null}</details>
    </> : null}
  </section>;
}
