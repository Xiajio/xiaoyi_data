import { useState, type FormEvent } from 'react';
import { BuildQuality } from './BuildQuality';
import { ExperimentFeedback } from './ExperimentFeedback';
import { CatalogApi, type Id, type Role } from './api';
import { canResearch, canReview, errorText, saveBlob, formatBytes, LoadState, StatusBadge, useApiData } from './ui';

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
  const [selectedId, setSelectedId] = useState<Id | null>(null);
  const [filter, setFilter] = useState('all');
  const items = builds.data?.items ?? [];
  const filters = [{ key: 'all', label: '全部版本' }, { key: 'ready', label: '待发布' }, { key: 'published', label: '已发布' }, { key: 'active', label: '处理中' }, { key: 'failed', label: '失败' }];
  const matches = (status: string, key: string) => key === 'all' || (key === 'active' ? ['queued', 'running'].includes(status) : status === key);
  const visibleBuilds = items.filter(build => matches(build.status, filter));
  const selected = visibleBuilds.find(build => build.id === selectedId && ['ready', 'published'].includes(build.status)) ?? visibleBuilds.find(build => ['ready', 'published'].includes(build.status));
  const inspect = (id: Id) => {
    setSelectedId(id);
    requestAnimationFrame(() => {
      const region = document.getElementById('selected-build-quality');
      region?.querySelector<HTMLElement>('h3')?.focus({ preventScroll: true });
      region?.scrollIntoView({ block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
    });
  };
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
  return <>
    <header className="pp-heading"><div><h1>数据构建</h1><p className="muted pp-subtitle">从已批准的资产生成固定样本版本，查看质量问题并保留每次处理依据。</p></div></header>
    {canResearch(role) && <section className="panel pp-body dv-build-form"><h2>创建数据版本</h2><form onSubmit={submit}><div className="dv-form-grid">
      <label className="pp-form-field dv-form-wide">输入资产编号<input required value={assetIds} onChange={event => setAssetIds(event.target.value)} placeholder="例如 12, 13；在文件与下载范围中查看" /></label>
      <label className="pp-form-field">数据类型<select value={modality} onChange={event => setModality(event.target.value)}><option value="table">表格</option><option value="text">文本</option><option value="image">图像</option></select></label>
      {modality === 'text' && <label className="pp-form-field">文本字段<input required value={textColumn} onChange={event => setTextColumn(event.target.value)} /></label>}
      <label className="pp-form-field">标签字段（可选）<input value={labelColumn} onChange={event => setLabelColumn(event.target.value)} placeholder="例如 category" /></label>
      <label className="pp-form-field">实体分组字段（可选）<input value={groupColumn} onChange={event => setGroupColumn(event.target.value)} placeholder="例如 document_id；同组样本进入同一切分" /></label>
      </div><label><input type="checkbox" checked={deduplicate} onChange={event => setDeduplicate(event.target.checked)} />精确去重，保留排除原因</label>
      <div className="dv-form-footer"><p className="pp-footnote">固定种子 42，训练 / 验证 / 测试比例 80% / 10% / 10%；比例按组分配，实际样本量可能不同。图像配对与高级配方可通过 CLI 或 API 提交。</p>
      <button className="btn primary" disabled={acting}>提交构建</button></div>
    </form></section>}
    {error && <p role="alert" className="cat-error">{error}</p>}
    <section className="panel pp-space-top pp-body"><div className="dv-workspace-head"><div><h2>固定数据版本</h2><p className="pp-footnote">最近 100 个版本；各版本独立计数，不合并为唯一样本总量。</p></div><button className="btn small" disabled={builds.loading} onClick={builds.reload}>刷新版本</button></div><LoadState loading={builds.loading} error={builds.error} onRetry={builds.reload} />
      <div className="dv-filters" role="group" aria-label="筛选构建状态">{filters.map(item => <button key={item.key} aria-pressed={filter === item.key} onClick={() => setFilter(item.key)}>{item.label}<b>{items.filter(build => matches(build.status, item.key)).length}</b></button>)}</div>
      <div className="table-wrap"><table className="dv-build-table"><thead><tr><th>版本 / 数据类型</th><th>状态</th><th>样本数</th><th>操作</th></tr></thead><tbody>{visibleBuilds.map(build => <tr key={build.id} className={selected?.id === build.id ? 'dv-selected-row' : ''} data-testid={`build-${build.id}`}><td className="dv-build-identity"><strong>#{build.id}</strong><small>{({ text: '文本', table: '表格', image: '图像' } as Record<string, string>)[String(build.recipe.modality)] ?? '类型未提供'} · {Array.isArray(build.recipe.asset_ids) ? `${build.recipe.asset_ids.length} 个资产` : '输入待读取'}</small></td><td><StatusBadge status={build.status} />{build.error && <small className="cat-error">{build.error}</small>}</td><td>{['ready', 'published'].includes(build.status) ? build.sample_count.toLocaleString('zh-CN') : '待完成'}</td><td><div className="cat-actions">{['ready', 'published'].includes(build.status) && <>
        <button className="btn small" aria-pressed={selected?.id === build.id} onClick={() => inspect(build.id)}>清单与测量</button>
        {canResearch(role) && <button className="btn small" disabled={acting} onClick={() => void act(() => api.mineBuild(build.id), '重复与标签疑点检查已排队。')}>检查数据问题</button>}
        {canResearch(role) && build.status === 'published' && <button className="btn small" disabled={acting} onClick={() => void act(() => api.submitJob('build.export', { build_id: Number(build.id) }), '训练包已排队，完成后在下方任务中下载。')}>导出训练包</button>}
        {canReview(role) && kind === 'human' && build.status === 'ready' && <button className="btn small" disabled={acting} onClick={() => void act(() => api.publishBuild(build.id, build.manifest_hash), '固定数据版本已批准发布。')}>批准发布</button>}
      </>}</div></td></tr>)}</tbody></table></div>
      {builds.data?.items.length === 0 && <p className="muted">还没有数据构建。先批准获取资产，再提交处理配方。</p>}
      {items.length > 0 && visibleBuilds.length === 0 ? <p className="dv-empty-filter">此状态下没有版本。选择「全部版本」查看其他构建。</p> : null}
      {selected ? <BuildQuality key={selected.id} api={api} build={selected} /> : null}
    </section>
    <ExperimentFeedback api={api} role={role} onToast={onToast} onSubmitted={jobs.reload} />
    <section className="panel pp-space-top pp-body"><h2>我的后台任务</h2><p className="pp-footnote">显示最近 100 项任务，进度按当前处理阶段统计。</p><div className="dv-job-summary">{[{key: 'running', label: '运行中'}, {key: 'queued', label: '等待执行'}, {key: 'failed', label: '失败'}].map(item => <span key={item.key}>{item.label}<b>{jobs.data ? jobs.data.items.filter(job => job.status === item.key).length : '未读取'}</b></span>)}</div><LoadState loading={jobs.loading} error={jobs.error} onRetry={jobs.reload} />
      <div className="table-wrap"><table><thead><tr><th>任务</th><th>操作</th><th>状态</th><th>执行次数</th><th>管理</th></tr></thead><tbody>{jobs.data?.items.map(job => <tr key={job.id} data-testid={`background-job-${job.id}`}><td>#{job.id}</td><td>{jobLabel(job.kind)}</td><td><StatusBadge status={job.status} /><JobProgress value={job.progress} />{job.error && <small className="cat-error">{job.error}</small>}</td><td>{job.attempts}</td><td>{job.kind === 'build.export' && job.status === 'succeeded' && <button className="btn small" onClick={() => void act(async () => saveBlob(await api.jobArtifact(job.id), `dataset-build-${String(job.result.build_id)}.zip`), '训练包已下载。')}>下载训练包</button>}{canResearch(role) && ['queued','running'].includes(job.status) && <button className="btn small" disabled={acting || job.cancel_requested} onClick={() => void act(() => api.cancelJob(job.id), '已请求取消。')}>{job.cancel_requested ? '取消中…' : '取消'}</button>}{canResearch(role) && ['failed','cancelled'].includes(job.status) && ['asset.derive','profile.run','bundle.build','build.run','mining.run','experiment.import','build.export'].includes(job.kind) && <button className="btn small" disabled={acting} onClick={() => void act(() => api.retryJob(job.id), '重试已排队。')}>重试</button>}</td></tr>)}</tbody></table></div>
    </section>
  </>;
}

function jobLabel(kind: string) {
  return ({ 'qa.turn': '目录问答', 'summary.run': '整理摘要', 'research.run': '研究任务', 'collection.run': '来源采集', 'download.run': '下载文件', 'asset.import': '导入文件', 'asset.derive': '派生资产', 'bundle.build': '研究包打包', 'build.export': '训练包导出', 'profile.run': '质量画像', 'build.run': '样本构建', 'mining.run': '数据问题检查', 'experiment.import': '实验回流' } as Record<string, string>)[kind] || kind;
}

function JobProgress({ value }: { value: Record<string, unknown> }) {
  if (typeof value.stage !== 'string' || typeof value.processed !== 'number' || !Number.isFinite(value.processed) || value.processed < 0) return null;
  const labels: Record<string, string> = { reading_rows: '读取样本', selecting_rows: '选择样本', writing_shards: '写入分片', completed: '处理完成', copying_objects: '打包文件', deduplicating: '精确去重' };
  const counts = value.counts as Record<string, unknown> | undefined;
  const total = typeof value.total === 'number' && Number.isFinite(value.total) && value.total > 0 ? value.total : null;
  const processed = Math.max(0, value.processed);
  return <span className="dv-job-progress"><small className="muted">{labels[value.stage] || '处理中'}：{value.processed.toLocaleString()}{typeof value.total === 'number' ? ` / ${value.total.toLocaleString()}` : ''}{typeof counts?.bytes_copied === 'number' ? ` · ${formatBytes(counts.bytes_copied)}` : ''}</small>{total !== null ? <progress aria-label={`${labels[value.stage] || '当前阶段'}进度`} value={Math.min(processed, total)} max={total} /> : null}</span>;
}
