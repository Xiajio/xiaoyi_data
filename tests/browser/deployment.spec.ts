import { test, expect, type Page, type FrameLocator } from '@playwright/test';
import { createServer, type Server } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { resolve, extname, sep } from 'node:path';

const distRoot = resolve(process.cwd(),'dist');
const contentTypes:Record<string,string> = {'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.json':'application/json; charset=utf-8','.svg':'image/svg+xml','.png':'image/png','.woff':'font/woff','.woff2':'font/woff2','.ico':'image/x-icon'};
let server:Server;
let origin:string;

// Deliberately no SPA fallback or root asset alias: a bad absolute asset URL must fail.
test.beforeAll(async () => {
  await stat(resolve(distRoot,'index.html'));
  server = createServer(async (request,response) => {
    try {
      const pathname = decodeURIComponent(new URL(request.url ?? '/','http://localhost').pathname);
      if (pathname === '/favicon.ico') {response.writeHead(204);response.end();return;}
      if (!pathname.startsWith('/demo/')) {response.writeHead(404);response.end('Not found outside /demo/');return;}
      const relative = pathname.slice('/demo/'.length);
      const target = resolve(distRoot,relative || 'index.html');
      if (!target.startsWith(distRoot + sep)) {response.writeHead(403);response.end();return;}
      if (!(await stat(target)).isFile()) {response.writeHead(404);response.end();return;}
      response.writeHead(200,{'Content-Type':contentTypes[extname(target)] ?? 'application/octet-stream','Cache-Control':'no-store'});
      response.end(await readFile(target));
    } catch {response.writeHead(404);response.end('Not found');}
  });
  await new Promise<void>((done,reject) => {server.once('error',reject);server.listen(0,'127.0.0.1',done);});
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('临时静态服务未分配端口');
  origin = `http://127.0.0.1:${address.port}`;
});
test.afterAll(async () => {
  if (!server) return;
  server.closeAllConnections();
  await new Promise<void>((done,reject) => server.close(error => error ? reject(error) : done()));
});

function collectFailures(page:Page) {
  const failures:string[]=[];
  page.on('pageerror',error => failures.push(`pageerror: ${error.message}`));
  page.on('console',message => {if(message.type()==='error')failures.push(`console: ${message.text()}`);});
  page.on('response',response => {if(response.status()>=400)failures.push(`${response.status()} ${response.url()}`);});
  page.on('requestfailed',request => {if(request.failure()?.errorText !== 'net::ERR_ABORTED')failures.push(`${request.failure()?.errorText} ${request.url()}`);});
  return failures;
}
async function hostReady(page:Page) {
  await expect(page.frameLocator('#frame-slot iframe').getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  await expect(page.locator('#connection')).toContainText('状态：已就绪');
}
async function currentInstance(page:Page) {
  return (await page.locator('#connection').innerText()).match(/实例：(frame-[a-z0-9]+)/)?.[1] ?? '';
}
async function actualFrameWidth(page:Page) {
  return page.frameLocator('#frame-slot iframe').locator('html').evaluate(() => window.innerWidth);
}
async function reviewField(frame:FrameLocator,id:string,decision:string,value?:string) {
  const row = frame.getByTestId(`review-field-${id}`);
  if (!await row.isVisible()) await frame.getByRole('button',{name:/问题队列/}).click();
  await row.click();
  const decisionTab = frame.getByRole('button',{name:'核验与修订',exact:true});
  if (await decisionTab.isVisible()) await decisionTab.click();
  await frame.getByTestId('review-decision').selectOption(decision);
  if (value !== undefined) await frame.getByTestId('review-value').fill(value);
  await frame.getByTestId('review-note').fill(`已对照 ${id} 的合成原文；区分报告日期、计划状态与资料缺失，不改写来源。`);
  await frame.getByTestId('review-submit').click();
  await expect(row).toContainText(decision === 'confirm' ? '已确认' : decision === 'missing' ? '未记录' : '已修正');
}
async function noToolOverflow(frame:FrameLocator,width:number) {
  const dimensions = await frame.locator('html').evaluate(() => ({viewport:innerWidth,document:document.documentElement.scrollWidth}));
  expect(dimensions.viewport).toBe(width);
  expect(dimensions.document).toBeLessThanOrEqual(width + 1);
}

test('production assets work under /demo/ for standalone and host lifecycle without 404s or console errors',async ({page}) => {
  const failures=collectFailures(page);
  await page.goto(`${origin}/demo/`);
  await expect(page.getByTestId('demo-app')).toBeVisible();
  await expect(page.getByRole('heading',{name:'数据处理',exact:true})).toBeVisible();
  const standaloneLink=page.getByRole('link',{name:/查看宿主接入示例/});
  expect(await standaloneLink.evaluate(element => (element as HTMLAnchorElement).href)).toBe(`${origin}/demo/host.html`);
  await page.goto(`${origin}/demo/host.html`);await hostReady(page);
  const frame=page.frameLocator('#frame-slot iframe');
  expect(await page.locator('#frame-slot iframe').getAttribute('src')).toContain(`${origin}/demo/index.html?embed=1&hostOrigin=`);
  await expect(frame.getByRole('heading',{name:'工作概览',exact:true})).toBeVisible();
  await page.locator('#theme').selectOption('dark');await expect(frame.locator('html')).toHaveAttribute('data-theme','dark');
  await page.locator('#page').selectOption('cases');await page.locator('#navigate').click();
  await expect(frame.getByRole('heading',{name:'对象资料库',exact:true})).toBeVisible();
  const first=await currentInstance(page);expect(first).not.toBe('');
  await page.locator('#toggle').click();await expect(page.locator('#frame-slot iframe')).toHaveCount(0);
  await page.locator('#toggle').click();await hostReady(page);
  await expect.poll(() => currentInstance(page)).not.toBe(first);
  await expect(frame.locator('html')).toHaveAttribute('data-theme','dark');
  await page.locator('#field').click();await expect(frame.getByTestId('review-field-R-01')).toHaveAttribute('aria-pressed','true');
  const second=await currentInstance(page);
  await page.locator('#reopen').click();await hostReady(page);
  await expect.poll(() => currentInstance(page)).not.toBe(second);
  await expect(frame.getByRole('heading',{name:'核验工作台',exact:true})).toBeVisible();
  expect(failures).toEqual([]);
});

for (const width of [1600,1050,520]) {
  test(`full evidence review, freeze, approval and host download inside an actual ${width}px iframe`,async ({page}) => {
    const failures=collectFailures(page);
    // Keep the outer host large. The measured iframe viewport, not the browser viewport, is the test parameter.
    await page.setViewportSize({width:Math.max(width+400,1280),height:1100});
    await page.goto(`${origin}/demo/host.html`);await hostReady(page);
    await page.locator('#frame-slot').evaluate((element,frameWidth) => {element.style.width=`${frameWidth+2}px`;element.style.height='1050px';},width);
    await page.locator('#frame-slot iframe').evaluate((element,frameWidth) => {element.style.width=`${frameWidth}px`;element.style.height='1050px';},width);
    await expect.poll(() => actualFrameWidth(page)).toBe(width);
    const frame=page.frameLocator('#frame-slot iframe');
    await page.locator('#field').click();await expect(frame.getByRole('heading',{name:'核验工作台',exact:true})).toBeVisible();
    await noToolOverflow(frame,width);
    await reviewField(frame,'R-01','confirm');
    await reviewField(frame,'R-02','correct','2026-03-05');
    await reviewField(frame,'R-03','correct','计划');
    await reviewField(frame,'R-04','missing');
    await reviewField(frame,'R-05','confirm');
    await frame.getByRole('button',{name:'查看发布条件',exact:true}).click();
    await expect(frame.getByRole('heading',{name:'数据集与发布',exact:true})).toBeVisible();
    await noToolOverflow(frame,width);
    await frame.getByTestId('select-eligible').click();
    await expect(frame.getByLabel('选择字段 训练任务状态 R-03')).not.toBeChecked();
    await expect(frame.getByTestId('freeze-snapshot')).toBeEnabled();
    await frame.getByTestId('freeze-snapshot').click();
    await expect(frame.getByTestId(/^export-/).first()).toBeDisabled();
    await frame.getByLabel('演示角色').selectOption('lead');
    await frame.getByLabel('已核对本次演示快照的范围、来源与核验记录').check();
    await frame.getByTestId(/^approve-/).first().click();
    await expect(frame.getByTestId(/^export-/).first()).toBeEnabled();
    await frame.getByTestId(/^export-/).first().click();
    await expect(page.locator('#download-export')).toBeEnabled();
    const exportedEvent=JSON.parse(await page.locator('#export-output').inputValue());
    expect(exportedEvent.data.fieldIds).toEqual(['R-01','R-02','R-04','R-05','R-06']);
    const downloadPromise=page.waitForEvent('download');await page.locator('#download-export').click();
    const download=await downloadPromise;const file=await download.path();expect(file).toBeTruthy();
    const data=JSON.parse(await readFile(file!,'utf8'));
    expect(data).toEqual(exportedEvent.data);
    expect(data.fields.find((field:{id:string}) => field.id==='R-02').value).toBe('2026-03-05');
    expect(data.fields.find((field:{id:string}) => field.id==='R-03')).toBeUndefined();
    expect(data.fields.find((field:{id:string}) => field.id==='R-04').status).toBe('missing');
    expect(data.disclaimer).toContain('虚构');
    await noToolOverflow(frame,width);
    expect(failures).toEqual([]);
  });
}

test('switching a running iframe session and resetting a run revoke old timers',async ({page}) => {
  const failures=collectFailures(page);
  await page.clock.install();
  await page.clock.pauseAt(new Date(Date.now()+60000));
  await page.goto(`${origin}/demo/host.html`);await hostReady(page);
  const frame=page.frameLocator('#frame-slot iframe');
  const saved=(sessionId:string) => page.evaluate(id => JSON.parse(localStorage.getItem('xiaoyi-data-demo:v1:'+encodeURIComponent(id))!),sessionId);
  const hostNavigate=async (pageId:string) => {await page.locator('#page').selectOption(pageId);await page.locator('#navigate').click();};
  const startRun=async () => {
    await frame.getByRole('button',{name:/创建处理任务/}).click();
    await frame.getByRole('button',{name:'确认计划并执行',exact:true}).click();
    await expect.poll(async () => (await saved('demo-host-001')).runs[0]?.status).toBe('running');
  };
  await frame.getByLabel('演示角色').selectOption('manager');
  await hostNavigate('intake');
  await frame.getByRole('button',{name:'接入演示批次',exact:true}).click();
  await frame.getByRole('button',{name:'确认接入',exact:true}).click();
  await hostNavigate('processing');await startRun();
  const firstRunId=(await saved('demo-host-001')).runs[0].id;
  await page.locator('#session').click();
  await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-002');
  await expect(page.locator('#connection')).toContainText('状态：已就绪');
  const untouchedB=await saved('demo-host-002');
  expect(untouchedB.runs).toEqual([]);
  await page.clock.fastForward(15000);
  expect(await saved('demo-host-002')).toEqual(untouchedB);
  await page.locator('#previous-session').click();await hostReady(page);
  await expect.poll(async () => (await saved('demo-host-001')).runs.find((run:{id:string}) => run.id===firstRunId)?.status).toBe('interrupted');
  expect((await saved('demo-host-001')).runs.find((run:{id:string}) => run.id===firstRunId).step).toBe(0);
  await startRun();
  const nextRunId=(await saved('demo-host-001')).runs[0].id;expect(nextRunId).not.toBe(firstRunId);
  await page.locator('#reset').click();
  await expect.poll(async () => (await saved('demo-host-001'))?.runs).toEqual([]);
  const resetState=await saved('demo-host-001');
  expect(resetState.intakeDone).toBe(false);expect(resetState.snapshots).toEqual([]);expect(resetState.revision).toBe(1);
  await page.clock.fastForward(30000);
  expect(await saved('demo-host-001')).toEqual(resetState);
  await expect(frame.getByRole('heading',{name:'数据处理',exact:true})).toBeVisible();
  await expect(frame.getByRole('status').filter({hasText:'此演示任务已结束或不存在'})).toHaveCount(0);
  expect(failures).toEqual([]);
});
