import { expect, test, type Page } from '@playwright/test';
import { dashboardFixture } from './dashboard-fixture';

async function fixture(page: Page, options: { empty?: boolean; error?: boolean; loading?: boolean } = {}) {
  const controls = { error: !!options.error, release: () => {}, requests: [] as string[] }; const wait = options.loading ? new Promise<void>(resolve => { controls.release = resolve; }) : Promise.resolve();
  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url()); controls.requests.push(url.pathname + url.search); const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (url.pathname === '/api/auth/me') return json({ id: 1, username: 'reader', role: 'reader', kind: 'human' });
    if (url.pathname === '/api/overview/dashboard') { await wait; return controls.error ? json({ detail: '统计服务暂时不可用' }, 503) : json(dashboardFixture(Number(url.searchParams.get('days') || 14), options.empty ? 0 : 60)); }
    if (url.pathname === '/api/datasets') return json({ items: [], total: 0, size: 20, page: 1 });
    return json({ items: [] });
  }); return controls;
}
async function overview(page: Page) { await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: '工作概览', exact: true }).click(); }

test('real-data dashboard preserves multi-label and unknown counts and drills into exact filters', async ({ page }) => {
  const data = await fixture(page); await page.goto('/'); await expect(page.getByTestId('overview-total-datasets')).toHaveText('60');
  await expect(page.getByTestId('overview-modality-unknown')).toHaveAccessibleName('模态未标注 44 个数据集，查看目录'); await expect(page.getByTestId('overview-license-unknown')).toHaveAccessibleName('许可分类未知 45 个数据集，查看目录');
  await expect(page.getByTestId('catalog-dashboard')).toContainText('类别数量可能大于数据集总量'); await expect(page.getByText('历史失败', { exact: true })).toBeVisible(); await expect(page.getByText('最近任务 #3')).toBeVisible();
  await page.getByTestId('overview-modality-unknown').click(); await expect(page.getByLabel('模态筛选')).toHaveValue('unknown'); expect(data.requests.some(path => path.includes('/api/datasets?modality=unknown'))).toBe(true);
  await overview(page); await page.getByTestId('overview-modality-audio').click(); await expect(page.getByLabel('模态筛选')).toHaveValue('audio');
  await overview(page); await page.getByTestId('overview-license-unknown').click(); await expect(page.getByLabel('许可筛选')).toHaveValue('unknown'); expect(data.requests.some(path => path.includes('license_class=unknown'))).toBe(true);
});

test('period changes only the trend and keyboard selection exposes exact daily values and a table', async ({ page }) => {
  const data = await fixture(page); await page.goto('/'); const selection = page.getByTestId('overview-trend-selection'); await expect(selection).toContainText('首次入库 50 个数据集'); await expect(selection).toContainText('今天尚未结束'); await expect(page.locator('.ov-chart-grid span').first()).toHaveText('56'); await expect(page.locator('.ov-chart-grid span').nth(1)).toHaveText('28');
  await page.getByRole('button', { name: '近 30 天', exact: true }).click(); await expect(page.locator('[data-testid^="overview-day-"]')).toHaveCount(30); await expect(page.getByTestId('overview-total-datasets')).toHaveText('60'); expect(data.requests).toContain('/api/overview/dashboard?days=30');
  const first = page.locator('[data-testid^="overview-day-"]').first(); await first.focus(); await page.keyboard.press('Enter'); await expect(first).toHaveAttribute('aria-pressed', 'true'); await expect(selection).toContainText('首次入库 0 个数据集'); await expect(selection).toContainText('入库 0 个版本');
  await page.getByText('展开入库趋势数据表', { exact: true }).click(); const table = page.locator('details', { hasText: '展开入库趋势数据表' }).getByRole('table'); await expect(table.getByRole('row')).toHaveCount(31); await expect(table).toContainText('上海日期');
  await page.getByRole('button', { name: '待补资料 2 条，进入整理核验' }).click(); await expect(page.getByRole('heading', { name: '整理核验', exact: true })).toBeVisible();
});

test('empty catalog has real zero values and an actionable collection entry', async ({ page }) => {
  await fixture(page, { empty: true }); await page.goto('/'); await expect(page.getByTestId('overview-total-datasets')).toHaveText('0'); await expect(page.getByText('目录尚未收录数据集')).toBeVisible(); await expect(page.getByText('尚无已启用的同步计划。')).toBeVisible(); await expect(page.getByTestId('overview-trend-selection')).toContainText('首次入库 0 个数据集'); await expect(page.getByTestId('catalog-dashboard')).not.toContainText('NaN');
  await page.getByRole('button', { name: '开始采集', exact: true }).click(); await expect(page.getByRole('heading', { name: 'HF 采集', exact: true })).toBeVisible();
});

test('loading, initial failure and refresh failure never fabricate zero statistics', async ({ page }) => {
  const state = await fixture(page, { loading: true, error: true }); await page.goto('/'); await expect(page.getByLabel('正在加载')).toBeVisible(); await expect(page.getByTestId('overview-total-datasets')).toHaveCount(0); state.release(); await expect(page.getByRole('alert')).toContainText('统计服务暂时不可用');
  state.error = false; await page.getByRole('button', { name: '重试加载' }).click(); await expect(page.getByTestId('overview-total-datasets')).toHaveText('60');
  state.error = true; await page.getByRole('button', { name: '刷新统计', exact: true }).click(); await expect(page.getByRole('alert')).toContainText('保留上次成功读取的统计'); await expect(page.getByTestId('overview-total-datasets')).toHaveText('60');
});

for (const width of [1440, 390]) {
  test(`dashboard charts work in light and dark without horizontal page overflow at ${width}px`, async ({ page }, testInfo) => {
    await fixture(page); await page.setViewportSize({ width, height: width === 390 ? 844 : 1100 }); await page.goto('/'); await expect(page.getByTestId('catalog-dashboard')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true); await page.screenshot({ path: testInfo.outputPath(`overview-light-${width}.png`), fullPage: true });
    await page.getByRole('button', { name: '切换深色主题' }).click(); await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark'); await page.getByTestId('overview-day-2026-10-16').focus(); await page.keyboard.press('Enter'); await expect(page.getByTestId('overview-trend-selection')).toContainText('入库 55 个版本'); expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ path: testInfo.outputPath(`overview-dark-${width}.png`), fullPage: true });
  });
}
