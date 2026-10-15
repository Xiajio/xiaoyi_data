import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, CatalogApi, resolveCatalogBase } from '../src/catalog/api';

describe('resolveCatalogBase', () => {
  it('stays in demo mode without mode param or env base', () => {
    expect(resolveCatalogBase('', undefined)).toBeNull();
    expect(resolveCatalogBase('?foo=1', undefined)).toBeNull();
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

  it('posts review decisions with role and note as JSON', async () => {
    const fetchMock = stubFetch(200, { id: 7, status: 'confirmed' });
    const api = new CatalogApi('http://svc.test');
    await api.reviewPairing(7, 'confirmed', '已核对', 'reviewer');
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('http://svc.test/api/pairings/7/review');
    expect(JSON.parse(String(init.body))).toEqual({ decision: 'confirmed', note: '已核对', role: 'reviewer' });
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
