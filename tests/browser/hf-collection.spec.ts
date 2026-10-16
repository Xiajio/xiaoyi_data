import { dashboardFixture } from './dashboard-fixture';
import { expect, test, type Page } from '@playwright/test';
import type { CollectionJob, CollectionSchedule, CollectionScope, Role } from '../../src/catalog/api';

type Call = { path: string; body: Record<string, unknown> };
const defaultScope: CollectionScope = { search: '', author: '', languages: [], task_categories: [], limit: 50 };
async function fixture(page: Page, role: Role = 'admin', kind = 'human') {
  const calls: Call[] = []; const paths: string[] = []; const jobs: CollectionJob[] = []; const schedules: CollectionSchedule[] = [];
  const makeJob = (id: number, scope = defaultScope): CollectionJob => ({ id, actor: 'member', scope, status: 'queued', stage: 'queued', trigger: 'manual', stats: { found: 0, new: 0, updated: 0, duplicates: 0, skipped_stale: 0, failed: 0, processed: 0, failures: [] }, analysis_status: 'pending', dataset_version_ids: [], attempts: 0, created_at: '2026-10-16T01:00:00Z' });
  await page.route('**/api/**', async route => {
    const req = route.request(); const path = new URL(req.url()).pathname; const method = req.method(); const body = method === 'POST' ? req.postDataJSON() : {}; paths.push(path);
    if (method === 'POST') calls.push({ path, body });
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path === '/api/auth/me') return json({ id: 1, username: 'member', role, kind });
    if (path === '/api/overview/dashboard') return json(dashboardFixture(Number(new URL(req.url()).searchParams.get('days') || 14), 0));
    if (path === '/api/overview') return json({ datasets: 0, evidence: 0, runs: 0, by_license_class: {}, by_modality: {}, enrichment_candidates: 0, pairings_pending: 0, snapshots: {}, ideas: 0 });
    if (path === '/api/collection-jobs' && method === 'POST') { const job = makeJob(jobs.length + 1, body); jobs.unshift(job); return json(job, 201); }
    if (path === '/api/collection-jobs') return json({ items: jobs });
    const match = path.match(/^\/api\/collection-jobs\/(\d+)(?:\/(events|cancel|retry))?$/);
    if (match) {
      const job = jobs.find(item => String(item.id) === match[1])!;
      if (match[2] === 'events') return json({ items: [{ id: 1, kind: 'queued', message: '采集任务已保存到后台队列', created_at: job.created_at }] });
      if (match[2] === 'cancel') { job.cancel_requested = true; if (job.status === 'queued') job.status = 'cancelled'; }
      if (match[2] === 'retry') { const next = { ...makeJob(jobs.length + 1, job.scope), trigger: 'retry' as const }; jobs.unshift(next); return json(next, 201); }
      return json(job);
    }
    if (path === '/api/collection-schedules' && method === 'POST') { const schedule = { id: schedules.length + 1, ...body, next_run_at: body.enabled ? '2026-10-17T01:00:00Z' : null, last_run_at: null, last_job_id: null, created_by: 'member' }; schedules.push(schedule); return json(schedule, 201); }
    if (path === '/api/collection-schedules') return json({ items: schedules });
    if (path.startsWith('/api/collection-schedules/') && method === 'POST') { const schedule = schedules.find(item => String(item.id) === path.split('/').at(-1))!; Object.assign(schedule, body, { next_run_at: body.enabled ? '2026-10-17T01:00:00Z' : null }); return json(schedule); }
    if (path === '/api/dataset-versions/11/evidence') return json({ dataset_id: 1, dataset_version_id: 11, revision: 'a'.repeat(40), provenance_status: 'verified', items: [{ id: 91, dataset_version_id: 11, kind: 'card', location: 'README.md:4', excerpt: '固定版本的中文新闻分类数据，100 行。' }] });
    return json({ items: [] });
  });
  return { calls, paths, jobs, schedules, makeJob };
}
async function go(page: Page) { await page.goto('/'); await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: 'HF 采集', exact: true }).click(); await expect(page.getByRole('heading', { name: 'HF 采集', exact: true })).toBeVisible(); }

test('HF collection starts without a model, polls actual progress, exposes partial failures and fixed evidence', async ({ page }, testInfo) => {
  const data = await fixture(page); await go(page); const form = page.getByTestId('hf-manual-form');
  await form.getByLabel('关键词', { exact: true }).fill(' news '); await form.getByLabel('作者或组织').fill('lab'); await form.getByLabel('语言', { exact: true }).fill('zh, en, zh'); await form.getByLabel('任务类别').fill('text-classification');
  await form.getByRole('button', { name: '立即采集', exact: true }).click(); const row = page.getByTestId('hf-job-1'); await expect(row).toContainText('排队中');
  expect(data.calls[0]).toEqual({ path: '/api/collection-jobs', body: { search: 'news', author: 'lab', languages: ['zh', 'en'], task_categories: ['text-classification'], limit: 50 } });
  Object.assign(data.jobs[0], { status: 'running', stage: 'collecting', attempts: 1, current_source_key: 'lab/news', stats: { ...data.jobs[0].stats, found: 3, processed: 1, new: 1 } });
  await expect(row).toContainText('运行中', { timeout: 7000 }); await expect(row).toContainText('已处理 1'); await expect(page.getByTestId('hf-job-detail')).toContainText('当前：lab/news');
  Object.assign(data.jobs[0], { status: 'partial', stage: 'finished', current_source_key: null, analysis_status: 'rule_based', dataset_version_ids: [11], stats: { found: 3, processed: 3, new: 1, updated: 1, duplicates: 0, skipped_stale: 0, failed: 1, failures: [{ source_key: 'lab/private', error_kind: 'access_denied', error: '来源拒绝访问，请核对权限' }] } });
  await expect(row).toContainText('部分完成', { timeout: 7000 }); const detail = page.getByTestId('hf-job-detail'); await expect(detail).toContainText('access_denied'); await expect(detail).toContainText('来源拒绝访问，请核对权限'); await expect(detail).toContainText('规则整理完成');
  await page.getByRole('button', { name: '查看版本 #11 证据' }).click(); await expect(detail).toContainText('固定版本的中文新闻分类数据'); await expect(detail).toContainText('a'.repeat(40));
  expect(data.paths.some(path => /agent-runs|download-requests|\/files$/.test(path))).toBe(false); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: testInfo.outputPath('hf-collection-desktop.png'), fullPage: true });
  await row.getByRole('button', { name: '重试采集' }).click(); const retry = page.getByTestId('hf-job-2'); await expect(retry).toContainText('排队中'); await retry.getByRole('button', { name: '取消采集' }).click(); await expect(retry).toContainText('已取消');
});

test('schedules default to daily 50 paused and preserve their scope through enable, edit and pause', async ({ page }) => {
  const data = await fixture(page); await go(page); await page.getByRole('button', { name: '新建同步计划' }).click(); const form = page.getByTestId('hf-schedule-form');
  await expect(form.getByLabel('保存后启用同步')).not.toBeChecked(); await expect(form.getByLabel('同步频率')).toHaveValue('1440'); await expect(form.getByLabel('每次上限')).toHaveValue('50');
  await form.getByLabel('计划名称').fill('每日新闻'); await form.getByLabel('关键词', { exact: true }).fill('news'); await form.getByRole('button', { name: '保存已暂停计划' }).click(); const row = page.getByTestId('hf-schedule-1'); await expect(row).toContainText('已暂停');
  expect(data.calls[0].body).toEqual({ name: '每日新闻', scope: { ...defaultScope, search: 'news' }, interval_minutes: 1440, enabled: false });
  await row.getByRole('button', { name: '启用计划' }).click(); await expect(row).toContainText('已启用'); expect(data.calls[1].body).toMatchObject({ enabled: true, scope: { search: 'news' } });
  await row.getByRole('button', { name: '编辑计划' }).click(); await form.getByLabel('语言', { exact: true }).fill('zh'); await form.getByLabel('同步频率').selectOption('60'); await form.getByRole('button', { name: '保存并启用计划' }).click(); await expect(row).toContainText('每小时');
  await row.getByRole('button', { name: '暂停计划' }).click(); await expect(row).toContainText('暂停期间不执行'); expect(data.calls.at(-1)?.body).toMatchObject({ enabled: false, interval_minutes: 60, scope: { languages: ['zh'] } });
});

for (const [role, kind] of [['reader', 'human'], ['researcher', 'human'], ['admin', 'agent']] as const) {
  test(`${kind} ${role} follows collection ownership and schedule permissions`, async ({ page }) => {
    const data = await fixture(page, role, kind); data.jobs.push({ ...data.makeJob(1), actor: 'someone-else' }, data.makeJob(2)); await go(page);
    const create = page.getByTestId('hf-manual-form').getByRole('button', { name: '立即采集', exact: true });
    if (role === 'reader') { await expect(create).toBeDisabled(); await expect(page.getByRole('button', { name: '取消采集' })).toHaveCount(0); }
    else { await expect(create).toBeEnabled(); await expect(page.getByTestId('hf-job-2').getByRole('button', { name: '取消采集' })).toBeVisible(); if (role === 'researcher' || kind === 'agent') await expect(page.getByTestId('hf-job-1').getByRole('button', { name: '取消采集' })).toHaveCount(0); }
    await expect(page.getByRole('button', { name: '新建同步计划' })).toHaveCount(0); expect(data.calls).toEqual([]);
  });
}

test('collection controls and schedule form fit a narrow screen', async ({ page }, testInfo) => {
  await fixture(page); await page.setViewportSize({ width: 390, height: 844 }); await page.goto('/'); await page.getByRole('button', { name: '切换导航' }).click(); await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: 'HF 采集', exact: true }).click();
  await page.getByRole('button', { name: '新建同步计划' }).click(); await expect(page.getByTestId('hf-schedule-form')).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true); await page.screenshot({ path: testInfo.outputPath('hf-collection-mobile.png'), fullPage: true });
});

test('running cancellation stays visible until the worker safely stops', async ({ page }) => {
  const data = await fixture(page); data.jobs.push({ ...data.makeJob(1), status: 'running', stage: 'collecting', current_source_key: 'lab/news', attempts: 1 }); await go(page);
  const row = page.getByTestId('hf-job-1'); await row.getByRole('button', { name: '取消采集' }).click();
  await expect(row.getByRole('button', { name: '取消中…' })).toBeDisabled(); await expect(row).toContainText('取消中');
  await expect(page.getByTestId('hf-job-detail')).toContainText('等待当前网络请求结束后安全停止', { timeout: 7000 });
  Object.assign(data.jobs[0], { status: 'cancelled', stage: 'finished' }); await expect(row).toContainText('已取消', { timeout: 7000 });
  expect(data.calls.filter(call => call.path.endsWith('/cancel'))).toHaveLength(1);
});


test('a terminal task without saved versions does not claim candidate enrichment', async ({ page }) => {
  const data = await fixture(page); data.jobs.push({ ...data.makeJob(1), status: 'failed', stage: 'finished', analysis_status: 'rule_based', error: '来源列举未完成' }); await go(page);
  const detail = page.getByTestId('hf-job-detail'); await expect(detail).toContainText('尚无可整理的数据版本'); await expect(detail).not.toContainText('规则整理完成');
});
