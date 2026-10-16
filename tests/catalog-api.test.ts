import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, CatalogApi, requestKey, resolveCatalogBase } from '../src/catalog/api';

describe('resolveCatalogBase', () => {
  it('defaults to catalog mode and keeps explicit demos and embeds separate', () => {
    expect(resolveCatalogBase('', undefined)).toBe('http://127.0.0.1:5280');
    expect(resolveCatalogBase('?mode=demo', 'http://svc.test')).toBeNull();
    expect(resolveCatalogBase('?embed=1', 'http://svc.test')).toBeNull();
  });
  it('uses default local port in catalog mode and honors api override', () => {
    expect(resolveCatalogBase('?mode=catalog', undefined)).toBe('http://127.0.0.1:5280');
    expect(resolveCatalogBase('?mode=catalog&api=http://10.10.70.12:5290', undefined)).toBe('http://10.10.70.12:5290');
  });
  it('env base alone switches to catalog mode', () => {
    expect(resolveCatalogBase('', 'http://catalog.internal:5280')).toBe('http://catalog.internal:5280');
    expect(resolveCatalogBase('?api=https://override.example', 'http://catalog.internal:5280')).toBe('https://override.example');
  });
});

describe('CatalogApi', () => {
  afterEach(() => vi.unstubAllGlobals());

  function stubFetch(status: number, payload: unknown) {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL) => new Response(typeof payload === 'string' ? payload : JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);
    return fetchMock;
  }

  it('requests one authenticated dashboard aggregation for the selected trend period', async () => {
    const fetchMock = stubFetch(200, { totals: {}, trend: { days: 30 } });
    await new CatalogApi('http://svc.test').dashboard(30);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://svc.test/api/overview/dashboard?days=30'); expect(init.credentials).toBe('include');
  });

  it('uses authenticated owner-scoped Q&A paths and stable client request keys', async () => {
    const fetchMock = stubFetch(200, { id: 'conversation/one', status: 'queued' });
    const api = new CatalogApi('http://svc.test');
    await api.qaCapabilities(); await api.qaConversations(); await api.createQaConversation('采集情况');
    await api.qaConversation('conversation/one'); await api.askQa('conversation/one', '收集了多少？', 'logical-request-key');
    await api.cancelQaTurn('turn/one'); await api.retryQaTurn('turn/one');
    const calls = fetchMock.mock.calls as unknown as [string, RequestInit][];
    expect(calls.map(([url]) => url)).toEqual([
      'http://svc.test/api/qa-capabilities', 'http://svc.test/api/qa-conversations', 'http://svc.test/api/qa-conversations',
      'http://svc.test/api/qa-conversations/conversation%2Fone', 'http://svc.test/api/qa-conversations/conversation%2Fone/turns',
      'http://svc.test/api/qa-turns/turn%2Fone/cancel', 'http://svc.test/api/qa-turns/turn%2Fone/retry',
    ]);
    expect(calls.every(([, init]) => init.credentials === 'include')).toBe(true);
    expect(JSON.parse(String(calls[4][1].body))).toEqual({ question: '收集了多少？', request_key: 'logical-request-key' });
  });

  it('builds filtered dataset queries', async () => {
    const fetchMock = stubFetch(200, { total: 0, page: 1, size: 20, items: [] });
    const api = new CatalogApi('http://svc.test');
    await api.datasets({ modality: 'text', licenseClass: 'permissive', q: '情感', page: 2, size: 50 });
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('modality=text');
    expect(url).toContain('license_class=permissive');
    expect(url).toContain('q=%E6%83%85%E6%84%9F');
    expect(url).toContain('page=2');
    expect(url).toContain('size=50');
  });

  it('posts review decisions using the session identity, without a client role', async () => {
    const fetchMock = stubFetch(200, { id: 7, status: 'confirmed' });
    const api = new CatalogApi('http://svc.test');
    await api.reviewPairing(7, 'confirmed', '已核对');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://svc.test/api/pairings/7/review');
    expect(init.credentials).toBe('include');
    expect(JSON.parse(String(init.body))).toEqual({ decision: 'confirmed', note: '已核对' });
  });

  it('submits research constraints and bounded tool budgets intact', async () => {
    const fetchMock = stubFetch(201, { id: 'run-uuid', status: 'queued' });
    const api = new CatalogApi('http://svc.test/');
    const request = { goal: '寻找中文数据集', hard_constraints: { modalities: ['text'], license_class: ['permissive'], min_rows: 50 }, preferences: { notes: '优先小型数据' }, sources: ['manual'], max_candidates: 100, max_tool_calls: 20 };
    await api.createAgentRun(request);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://svc.test/api/agent-runs'); expect(JSON.parse(String(init.body))).toEqual(request);
    expect(init.credentials).toBe('include');
  });

  it('encodes string identifiers and never sends a review role', async () => {
    const fetchMock = stubFetch(200, { id: 'run/one' });
    const api = new CatalogApi('http://svc.test');
    await api.cancelAgentRun('run/one');
    expect(fetchMock.mock.calls[0][0]).toBe('http://svc.test/api/agent-runs/run%2Fone/cancel');
    await api.reviewEnrichment(3, 'corrected', '已核对', { level: 'partial' });
    const [, init] = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ decision: 'corrected', note: '已核对', value: { level: 'partial' } });
  });

  it('binds a download request to an exact version, range and size cap', async () => {
    const fetchMock = stubFetch(201, { id: 4, status: 'pending' });
    await new CatalogApi('http://svc.test').requestDownload(11, ['train.csv'], 2048);
    const [, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ dataset_version_id: 11, files: ['train.csv'], max_bytes: 2048 });
  });

  it('retrieves a binary research bundle using the authenticated session', async () => {
    const fetchMock = stubFetch(200, 'PK bundle');
    const blob = await new CatalogApi('http://svc.test').bundle(8);
    expect(await blob.text()).toBe('PK bundle');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://svc.test/api/snapshots/8/bundle'); expect(init.credentials).toBe('include');
  });

  it('surfaces server detail as ApiError message', async () => {
    stubFetch(400, { detail: '只能导出已批准的快照' });
    const api = new CatalogApi('http://svc.test');
    const error = await api.exportSnapshot(3).catch(reason => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(400);
    expect((error as ApiError).message).toBe('只能导出已批准的快照');
  });

  it('wraps connection failures with a hint', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('fetch failed'); }));
    const api = new CatalogApi('http://down.test');
    const error = await api.overview().catch(reason => reason);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(0);
    expect((error as ApiError).message).toContain('无法连接目录服务');
  });
});

describe('requestKey', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('uses crypto.randomUUID when available', () => {
    vi.stubGlobal('crypto', { randomUUID: () => 'fixed-uuid' });
    expect(requestKey()).toBe('fixed-uuid');
  });

  it('falls back to a server-valid UUID shape without randomUUID (insecure LAN context)', () => {
    vi.stubGlobal('crypto', { getRandomValues: (bytes: Uint8Array) => { bytes.fill(7); return bytes; } });
    const key = requestKey();
    expect(key).toMatch(/^[A-Za-z0-9_-]{36}$/);
    expect(key[14]).toBe('4');
  });

  it('still produces unique keys when crypto is absent entirely', () => {
    vi.stubGlobal('crypto', undefined);
    const key = requestKey();
    expect(key).toMatch(/^[A-Za-z0-9_-]{36}$/);
    expect(requestKey()).not.toBe(key);
  });
});
