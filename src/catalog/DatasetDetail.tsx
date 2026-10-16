import { useState } from 'react';
import { X } from 'lucide-react';
import { CatalogApi, type DatasetItem, type Id, type Role } from './api';
import { VersionAssets } from './Assets';
import { JsonValue, LoadState, StatusBadge, useApiData } from './ui';
export function DatasetDetail({ api, id, role, onToast, onClose }: { api: CatalogApi; id: Id; role: Role; onToast: (text: string) => void; onClose: () => void }) {
  const { data, error, loading, reload } = useApiData(client => client.dataset(id), api, [id]); const [versionId, setVersionId] = useState('');
  if (loading || error || !data) return <section className="panel pp-space-top"><LoadState loading={loading} error={error} onRetry={reload} /></section>;
  const { dataset, evidence, enrichments } = data; const versions = [...data.versions].sort((a, b) => Number(b.id) - Number(a.id)); const version = versions.find(v => String(v.id) === versionId) ?? versions[0];
  const latestId = dataset.dataset_version_id ?? versions[0]?.id;
  const metadata = (version?.metadata ?? (String(version?.id) === String(latestId) ? dataset : null)) as Partial<DatasetItem> | null;
  const versionEvidence = evidence.filter(item => item.dataset_version_id != null ? String(item.dataset_version_id) === String(version?.id) : String(version?.id) === String(latestId));
  const versionEnrichments = enrichments.filter(item => item.dataset_version_id != null ? String(item.dataset_version_id) === String(version?.id) : String(version?.id) === String(latestId));
  return <section className="panel pp-space-top" data-testid="dataset-detail"><div className="pp-panel-head"><div><h2>{dataset.source_key}</h2><a href={dataset.url} target="_blank" rel="noreferrer">查看官方来源</a></div><button className="btn quiet" onClick={onClose} aria-label="关闭详情"><X size={16} /></button></div>
    <div className="pp-body"><label className="pp-form-field">查看数据版本<select value={String(version?.id ?? '')} onChange={event => setVersionId(event.target.value)}>{versions.map(v => <option key={v.id} value={v.id}>#{v.id} · {v.revision}</option>)}</select></label>
      {version?.provenance_status && <StatusBadge status={version.provenance_status} />}
      {metadata ? <dl className="cat-metadata"><div><dt>许可（来源自报）</dt><dd>{metadata.license || '未标注'} · {({ permissive: '宽松许可', copyleft: '著佐权', 'research-only': '仅限研究' } as Record<string, string>)[metadata.license_class ?? ''] ?? '许可未知'}</dd></div><div><dt>规模（来源自报）</dt><dd>{metadata.size_rows == null ? '未知' : `${metadata.size_rows} 行`}</dd></div><div><dt>任务类别</dt><dd>{metadata.task_categories?.join('、') || '未标注'}</dd></div><div><dt>语言</dt><dd>{metadata.languages?.join('、') || '未标注'}</dd></div></dl> : <p className="cat-error">历史版本元数据尚未验证。请核对该版本的原始证据。</p>}
      <div className="cat-evidence-layout pp-space-top"><section><h3>整理候选（{versionEnrichments.length}）</h3>{versionEnrichments.map(item => <div key={item.id} className="cat-candidate"><div className="cat-review-head"><strong>{item.kind === 'quality' ? '目录信息完整度' : item.kind}</strong><StatusBadge status={item.status} /></div><p className="pp-footnote">{item.producer} · {item.status === 'confirmed' || item.status === 'corrected' ? '人工确认' : '自动整理候选'}</p><JsonValue value={item.value} /></div>)}{!versionEnrichments.length && <p className="muted">该版本尚无整理结果。</p>}</section>
      <section><h3>版本证据摘录</h3>{versionEvidence.map(item => <blockquote key={item.id} className="cat-evidence"><p className="pp-footnote">#{item.id} · {item.kind} · {item.location}</p><p>{item.excerpt || '未提供摘录'}</p></blockquote>)}{!versionEvidence.length && <p className="muted">该版本没有证据记录，不能借用最新版本的证据。</p>}</section></div>
      {version && <VersionAssets api={api} id={version.id} key={version.id} role={role} onToast={onToast} />}
    </div></section>;
}
