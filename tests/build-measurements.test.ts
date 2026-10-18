import { describe, expect, it } from 'vitest';
import { buildMeasurements, currentMiningProfile, labelSlices, measuredCount, proportion } from '../src/catalog/buildMeasurements';

const manifest = { format: 'xiaoyi.samples/1', counts: { input_rows: 100, invalid_rows: 5, exact_duplicates: 10, before_selection: 85, budget_excluded: 15, output_rows: 70 }, splits: { train: 50, validation: 10, test: 10 }, steps: [] };
describe('build quality measurements', () => {
  it('reconciles retained rows against distinct exclusion reasons without summing stages', () => {
    const value = buildMeasurements({ manifest });
    expect(value.valid).toBe(95); expect(value.unique).toBe(85); expect(value.output).toBe(70);
    expect(value.balanced).toBe(true); expect(value.retainedPercent).toBe(70); expect(value.splitBalanced).toBe(true);
  });
  it('reads zero-valued exclusions from immutable operator steps when sparse counters omit them', () => {
    const value = buildMeasurements({ manifest: { ...manifest, counts: { input_rows: 10, output_rows: 10, before_selection: 10 }, steps: [{ operator: 'validate/1', excluded: 0 }, { operator: 'exact-dedup/1', excluded: 0 }, { operator: 'budget-select/1', excluded: 0 }] } });
    expect(value.balanced).toBe(true); expect(value.invalid).toBe(0); expect(value.duplicates).toBe(0);
  });
  it('does not turn unreported fields into zero or invent a retention rate', () => {
    const value = buildMeasurements({ manifest: { counts: { output_rows: 2 } } });
    expect(value.input).toBeNull(); expect(value.invalid).toBeNull(); expect(value.retainedPercent).toBeNull(); expect(value.splitTotal).toBeNull();
  });
  it('distinguishes empty measured input from absent measurements', () => {
    const value = buildMeasurements({ manifest: { ...manifest, counts: { input_rows: 0, output_rows: 0, invalid_rows: 0, exact_duplicates: 0, budget_excluded: 0 }, splits: {} } });
    expect(value.balanced).toBe(true); expect(value.splitTotal).toBe(0); expect(value.retainedPercent).toBeNull();
  });
  it('refuses misleading percentages when totals disagree or are invalid', () => {
    const value = buildMeasurements({ manifest: { ...manifest, counts: { ...manifest.counts, output_rows: 101 } } });
    expect(value.balanced).toBe(false); expect(value.splitBalanced).toBe(false); expect(value.retainedPercent).toBeNull();
    for (const bad of [undefined, '12', -1, NaN, Infinity, 1.5]) expect(measuredCount(bad)).toBeNull();
    expect(proportion(0, 0)).toBeNull(); expect(proportion(5, 10)).toBe(50);
  });
  it('withholds all split percentages when reported split counts exceed the output', () => {
    const value = buildMeasurements({ manifest: { ...manifest, splits: { train: 60, validation: 0, test: 60 } } });
    expect(value.splitBalanced).toBe(false); expect(value.splits.map(item => item.percentage)).toEqual([null, null, null]);
    expect(value.splits[0].value).toBe(60);
  });
  it('handles unknown splits without silently dropping them from the denominator', () => {
    const value = buildMeasurements({ manifest: { ...manifest, splits: { train: 60, holdout: 10 } } });
    expect(value.splitBalanced).toBe(true); expect(value.splits.find(item => item.key === 'holdout')).toMatchObject({ value: 10, label: 'holdout' });
  });
  it('selects the newest loaded mining measurement and aggregates only the chosen split', () => {
    const profile = { id: 9, producer: 'candidate-mining/1', summary: { slices: [{ label: 'A', split: 'train', count: 5 }, { label: 'A', split: 'test', count: 2 }, { label: 'B', split: 'train', count: 3 }, { label: 'bad', split: 'train', count: -1 }] } };
    const latest = currentMiningProfile([{ id: 20, producer: 'cpu-profile/1' }, profile, { id: 2, producer: 'candidate-mining/1' }]);
    expect(latest).toBe(profile); expect(labelSlices(latest, 'all')).toEqual([{ label: 'A', count: 7 }, { label: 'B', count: 3 }]);
    expect(labelSlices(latest, 'test')).toEqual([{ label: 'A', count: 2 }]); expect(labelSlices(null, 'all')).toEqual([]);
  });
});
