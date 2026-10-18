import { useId } from 'react';
import type { ExperimentItem, Id } from './api';
import './experiment-visuals.css';

export interface ComparisonRow {
  experimentId: Id;
  buildId: Id;
  accuracy: number | null;
  coverage: number | null;
  errorCount: number | null;
  predictions: number | null;
  expectedSamples: number | null;
  delta: number | null;
}
export interface ComparisonView {
  baselineId: Id;
  evaluationHash: string;
  rows: ComparisonRow[];
}

const record = (value: unknown): Record<string, unknown> | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
const identifier = (value: unknown): value is Id => typeof value === 'number' ? Number.isSafeInteger(value) && value > 0 : typeof value === 'string' && /^[1-9]\d*$/.test(value);
export const rateValue = (value: unknown): number | null => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
export const countValue = (value: unknown): number | null => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
export const formatPercent = (value: unknown, missing = '未知'): string => rateValue(value) === null ? missing : `${(Number(value) * 100).toFixed(2)}%`;
export const formatCount = (value: unknown): string => countValue(value) === null ? '未知' : Number(value).toLocaleString('zh-CN');
export function formatPointChange(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < -1 || value > 1) return '差值未知';
  const points = Number((value * 100).toFixed(2));
  return `${points > 0 ? '+' : ''}${points === 0 ? '0.00' : points.toFixed(2)} 个百分点`;
}

/** Only request ordering changes; the user's selection/display order is preserved. */
export function comparisonRequestIds(selected: readonly Id[], baseline?: Id | null): Id[] {
  const first = selected.find(id => String(id) === String(baseline)) ?? selected[0];
  return first === undefined ? [] : [first, ...selected.filter(id => String(id) !== String(first))];
}

/** Validate the response envelope, not experiment eligibility: only the API decides eligibility. */
export function readComparison(value: unknown, selection: readonly Id[], baseline: Id): ComparisonView | null {
  const response = record(value);
  if (!response || !identifier(response.baseline_experiment_id) || String(response.baseline_experiment_id) !== String(baseline)
    || typeof response.evaluation_build_hash !== 'string' || !response.evaluation_build_hash.trim()
    || !Array.isArray(response.items) || response.items.length !== selection.length || selection.length < 2 || selection.length > 20) return null;
  const rows = new Map<string, ComparisonRow>();
  for (const item of response.items) {
    const row = record(item);
    if (!row || !identifier(row.experiment_id) || !identifier(row.build_id) || rows.has(String(row.experiment_id))) return null;
    const metrics = record(row.metrics) ?? {};
    rows.set(String(row.experiment_id), {
      experimentId: row.experiment_id, buildId: row.build_id,
      accuracy: rateValue(metrics.accuracy), coverage: rateValue(metrics.coverage),
      errorCount: countValue(metrics.error_count), predictions: countValue(metrics.predictions), expectedSamples: countValue(metrics.expected_samples),
      // The server's supplied difference is authoritative; never substitute a local subtraction.
      delta: typeof row.accuracy_delta === 'number' && Number.isFinite(row.accuracy_delta) && row.accuracy_delta >= -1 && row.accuracy_delta <= 1 ? row.accuracy_delta : null,
    });
  }
  if (new Set(selection.map(String)).size !== selection.length || !rows.has(String(baseline)) || selection.some(id => !rows.has(String(id)))) return null;
  return { baselineId: response.baseline_experiment_id, evaluationHash: response.evaluation_build_hash, rows: selection.map(id => rows.get(String(id))!) };
}

const textValue = (value: unknown): string => typeof value === 'string' && value.trim() ? value : '未提供';
const splitLabel = (value: unknown): string => ({ test: '测试集', validation: '验证集', train: '训练集' } as Record<string, string>)[String(value)] ?? textValue(value);

export function ExperimentComparison({ value, baseline }: { value: ComparisonView; baseline?: ExperimentItem }) {
  const headingId = useId();
  return <section className="ec-comparison" aria-labelledby={headingId} data-testid="experiment-comparison">
    <div className="ec-heading"><h3 id={headingId}>准确率对照</h3><span>固定量尺 0–100% · 基线 #{value.baselineId}</span></div>
    <p className="ec-verification">服务端已核对模型、训练预算、随机种子与完整评估协议一致。结果按勾选顺序呈现。</p>
    <dl className="ec-context">
      <div><dt>共同模型</dt><dd>{textValue(baseline?.model.name)}<small>版本 {textValue(baseline?.model.version)}</small></dd></div>
      <div><dt>训练预算</dt><dd>{textValue(baseline?.model.training_budget)}<small>随机种子 {formatCount(baseline?.model.seed)}</small></dd></div>
      <div><dt>固定评估版本</dt><dd>{baseline?.protocol.evaluation_build_id != null ? `#${baseline.protocol.evaluation_build_id}` : '未提供'} · {splitLabel(baseline?.protocol.split)}</dd></div>
    </dl>
    <p className="ec-hash"><span>评估清单 SHA-256</span><code>{value.evaluationHash}</code></p>
    <div className="ec-axis" aria-hidden="true"><span>实验 / 训练版本</span><div>{[0, 25, 50, 75, 100].map(tick => <span key={tick}>{tick}%</span>)}</div><span>准确率 / 较基线</span></div>
    <ol className="ec-rows">{value.rows.map(row => {
      const isBaseline = String(row.experimentId) === String(value.baselineId);
      const direction = row.delta === null || Math.abs(row.delta) < 0.00005 ? 'neutral' : row.delta > 0 ? 'positive' : 'negative';
      return <li key={row.experimentId} className={`ec-row${isBaseline ? ' is-baseline' : ''}`} data-testid={`comparison-experiment-${row.experimentId}`}>
        <div className="ec-row-name"><strong>实验 #{row.experimentId}</strong>{isBaseline && <span className="ec-baseline">基线</span>}<small>训练版本 #{row.buildId}</small></div>
        <div className="ec-row-bar">{row.accuracy === null ? <span className="ec-unknown">准确率未知，未绘制</span> : <div className="ec-track" aria-hidden="true"><span className="ec-fill" style={{ width: `${row.accuracy * 100}%` }} />{[25, 50, 75].map(tick => <i key={tick} style={{ left: `${tick}%` }} />)}</div>}</div>
        <div className="ec-row-values"><strong>{formatPercent(row.accuracy)}</strong><span className={`ec-delta is-${direction}`}>{isBaseline ? '对照基线' : formatPointChange(row.delta)}</span></div>
        <div className="ec-row-details"><span>覆盖 {formatPercent(row.coverage)}</span><span>预测 {formatCount(row.predictions)} / 应评估 {formatCount(row.expectedSamples)}</span><span>错误 {formatCount(row.errorCount)} 个</span></div>
      </li>;
    })}</ol>
    <p className="ec-note">百分点差值由服务端返回的准确率差换算；未提供或超出范围的指标显示为未知。准确率不代表数据在其他模型或任务上的收益。</p>
  </section>;
}
