import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  comparisonRequestIds, countValue, ExperimentComparison, formatCount, formatPercent,
  formatPointChange, rateValue, readComparison,
} from '../src/catalog/ExperimentComparison';

const response = () => ({
  baseline_experiment_id: 11,
  evaluation_build_hash: 'a'.repeat(64),
  items: [
    { experiment_id: 11, build_id: 2, metrics: { accuracy: 0.5, coverage: 1, predictions: 40, expected_samples: 40, error_count: 20 }, accuracy_delta: 0 },
    { experiment_id: 10, build_id: 3, metrics: { accuracy: 0.75, coverage: 1, predictions: 40, expected_samples: 40, error_count: 10 }, accuracy_delta: 0.25 },
  ],
});

describe('experiment measurement formatting', () => {
  it('keeps true zero and full scale distinct from unknown', () => {
    expect(rateValue(0)).toBe(0);
    expect(rateValue(1)).toBe(1);
    expect(formatPercent(0)).toBe('0.00%');
    expect(formatPercent(1)).toBe('100.00%');
    for (const value of [undefined, null, '', '0.5', false, NaN, Infinity, -0.01, 1.01]) {
      expect(rateValue(value)).toBeNull();
      expect(formatPercent(value)).toBe('未知');
    }
    expect(formatPercent(undefined, '待测量')).toBe('待测量');
  });
  it('does not turn missing or invalid counts into zero', () => {
    expect(countValue(0)).toBe(0);
    expect(formatCount(0)).toBe('0');
    expect(formatCount(1234)).toBe('1,234');
    for (const value of [undefined, null, '0', false, -1, 0.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
      expect(countValue(value)).toBeNull();
      expect(formatCount(value)).toBe('未知');
    }
  });
  it('shows signed percentage points without presenting relative percent change', () => {
    expect(formatPointChange(0.125)).toBe('+12.50 个百分点');
    expect(formatPointChange(-0.125)).toBe('-12.50 个百分点');
    expect(formatPointChange(0)).toBe('0.00 个百分点');
    expect(formatPointChange(-0.000001)).toBe('0.00 个百分点');
    for (const value of [undefined, null, '0.1', NaN, Infinity, -1.1, 1.1]) expect(formatPointChange(value)).toBe('差值未知');
  });
});

describe('experiment baseline and response mapping', () => {
  it('defaults to first selected and changes only request order for an explicit baseline', () => {
    const selection = [11, 10, 12];
    expect(comparisonRequestIds(selection)).toEqual([11, 10, 12]);
    expect(comparisonRequestIds(selection, 10)).toEqual([10, 11, 12]);
    expect(comparisonRequestIds(selection, '10')).toEqual([10, 11, 12]);
    expect(comparisonRequestIds(selection, 99)).toEqual([11, 10, 12]);
    expect(comparisonRequestIds([])).toEqual([]);
    expect(selection).toEqual([11, 10, 12]);
  });
  it('maps the actual API envelope to original selection order with authoritative deltas', () => {
    const raw = response();
    raw.items[1].accuracy_delta = 0.123; // Deliberately different: never recompute a server result in the UI.
    const view = readComparison(raw, [10, 11], 11)!;
    expect(view.baselineId).toBe(11);
    expect(view.evaluationHash).toBe(raw.evaluation_build_hash);
    expect(view.rows.map(row => row.experimentId)).toEqual([10, 11]);
    expect(view.rows[0]).toEqual({ experimentId: 10, buildId: 3, accuracy: 0.75, coverage: 1, predictions: 40, expectedSamples: 40, errorCount: 10, delta: 0.123 });
    expect(readComparison(raw, ['11', '10'], '11')?.rows.map(row => row.experimentId)).toEqual([11, 10]);
  });
  it('keeps metrics unknown while retaining identifiable response rows', () => {
    const raw = response();
    const absent = { ...raw, items: [raw.items[0], { experiment_id: 10, build_id: 3, metrics: { accuracy: null, coverage: 2, predictions: '40', error_count: -1 }, accuracy_delta: NaN }] };
    expect(readComparison(absent, [11, 10], 11)?.rows[1]).toEqual({ experimentId: 10, buildId: 3, accuracy: null, coverage: null, predictions: null, expectedSamples: null, errorCount: null, delta: null });
  });
  it.each([
    ['old incomplete response', { baseline_experiment_id: 11, accuracy_delta: 0.25 }],
    ['missing evaluation hash', { ...response(), evaluation_build_hash: '' }],
    ['wrong baseline', { ...response(), baseline_experiment_id: 10 }],
    ['missing selected experiment', { ...response(), items: response().items.slice(0, 1) }],
    ['duplicate response identity', { ...response(), items: [response().items[0], response().items[0]] }],
    ['unrequested identity', { ...response(), items: [response().items[0], { ...response().items[1], experiment_id: 99 }] }],
    ['invalid build identity', { ...response(), items: [response().items[0], { ...response().items[1], build_id: null }] }],
    ['array instead of envelope', []],
  ])('does not draw %s', (_label, raw) => {
    expect(readComparison(raw, [11, 10], 11)).toBeNull();
  });
  it('requires an unambiguous selection but does not substitute frontend eligibility checks', () => {
    expect(readComparison(response(), [11, '11'], 11)).toBeNull();
    expect(readComparison(response(), [11], 11)).toBeNull();
    // Model and protocol equality belong to the server; they are not in this response envelope.
    expect(readComparison(response(), [11, 10], 11)).not.toBeNull();
  });
});

describe('comparison rendering', () => {
  it('renders fixed scale bars and numeric alternatives, including true zero', () => {
    const raw = response();
    raw.items[0].metrics.accuracy = 0;
    raw.items[1].metrics.accuracy = 1;
    const html = renderToStaticMarkup(createElement(ExperimentComparison, { value: readComparison(raw, [11, 10], 11)! }));
    expect(html).toContain('固定量尺 0–100%');
    expect(html).toContain('width:0%');
    expect(html).toContain('width:100%');
    expect(html).toContain('0.00%');
    expect(html).toContain('100.00%');
    expect(html).toContain('+25.00 个百分点');
    expect(html).toContain('应评估 40');
  });
  it('shows unknown text instead of a zero bar when no accuracy was supplied', () => {
    const raw = { ...response(), items: response().items.map(item => ({ ...item, metrics: {} })) };
    const html = renderToStaticMarkup(createElement(ExperimentComparison, { value: readComparison(raw, [11, 10], 11)! }));
    expect(html).toContain('准确率未知，未绘制');
    expect(html).toContain('错误 未知 个');
    expect(html).not.toContain('class="ec-track"');
    expect(html).not.toContain('width:0%');
  });
});
