import { expect, test, type Page } from '@playwright/test';
import type { VersionItem } from '../../src/catalog/api';
import { dashboardFixture } from './dashboard-fixture';

const currentDataset = { id: 1, source_key: 'lab/news', name: '新闻语料', author: 'lab', url: 'https://example.org/news', license: 'current-license', license_class: 'permissive', modalities: ['image'], modality_source: 'tags', task_categories: [], languages: ['en'], size_rows: 9800, downloads: 9876, likes: 99, missing: [], dataset_version_id: 11 };
const oldVersion: VersionItem = { id: 10, revision: 'old-revision', provenance_status: 'verified', metadata: { license: 'legacy-license', license_class: 'copyleft', modalities: ['text'], size_rows: 120, downloads: 321, likes: 2, missing: ['languages'], task_categories: ['text-classification'] } };

async function openReview(page: Page, version: VersionItem | null) {
  const candidate = { id: 3, dataset_id: 1, dataset_version_id: 10, dataset: 'lab/news', kind: 'quality', producer: 'catalog-v1', status: 'candidate', value: { level: 'partial', reasons: ['待核对版本目录'] }, evidence_ids: [100, 101, 102] };
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const json = (value: unknown) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) });
    if (path === '/api/auth/me') return json({ id: 1, username: 'reviewer', role: 'reviewer', kind: 'human' });
    if (path === '/api/overview/dashboard') return json(dashboardFixture(14, 1));
    if (path === '/api/qa-capabilities') return json({ configured: false, reason: 'not configured', limits: {}, tools: [] });
    if (path === '/api/review/enrichments') return json({ items: [candidate] });
    if (path === '/api/datasets/1') return json({ dataset: currentDataset,
      versions: [...(version ? [version] : []), { id: 11, revision: 'new-revision', provenance_status: 'verified', metadata: currentDataset }],
      enrichments: [candidate], evidence: [
        { id: 100, dataset_version_id: 10, kind: 'card', location: 'old/README.md:4', excerpt: '旧版许可 legacy-license。另列待核对名称 current-license。' },
        { id: 101, dataset_version_id: 11, kind: 'card', location: 'new/README.md:4', excerpt: '当前版本证据，不得混入。' },
        { id: 102, kind: 'card', location: 'README.md:4', excerpt: '未绑定版本的历史证据，不得混入。' },
      ] });
    return json({ items: [] });
  });
  await page.goto('/');
  await page.getByRole('navigation', { name: '科研工作区' }).getByRole('button', { name: '整理核验', exact: true }).click();
  await expect(page.getByTestId('review-facts')).toBeVisible();
}

test('historical candidate facts and evidence highlighting use its immutable version', async ({ page }) => {
  await openReview(page, oldVersion);
  const facts = page.getByTestId('review-facts');
  await expect(facts).toContainText('版本 #10');
  await expect(facts).toContainText('legacy-license');
  await expect(facts).toContainText('120 行');
  await expect(facts).toContainText('text');
  await expect(facts).not.toContainText('current-license');
  await expect(facts).not.toContainText('9,800');
  await expect(facts).not.toContainText('image');
  await expect(page.locator('#evidence-100 mark')).toHaveText(['legacy-license']);
  await expect(page.locator('#evidence-101')).toHaveCount(0);
  await expect(page.locator('#evidence-102')).toHaveCount(0);
});

for (const [label, version] of [
  ['missing version', null],
  ['missing metadata', { ...oldVersion, metadata: undefined }],
  ['unverified history', { ...oldVersion, provenance_status: 'legacy_unverified' }],
] as const) {
  test(`${label} displays an unverified notice without current metadata`, async ({ page }) => {
    await openReview(page, version);
    const facts = page.getByTestId('review-facts');
    await expect(facts).toContainText('历史版本未验证');
    await expect(facts).not.toContainText('current-license');
    await expect(facts).not.toContainText('9,800');
    await expect(page.locator('#evidence-100 mark')).toHaveCount(0);
  });
}
