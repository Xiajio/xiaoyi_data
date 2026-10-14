import { ArrowRight, Database, FolderSearch } from 'lucide-react';
import { getInventorySummary } from '../domain/model';
import type { AssetKind, DemoAction, DemoState } from '../domain/types';
import './inventory.css';

type Props = { state: DemoState; dispatch: (action: DemoAction) => void };
const KINDS: [AssetKind, string][] = [['text', '病历与报告'], ['table', '科研表格'], ['wsi', '病理切片'], ['image', '医学影像']];
const STATUS_LABELS = { pending: '待处理', processing: '处理中', processed: '已处理', failed: '处理失败', restricted: '受限待核查' };
export function InventoryOverview({ state, dispatch }: Props) {
  const summary = getInventorySummary(state);
  const navigate = (assetStatus: DemoState['assetFilter']) => dispatch({ type: 'NAVIGATE', page: 'processing', assetStatus });
  const unfinished = summary.total - summary.processed;
  return <section className="panel pp-inventory pp-space-bottom" aria-label="数据库整体情况">
    <div className="pp-panel-head"><div><h2><Database size={17} aria-hidden="true" />数据库整体情况</h2><p className="muted">演示资料目录 · 按资料项统计，人工字段核验另行计量</p></div><button className="btn" onClick={() => dispatch({ type: 'NAVIGATE', page: 'intake' })}><FolderSearch size={15} />扫描服务器目录</button></div>
    <div className="pp-inventory-metrics">
      <button onClick={() => navigate('all')}><span>资料总量</span><strong><span data-testid="inventory-total">{summary.total}</span><small>项</small></strong><span className="pp-inventory-hint">预置目录与扫描入库资料<ArrowRight size={13} /></span></button>
      <button onClick={() => navigate('processed')}><span>已完成模拟处理</span><strong><span data-testid="inventory-processed">{summary.processed}</span><small>项</small></strong><span className="pp-inventory-hint">仅统计成功完成的处理任务<ArrowRight size={13} /></span></button>
      <div><span>未完成处理</span><strong><span data-testid="inventory-unfinished">{unfinished}</span><small>项</small></strong><span className="pp-inventory-hint">包括待处理、处理中、失败与受限</span></div>
    </div>
    <div className="pp-inventory-body"><div className="pp-inventory-bar" aria-label={`总量 ${summary.total} 项，已处理 ${summary.processed} 项，未完成 ${unfinished} 项`}>{(['processed', 'processing', 'pending', 'failed', 'restricted'] as const).map(status => summary[status] > 0 ? <span key={status} className={`pp-inventory-segment is-${status}`} style={{ width: `${summary[status] / Math.max(summary.total, 1) * 100}%` }} title={`${STATUS_LABELS[status]} ${summary[status]} 项`} /> : null)}</div><div className="pp-inventory-breakdown">{(['pending', 'processing', 'failed', 'restricted'] as const).map(status => <button key={status} onClick={() => navigate(status)} aria-label={`查看${STATUS_LABELS[status]}资料 ${summary[status]} 项`}><i className={`is-${status}`} /><span>{STATUS_LABELS[status]}</span><strong data-testid={`inventory-${status}`}>{summary[status]}</strong><ArrowRight size={12} /></button>)}</div></div>
    <div className="table-wrap"><table className="pp-inventory-table"><thead><tr><th>资料类型</th><th>总量</th><th>已处理</th><th>待处理</th><th>处理中</th><th>失败</th><th>受限</th></tr></thead><tbody>{KINDS.map(([kind, label]) => { const counts = summary.byKind[kind]; return <tr key={kind}><th scope="row">{label}</th><td>{counts.total}</td><td>{counts.processed}</td><td>{counts.pending}</td><td>{counts.processing}</td><td>{counts.failed}</td><td>{counts.restricted}</td></tr>; })}</tbody></table></div>
    <p className="pp-inventory-footnote">“已建档”表示资料目录可供查看，不等于已处理。处理完成后仍需字段核验与发布审批；图像资料处理仅模拟基础建档。</p>
  </section>;
}
