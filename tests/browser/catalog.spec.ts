import { expect, test, type Page } from '@playwright/test';

// 策展模式 e2e：/dist 页面 + 真实目录服务（fixture 库，由 global-setup 重建）。
const URL_ = '/?mode=catalog&api=http://127.0.0.1:5280';
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
  await expect(page.locator('.cat-chip-list').first()).toContainText('text · 1');
  await expect(page.locator('.cat-chip-list').first()).toContainText('3d · 1');
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
  await page.getByTestId('dataset-acme/zh-news').click();
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
  await page.getByLabel('策展角色').selectOption('manager');
  await expect(page.getByRole('button', { name: '确认', exact: true }).first()).toBeDisabled();
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
  await expect(idea).toContainText('已确认配对 2');
  await expect(idea.getByRole('button', { name: '冻结快照', exact: true })).toBeDisabled(); // 审核员视角不可冻结
  await page.getByLabel('策展角色').selectOption('manager');
  await idea.getByRole('button', { name: '冻结快照', exact: true }).click();
  await expect(page.getByRole('status')).toContainText('已冻结');

  await page.getByLabel('策展角色').selectOption('lead');
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
    .toEqual(['acme/photo-cls', 'acme/zh-news']);

  await nav(page, '运行与审计');
  await page.locator('.cat-tabs').getByRole('button', { name: '操作审计' }).click();
  await expect(page.locator('table')).toContainText('pairing_review');
  await expect(page.locator('table')).toContainText('snapshot_freeze');
  await expect(page.locator('table')).toContainText('snapshot_approve');
});

test('catalog mode shows a clear error when the service is unreachable', async ({ page }) => {
  await page.goto('/?mode=catalog&api=http://127.0.0.1:5999');
  await expect(page.getByText('无法连接目录服务')).toBeVisible();
});
