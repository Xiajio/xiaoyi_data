import { expect, test, type Page } from '@playwright/test';
import type { QaCapabilities, QaConversationDetail, QaTurn } from '../../src/catalog/api';
import { dashboardFixture } from './dashboard-fixture';

async function fixture(page: Page) {
  const calls: { path: string; body: Record<string, unknown> }[] = [];
  const capabilities: QaCapabilities = { configured: true, model: 'local-fixture-model', reason: '', limits: { max_model_calls: 3, max_tool_calls: 6 }, tools: ['catalog_stats', 'search_datasets'] };
  const conversations: QaConversationDetail[] = [];
  const state = { submissionFailures: 0, detailFailures: 0, questionPosts: 0, detailGets: 0, nextId: 1 };
  const makeTurn = (id: number, conversationId: number, question: string): QaTurn => ({ id, conversation_id: conversationId, question, status: 'queued', stage: 'queued', answer: null, sources: [], model: null, usage: {}, model_calls: 0, tool_calls: 0, error: null, created_at: '2026-10-16T02:00:00Z', finished_at: null });
  const complete = (turn: QaTurn) => Object.assign(turn, { status: 'completed', stage: 'completed', model: 'local-fixture-model', model_calls: 2, tool_calls: 1, usage: { input_tokens: 240, output_tokens: 54 }, finished_at: '2026-10-16T02:00:10Z', sources: [{ id: 'source-1', kind: 'catalog_stats', title: '本地目录统计', locator: 'catalog://datasets/1', captured_at: '2026-10-16T02:00:02Z', dataset_version_id: 11, truncated: true, content: '数据集共 60 个。\n<img src=x onerror="alert(1)">\n许可未分类 45 个。' }], answer: { claims: [{ text: '当前已收集 60 个数据集。', citations: [{ source_id: 'source-1', quote: '数据集共 60 个。' }] }], uncertainty: ['45 个数据集的许可分类仍待核验。'] } });
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname; const method = request.method(); const body = method === 'POST' ? request.postDataJSON() : {};
    if (method === 'POST') calls.push({ path, body });
    const json = (value: unknown, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) });
    if (path === '/api/auth/me') return json({ id: 1, username: 'reader', role: 'reader', kind: 'human' });
    if (path === '/api/overview/dashboard') return json(dashboardFixture());
    if (path === '/api/overview') return json({ datasets: 60, evidence: 92, runs: 4, by_license_class: {}, by_modality: {}, enrichment_candidates: 120, pairings_pending: 0, snapshots: {}, ideas: 0 });
    if (path === '/api/qa-capabilities') return json(capabilities);
    if (path === '/api/qa-conversations' && method === 'GET') return json({ items: conversations.map(({ turns: _turns, ...conversation }) => conversation) });
    if (path === '/api/qa-conversations' && method === 'POST') { const conversation = { id: conversations.length + 1, title: body.title || '新对话', actor: 'reader', created_at: '2026-10-16T02:00:00Z', updated_at: '2026-10-16T02:00:00Z', turns: [] }; conversations.unshift(conversation); return json(conversation, 201); }
    const match = path.match(/^\/api\/qa-conversations\/(\d+)(\/turns)?$/);
    if (match) {
      const conversation = conversations.find(item => String(item.id) === match[1])!;
      if (match[2] && method === 'POST') { state.questionPosts++; if (state.submissionFailures > 0) { state.submissionFailures--; return json({ detail: '模型服务暂时不可用，请保留草稿后重试。' }, 503); } const turn = makeTurn(state.nextId++, Number(conversation.id), body.question); conversation.turns.push(turn); return json(turn, 202); }
      state.detailGets++; if (state.detailFailures > 0) { state.detailFailures--; return json({ detail: '读取对话超时' }, 500); } return json(conversation);
    }
    const action = path.match(/^\/api\/qa-turns\/(\d+)\/(cancel|retry)$/);
    if (action) { const turn = conversations.flatMap(item => item.turns).find(item => String(item.id) === action[1])!; Object.assign(turn, { status: action[2] === 'cancel' ? 'cancelled' : 'queued', stage: action[2] === 'cancel' ? 'cancelled' : 'queued' }); return json(turn); }
    return json({ items: [] });
  });
  return { calls, capabilities, conversations, state, makeTurn, complete };
}
async function go(page: Page) { await page.goto('/'); await page.getByRole('button', { name: '数据问答', exact: true }).click(); await expect(page.getByRole('heading', { name: '数据问答', exact: true })).toBeVisible(); }

test('reader asks multiple turns, sees actual status and quoted evidence, and historical conversations persist', async ({ page }, testInfo) => {
  const data = await fixture(page); await go(page); await page.getByRole('button', { name: '目前收集了多少数据集？' }).click(); await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await expect(page.getByTestId('qa-turn-1')).toContainText('排队中'); expect(data.calls[1].body).toMatchObject({ question: '目前收集了多少数据集？' }); expect(data.calls[1].body.request_key).toMatch(/^[\w-]{36}$/);
  Object.assign(data.conversations[0].turns[0], { status: 'running', stage: 'querying' }); await expect(page.getByTestId('qa-turn-1')).toContainText('查询本地目录', { timeout: 6000 });
  data.complete(data.conversations[0].turns[0]); const turn = page.getByTestId('qa-turn-1'); await expect(turn).toContainText('当前已收集 60 个数据集。', { timeout: 6000 }); await expect(turn).toContainText('45 个数据集的许可分类仍待核验');
  await turn.getByRole('button', { name: '本地目录统计', exact: true }).click(); const source = page.getByRole('region', { name: '引用来源详情' }); await expect(source).toContainText('数据集共 60 个。'); await expect(source).toContainText('#11'); await expect(source).toContainText('仅保存了截取内容'); await expect(source).toContainText('上海时间'); await source.getByText('展开本次保存的来源记录').click(); await expect(source.locator('pre')).toContainText('<img src=x onerror="alert(1)">'); await expect(source.locator('img')).toHaveCount(0);
  await turn.getByText('执行详情 · local-fixture-model').click(); await expect(turn).toContainText('input_tokens240');
  await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('qa-desktop-light.png'), fullPage: true });
  await page.getByRole('button', { name: '切换深色主题' }).click(); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('qa-desktop-dark.png'), fullPage: true });
  const settledGets = data.state.detailGets; await page.waitForTimeout(2400); expect(data.state.detailGets).toBe(settledGets);
  const composer = page.getByLabel('继续提问'); await composer.fill('其中图像数据有哪些？'); await composer.press('Enter'); await expect(composer).toHaveValue('其中图像数据有哪些？\n'); expect(data.state.questionPosts).toBe(1); await composer.press('Control+Enter'); await expect(page.getByTestId('qa-turn-2')).toContainText('排队中');
  await page.getByTestId('qa-turn-2').getByRole('button', { name: '取消回答' }).click(); await expect(page.getByTestId('qa-turn-2')).toContainText('已取消');
  await page.getByRole('button', { name: '新建对话', exact: true }).click(); await expect(page.getByText('从一个具体问题开始')).toBeVisible();
  await page.getByRole('complementary', { name: '问答历史' }).getByRole('button', { name: /目前收集了多少数据集/ }).click(); await expect(page.getByTestId('qa-turn-1')).toContainText('当前已收集 60 个数据集'); await expect(page.getByTestId('qa-turn-2')).toContainText('已取消');
  expect(data.calls.some(call => /collection-jobs|agent-runs|download-requests|approve|review/.test(call.path))).toBe(false);
});

test('unconfigured model shows directory facts, preserves draft and never submits an offline answer', async ({ page }, testInfo) => {
  const data = await fixture(page); data.capabilities.configured = false; data.capabilities.model = null; data.capabilities.reason = '尚未配置模型 API。'; await go(page);
  await expect(page.getByRole('region', { name: '模型配置状态' })).toContainText('尚未配置模型 API'); await expect(page.getByText('直接读取目录统计，非模型回答。')).toBeVisible(); await page.getByRole('button', { name: '目录里有哪些图像数据集？' }).click(); await expect(page.getByLabel('你的问题')).toHaveValue('目录里有哪些图像数据集？'); await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeDisabled(); expect(data.calls).toEqual([]);
  await page.getByText('服务端配置项', { exact: true }).click(); await expect(page.getByRole('region', { name: '模型配置状态' })).toContainText('XIAOYI_LLM_API_KEY'); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('qa-config-missing.png'), fullPage: true });
  data.capabilities.configured = true; data.capabilities.model = 'local-fixture-model'; await page.getByRole('button', { name: '重新检查配置' }).click(); await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeEnabled(); await expect(page.getByLabel('你的问题')).toHaveValue('目录里有哪些图像数据集？');
});

test('503 keeps question and idempotency key; failure retry and transient poll errors remain actionable', async ({ page }) => {
  const data = await fixture(page); data.state.submissionFailures = 1; await go(page); await page.getByLabel('你的问题').fill('最近的失败原因'); await page.getByRole('button', { name: '发送问题', exact: true }).click(); await expect(page.getByRole('alert')).toContainText('模型服务暂时不可用'); await expect(page.getByLabel('继续提问')).toHaveValue('最近的失败原因'); await page.getByRole('button', { name: '发送问题', exact: true }).click(); await expect(page.getByTestId('qa-turn-1')).toContainText('排队中');
  const posts = data.calls.filter(call => call.path.endsWith('/turns')); expect(posts).toHaveLength(2); expect(posts[0].body.request_key).toEqual(posts[1].body.request_key);
  Object.assign(data.conversations[0].turns[0], { status: 'failed', stage: 'failed', error: '引用未通过验证，请缩小查询范围。' }); await expect(page.getByTestId('qa-turn-1')).toContainText('引用未通过验证', { timeout: 6000 }); await page.getByRole('button', { name: '重试回答', exact: true }).click(); await expect(page.getByTestId('qa-turn-1')).toContainText('排队中');
  data.state.detailFailures = 1; await expect(page.getByRole('alert')).toContainText('对话刷新失败', { timeout: 6000 }); await page.getByRole('button', { name: '刷新对话', exact: true }).click(); await expect(page.getByTestId('qa-turn-1')).toContainText('排队中'); await page.getByRole('button', { name: '取消回答', exact: true }).click(); await expect(page.getByTestId('qa-turn-1')).toContainText('已取消');
});

test('mobile light and dark keep full controls and source records inside viewport', async ({ page }, testInfo) => {
  const data = await fixture(page); const turn = data.makeTurn(1, 1, '目录中的许可信息现在完整吗？'); data.complete(turn); data.conversations.push({ id: 1, title: turn.question, actor: 'reader', created_at: turn.created_at, updated_at: turn.created_at, turns: [turn] });
  await page.setViewportSize({ width: 390, height: 844 }); await go(page); await page.getByRole('complementary', { name: '问答历史' }).getByRole('button', { name: /目录中的许可信息/ }).click(); await page.getByRole('button', { name: '本地目录统计', exact: true }).click(); await page.getByText('展开本次保存的来源记录').click();
  await page.getByLabel('继续提问').fill('补充问题'); await expect(page.getByRole('button', { name: '发送问题', exact: true })).toBeEnabled(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('qa-mobile-light.png'), fullPage: true }); await page.getByRole('button', { name: '切换深色主题' }).click(); expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true); await page.evaluate(() => window.scrollTo(0, 0)); await page.screenshot({ animations: 'disabled', path: testInfo.outputPath('qa-mobile-dark.png'), fullPage: true });
});


test('IME composition never submits and rapid clicks submit only once; leaving an active conversation stops polling', async ({ page }) => {
  const data = await fixture(page); await go(page); const composer = page.getByLabel('你的问题'); await composer.fill('中文问题');
  await composer.dispatchEvent('keydown', { key: 'Enter', ctrlKey: true, isComposing: true }); expect(data.state.questionPosts).toBe(0);
  await page.getByRole('button', { name: '发送问题', exact: true }).evaluate(button => { (button as HTMLButtonElement).click(); (button as HTMLButtonElement).click(); });
  await expect(page.getByTestId('qa-turn-1')).toContainText('排队中'); expect(data.state.questionPosts).toBe(1);
  await page.getByRole('button', { name: '新建对话', exact: true }).click(); const reads = data.state.detailGets; await page.waitForTimeout(2400); expect(data.state.detailGets).toBe(reads);
});
