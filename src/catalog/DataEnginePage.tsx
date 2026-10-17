import { useState, type FormEvent } from 'react';
import { ExperimentFeedback } from './ExperimentFeedback';
import { CatalogApi, type DatasetBuild, type Role } from './api';
import { canResearch, canReview, errorText, saveBlob, JsonValue, LoadState, StatusBadge, useApiData } from './ui';

export function DataEnginePage({ api, role, kind, onToast }: { api: CatalogApi; role: Role; kind: string; onToast: (message: string) => void }) {
  const builds = useApiData(client => client.builds(), api, [], 4000);
  const jobs = useApiData(client => client.jobs(), api, [], 4000);
  const [assetIds, setAssetIds] = useState('');
  const [modality, setModality] = useState('table');
  const [textColumn, setTextColumn] = useState('text');
  const [labelColumn, setLabelColumn] = useState('');
  const [groupColumn, setGroupColumn] = useState('');
  const [deduplicate, setDeduplicate] = useState(true);
  const [acting, setActing] = useState(false);
  const [selected, setSelected] = useState<DatasetBuild | null>(null);
  const [details, setDetails] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState('');
  const act = async (work: () => Promise<unknown>, message: string) => {
    setActing(true); setError('');
    try { await work(); builds.reload(); jobs.reload(); onToast(message); }
    catch (reason) { setError(errorText(reason)); }
    finally { setActing(false); }
  };
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const ids = assetIds.split(/[,，\s]+/).filter(Boolean).map(Number);
    if (!ids.length || ids.some(id => !Number.isSafeInteger(id) || id <= 0)) { setError('请输入有效的资产编号。'); return; }
    void act(() => api.createBuild({ asset_ids: ids, modality, exact_dedup: deduplicate, seed: 42,
      ...(modality === 'text' ? { text_column: textColumn } : {}),
      ...(labelColumn ? { label_column: labelColumn } : {}), ...(groupColumn ? { group_column: groupColumn } : {}) }), '构建任务已排队。');
  };
  const inspect = (build: DatasetBuild) => void act(async () => {
    const [manifest, profiles] = await Promise.all([api.buildManifest(build.id), api.buildProfiles(build.id)]);
    setSelected(build); setDetails({ ...manifest, profiles: profiles.items });
  }, '已读取固定清单与测量记录。');
  return <>
    <header className="pp-heading"><div><h1>数据构建</h1><p className="muted pp-subtitle">从已批准的资产生成固定样本版本，查看质量问题并保留每次处理依据。</p></div></header>
    {canResearch(role) && <section className="panel pp-body"><h2>创建数据版本</h2><form onSubmit={submit}>
      <label className="pp-form-field">输入资产编号<input required value={assetIds} onChange={event => setAssetIds(event.target.value)} placeholder="例如 12, 13；在文件与下载范围中查看" /></label>
      <label className="pp-form-field">数据类型<select value={modality} onChange={event => setModality(event.target.value)}><option value="table">表格</option><option value="text">文本</option><option value="image">图像</option></select></label>
      {modality === 'text' && <label className="pp-form-field">文本字段<input required value={textColumn} onChange={event => setTextColumn(event.target.value)} /></label>}
      <label className="pp-form-field">标签字段（可选）<input value={labelColumn} onChange={event => setLabelColumn(event.target.value)} placeholder="例如 category" /></label>
      <label className="pp-form-field">实体分组字段（可选）<input value={groupColumn} onChange={event => setGroupColumn(event.target.value)} placeholder="例如 document_id；同组样本进入同一切分" /></label>
      <label><input type="checkbox" checked={deduplicate} onChange={event => setDeduplicate(event.target.checked)} />精确去重，保留排除原因</label>
      <p className="pp-footnote">固定种子 42，训练 / 验证 / 测试比例 80% / 10% / 10%；比例按组分配，实际样本量可能不同。图像配对与高级配方可通过 CLI 或 API 提交。</p>
      <button className="btn primary" disabled={acting}>提交构建</button>
    </form></section>}
    {error && <p role="alert" className="cat-error">{error}</p>}
    <section className="panel pp-space-top pp-body"><h2>固定数据版本</h2><p className="pp-footnote">显示最近 100 个版本；完整历史可通过 API 分页读取。</p><LoadState loading={builds.loading} error={builds.error} onRetry={builds.reload} />
      <div className="table-wrap"><table><thead><tr><th>版本</th><th>状态</th><th>样本数</th><th>操作</th></tr></thead><tbody>{builds.data?.items.map(build => <tr key={build.id} data-testid={`build-${build.id}`}><td>#{build.id}</td><td><StatusBadge status={build.status} />{build.error && <small className="cat-error">{build.error}</small>}</td><td>{build.sample_count}</td><td><div className="cat-actions">{['ready', 'published'].includes(build.status) && <>
        <button className="btn small" disabled={acting} onClick={() => inspect(build)}>清单与测量</button>
        {canResearch(role) && <button className="btn small" disabled={acting} onClick={() => void act(() => api.mineBuild(build.id), '重复与标签疑点检查已排队。')}>检查数据问题</button>}
        {canResearch(role) && build.status === 'published' && <button className="btn small" disabled={acting} onClick={() => void act(() => api.submitJob('build.export', { build_id: Number(build.id) }), '训练包已排队，完成后在下方任务中下载。')}>导出训练包</button>}
        {canReview(role) && kind === 'human' && build.status === 'ready' && <button className="btn small" disabled={acting} onClick={() => void act(() => api.publishBuild(build.id, build.manifest_hash), '固定数据版本已批准发布。')}>批准发布</button>}
      </>}</div></td></tr>)}</tbody></table></div>
      {builds.data?.items.length === 0 && <p className="muted">还没有数据构建。先批准获取资产，再提交处理配方。</p>}
      {selected && details && <details open><summary>版本 #{selected.id} 的固定清单与测量</summary><JsonValue value={details} /></details>}
    </section>
    <ExperimentFeedback api={api} role={role} onToast={onToast} onSubmitted={jobs.reload} />
    <section className="panel pp-space-top pp-body"><h2>我的后台任务</h2><p className="pp-footnote">显示最近 100 项任务。</p><LoadState loading={jobs.loading} error={jobs.error} onRetry={jobs.reload} />
      <div className="table-wrap"><table><thead><tr><th>任务</th><th>操作</th><th>状态</th><th>执行次数</th><th>管理</th></tr></thead><tbody>{jobs.data?.items.map(job => <tr key={job.id} data-testid={`background-job-${job.id}`}><td>#{job.id}</td><td>{jobLabel(job.kind)}</td><td><StatusBadge status={job.status} />{job.error && <small className="cat-error">{job.error}</small>}</td><td>{job.attempts}</td><td>{job.kind === 'build.export' && job.status === 'succeeded' && <button className="btn small" onClick={() => void act(async () => saveBlob(await api.jobArtifact(job.id), `dataset-build-${String(job.result.build_id)}.zip`), '训练包已下载。')}>下载训练包</button>}{canResearch(role) && ['queued','running'].includes(job.status) && <button className="btn small" disabled={acting || job.cancel_requested} onClick={() => void act(() => api.cancelJob(job.id), '已请求取消。')}>{job.cancel_requested ? '取消中…' : '取消'}</button>}{canResearch(role) && ['failed','cancelled'].includes(job.status) && ['asset.derive','profile.run','bundle.build','build.run','mining.run','experiment.import','build.export'].includes(job.kind) && <button className="btn small" disabled={acting} onClick={() => void act(() => api.retryJob(job.id), '重试已排队。')}>重试</button>}</td></tr>)}</tbody></table></div>
    </section>
  </>;
}

function jobLabel(kind: string) {
  return ({ 'qa.turn': '目录问答', 'summary.run': '整理摘要', 'research.run': '研究任务', 'collection.run': '来源采集', 'download.run': '下载文件', 'asset.import': '导入文件', 'asset.derive': '派生资产', 'bundle.build': '研究包打包', 'build.export': '训练包导出', 'profile.run': '质量画像', 'build.run': '样本构建', 'mining.run': '数据问题检查', 'experiment.import': '实验回流' } as Record<string, string>)[kind] || kind;
}
