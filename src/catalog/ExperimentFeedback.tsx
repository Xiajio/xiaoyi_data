import { useEffect, useRef, useState, type FormEvent } from 'react';
import { CatalogApi, type ExperimentItem, type Id, type Role } from './api';
import { comparisonRequestIds, ExperimentComparison, formatCount, formatPercent, readComparison, type ComparisonView } from './ExperimentComparison';
import { canResearch, errorText, JsonValue, LoadState, saveBlob, StatusBadge, useApiData } from './ui';

export function ExperimentFeedback({ api, role, onToast, onSubmitted }: { api: CatalogApi; role: Role; onToast: (message: string) => void; onSubmitted: () => void }) {
  const experiments = useApiData(client => client.experiments(), api, [], 4000);
  const [form, setForm] = useState({ build: '', evaluation: '', predictions: '', name: '', version: '', budget: '', seed: '42', split: 'test' });
  const [selected, setSelected] = useState<Id[]>([]);
  const [baseline, setBaseline] = useState<Id | null>(null);
  const [comparison, setComparison] = useState<{ raw: Record<string, unknown>; view: ComparisonView | null; baseline?: ExperimentItem; api: CatalogApi } | null>(null);
  const [comparing, setComparing] = useState(false);
  const [comparisonError, setComparisonError] = useState('');
  const comparisonGeneration = useRef(0);
  const baselineId = comparisonRequestIds(selected, baseline)[0];
  useEffect(() => {
    comparisonGeneration.current += 1;
    setSelected([]); setBaseline(null); setComparison(null); setComparisonError(''); setComparing(false);
    return () => { comparisonGeneration.current += 1; };
  }, [api]);
  const clearComparison = () => {
    comparisonGeneration.current += 1;
    setComparison(null); setComparisonError(''); setComparing(false);
  };
  const compare = async () => {
    if (selected.length < 2 || selected.length > 20 || baselineId === undefined) return;
    const generation = ++comparisonGeneration.current;
    const selection = [...selected];
    const baselineSnapshot = experiments.data?.items.find(item => String(item.id) === String(baselineId));
    // Clear before awaiting: a failed or superseded request must never retain an old chart.
    setComparison(null); setComparisonError(''); setComparing(true);
    try {
      const raw = await api.compareExperiments(comparisonRequestIds(selection, baselineId));
      if (generation !== comparisonGeneration.current) return;
      setComparison({ raw, view: readComparison(raw, selection, baselineId), baseline: baselineSnapshot, api });
    } catch (reason) {
      if (generation === comparisonGeneration.current) setComparisonError(errorText(reason));
    } finally {
      if (generation === comparisonGeneration.current) setComparing(false);
    }
  };
  const [acting, setActing] = useState(false);
  const [error, setError] = useState('');
  const field = (key: keyof typeof form, value: string) => setForm(previous => ({ ...previous, [key]: value }));
  const act = async (work: () => Promise<unknown>) => {
    setActing(true); setError('');
    try { await work(); } catch (reason) { setError(errorText(reason)); } finally { setActing(false); }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if ([form.build, form.evaluation, form.predictions].some(value => !Number.isSafeInteger(Number(value)) || Number(value) <= 0) || !Number.isSafeInteger(Number(form.seed)) || Number(form.seed) < 0) { setError('请填写有效的版本、资产编号和随机种子。'); return; }
    void act(async () => {
      await api.importExperiment({ build_id: Number(form.build), prediction_asset_id: Number(form.predictions),
        model: { name: form.name, version: form.version, training_budget: form.budget, seed: Number(form.seed) },
        protocol: { version: '1', task: 'classification', evaluation_build_id: Number(form.evaluation), split: form.split, require_complete: true } });
      experiments.reload(); onSubmitted(); onToast('实验结果导入已排队。');
    });
  };
  return <section className="panel pp-space-top pp-body"><h2>实验反馈</h2>
    <p className="muted">接收外部分类模型的预测。对照比较要求模型版本、训练预算、随机种子和完整评估集一致。</p>
    {canResearch(role) && <details><summary>导入实验结果</summary><form onSubmit={submit} data-testid="experiment-form">
      <label className="pp-form-field">训练数据版本<input required inputMode="numeric" value={form.build} onChange={event => field('build', event.target.value)} /></label>
      <label className="pp-form-field">评估数据版本<input required inputMode="numeric" value={form.evaluation} onChange={event => field('evaluation', event.target.value)} /></label>
      <label className="pp-form-field">预测文件资产编号<input required inputMode="numeric" value={form.predictions} onChange={event => field('predictions', event.target.value)} /></label>
      <p className="pp-footnote">先批准导入 JSONL 或 Parquet 预测文件；每行包含 sample_id、评估版本 build_id 和 prediction，必须覆盖所选评估切分。</p>
      <label className="pp-form-field">模型名称<input required maxLength={256} value={form.name} onChange={event => field('name', event.target.value)} /></label>
      <label className="pp-form-field">模型版本<input required maxLength={256} value={form.version} onChange={event => field('version', event.target.value)} /></label>
      <label className="pp-form-field">训练预算<input required maxLength={256} value={form.budget} onChange={event => field('budget', event.target.value)} placeholder="例如相同 80 个训练样本、10 轮" /></label>
      <label className="pp-form-field">随机种子<input required inputMode="numeric" value={form.seed} onChange={event => field('seed', event.target.value)} /></label>
      <label className="pp-form-field">评估切分<select value={form.split} onChange={event => field('split', event.target.value)}><option value="test">测试集</option><option value="validation">验证集</option><option value="train">训练集</option></select></label>
      <button className="btn primary" disabled={acting || comparing}>提交实验结果</button>
    </form></details>}
    {error && <p className="cat-error" role="alert">{error}</p>}
    <LoadState loading={experiments.loading} error={experiments.error} onRetry={experiments.reload} />
    <div className="table-wrap"><table aria-label="实验记录"><thead><tr><th>对照</th><th>实验 / 训练版本</th><th>状态</th><th>模型</th><th>准确率</th><th>错误样本</th></tr></thead><tbody>{experiments.data?.items.map(item => <tr key={item.id} data-testid={`experiment-${item.id}`}>
      <td><input type="checkbox" aria-label={`选择实验 ${item.id}`} disabled={item.status !== 'completed'} checked={selected.includes(item.id)} onChange={event => { const checked = event.target.checked; setSelected(previous => checked ? [...previous, item.id] : previous.filter(id => id !== item.id)); if (!checked && String(baseline) === String(item.id)) setBaseline(null); clearComparison(); }} /></td>
      <td>#{item.id} / #{item.build_id}</td><td><StatusBadge status={item.status} />{item.error && <small className="cat-error">{item.error}</small>}</td><td>{item.model.name} · {item.model.version}</td><td>{formatPercent(item.metrics.accuracy, '待测量')}</td>
      <td>{canResearch(role) && item.status === 'completed' && <button className="btn small" disabled={acting || comparing} onClick={() => void act(async () => saveBlob(await api.experimentErrors(item.id), `experiment-${item.id}-errors.parquet`))}>下载错误样本（{formatCount(item.metrics.error_count)}）</button>}</td>
    </tr>)}</tbody></table></div>
    {experiments.data?.items.length === 0 && <p className="muted">尚未导入实验。构建数据版本并完成外部训练后，将逐样本预测回流到这里。</p>}
    <p className="pp-footnote">显示最近 100 项实验。先勾选基线实验，再选对照实验（最多 20 项）。</p>
    <div className="ec-toolbar">
      {selected.length > 0 && <label>对照基线<select aria-label="对照基线" value={String(baselineId)} onChange={event => { setBaseline(selected.find(id => String(id) === event.target.value) ?? null); clearComparison(); }}>
        {selected.map(id => <option key={id} value={String(id)}>实验 #{id}{String(id) === String(selected[0]) ? '（首选）' : ''}</option>)}
      </select></label>}
      <button className="btn" disabled={acting || comparing || selected.length < 2 || selected.length > 20} onClick={() => void compare()}>比较所选实验{baselineId !== undefined ? `（基线 #${baselineId}）` : ''}</button>
      {comparing && <span className="ec-working" role="status">正在核对实验与评估协议…</span>}
    </div>
    {selected.length > 20 && <p className="ec-response-warning" role="status">已选 {selected.length} 项；单次最多比较 20 项，请减少选择。</p>}
    {comparisonError && <p className="cat-error" role="alert">{comparisonError}</p>}
    {comparison?.api === api && <>
      {comparison.view ? <ExperimentComparison value={comparison.view} baseline={comparison.baseline} /> : <p className="ec-response-warning" role="status">对照响应缺少完整的实验清单，暂不能绘图。请查看原始结果或重新比较。</p>}
      <details className="ec-raw"><summary>查看原始对照 JSON</summary><JsonValue value={comparison.raw} /></details>
    </>}
  </section>;
}
