import { useState, type FormEvent } from 'react';
import { CatalogApi, type Id, type Role } from './api';
import { canResearch, errorText, JsonValue, LoadState, saveBlob, StatusBadge, useApiData } from './ui';

export function ExperimentFeedback({ api, role, onToast, onSubmitted }: { api: CatalogApi; role: Role; onToast: (message: string) => void; onSubmitted: () => void }) {
  const experiments = useApiData(client => client.experiments(), api, [], 4000);
  const [form, setForm] = useState({ build: '', evaluation: '', predictions: '', name: '', version: '', budget: '', seed: '42', split: 'test' });
  const [selected, setSelected] = useState<Id[]>([]);
  const [comparison, setComparison] = useState<Record<string, unknown> | null>(null);
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
      <button className="btn primary" disabled={acting}>提交实验结果</button>
    </form></details>}
    {error && <p className="cat-error" role="alert">{error}</p>}
    <LoadState loading={experiments.loading} error={experiments.error} onRetry={experiments.reload} />
    <div className="table-wrap"><table><thead><tr><th>对照</th><th>实验 / 训练版本</th><th>状态</th><th>模型</th><th>准确率</th><th>错误样本</th></tr></thead><tbody>{experiments.data?.items.map(item => <tr key={item.id} data-testid={`experiment-${item.id}`}>
      <td><input type="checkbox" aria-label={`选择实验 ${item.id}`} disabled={item.status !== 'completed'} checked={selected.includes(item.id)} onChange={event => { setSelected(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id)); setComparison(null); }} /></td>
      <td>#{item.id} / #{item.build_id}</td><td><StatusBadge status={item.status} />{item.error && <small className="cat-error">{item.error}</small>}</td><td>{item.model.name} · {item.model.version}</td><td>{typeof item.metrics.accuracy === 'number' ? `${(item.metrics.accuracy * 100).toFixed(2)}%` : '待测量'}</td>
      <td>{canResearch(role) && item.status === 'completed' && <button className="btn small" disabled={acting} onClick={() => void act(async () => saveBlob(await api.experimentErrors(item.id), `experiment-${item.id}-errors.parquet`))}>下载错误样本（{item.metrics.error_count ?? 0}）</button>}</td>
    </tr>)}</tbody></table></div>
    {experiments.data?.items.length === 0 && <p className="muted">尚未导入实验。构建数据版本并完成外部训练后，将逐样本预测回流到这里。</p>}
    <p className="pp-footnote">显示最近 100 项实验。先勾选基线实验，再选对照实验（最多 20 项）。</p>
    <button className="btn" disabled={acting || selected.length < 2 || selected.length > 20} onClick={() => void act(async () => setComparison(await api.compareExperiments(selected)))}>比较所选实验{selected.length > 0 ? `（基线 #${selected[0]}）` : ''}</button>
    {comparison && <JsonValue value={comparison} />}
  </section>;
}
