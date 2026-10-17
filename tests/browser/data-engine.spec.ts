import { expect, test, type Page } from '@playwright/test';
import { dashboardFixture } from './dashboard-fixture';
import type { DatasetBuild, BackgroundJob, ExperimentItem, Role } from '../../src/catalog/api';

async function fixture(page: Page, role: Role = 'admin', kind = 'human') {
  const builds: DatasetBuild[] = [];
  const jobs: BackgroundJob[] = [];
  const experiments: ExperimentItem[] = [];
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const addJob = (kind: string) => { const job: BackgroundJob = { id: jobs.length + 1, job_id: jobs.length + 1, kind, queue: 'cpu', status: 'queued', attempts: 0, cancel_requested: false, result: {}, progress: {}, error: '' }; jobs.unshift(job); return job; };
  const addBuild = () => { const build: DatasetBuild = { id: builds.length + 1, status: 'queued', recipe: {}, sample_count: 0, manifest_hash: 'a'.repeat(64), error: '', approved_by: '' }; builds.unshift(build); return build; };
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    const body = request.method() === 'POST' ? request.postDataJSON() : {};
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (request.method() === 'POST') calls.push({ path, body });
    if (path === '/api/auth/me') return json({ id: 1, username: 'tester', role, kind });
    if (path === '/api/overview/dashboard') return json(dashboardFixture(14, 0));
    if (path === '/api/builds') {
      if (request.method() === 'POST') { const build = addBuild(); const job = addJob('build.run'); return json({ build_id: build.id, job_id: job.id }, 202); }
      return json({ items: builds });
    }
    if (path.endsWith('/manifest')) return json({ manifest: { counts: { output_rows: 2 }, input_hash: 'fixed-source-sha256' }, sha256: 'a'.repeat(64) });
    if (path.endsWith('/profiles')) return json({ items: [{ summary: { duplicates: 1 } }] });
    if (path.endsWith('/publish')) { builds[0].status = 'published'; return json(builds[0]); }
    if (path === '/api/jobs') {
      if (request.method() === 'POST') return json(addJob(String(body.kind)), 202);
      return json({ items: jobs });
    }
    if (path.endsWith('/cancel')) { jobs[0].cancel_requested = true; return json(jobs[0]); }
    if (path.endsWith('/artifact')) return route.fulfill({ contentType: 'application/zip', body: 'fixture' });
    if (path === '/api/experiments') return json({ items: experiments });
    if (path === '/api/experiments/import') return json({ experiment_id: 1, job_id: addJob('experiment.import').id }, 202);
    if (path === '/api/experiments/compare') return json({ baseline_experiment_id: body.experiment_ids[0], accuracy_delta: 0.25 });
    return json({ items: [] });
  });
  return { builds, jobs, experiments, calls, addBuild, addJob };
}
async function go(page: Page) {
  await page.goto('/');
  await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: '数据构建', exact: true }).click();
  await expect(page.getByRole('heading', { name: '数据构建', exact: true })).toBeVisible();
}

test('builds require publication before asynchronous export and expose pending cancellation', async ({ page }, testInfo) => {
  const data = await fixture(page); await go(page);
  await page.getByLabel('输入资产编号').fill('12, 13'); await page.getByLabel('数据类型').selectOption('text');
  await page.getByLabel('标签字段（可选）').fill('label'); await page.getByRole('button', { name: '提交构建', exact: true }).click();
  expect(data.calls[0]).toEqual({ path: '/api/builds', body: { recipe: { asset_ids: [12, 13], modality: 'text', text_column: 'text', label_column: 'label', exact_dedup: true, seed: 42 } } });
  const row = page.getByTestId('build-1'); await expect(row).toContainText('排队中'); await expect(row.getByRole('button', { name: '导出训练包' })).toHaveCount(0);
  Object.assign(data.builds[0], { status: 'ready', sample_count: 2 }); Object.assign(data.jobs[0], { status: 'succeeded', attempts: 1 });
  await expect(row).toContainText('待发布', { timeout: 7000 });
  await row.getByRole('button', { name: '清单与测量' }).click(); await expect(page.getByText('fixed-source-sha256', { exact: false })).toBeVisible();
  await row.getByRole('button', { name: '批准发布' }).click(); expect(data.calls.at(-1)?.body).toEqual({ manifest_hash: 'a'.repeat(64) });
  await row.getByRole('button', { name: '导出训练包' }).click();
  const task = page.getByTestId('background-job-2'); await expect(task).toContainText('训练包导出');
  Object.assign(data.jobs[0], { status: 'running' });
  await task.getByRole('button', { name: '取消', exact: true }).click(); await expect(task.getByRole('button', { name: '取消中…' })).toBeDisabled();
  Object.assign(data.jobs[0], { status: 'succeeded', result: { build_id: 1 } });
  await expect(task.getByRole('button', { name: '下载训练包' })).toBeVisible({ timeout: 7000 });
  const download = page.waitForEvent('download'); await task.getByRole('button', { name: '下载训练包' }).click(); expect((await download).suggestedFilename()).toBe('dataset-build-1.zip');
  await page.screenshot({ path: testInfo.outputPath('data-engine-desktop.png'), fullPage: true });
});

test('experiment form pins evaluation protocol and comparisons preserve the selected baseline', async ({ page }) => {
  const data = await fixture(page); await go(page); await page.getByText('导入实验结果', { exact: true }).click();
  const form = page.getByTestId('experiment-form');
  for (const [label, value] of [['训练数据版本', '1'], ['评估数据版本', '2'], ['预测文件资产编号', '3'], ['模型名称', 'classifier'], ['模型版本', 'fixed/1'], ['训练预算', '80 samples']]) await form.getByLabel(label, { exact: true }).fill(value);
  await form.getByRole('button', { name: '提交实验结果' }).click();
  expect(data.calls.at(-1)).toEqual({ path: '/api/experiments/import', body: { build_id: 1, prediction_asset_id: 3, model: { name: 'classifier', version: 'fixed/1', training_budget: '80 samples', seed: 42 }, protocol: { version: '1', task: 'classification', evaluation_build_id: 2, split: 'test', require_complete: true } } });
  for (const id of [10, 11]) data.experiments.push({ id, build_id: id, status: 'completed', model: { name: 'classifier', version: 'fixed/1', training_budget: '80 samples', seed: 42 }, protocol: { evaluation_build_id: 2, split: 'test' }, metrics: { accuracy: id === 10 ? 0.5 : 0.75, error_count: 10 }, error: '' });
  await page.getByLabel('选择实验 11').check({ timeout: 7000 }); await page.getByLabel('选择实验 10').check();
  await page.getByRole('button', { name: '比较所选实验（基线 #11）' }).click();
  expect(data.calls.at(-1)?.body).toEqual({ experiment_ids: [11, 10] });
  await expect(page.getByText('accuracy_delta', { exact: false })).toBeVisible();
});

for (const [role, kind] of [['reader', 'human'], ['admin', 'agent']] as const) {
  test(`${kind} ${role} cannot grant publication in the data workbench`, async ({ page }) => {
    const data = await fixture(page, role, kind); Object.assign(data.addBuild(), { status: 'ready' }); await go(page);
    await expect(page.getByRole('button', { name: '批准发布' })).toHaveCount(0);
    if (role === 'reader') { await expect(page.getByRole('button', { name: '提交构建', exact: true })).toHaveCount(0); await expect(page.getByText('导入实验结果', { exact: true })).toHaveCount(0); }
  });
}

test('data and experiment forms fit a narrow screen', async ({ page }, testInfo) => {
  await fixture(page); await page.setViewportSize({ width: 390, height: 844 }); await page.goto('/');
  await page.getByRole('button', { name: '切换导航' }).click();
  await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: '数据构建', exact: true }).click();
  await page.getByText('导入实验结果', { exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('data-engine-mobile.png'), fullPage: true });
});
