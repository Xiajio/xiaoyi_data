export function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
export function measuredCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null;
}
export const displayCount = (value: number | null) => value === null ? '未提供' : value.toLocaleString('zh-CN');
export function proportion(value: number | null, total: number | null): number | null {
  return value !== null && total !== null && total > 0 && value <= total ? value / total * 100 : null;
}
export function buildMeasurements(response: Record<string, unknown>) {
  const manifest = asRecord(response.manifest);
  const counts = asRecord(manifest.counts);
  const steps = Array.isArray(manifest.steps) ? manifest.steps.map(asRecord) : [];
  const excluded = (key: string, operator: string) => measuredCount(counts[key]) ?? measuredCount(steps.find(step => step.operator === operator)?.excluded);
  const input = measuredCount(counts.input_rows);
  const output = measuredCount(counts.output_rows);
  const invalid = excluded('invalid_rows', 'validate/1');
  const duplicates = excluded('exact_duplicates', 'exact-dedup/1');
  const budget = excluded('budget_excluded', 'budget-select/1');
  const unique = measuredCount(counts.before_selection);
  const valid = input !== null && invalid !== null && invalid <= input ? input - invalid : null;
  const reasons = [
    { key: 'invalid', label: '校验排除', value: invalid, tone: 'invalid' },
    { key: 'duplicates', label: '精确重复', value: duplicates, tone: 'duplicates' },
    { key: 'budget', label: '预算选择排除', value: budget, tone: 'budget' },
  ];
  const known = reasons.every(item => item.value !== null);
  const balanced = input !== null && output !== null && known && output + reasons.reduce((sum, item) => sum + item.value!, 0) === input;
  const splitRecord = asRecord(manifest.splits);
  const labels: Record<string, string> = { train: '训练集', validation: '验证集', test: '测试集' };
  const keys = ['train', 'validation', 'test', ...Object.keys(splitRecord).filter(key => !(key in labels))];
  const hasSplits = manifest.splits !== null && typeof manifest.splits === 'object' && !Array.isArray(manifest.splits);
  const splits = keys.map(key => ({ key, label: labels[key] ?? key,
    value: measuredCount(splitRecord[key]) ?? (hasSplits && manifest.format === 'xiaoyi.samples/1' && !(key in splitRecord) ? 0 : null) }));
  const splitTotal = splits.every(item => item.value !== null) ? splits.reduce((sum, item) => sum + item.value!, 0) : null;
  const splitBalanced = output !== null && splitTotal === output;
  return { manifest, input, output, invalid, duplicates, budget, valid, unique, reasons, balanced, splitTotal,
    splits: splits.map(item => ({ ...item, percentage: splitBalanced ? proportion(item.value, output) : null })),
    retainedPercent: proportion(output, input), splitBalanced };
}
export function currentMiningProfile(profiles: Record<string, unknown>[]) {
  return profiles.filter(item => item.producer === 'candidate-mining/1')
    .reduce<Record<string, unknown> | null>((latest, item) => !latest || Number(item.id) > Number(latest.id) ? item : latest, null);
}
export function labelSlices(profile: Record<string, unknown> | null, split: string) {
  const summary = asRecord(profile?.summary);
  const slices = Array.isArray(summary.slices) ? summary.slices.map(asRecord) : [];
  const totals = new Map<string, number>();
  for (const slice of slices) {
    const count = measuredCount(slice.count);
    if (count === null || typeof slice.label !== 'string' || (split !== 'all' && slice.split !== split)) continue;
    totals.set(slice.label, (totals.get(slice.label) ?? 0) + count);
  }
  return [...totals].map(([label, count]) => ({ label, count })).sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, 'zh-CN'));
}
