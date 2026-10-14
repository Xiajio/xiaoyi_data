import { test, expect, type Page, type FrameLocator } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { createInitialState } from '../../src/domain/seed';
import { transition } from '../../src/domain/model';
import type { DemoState, AssetProcessingStatus, ScanRun } from '../../src/domain/types';

type Surface=Page|FrameLocator;
const key=(sessionId='standalone')=>'xiaoyi-data-demo:v1:'+encodeURIComponent(sessionId);
async function saved(page:Page,sessionId='standalone'):Promise<DemoState> {
  return page.evaluate(storageKey=>JSON.parse(localStorage.getItem(storageKey)!),key(sessionId));
}
function managerState(sessionId='standalone',withSnapshot=false) {
  let state=createInitialState(sessionId);
  if(withSnapshot){
    state=transition(state,{type:'SELECT_FIELDS',ids:['R-06']});
    state=transition(state,{type:'FREEZE'});
    state=transition(state,{type:'SET_ROLE',role:'lead'});
    state=transition(state,{type:'APPROVE',snapshotId:state.snapshots[0].id});
  }
  state=transition(state,{type:'SET_ROLE',role:'manager'});
  return transition(state,{type:'INTAKE'});
}
async function prepare(page:Page,state:DemoState) {
  await page.clock.install();await page.clock.pauseAt(new Date(Date.now()+60000));
  await page.addInitScript(({storageKey,state})=>{
    if(!localStorage.getItem(storageKey))localStorage.setItem(storageKey,JSON.stringify(state));
  },{storageKey:key(state.sessionId),state});
}
async function navigate(surface:Surface,label:string) {
  const sidebar=surface.locator('.sidebar');
  if(!await sidebar.isVisible())await surface.getByLabel('切换导航').click();
  await sidebar.getByRole('button',{name:label,exact:true}).click();
  await expect(surface.getByRole('heading',{name:label,exact:true})).toBeVisible();
  if(label==='工作概览')await surface.getByRole('button',{name:'处理与运行',exact:true}).click();
}
async function advanceUntil(page:Page,condition:()=>Promise<boolean>,description:string) {
  for(let step=0;step<16;step++){
    if(await condition())return;
    await page.clock.runFor(1000);
  }
  expect(await condition(),description).toBe(true);
}
async function finishScan(page:Page,sessionId='standalone') {
  await advanceUntil(page,async()=>(await saved(page,sessionId)).scans[0]?.status==='completed','模拟目录扫描应完成');
  return (await saved(page,sessionId)).scans[0];
}
async function startRun(page:Page,surface:Surface=page,sessionId='standalone') {
  await navigate(surface,'数据处理');
  await surface.getByRole('button',{name:/创建处理任务/}).click();
  await surface.getByRole('button',{name:'确认计划并执行',exact:true}).click();
  await expect.poll(async()=>(await saved(page,sessionId)).runs[0]?.status).toBe('running');
}
async function finishRun(page:Page,sessionId='standalone') {
  await advanceUntil(page,async()=>(await saved(page,sessionId)).runs[0]?.status==='completed','模拟资料处理应完成');
}
async function counts(surface:Surface,expected:Partial<Record<'total'|AssetProcessingStatus,number>>) {
  for(const [name,value] of Object.entries(expected))await expect(surface.getByTestId(`inventory-${name}`).first()).toHaveText(String(value));
}
function scanClasses(scan:ScanRun) {
  return Object.fromEntries(['new','duplicate','unsupported','unreadable'].map(status=>[status,scan.entries.filter(entry=>entry.status===status).length]));
}
function watchErrors(page:Page) {
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
  return errors;
}
async function capture(page:Page,name:string) {
  await mkdir('output/playwright',{recursive:true});
  await page.screenshot({path:'output/playwright/'+name+'.png',fullPage:true,animations:'disabled'});
}
async function noOverflow(surface:Surface,width:number) {
  const dimensions=await surface.locator('html').evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth}));
  expect(dimensions.viewport).toBe(width);expect(dimensions.scroll).toBeLessThanOrEqual(width+1);
}

test('manual scan classifies entries, imports only new assets, processes inventory and deduplicates repeat scans',async({page})=>{
  test.setTimeout(60000);const errors=watchErrors(page);
  const initial=managerState('standalone',true);await prepare(page,initial);await page.goto('/');
  await navigate(page,'工作概览');await counts(page,{total:12,pending:10,processing:0,processed:0,failed:0,restricted:2});await capture(page,'inventory-1440');
  await navigate(page,'数据处理');await counts(page,{total:12,pending:10,processing:0,processed:0,failed:0,restricted:2});
  await startRun(page);await counts(page,{total:12,pending:0,processing:10,processed:0,restricted:2});
  await finishRun(page);await counts(page,{total:12,pending:0,processing:0,processed:10,failed:0,restricted:2});
  expect((await saved(page)).runs[0].assetIds).toHaveLength(10);
  await navigate(page,'数据接入');
  await page.getByTestId('scan-directory').selectOption('inbox');await page.getByTestId('scan-recursive').check();
  await page.getByTestId('scan-start').click();await expect(page.getByTestId('scan-progress')).toBeVisible();
  const firstScan=await finishScan(page);await capture(page,'scan-1440');
  expect(firstScan.entries).toHaveLength(7);expect(scanClasses(firstScan)).toEqual({new:4,duplicate:1,unsupported:1,unreadable:1});
  for(const [status,count] of Object.entries({new:4,duplicate:1,unsupported:1,unreadable:1}))await expect(page.getByTestId('scan-result-'+status)).toHaveText(String(count));
  expect((await saved(page)).assets).toHaveLength(12);
  await expect(page.getByTestId('scan-import')).toBeEnabled();await page.getByTestId('scan-import').click();
  await expect.poll(async()=>(await saved(page)).assets.length).toBe(16);
  const imported=await saved(page);expect(imported.scans[0].importedAssetIds).toHaveLength(4);
  expect(imported.assets.filter(asset=>!initial.assets.some(original=>original.id===asset.id)).map(asset=>asset.id).sort()).toEqual(['F-013','F-014','F-015','F-016']);
  expect(imported.fields).toEqual(initial.fields);expect(imported.snapshots).toEqual(initial.snapshots);
  await navigate(page,'数据处理');await counts(page,{total:16,pending:4,processing:0,processed:10,failed:0,restricted:2});
  await page.getByTestId('asset-processing-filter').selectOption('pending');
  await expect(page.locator('.pp-table tbody tr')).toHaveCount(4);
  await page.getByTestId('asset-processing-filter').selectOption('restricted');
  await expect(page.locator('.pp-table tbody tr')).toHaveCount(2);
  await page.getByTestId('asset-processing-filter').selectOption('all');
  await startRun(page);expect((await saved(page)).runs[0].assetIds).toHaveLength(4);
  await finishRun(page);await counts(page,{total:16,pending:0,processing:0,processed:14,failed:0,restricted:2});
  await navigate(page,'数据接入');await page.getByTestId('scan-start').click();
  const secondScan=await finishScan(page);
  expect(secondScan.id).not.toBe(firstScan.id);expect(scanClasses(secondScan)).toEqual({new:0,duplicate:5,unsupported:1,unreadable:1});
  await expect(page.getByTestId('scan-result-new')).toHaveText('0');await expect(page.getByTestId('scan-result-duplicate')).toHaveText('5');
  await expect(page.getByTestId('scan-import')).toBeDisabled();
  const final=await saved(page);expect(final.assets).toHaveLength(16);expect(new Set(final.assets.map(asset=>asset.id)).size).toBe(16);
  expect(final.fields).toEqual(initial.fields);expect(final.snapshots).toEqual(initial.snapshots);expect(errors).toEqual([]);
});

test('automatic scans wait for the configured interval and stop scheduling when disabled',async({page})=>{
  test.setTimeout(60000);const errors=watchErrors(page);
  await prepare(page,managerState());await page.goto('/');await navigate(page,'数据接入');
  await page.getByTestId('scan-directory').selectOption('inbox');await page.getByTestId('scan-recursive').check();
  await page.getByTestId('scan-interval').selectOption('15');await page.getByTestId('scan-auto').check();
  await expect.poll(async()=>(await saved(page)).scanConfig.autoEnabled).toBe(true);
  expect((await saved(page)).scanConfig.nextScanAt).toBeTruthy();
  await page.clock.runFor(14000);expect((await saved(page)).scans).toHaveLength(0);
  await page.clock.runFor(1500);
  await expect.poll(async()=>(await saved(page)).scans[0]?.trigger).toBe('automatic');
  await finishScan(page);
  expect((await saved(page)).assets).toHaveLength(12);
  await page.getByTestId('scan-auto').uncheck();
  await expect.poll(async()=>(await saved(page)).scanConfig.autoEnabled).toBe(false);
  const scanCount=(await saved(page)).scans.length;
  await page.clock.runFor(65000);expect((await saved(page)).scans).toHaveLength(scanCount);
  await page.getByTestId('scan-directory').selectOption('archive');await page.getByTestId('scan-recursive').uncheck();
  await page.getByTestId('scan-interval').selectOption('30');
  await expect.poll(async()=>(await saved(page)).scanConfig.intervalSeconds).toBe(30);
  await page.getByTestId('scan-interval').selectOption('60');
  expect((await saved(page)).scanConfig).toMatchObject({directoryId:'archive',recursive:false,intervalSeconds:60,autoEnabled:false});
  expect(errors).toEqual([]);
});

test('refresh interrupts a running scan and requires explicit re-enabling of auto scanning',async({page})=>{
  const errors=watchErrors(page);await prepare(page,managerState());await page.goto('/');await navigate(page,'数据接入');
  await page.getByTestId('scan-interval').selectOption('15');await page.getByTestId('scan-auto').check();
  await page.clock.runFor(15100);await expect.poll(async()=>(await saved(page)).scans[0]?.status).toBe('running');
  const scanId=(await saved(page)).scans[0].id;
  await page.reload();await expect(page.getByTestId('demo-app')).toBeVisible();
  await expect.poll(async()=>(await saved(page)).scans.find(scan=>scan.id===scanId)?.status).toBe('interrupted');
  expect((await saved(page)).scanConfig.autoEnabled).toBe(false);
  await expect(page.getByTestId('scan-auto')).not.toBeChecked();
  const count=(await saved(page)).scans.length;
  await page.clock.runFor(65000);expect((await saved(page)).scans).toHaveLength(count);
  expect((await saved(page)).assets).toHaveLength(12);expect(errors).toEqual([]);
});

test('stopping a manual scan prevents late progress or import',async({page})=>{
  const errors=watchErrors(page);await prepare(page,managerState());await page.goto('/');await navigate(page,'数据接入');
  await page.getByTestId('scan-start').click();await expect.poll(async()=>(await saved(page)).scans[0]?.status).toBe('running');
  await page.getByTestId('scan-stop').click();await expect.poll(async()=>(await saved(page)).scans[0]?.status).toBe('interrupted');
  const stopped=structuredClone((await saved(page)).scans[0]);
  await page.clock.runFor(10000);expect((await saved(page)).scans[0]).toEqual(stopped);
  await expect(page.getByTestId('scan-import')).toHaveCount(0);expect((await saved(page)).assets).toHaveLength(12);expect(errors).toEqual([]);
});

test('host session switch and reset revoke old scan callbacks without writing into another session',async({page})=>{
  test.setTimeout(60000);const errors=watchErrors(page);await prepare(page,managerState('demo-host-001'));await page.goto('/host.html');
  const frame=page.frameLocator('#frame-slot iframe');
  await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  await navigate(frame,'数据接入');await frame.getByTestId('scan-auto').check();await frame.getByTestId('scan-start').click();
  await expect.poll(async()=>(await saved(page,'demo-host-001')).scans[0]?.status).toBe('running');
  const scanId=(await saved(page,'demo-host-001')).scans[0].id;
  await page.locator('#session').click();await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-002');
  const initialB=await saved(page,'demo-host-002');expect(initialB.scans).toEqual([]);
  await page.clock.runFor(65000);expect(await saved(page,'demo-host-002')).toEqual(initialB);
  await page.locator('#previous-session').click();await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  expect((await saved(page,'demo-host-001')).scans.find(scan=>scan.id===scanId)?.status).toBe('interrupted');
  expect((await saved(page,'demo-host-001')).scanConfig.autoEnabled).toBe(false);
  await navigate(frame,'数据接入');await frame.getByTestId('scan-start').click();
  await expect.poll(async()=>(await saved(page,'demo-host-001')).scans[0]?.status).toBe('running');
  await page.locator('#reset').click();await expect.poll(async()=>(await saved(page,'demo-host-001')).scans).toEqual([]);
  const resetState=await saved(page,'demo-host-001');await page.clock.runFor(65000);
  expect(await saved(page,'demo-host-001')).toEqual(resetState);expect(resetState.assets).toHaveLength(12);expect(resetState.scanConfig.autoEnabled).toBe(false);
  expect(errors).toEqual([]);
});

for(const width of [520,1050]){
  test(`scan controls and whole-inventory overview remain usable at ${width}px without page overflow`,async({page})=>{
    test.setTimeout(60000);const errors=watchErrors(page);await page.setViewportSize({width,height:1000});
    await prepare(page,managerState());await page.goto('/');await navigate(page,'工作概览');
    await counts(page,{total:12,pending:10,processed:0,processing:0,failed:0,restricted:2});await noOverflow(page,width);await capture(page,'inventory-'+width);
    await navigate(page,'数据接入');await noOverflow(page,width);
    await page.getByTestId('scan-directory').selectOption('archive');await page.getByTestId('scan-recursive').uncheck();await page.getByTestId('scan-interval').selectOption('60');
    await page.getByTestId('scan-directory').selectOption('inbox');await page.getByTestId('scan-recursive').check();
    await page.getByTestId('scan-start').click();await finishScan(page);await noOverflow(page,width);await capture(page,'scan-'+width);
    await page.getByTestId('scan-import').click();await expect.poll(async()=>(await saved(page)).assets.length).toBe(16);
    await navigate(page,'工作概览');await counts(page,{total:16,pending:14,processed:0,processing:0,failed:0,restricted:2});await noOverflow(page,width);
    await navigate(page,'数据处理');await page.getByTestId('asset-processing-filter').selectOption('pending');
    await noOverflow(page,width);expect(errors).toEqual([]);
  });
}
