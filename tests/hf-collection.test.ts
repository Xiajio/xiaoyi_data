import { afterEach, describe, expect, it, vi } from 'vitest';
import { CatalogApi } from '../src/catalog/api';
import { scopeFromDraft } from '../src/catalog/HfCollectionPage';

const scope = { search: 'news', author: 'lab', languages: ['zh', 'en'], task_categories: ['text-classification'], limit: 50 };
describe('HF collection scope', () => {
  it('trims and deduplicates multilingual filters while preserving the bounded selection', () => {
    expect(scopeFromDraft({ search: ' news ', author: ' lab ', languages: 'zh，en, zh\n', tasks: 'text-classification,text-classification', limit: '50' })).toEqual(scope);
  });
  it.each(['', '0', '301', '1.5', 'NaN'])('rejects invalid limit %s before starting a job', limit => {
    expect(() => scopeFromDraft({ search: '', author: '', languages: '', tasks: '', limit })).toThrow('1 至 300');
  });
});
describe('HF collection API contract', () => {
  afterEach(() => vi.unstubAllGlobals());
  const setup = () => {
    const fetchMock = vi.fn(async (_url: RequestInfo | URL, _init?: RequestInit) => new Response(JSON.stringify({ id: 1, items: [] }), { status: 200 }));
    vi.stubGlobal('fetch', fetchMock); return { api: new CatalogApi('http://catalog.test'), fetchMock };
  };
  it('submits only a flat collection scope with session credentials', async () => {
    const { api, fetchMock } = setup(); await api.createCollectionJob(scope);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('http://catalog.test/api/collection-jobs'); expect(init?.credentials).toBe('include');
    expect(JSON.parse(String(init?.body))).toEqual(scope);
  });
  it('preserves a disabled schedule and its exact scope when creating and enabling it', async () => {
    const { api, fetchMock } = setup(); const value = { name: '每日新闻', scope, interval_minutes: 1440, enabled: false };
    await api.saveCollectionSchedule(value); await api.saveCollectionSchedule({ ...value, enabled: true }, 'schedule/2');
    expect(fetchMock.mock.calls[0][0]).toBe('http://catalog.test/api/collection-schedules');
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual(value);
    expect(fetchMock.mock.calls[1][0]).toBe('http://catalog.test/api/collection-schedules/schedule%2F2');
    expect(JSON.parse(String(fetchMock.mock.calls[1][1]?.body))).toEqual({ ...value, enabled: true });
  });
  it('targets one job for events, cancellation and retry and one version for evidence', async () => {
    const { api, fetchMock } = setup();
    await api.collectionEvents('job/1', 12); await api.cancelCollectionJob('job/1'); await api.retryCollectionJob('job/1'); await api.versionEvidence(23);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      'http://catalog.test/api/collection-jobs/job%2F1/events?after_id=12', 'http://catalog.test/api/collection-jobs/job%2F1/cancel',
      'http://catalog.test/api/collection-jobs/job%2F1/retry', 'http://catalog.test/api/dataset-versions/23/evidence',
    ]);
    expect(fetchMock.mock.calls[1][1]?.method).toBe('POST'); expect(fetchMock.mock.calls[2][1]?.method).toBe('POST');
  });
});
