import { dashboardFixture } from './dashboard-fixture';
import { expect, test, type Page } from '@playwright/test';

type Call = { path: string; body: Record<string, unknown> };
async function mockWorkbench(page: Page, { login = false, role = 'admin', withAssets = false } = {}) {
  let authenticated = !login; const calls: Call[] = [];
  const dataset = { id: 1, source_key: 'lab/news', name: '新闻语料', author: 'lab', url: 'https://example.org/news', license: 'mit', license_class: 'permissive', modalities: ['text'], modality_source: 'tags', task_categories: ['text-classification'], languages: ['zh'], size_rows: 100, downloads: 4, likes: 1, missing: [], dataset_version_id: 11 };
  const evidence = [{ id: 101, dataset_version_id: 11, kind: 'card', location: 'README.md:4', excerpt: '中文新闻语料，MIT 许可，包含 100 行。' }];
  let enrichment = { id: 3, dataset_id: 1, dataset_version_id: 11, dataset: 'lab/news', kind: 'quality', producer: 'catalog-v1', status: 'candidate', value: { level: 'complete', reasons: ['有结构与许可说明'] }, evidence_ids: [101] };
  const runs: Record<string, unknown>[] = [];
  const downloads: Record<string, unknown>[] = [];
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname; const method = request.method(); const body = method === 'POST' ? request.postDataJSON() as Record<string, unknown> : {};
    if (method === 'POST') calls.push({ path, body });
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path === '/api/auth/me') return authenticated ? json({ id: 1, username: 'researcher', role, kind: 'human' }) : json({ detail: '请先登录' }, 401);
    if (path === '/api/auth/login') { authenticated = true; return json({ id: 1, username: 'researcher', role, kind: 'human' }); }
    if (path === '/api/auth/logout') { authenticated = false; return json({ ok: true }); }
    if (path === '/api/overview/dashboard') return json(dashboardFixture(Number(new URL(request.url()).searchParams.get('days') || 14), 1));
    if (path === '/api/overview') return json({ datasets: 1, evidence: 1, runs: 0, by_license_class: { permissive: 1 }, by_modality: { text: 1 }, enrichment_candidates: 1, pairings_pending: 0, snapshots: {}, ideas: 1 });
    if (path === '/api/datasets') return json({ items: [dataset], total: 1, page: 1, size: 20 });
    if (path === '/api/datasets/1') return json({ dataset, versions: [{ id: 10, revision: 'old-revision', provenance_status: 'historical_unverified', metadata: { ...dataset, license: 'GPL-3.0', license_class: 'copyleft', size_rows: 30 } }, { id: 11, revision: 'a'.repeat(40), provenance_status: 'verified', metadata: dataset }], evidence, enrichments: [enrichment] });
    if (path === '/api/datasets/register') return json({ id: 2, status: 'completed' }, 201);
    if (path === '/api/dataset-versions/11/files') return json({ revision: 'a'.repeat(40), items: [{ path: 'train.csv', size: 2048 }, { path: 'test.csv', size: 512 }] });
    if (path === '/api/dataset-versions/10/files') return json({ detail: '历史版本未验证' }, 400);
    if (/\/api\/dataset-versions\/\d+\/assets/.test(path)) return json({ items: withAssets ? [{ id: 9, path: 'train.csv', size_bytes: 2048, sha256: 'b'.repeat(64) }] : [], reports: [], overlap: [] });
    if (path === '/api/assets/9/preview') return json({ kind: 'table', rows: [{ text: '已批准获取的新闻样本', label: '科技' }], sampled: true, limit: 100 });
    if (path === '/api/review/enrichments') return json({ items: new URL(request.url()).searchParams.get('status') === enrichment.status ? [enrichment] : [] });
    if (path === '/api/review/enrichments/3') { enrichment = { ...enrichment, status: body.decision === 'corrected' ? 'confirmed' : String(body.decision) }; return json(enrichment); }
    if (path === '/api/agent-runs' && method === 'POST') { const run = { id: 'run-1', goal: body.goal, payload: body, status: 'queued', stage: 'queued', tool_calls: 0, created_at: '2026-10-15T10:00:00Z', result: {} }; runs.push(run); return json(run, 201); }
    if (path === '/api/agent-runs') return json({ items: runs });
    if (path.endsWith('/events')) return json({ items: [{ id: 1, kind: 'queued', message: '科研任务已进入执行队列', detail: { max_candidates: 100 }, created_at: '2026-10-15T10:00:00Z' }] });
    if (path.endsWith('/cancel')) { runs[0].status = 'cancelled'; return json(runs[0]); }
    if (path.endsWith('/retry')) { runs[0].status = 'queued'; return json(runs[0]); }
    if (path === '/api/download-requests' && method === 'POST') { const item = { id: 4, ...body, status: 'pending', revision: 'a'.repeat(40), license_basis: 'mit', estimated_bytes: 2048 }; downloads.push(item); return json(item, 201); }
    if (path === '/api/download-requests') return json({ items: downloads });
    if (path === '/api/download-requests/4/decision') { downloads[0].status = body.decision; return json(downloads[0]); }
    if (path === '/api/pairings') return json({ items: [] });
    if (path === '/api/ideas') return json({ items: [{ id: 1, external_id: 'research-run-1', title: '文本研究', statement: '比较新闻分类数据集', source_refs: [], requirements: {}, status: { pairings: [], snapshots: [{ id: 8, version: 1, status: 'approved', approved_at: '2026-10-15T10:00:00Z', pairing_ids: [] }] } }] });
    if (path === '/api/snapshots/8/bundle') return route.fulfill({ contentType: 'application/zip', body: Buffer.from('PK\u0005\u0006' + '\u0000'.repeat(18)) });
    if (path === '/api/snapshots/8/export') return json({ type: 'idea_dataset_snapshot' });
    return json({ items: [] });
  });
  return calls;
}
async function nav(page: Page, label: string) { await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: label, exact: true }).click(); }

test('login uses a session and creates, cancels and retries a bounded research task', async ({ page }, testInfo) => {
  const calls = await mockWorkbench(page, { login: true }); await page.goto('/');
  await page.getByLabel('账号', { exact: true }).fill('researcher'); await page.getByLabel('密码', { exact: true }).fill('example-password'); await page.getByRole('button', { name: '登录工作区', exact: true }).click();
  await expect(page.getByTestId('catalog-app')).toBeVisible(); await expect(page.getByLabel('策展角色')).toHaveCount(0); await nav(page, '科研任务');
  await page.getByLabel('科研目标', { exact: true }).fill('查找中文新闻分类数据集'); await page.getByLabel('文本', { exact: true }).check(); await page.getByLabel('语言', { exact: true }).fill('zh'); await page.getByLabel('许可要求').selectOption('permissive'); await page.getByLabel('排序偏好').fill('优先小型数据集'); await page.getByRole('button', { name: '创建科研任务' }).click();
  await expect(page.getByTestId('agent-run-run-1')).toContainText('排队中'); await expect(page.getByText('科研任务已进入执行队列')).toBeVisible();
  const request = calls.find(call => call.path === '/api/agent-runs')!; expect(request.body).toMatchObject({ goal: '查找中文新闻分类数据集', hard_constraints: { modalities: ['text'], languages: ['zh'], license_class: ['permissive'] }, preferences: { notes: '优先小型数据集' }, max_candidates: 100, max_tool_calls: 20 });
  await page.getByRole('button', { name: '取消任务' }).click(); await expect(page.getByTestId('agent-run-run-1')).toContainText('已取消'); await page.getByRole('button', { name: '重试任务' }).click(); await expect(page.getByTestId('agent-run-run-1')).toContainText('排队中');
  expect(calls.every(call => !('role' in call.body))).toBe(true);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: testInfo.outputPath('research-desktop.png'), fullPage: true });
});

test('version evidence stays isolated and download approval keeps the selected file range', async ({ page }) => {
  const calls = await mockWorkbench(page); await page.goto('/'); await nav(page, '数据目录'); await page.getByRole('button', { name: 'lab/news', exact: true }).click();
  const detail = page.getByTestId('dataset-detail'); await expect(detail).toContainText('100 行'); await detail.getByLabel('查看数据版本').selectOption('10'); await expect(detail).toContainText('GPL-3.0'); await expect(detail).not.toContainText('中文新闻语料，MIT');
  await detail.getByLabel('查看数据版本').selectOption('11'); await detail.getByLabel(/train.csv/).check(); await detail.getByRole('button', { name: '申请下载' }).click(); await expect(page.getByRole('status')).toContainText('下载申请已提交');
  expect(calls.find(call => call.path === '/api/download-requests')?.body).toEqual({ dataset_version_id: 11, files: ['train.csv'], max_bytes: 104857600 });
  await nav(page, '下载管理'); const request = page.getByTestId('download-4'); await request.getByText('批准范围 · 1 个文件').click(); await expect(request).toContainText('train.csv'); await expect(request).not.toContainText('test.csv'); await request.getByLabel('审批说明').fill('已核对 MIT 许可与样本用途'); await request.getByRole('button', { name: '批准下载范围' }).click(); await expect(request).toContainText('已批准');
});

test('review displays source evidence and records a correction note', async ({ page }) => {
  const calls = await mockWorkbench(page); await page.goto('/'); await nav(page, '整理核验'); await expect(page.getByTestId('enrichment-3')).toContainText('中文新闻语料，MIT 许可'); await page.getByRole('button', { name: '更正候选', exact: true }).click(); await page.getByLabel('更正候选内容').fill('{"level":"partial","reasons":["缺少切分说明"]}'); await page.getByLabel('核验说明').fill('来源没有说明训练与测试切分'); await page.getByRole('button', { name: '保存更正' }).click(); await expect(page.getByRole('status')).toContainText('已更正');
  expect(calls.find(call => call.path === '/api/review/enrichments/3')?.body).toEqual({ decision: 'corrected', note: '来源没有说明训练与测试切分', value: { level: 'partial', reasons: ['缺少切分说明'] } });
});

test('approved snapshots offer a research bundle and readers cannot approve downloads', async ({ page }) => {
  await mockWorkbench(page, { role: 'reader' }); await page.goto('/'); await nav(page, '配对与发布'); const download = page.waitForEvent('download'); await page.getByRole('button', { name: '下载研究包' }).click(); expect((await download).suggestedFilename()).toBe('research-bundle-8.zip'); await nav(page, '科研任务'); await expect(page.getByRole('button', { name: '创建科研任务' })).toBeDisabled();
});

test('manual source registration and narrow workbench navigation remain usable', async ({ page }, testInfo) => {
  const calls = await mockWorkbench(page); await page.setViewportSize({ width: 390, height: 844 }); await page.goto('/'); await page.getByRole('button', { name: '切换导航' }).click(); await nav(page, '数据目录'); await page.getByText('人工登记数据集', { exact: true }).click();
  await page.getByLabel('来源标识').fill('lab/mesh'); await page.getByLabel('数据集名称', { exact: true }).fill('三维网格'); await page.getByLabel('官方页面').fill('https://example.org/mesh'); await page.getByLabel('版本或发布日期').fill('v1'); await page.getByLabel('三维', { exact: true }).check(); await page.getByLabel('来源说明与访问条件').fill('公开发布的研究网格，访问时需核验许可。'); await page.getByRole('button', { name: '登记数据集', exact: true }).click(); await expect(page.getByRole('status')).toContainText('数据集已登记');
  expect(calls.find(call => call.path === '/api/datasets/register')?.body).toMatchObject({ source_key: 'lab/mesh', modalities: ['3d'], access_conditions: '公开来源，仍需核对许可' });
  await page.getByRole('button', { name: '切换导航' }).click(); await nav(page, '科研任务'); await expect(page.getByLabel('科研目标', { exact: true })).toBeVisible(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true); await page.screenshot({ path: testInfo.outputPath('research-mobile.png'), fullPage: true });
});

test('sample preview only reads an already acquired asset', async ({ page }) => {
  await mockWorkbench(page, { withAssets: true }); await page.goto('/'); await nav(page, '数据目录'); await page.getByRole('button', { name: 'lab/news', exact: true }).click();
  await page.getByRole('button', { name: '预览样本' }).click(); await expect(page.locator('.cat-preview')).toContainText('已批准获取的新闻样本'); await expect(page.locator('.cat-preview')).toContainText('本次展示 1 行'); await expect(page.locator('.cat-preview')).toContainText('不代表全量数据');
});
