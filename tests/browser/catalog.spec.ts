import { expect, test, type Page } from '@playwright/test';

// 策展模式 e2e：/dist 页面 + 真实目录服务（fixture 库，由 global-setup 重建）。
const URL_ = '/?mode=catalog&api=http://127.0.0.1:5281';
async function go(page: Page) { await page.goto(URL_); await expect(page.getByTestId('catalog-app')).toBeVisible(); }
async function nav(page: Page, label: string) {
  await page.locator('.sidebar').getByRole('button', { name: label, exact: true }).click();
  await expect(page.getByRole('heading', { name: label, exact: true })).toBeVisible();
}

test('overview shows live catalog statistics from the service', async ({ page }) => {
  const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
  await go(page);
  await expect(page.getByRole('heading', { name: '工作概览', exact: true })).toBeVisible();
  await expect(page.locator('.pp-stat strong').first()).toHaveText('3');
  await expect(page.getByTestId('overview-modality-text')).toHaveAccessibleName('文本 1 个数据集，查看目录');
  await expect(page.getByTestId('overview-modality-3d')).toHaveAccessibleName('三维 1 个数据集，查看目录');
  await expect(page.getByTestId('catalog-dashboard')).toContainText('科研数据中台');
  expect(errors).toEqual([]);
});

test('dataset catalog filters and drills into evidence', async ({ page }) => {
  await go(page);
  await nav(page, '数据目录');
  await expect(page.getByTestId('dataset-acme/zh-news')).toBeVisible();
  await expect(page.getByTestId('dataset-acme/mesh-3d')).toBeVisible();
  await page.getByLabel('模态筛选').selectOption('text');
  await expect(page.getByTestId('dataset-acme/zh-news')).toBeVisible();
  await expect(page.getByTestId('dataset-acme/mesh-3d')).toHaveCount(0);
  await page.getByLabel('模态筛选').selectOption('');
  await page.getByLabel('搜索数据集').fill('新闻');
  await expect(page.getByTestId('dataset-acme/photo-cls')).toHaveCount(0);
  await page.getByTestId('dataset-acme/zh-news').getByRole('button').click();
  const detail = page.getByTestId('dataset-detail');
  await expect(detail).toContainText('宽松许可');
  await expect(detail).toContainText('5000 行');
  await expect(detail).toContainText('中文新闻分类数据集');
  await expect(detail).toContainText('整理候选');
});

test('enrichment review moves candidates out of the queue with audit trail', async ({ page }) => {
  await go(page);
  await nav(page, '整理核验');
  const first = page.locator('[data-testid^="enrichment-"]').first();
  await expect(first).toBeVisible();
  const cardId = (await first.getAttribute('data-testid'))!;
  await first.getByRole('button', { name: '确认', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已确认');
  await expect(page.getByTestId(cardId)).toHaveCount(0);
  await expect(page.getByLabel('策展角色')).toHaveCount(0);
});

test('the server blocks incompatible modality and license during pairing confirmation', async ({ page }) => {
  await go(page); await nav(page, '配对与发布');
  const incompatible = page.locator('[data-testid^="pairing-"]', { hasText: 'acme/photo-cls' });
  await expect(incompatible).toBeVisible();
  const response = page.waitForResponse(value => value.url().endsWith('/review') && value.request().method() === 'POST');
  await incompatible.getByRole('button', { name: '确认', exact: true }).click();
  expect((await response).status()).toBe(400);
  await expect(page.getByRole('status')).toContainText('科研硬约束不满足');
  await expect(incompatible).toBeVisible();
});

test('pairing review, freeze, approval and export run the full chain in the UI', async ({ page }) => {
  await go(page);
  await nav(page, '配对与发布');
  const pending = page.locator('[data-testid^="pairing-"]', { hasText: 'acme/zh-news' });
  await expect(pending).toBeVisible();
  await expect(pending).toContainText('智能体提案');
  await pending.getByRole('button', { name: '确认', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已确认配对');

  const idea = page.getByTestId('idea-e2e-idea-1');
  await expect(idea).toContainText('已确认配对 1');
  await idea.getByRole('button', { name: '冻结快照', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已冻结');

  const snapshot = idea.locator('.cat-snapshot', { hasText: 'V1' });
  await snapshot.getByRole('button', { name: '批准', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已批准');

  const downloadPromise = page.waitForEvent('download');
  await snapshot.getByRole('button', { name: '导出', exact: true }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toMatch(/catalog-snapshot-\d+\.json/);
  const stream = await download.createReadStream();
  let text = ''; for await (const chunk of stream!) text += chunk.toString();
  const exported = JSON.parse(text);
  expect(exported.type).toBe('idea_dataset_snapshot');
  expect(exported.idea.external_id).toBe('e2e-idea-1');
  expect(exported.datasets.map((entry: { dataset: { source_key: string } }) => entry.dataset.source_key).sort())
    .toEqual(['acme/zh-news']);

  await nav(page, '运行与审计');
  await page.locator('.cat-tabs').getByRole('button', { name: '操作审计' }).click();
  await expect(page.locator('table')).toContainText('pairing_review');
  await expect(page.locator('table')).toContainText('snapshot_freeze');
  await expect(page.locator('table')).toContainText('snapshot_approve');
});

test('catalog mode shows a clear error when the service is unreachable', async ({ page }) => {
  await page.goto('/?mode=catalog&api=http://127.0.0.1:5999');
  await expect(page.getByRole('heading', { name: '无法连接目录服务', exact: true })).toBeVisible();
});

test('a licensed research task is accepted by the real service and can be cancelled', async ({ page }) => {
  await go(page); await nav(page, '科研任务');
  await page.getByLabel('科研目标', { exact: true }).fill('寻找用于中文新闻分类研究的开放文本数据集');
  await page.getByLabel('文本', { exact: true }).check();
  await page.getByLabel('许可要求', { exact: true }).selectOption('permissive');
  await page.getByLabel('Hugging Face', { exact: true }).uncheck();
  const submitted = page.waitForResponse(response => response.url().endsWith('/api/agent-runs') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '创建科研任务', exact: true }).click();
  const response = await submitted; expect(response.status()).toBe(201);
  const run = await response.json();
  expect(run.payload.hard_constraints.license_class).toEqual(['permissive']);
  expect(run.payload.hard_constraints.modalities).toEqual(['text']);
  const row = page.getByTestId(`agent-run-${run.id}`); await expect(row).toContainText('排队中');
  await row.getByRole('button', { name: '取消任务', exact: true }).click();
  await expect(row).toContainText('已取消');
});

test('HF jobs and paused daily schedules persist through the real API without running network collection', async ({ page }) => {
  await go(page); await nav(page, 'HF 采集'); const form = page.getByTestId('hf-manual-form');
  await form.getByLabel('关键词', { exact: true }).fill('e2e-offline-collection'); await form.getByLabel('语言', { exact: true }).fill('zh');
  const created = page.waitForResponse(response => response.url().endsWith('/api/collection-jobs') && response.request().method() === 'POST');
  await form.getByRole('button', { name: '立即采集', exact: true }).click(); const response = await created; expect(response.status()).toBe(201); const job = await response.json();
  expect(job.scope).toMatchObject({ search: 'e2e-offline-collection', languages: ['zh'], limit: 50 }); expect(job.status).toBe('queued'); expect(job.attempts).toBe(0);
  const row = page.getByTestId(`hf-job-${job.id}`); await row.getByRole('button', { name: '取消采集' }).click(); await expect(row).toContainText('已取消');
  await page.getByRole('button', { name: '新建同步计划' }).click(); const scheduleForm = page.getByTestId('hf-schedule-form'); await expect(scheduleForm.getByLabel('保存后启用同步')).not.toBeChecked();
  await scheduleForm.getByLabel('计划名称').fill('E2E 已暂停每日同步');
  const saved = page.waitForResponse(value => value.url().endsWith('/api/collection-schedules') && value.request().method() === 'POST');
  await scheduleForm.getByRole('button', { name: '保存已暂停计划' }).click(); const scheduleResponse = await saved; expect(scheduleResponse.status()).toBe(201); const schedule = await scheduleResponse.json();
  expect(schedule).toMatchObject({ enabled: false, interval_minutes: 1440, scope: { limit: 50 } });
  await page.reload(); await nav(page, 'HF 采集'); await expect(page.getByTestId(`hf-schedule-${schedule.id}`)).toContainText('已暂停'); await expect(page.getByTestId(`hf-job-${job.id}`)).toContainText('已取消');
});


test('real overview chart opens the corresponding catalog filter', async ({ page }) => {
  await go(page); await page.getByTestId('overview-modality-text').click(); await expect(page.getByLabel('模态筛选')).toHaveValue('text'); await expect(page.getByTestId('dataset-acme/zh-news')).toBeVisible(); await expect(page.getByTestId('dataset-acme/photo-cls')).toHaveCount(0);
  await nav(page, '工作概览'); await page.getByTestId('overview-license-copyleft').click(); await expect(page.getByLabel('许可筛选')).toHaveValue('copyleft'); await expect(page.getByTestId('dataset-acme/photo-cls')).toBeVisible(); await expect(page.getByTestId('dataset-acme/zh-news')).toHaveCount(0);
});
