import { CatalogApi } from './api';
import { dateTime, formatBytes, LoadState, useApiData } from './ui';

const QUEUE_LABELS = { interactive: '交互任务', io: '下载与导入', cpu: '构建与计算' } as const;

function waitTime(seconds: number | null) {
  if (seconds === null) return '无等待';
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${(seconds / 3600).toFixed(1)} 小时`;
}

// Mount only when canReview(role); /api/operations also enforces this permission.
export function QueueHealth({ api }: { api: CatalogApi }) {
  const status = useApiData(client => client.operations(), api, [], 15000);
  const data = status.data;
  return <section className="panel pp-space-top pp-body" aria-labelledby="queue-health-heading">
    <div className="pp-panel-head"><div><h2 id="queue-health-heading">队列与存储健康</h2>
      <p className="muted">页面可见时每 15 秒刷新。任务等待与人工审批分别统计。</p></div>
      <button className="btn" disabled={status.loading} onClick={status.reload}>刷新健康状态</button></div>
    <LoadState loading={status.loading} error={status.error} onRetry={status.reload} />
    {data?.queues && data.approval_waiting ? <>
      <p className="pp-footnote">数据时间：{dateTime(data.as_of)}{status.error ? ' · 当前显示上次成功读取的结果' : ''}</p>
      <div className="table-wrap"><table aria-label="队列健康"><thead><tr><th>队列</th><th>等待 / 延后</th><th>运行</th><th>失败 / 失败率</th><th>最早等待</th><th>执行器心跳</th></tr></thead>
        <tbody>{(Object.keys(QUEUE_LABELS) as (keyof typeof QUEUE_LABELS)[]).map(name => {
          const queue = data.queues[name];
          const finished = queue.failed + queue.succeeded;
          return <tr key={name}>
            <td>{QUEUE_LABELS[name]}</td>
            <td>{queue.waiting} / {queue.delayed}</td>
            <td>{queue.running}{queue.expired_running > 0 ? <small className="cat-error">其中 {queue.expired_running} 项租约过期</small> : null}</td>
            <td>{queue.failed} / {finished ? `${(queue.failed / finished * 100).toFixed(1)}%` : '暂无完成任务'}</td>
            <td>{waitTime(queue.oldest_wait_seconds)}</td>
            <td><span className={`badge ${queue.heartbeat.healthy ? 'good' : 'bad'}`}>{queue.heartbeat.healthy ? `${queue.heartbeat.active_workers} 个在线` : '暂无有效心跳'}</span><small>{dateTime(queue.heartbeat.last_seen_at)}</small></td>
          </tr>;
        })}</tbody></table></div>
      <p className="pp-footnote">失败率 = 当前失败任务数 ÷（成功 + 失败任务数），覆盖保留的全部任务；取消、等待和运行任务不计入。有效心跳为最近 60 秒内仍在运行的执行器。</p>
      <dl className="cat-detail-grid">
        <dt>资产体积</dt><dd>{formatBytes(data.asset_bytes)}</dd>
        <dt>磁盘可用</dt><dd>{formatBytes(data.disk_free_bytes)}</dd>
        <dt>研究待审批</dt><dd>{data.approval_waiting.research_runs}</dd>
        <dt>下载待审批</dt><dd>{data.approval_waiting.download_requests}</dd>
        <dt>内容待核验</dt><dd>{data.review_backlog}</dd>
      </dl>
    </> : data ? <p role="alert">服务尚未返回完整队列统计，请确认服务版本。</p> : null}
  </section>;
}
