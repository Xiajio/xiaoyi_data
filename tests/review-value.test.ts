import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { VersionItem } from '../src/catalog/api';
import { describe, expect, it } from 'vitest';
import { buildHighlightTokens, DatasetFacts, normalizeLevel, reviewVersionMetadata, reasonTone, summarizeSchema, truncateVectorValue } from '../src/catalog/reviewValue';

describe('normalizeLevel', () => {
  it('maps known levels to tones and Chinese labels', () => {
    expect(normalizeLevel('high')).toEqual({ label: '高', tone: 'good' });
    expect(normalizeLevel('medium')).toEqual({ label: '中', tone: 'warn' });
    expect(normalizeLevel('low')).toEqual({ label: '低', tone: 'bad' });
    expect(normalizeLevel('complete')).toEqual({ label: '完整', tone: 'good' });
    expect(normalizeLevel('partial')).toEqual({ label: '部分', tone: 'warn' });
  });
  it('keeps unknown levels as neutral raw text and ignores missing ones', () => {
    expect(normalizeLevel('excellent')).toEqual({ label: 'excellent', tone: '' });
    expect(normalizeLevel(undefined)).toBeNull();
    expect(normalizeLevel('')).toBeNull();
    expect(normalizeLevel(4)).toBeNull();
  });
});

describe('reasonTone', () => {
  it('flags risk reasons', () => {
    expect(reasonTone('许可未知')).toBe('warn');
    expect(reasonTone('缺少数据集卡片')).toBe('warn');
    expect(reasonTone('卡片内容较少')).toBe('warn');
    expect(reasonTone('超过 8 个月未更新')).toBe('warn');
    expect(reasonTone('来源更新日期未知')).toBe('warn');
  });
  it('keeps positive reasons', () => {
    expect(reasonTone('卡片内容充实（≥500 字符）')).toBe('good');
    expect(reasonTone('下载量 4212 较高')).toBe('good');
    expect(reasonTone('近半年有更新')).toBe('good');
    expect(reasonTone('许可已标注（permissive）')).toBe('good');
  });
});

describe('truncateVectorValue', () => {
  it('truncates long vectors with a dimension note', () => {
    const value = { dim: 12, vector: Array.from({ length: 12 }, (_, index) => index), text_hash: 'abc' };
    const result = truncateVectorValue(value);
    expect(result.vector).toHaveLength(9);
    expect((result.vector as unknown[]).slice(0, 8)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect((result.vector as unknown[])[8]).toBe('… 共 12 维');
    expect(value.vector).toHaveLength(12);
  });
  it('leaves short or missing vectors untouched', () => {
    const short = { vector: [1, 2, 3] };
    expect(truncateVectorValue(short)).toBe(short);
    const none = { dim: 3 };
    expect(truncateVectorValue(none)).toBe(none);
  });
});

describe('summarizeSchema', () => {
  it('normalizes features, splits and totals', () => {
    const summary = summarizeSchema({
      present: true,
      features: [{ name: 'text', dtype: 'string' }, { name: 'label' }],
      splits: [{ name: 'train', num_examples: 80 }, { name: 'test' }],
      dataset_size: 2048, download_size: 1024,
    });
    expect(summary.present).toBe(true);
    expect(summary.features).toEqual([{ name: 'text', dtype: 'string' }, { name: 'label', dtype: '' }]);
    expect(summary.splits).toEqual([{ name: 'train', num_examples: 80 }, { name: 'test', num_examples: null }]);
    expect(summary.totalExamples).toBe(80);
    expect(summary.datasetSize).toBe(2048);
  });
  it('prefers explicit totals and marks absent structures', () => {
    expect(summarizeSchema({ present: false }).present).toBe(false);
    const summary = summarizeSchema({ present: true, total_examples: 500, splits: [{ name: 'train', num_examples: 80 }] });
    expect(summary.totalExamples).toBe(500);
    expect(summarizeSchema({ present: true }).totalExamples).toBeNull();
  });
});

describe('buildHighlightTokens', () => {
  it('collects numbers, parenthesized notes, splits, quotes and dataset facts', () => {
    const tokens = buildHighlightTokens(
      { reasons: ['下载量 4212 较高', '许可已标注（permissive）'], splits: [{ name: 'train' }], claims: [{ text: 'x', quotes: ['短', '足够长的引文内容'] }] },
      { downloads: 4212, license: 'mit', size_rows: 100 } as never,
    );
    expect(tokens).toContain('4212');
    expect(tokens).toContain('permissive');
    expect(tokens).toContain('train');
    expect(tokens).toContain('足够长的引文内容');
    expect(tokens).toContain('mit');
    expect(tokens).toContain('100');
    expect(tokens).not.toContain('短');
  });
  it('deduplicates and caps the token list', () => {
    const tokens = buildHighlightTokens({ reasons: ['下载量 4212 较高', '下载量 4212 较高'] }, null);
    expect(tokens.filter(token => token === '4212')).toHaveLength(1);
    expect(tokens.length).toBeLessThanOrEqual(40);
  });
});


describe('reviewVersionMetadata', () => {
  const oldMetadata = { license: 'legacy-license', license_class: 'copyleft', size_rows: 120, downloads: 321 };
  const newMetadata = { license: 'current-license', license_class: 'permissive', size_rows: 9800, downloads: 9876 };
  const versions: VersionItem[] = [
    { id: 11, revision: 'new', provenance_status: 'verified', metadata: newMetadata },
    { id: 10, revision: 'old', provenance_status: 'verified', metadata: oldMetadata },
  ];
  it('uses the selected immutable version for facts and highlight tokens', () => {
    const metadata = reviewVersionMetadata(versions, '10');
    expect(metadata).toEqual(oldMetadata);
    const tokens = buildHighlightTokens({}, metadata);
    expect(tokens).toEqual(expect.arrayContaining(['legacy-license', '120', '321']));
    expect(tokens).not.toEqual(expect.arrayContaining(['current-license', '9800', '9876']));
    const html = renderToStaticMarkup(createElement(DatasetFacts, { dataset: metadata, versionId: 10 }));
    expect(html).toContain('legacy-license');
    expect(html).toContain('120 行');
    expect(html).not.toContain('current-license');
    expect(html).not.toContain('9,800');
  });
  it.each([
    { id: 10, revision: 'old', provenance_status: 'verified' },
    { id: 10, revision: 'old', provenance_status: 'verified', metadata: {} },
    { id: 10, revision: 'old', provenance_status: 'legacy_unverified', metadata: oldMetadata },
    { id: 10, revision: 'old', metadata: oldMetadata },
  ])('does not substitute current data for missing or unverified metadata (%j)', version => {
    const metadata = reviewVersionMetadata([versions[0], version], 10);
    expect(metadata).toBeNull();
    expect(buildHighlightTokens({}, metadata)).toEqual([]);
    const html = renderToStaticMarkup(createElement(DatasetFacts, { dataset: metadata, versionId: 10 }));
    expect(html).toContain('历史版本未验证');
    expect(html).not.toContain('current-license');
  });
  it('does not default missing version identifiers to the latest version', () => {
    expect(reviewVersionMetadata(versions, undefined)).toBeNull();
    expect(reviewVersionMetadata(versions, 999)).toBeNull();
    expect(reviewVersionMetadata(undefined, 10)).toBeNull();
  });
  it('preserves unknown fields in incomplete metadata instead of filling current values or zeroes', () => {
    const html = renderToStaticMarkup(createElement(DatasetFacts, { dataset: { license: 'legacy-license' }, versionId: 10 }));
    expect(html).toContain('legacy-license');
    expect(html).toContain('未知');
    expect(html).not.toContain('齐');
    expect(html).not.toContain('>0<');
  });
});
