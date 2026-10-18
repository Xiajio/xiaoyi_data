import { CatalogApi } from './api';
import { dateTime, formatBytes, LoadState, useApiData } from './ui';
import { countWidth, failureSummary, heartbeatSummary, knownCount, QUEUE_LABELS, QUEUE_NAMES, QUEUE_STATES, queueScale, snapshotFreshness, waitTime } from './queue-health';
import './queue-health.css';

const recordedTime = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? dateTime(value) : '未记录';
const countText = (value: unknown) => {
  const count = knownCount(value);
  return count === null ? '未返回' : count.toLocaleString('zh-CN');
};

// Mount only when canReview(role); /api/operations also enforces this permission.
export function QueueHealth({ api }: { api: CatalogApi }) {
  const status = useApiData(client => client.operations(), api, [], 15000);
  const data = status.data;
  const scale = queueScale(data?.queues ?? {});
  const freshness = snapshotFreshness(data?.as_of, Date.now(), Boolean(status.error));
  return <section className="panel pp-space-top pp-body qh-panel" aria-labelledby="queue-health-heading">
    <div className="pp-panel-head qh-heading"><div><h2 id="queue-health-heading">队列与存储健康</h2>
      <p className="muted">页面可见时每 15 秒刷新。任务等待与人工审批分别统计。</p></div>
      <button className="btn" disabled={status.loading} onClick={status.reload}>刷新健康状态</button></div>
    <LoadState loading={status.loading} error={status.error} onRetry={status.reload} />
    {data?.queues && data.approval_waiting ? <>
      <div className="qh-observed"><span>数据时间：{recordedTime(data.as_of)}</span><span>当前任务快照</span></div>
      {freshness.notice ? <p className="qh-stale" role="status">{freshness.notice}</p> : null}
      <figure className="qh-figure" aria-labelledby="queue-count-heading" aria-describedby="queue-count-note">
        <figcaption><h3 id="queue-count-heading">三队列任务数量</h3>
          <span>统一刻度：0 至 {scale.maximum.toLocaleString('zh-CN')} 项</span></figcaption>
        <p id="queue-count-note" className="qh-note">条长表示当前任务数量，不表示执行器利用率。延后任务尚未到执行时间。</p>
        <div className="qh-rows">{QUEUE_NAMES.map(name => {
          const queue = data.queues[name];
          const heartbeat = heartbeatSummary(queue);
          const failure = failureSummary(queue);
          const expired = knownCount(queue?.expired_running);
          return <section className="qh-row" key={name} aria-label={QUEUE_LABELS[name]}>
            <div className="qh-queue">
              <h3>{QUEUE_LABELS[name]}</h3>
              <span className={`qh-heartbeat ${freshness.stale ? 'is-stale' : `is-${heartbeat.tone}`}`}>
                <i aria-hidden="true" />{freshness.stale ? '快照记录：' : ''}{heartbeat.text}
              </span>
              <p>最早等待：{waitTime(queue?.oldest_wait_seconds, queue?.waiting)}</p>
              {expired !== null && expired > 0 ? <p className="qh-expired">运行中 {countText(expired)} 项租约过期，需检查执行器</p> : null}
              {expired === null ? <p className="qh-note">租约过期统计未返回</p> : null}
            </div>
            <dl className="qh-bars">{QUEUE_STATES.map(({ key, label }) => <div key={key}>
              <dt>{label}</dt><dd>
                <span className="qh-track" aria-hidden="true"><span className={`qh-bar qh-bar-${key}`} style={{ width: `${countWidth(queue?.[key], scale.maximum)}%` }} /></span>
                <strong>{countText(queue?.[key])}</strong>
              </dd>
            </div>)}</dl>
            <div className="qh-outcome"><span>历史失败率</span><strong>{failure.rate}</strong>
              <p>{countText(failure.failed)} 失败 / {countText(failure.finished)} 成功与失败</p>
              <small>取消 {countText(queue?.cancelled)} 项，未计入分母</small></div>
          </section>;
        })}</div>
      </figure>
      <p className="qh-note qh-definition">失败率 = 当前失败任务数 ÷（成功 + 失败任务数），覆盖保留的全部任务；取消、等待和运行任务不计入。有效心跳为最近 60 秒内仍在运行的执行器。</p>
      <details className="qh-details">
        <summary>查看队列数据明细</summary>
        <div className="table-wrap" tabIndex={0} role="region" aria-label="可横向滚动的队列健康明细"><table aria-label="队列健康">
          <thead><tr><th scope="col">队列</th><th scope="col">等待 / 延后</th><th scope="col">运行</th><th scope="col">失败 / 失败率</th><th scope="col">最早等待</th><th scope="col">执行器心跳</th></tr></thead>
          <tbody>{QUEUE_NAMES.map(name => {
            const queue = data.queues[name];
            const heartbeat = heartbeatSummary(queue);
            const failure = failureSummary(queue);
            return <tr key={name}>
              <th scope="row">{QUEUE_LABELS[name]}</th>
              <td>{countText(queue?.waiting)} / {countText(queue?.delayed)}</td>
              <td>{countText(queue?.running)}{(knownCount(queue?.expired_running) ?? 0) > 0 ? <small className="qh-expired">其中 {countText(queue?.expired_running)} 项租约过期</small> : null}</td>
              <td>{countText(queue?.failed)} / {failure.rate}<small>分母 {countText(failure.finished)} 项</small></td>
              <td>{waitTime(queue?.oldest_wait_seconds, queue?.waiting)}</td>
              <td>{heartbeat.text}<small>最后记录：{recordedTime(queue?.heartbeat?.last_seen_at)}</small></td>
            </tr>;
          })}</tbody></table></div>
      </details>
      <div className="qh-support">
        <section aria-labelledby="queue-storage-heading"><h3 id="queue-storage-heading">存储空间</h3>
          <dl className="qh-facts"><div><dt>磁盘可用</dt><dd>{formatBytes(knownCount(data.disk_free_bytes))}</dd></div>
            <div><dt>已登记资产体积</dt><dd>{formatBytes(knownCount(data.asset_bytes))}</dd></div></dl>
          <p className="qh-note">可用空间属于资产所在磁盘；登记资产体积不包含其他文件。服务未提供磁盘总量。</p>
        </section>
        <section aria-labelledby="queue-approval-heading"><h3 id="queue-approval-heading">等待人工处理</h3>
          <dl className="qh-facts"><div><dt>研究待审批</dt><dd>{countText(data.approval_waiting.research_runs)} 项</dd></div>
            <div><dt>下载待审批</dt><dd>{countText(data.approval_waiting.download_requests)} 项</dd></div>
            <div><dt>内容待核验</dt><dd>{countText(data.review_backlog)} 项</dd></div></dl>
          <p className="qh-note">人工审批与内容核验单独统计，不计入上方执行器队列。</p>
        </section>
      </div>
    </> : data ? <p role="alert">服务尚未返回完整队列统计，请确认服务版本。</p> : null}
  </section>;
}
