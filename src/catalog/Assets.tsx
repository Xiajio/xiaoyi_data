import { useState, type FormEvent } from 'react';
import { Download, RotateCcw } from 'lucide-react';
import { CatalogApi, type DownloadRequest, type Id, type Role } from './api';
import { canResearch, canReview, dateTime, errorText, formatBytes, JsonValue, LoadState, StatusBadge, useApiData } from './ui';
export function VersionAssets({ api, id, role, onToast }: { api: CatalogApi; id: Id; role: Role; onToast: (text: string) => void }) {
  const assets = useApiData(client => client.assets(id), api, [id]); const files = useApiData(client => client.versionFiles(id), api, [id]);
  const [selected, setSelected] = useState<string[]>([]); const [preview, setPreview] = useState<Id | null>(null); const [maxMb, setMaxMb] = useState('100'); const [acting, setActing] = useState(false); const [error, setError] = useState('');
  const submit = async (event: FormEvent) => { event.preventDefault(); setActing(true); setError(''); try { await api.requestDownload(id, selected, Math.floor(Number(maxMb) * 1024 ** 2)); onToast('下载申请已提交，请到下载管理核对并审批。'); setSelected([]); } catch (reason) { setError(errorText(reason)); } finally { setActing(false); } };
  const estimated = (files.data?.items ?? []).filter(item => selected.includes(item.path)).reduce((sum, item) => sum + (item.size ?? 0), 0);
  return <div className="cat-version-assets"><h3>文件与下载范围</h3><p className="pp-footnote">仅获取所选文件；样本预览也需先批准下载。版本 #{id}</p><LoadState loading={files.loading} error={files.error} onRetry={files.reload} />
    {files.data && !files.loading && <form onSubmit={submit} className="cat-download-form"><p className="pp-footnote cat-break">锁定版本：{files.data.revision || '未提供可下载版本'}</p><div className="cat-file-list">{files.data.items.map(item => <label key={item.path}><input type="checkbox" checked={selected.includes(item.path)} onChange={e => setSelected(e.target.checked ? [...selected, item.path] : selected.filter(path => path !== item.path))} disabled={!canResearch(role) || acting} /><span>{item.path}</span><small>{formatBytes(item.size)}</small></label>)}{!files.data.items.length && <p className="muted">该版本没有可获取的文件清单。人工登记来源需要提供可验证的文件信息。</p>}</div>
      <div className="cat-download-actions"><label className="pp-form-field">下载上限（MB）<input type="number" min="0.001" step="0.001" required value={maxMb} onChange={e => setMaxMb(e.target.value)} /></label><p className="pp-footnote">已选 {selected.length} 个文件 · 已知体积 {formatBytes(estimated)}</p><button className="btn primary" disabled={!canResearch(role) || acting || !selected.length || !Number(maxMb)}><Download size={14} />{acting ? '正在申请…' : '申请下载'}</button></div>{error && <p className="cat-error" role="alert">{error}</p>}
    </form>}
    <h3 className="pp-space-top">已获取资产与内容报告</h3><LoadState loading={assets.loading} error={assets.error} onRetry={assets.reload} />
    {assets.data && !assets.loading && <><div className="table-wrap"><table><thead><tr><th>文件</th><th>实际体积</th><th>SHA-256</th><th>查看内容</th></tr></thead><tbody>{assets.data.items.map(item => <tr key={item.path}><td>{item.path}{item.id != null && <small>资产 #{item.id}</small>}</td><td>{formatBytes(item.size_bytes ?? item.size)}</td><td><code className="cat-hash">{item.sha256}</code></td><td>{item.id != null && <button className="btn small" onClick={() => setPreview(item.id!)}>预览样本</button>}</td></tr>)}</tbody></table></div>{!assets.data.items.length && <p className="muted">还没有下载文件。批准并完成下载后可查看实际测量结果。</p>}
      {assets.data.overlap && assets.data.overlap.length > 0 && <details className="cat-report"><summary>切分重叠检查</summary><JsonValue value={assets.data.overlap} /></details>}
      {preview !== null && <AssetPreviewPanel key={preview} api={api} id={preview} onClose={() => setPreview(null)} />}
      {assets.data.reports.map((report, index) => <details key={String(report.id ?? index)} className="cat-report" open={index === 0}><summary>{report.kind || '内容剖析'} <span className="badge">实际测量</span></summary><p className="pp-footnote">请结合报告的样本数和覆盖范围判断结论；抽样结果仅代表已检查的文件。</p><JsonValue value={report.report ?? report.value ?? report} /></details>)}
    </>}
  </div>;
}
function DownloadRow({ item, api, role, reload, onToast }: { item: DownloadRequest; api: CatalogApi; role: Role; reload: () => void; onToast: (text: string) => void }) {
  const [note, setNote] = useState(''); const [acting, setActing] = useState(false);
  const decide = async (decision: 'approved' | 'rejected') => { setActing(true); try { await api.decideDownload(item.id, decision, note); reload(); onToast(decision === 'approved' ? '下载范围已批准，等待执行。' : '下载申请已驳回。'); } catch (reason) { onToast(errorText(reason)); } finally { setActing(false); } };
  return <article className="cat-download-item" data-testid={`download-${item.id}`}><div className="cat-review-head"><div><h3>{item.dataset || `数据版本 #${item.dataset_version_id}`}</h3><p className="pp-footnote">申请 #{item.id} · {dateTime(item.created_at)}</p></div><StatusBadge status={item.status} /></div>
    <dl className="cat-metadata"><div><dt>锁定版本</dt><dd className="cat-break">{item.revision || `#${item.dataset_version_id}`}</dd></div><div><dt>预计 / 上限</dt><dd>{formatBytes(item.estimated_bytes)} / {formatBytes(item.max_bytes)}</dd></div>{item.actual_bytes != null && <div><dt>实际获取</dt><dd>{formatBytes(item.actual_bytes)}</dd></div>}</dl>
    <details><summary>批准范围 · {item.files.length} 个文件</summary><ul className="cat-paths">{item.files.map(file => <li key={file}>{file}</li>)}</ul></details>{item.license_basis != null && <details><summary>许可依据</summary><JsonValue value={item.license_basis} /></details>}
    {item.error && <p className="cat-error" role="alert">{item.error}</p>}{item.note && <p className="pp-footnote">审批说明：{item.note}</p>}
    {item.status === 'pending' && <div className="pp-space-top">{canReview(role) ? <><label className="pp-form-field">审批说明<input value={note} onChange={e => setNote(e.target.value)} placeholder="记录许可、文件范围与研究用途的核验依据" /></label><div className="cat-actions"><button className="btn" disabled={acting} onClick={() => void decide('rejected')}>驳回下载</button><button className="btn primary" disabled={acting} onClick={() => void decide('approved')}>批准下载范围</button></div></> : <p className="pp-permission">等待审核员或管理员核验许可与文件范围。</p>}</div>}
    {item.status === 'completed' && <details className="pp-space-top"><summary>查看资产与质量报告</summary><VersionAssets api={api} id={item.dataset_version_id} role={role} onToast={onToast} /></details>}
  </article>;
}
export function DownloadsPage({ api, role, onToast }: { api: CatalogApi; role: Role; onToast: (text: string) => void }) {
  const requests = useApiData(client => client.downloadRequests(), api, [], 4000);
  return <><header className="pp-heading"><div><h1>下载管理</h1><p className="muted pp-subtitle">核对版本、许可和文件范围，批准后由后台获取并校验。</p></div><button className="btn" onClick={requests.reload}><RotateCcw size={14} />刷新申请</button></header><LoadState loading={requests.loading} error={requests.error} onRetry={requests.reload} />{requests.data && !requests.loading && <section className="panel cat-downloads">{requests.data.items.map(item => <DownloadRow key={item.id} item={item} api={api} role={role} reload={requests.reload} onToast={onToast} />)}{!requests.data.items.length && <div className="empty"><Download size={24} /><strong>还没有下载申请</strong><p>在数据目录打开具体版本，选择所需文件并提交下载申请。</p></div>}</section>}</>;
}
function AssetPreviewPanel({ api, id, onClose }: { api: CatalogApi; id: Id; onClose: () => void }) {
  const { data, error, loading, reload } = useApiData(client => client.previewAsset(id), api, [id]);
  const rows = data?.kind === 'table' ? data.rows.map(row => row !== null && typeof row === 'object' && !Array.isArray(row) ? row : { value: row }) : [];
  const columns = [...new Set(rows.flatMap(row => Object.keys(row)))];
  return <section className="cat-preview"><div className="cat-review-head"><h3>已获取文件 · 样本预览</h3><button className="btn quiet" onClick={onClose}>关闭预览</button></div><p className="pp-footnote">预览来自已批准获取的文件，仅用于检查样本，不代表全量数据。</p><LoadState loading={loading} error={error} onRetry={reload} />{data && !loading && <>
    {data.kind === 'image' && <figure><img src={data.data_url} alt="已下载图像的缩略预览" className="cat-preview-image" /><figcaption className="pp-footnote">原图 {data.width} × {data.height} · 缩略图</figcaption></figure>}
    {data.kind === 'text' && <><p className="pp-footnote">最多预览 {data.limit} 行</p><pre className="cat-json">{data.text}</pre></>}
    {data.kind === 'table' && <><p className="pp-footnote">本次展示 {data.rows.length} 行 · 上限 {data.limit} 行</p><div className="table-wrap cat-preview-table"><table><thead><tr>{columns.map(column => <th key={column}>{column}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={index}>{columns.map(column => <td key={column}>{row[column] == null ? <span className="muted">空值</span> : typeof row[column] === 'object' ? JSON.stringify(row[column]) : String(row[column])}</td>)}</tr>)}</tbody></table></div></>}
    {(data.kind === '3d' || data.kind === 'file') && <><p className="pp-footnote">{data.note}</p><JsonValue value={data.report} /></>}
  </>}</section>;
}
