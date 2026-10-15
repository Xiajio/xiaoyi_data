import { test, expect, type Page, type FrameLocator } from '@playwright/test';
import { mkdir } from 'node:fs/promises';
import { createInitialState } from '../../src/domain/seed';
import { transition } from '../../src/domain/model';
import type { DemoState } from '../../src/domain/types';

const storageKey='xiaoyi-data-demo:v1:standalone';
const chartTitles=['多模态资料覆盖','资料处理分布','资料记载日期分布','研究字段核验'];
async function saved(page:Page):Promise<DemoState>{return page.evaluate(key=>JSON.parse(localStorage.getItem(key)!),storageKey);}
async function navigate(page:Page,label:string){
  const sidebar=page.locator('.sidebar');
  if(!await sidebar.isVisible())await page.getByLabel('切换导航').click();
  await sidebar.getByRole('button',{name:label,exact:true}).click();
  await expect(page.getByRole('heading',{name:label,exact:true})).toBeVisible();
}
async function dashboard(page:Page){
  await navigate(page,'工作概览');
  await page.getByRole('button',{name:'数据仪表台',exact:true}).click();
  await expect(page.getByTestId('research-dashboard')).toBeVisible();
}
async function metrics(page:Page|FrameLocator,patients:number,assets:number,fields:number){
  for(const [name,value]of Object.entries({patients,assets,fields}))await expect(page.getByTestId('dashboard-'+name)).toHaveText(String(value));
}
async function detailAssetIds(page:Page){
  const text=await page.getByTestId('dashboard-detail').innerText();
  return [...new Set(text.match(/\bF-\d{3}\b/g)??[])].sort();
}
async function advanceUntil(page:Page,condition:()=>Promise<boolean>){
  for(let tick=0;tick<16;tick++){if(await condition())return;await page.clock.runFor(1000);}
  expect(await condition()).toBe(true);
}
function watchErrors(page:Page){
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  page.on('console',message=>{if(message.type()==='error')errors.push(message.text());});
  return errors;
}
async function noOverflow(page:Page,width:number){
  const size=await page.evaluate(()=>({viewport:innerWidth,scroll:document.documentElement.scrollWidth}));
  expect(size.viewport).toBe(width);expect(size.scroll).toBeLessThanOrEqual(width+1);
}
async function screenshot(page:Page,name:string){
  await mkdir('output/playwright',{recursive:true});
  await page.screenshot({path:`output/playwright/${name}.png`,fullPage:true,animations:'disabled'});
}

test('default research dashboard filters real seeded records and drills into coverage, dates and field evidence',async({page})=>{
  const errors=watchErrors(page);await page.goto('/#/overview');
  await expect(page.getByRole('heading',{name:'工作概览',exact:true})).toBeVisible();
  await expect(page.getByTestId('research-dashboard')).toBeVisible();
  await metrics(page,4,12,6);
  for(const title of chartTitles)await expect(page.getByRole('heading',{name:title,exact:true})).toBeVisible();
  // Native keyboard activation must provide the same inspection path as a click.
  const coverage=page.getByTestId('coverage-P-DEMO-001-text');await coverage.focus();await coverage.press('Enter');
  await expect(page.getByTestId('dashboard-detail')).toBeVisible();
  expect(await detailAssetIds(page)).toEqual(['F-001','F-002']);
  await page.getByTestId('coverage-P-DEMO-004-image').click();
  await expect(page.getByTestId('dashboard-detail')).toContainText('该研究对象本批未提供此类资料');
  expect(await detailAssetIds(page)).toEqual([]);
  await page.getByTestId('dashboard-patient-filter').selectOption('P-DEMO-003');await metrics(page,1,2,1);
  await expect(page.getByTestId('dashboard-detail')).toHaveCount(0);
  await page.getByTestId('coverage-P-DEMO-003-text').click();expect(await detailAssetIds(page)).toEqual(['F-009']);
  await page.getByTestId('dashboard-patient-filter').selectOption('all');await metrics(page,4,12,6);
  await page.getByTestId('month-2026-03').click();
  expect(await detailAssetIds(page)).toEqual(['F-001','F-002','F-003','F-004','F-005']);
  await page.getByTestId('processing-text-pending').click();
  expect(await detailAssetIds(page)).toEqual(['F-001','F-002','F-006','F-009','F-011']);
  await page.getByTestId('field-R-02').click();
  const detail=page.getByTestId('dashboard-detail');await expect(detail).toContainText('原始候选');await expect(detail).toContainText('2026-03-03');
  await expect(detail).toContainText('原文依据');
  await detail.getByRole('button',{name:'前往核验 R-02',exact:true}).click();
  await expect(page.getByRole('heading',{name:'核验工作台',exact:true})).toBeVisible();
  await expect(page.getByTestId('review-field-R-02')).toHaveAttribute('aria-pressed','true');
  expect(errors).toEqual([]);
});

test('scan import, inventory processing and manual review update dashboard without inventing subjects or fields',async({page})=>{
  test.setTimeout(60000);const errors=watchErrors(page);
  let initial=createInitialState('standalone');initial=transition(initial,{type:'SET_ROLE',role:'manager'});initial=transition(initial,{type:'INTAKE'});
  await page.clock.install();await page.clock.pauseAt(new Date(Date.now()+60000));
  await page.addInitScript(({key,state})=>{if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify(state));},{key:storageKey,state:initial});
  await page.goto('/#/overview');await metrics(page,4,12,6);
  await navigate(page,'数据接入');await page.getByTestId('scan-directory').selectOption('inbox');await page.getByTestId('scan-recursive').check();
  await page.getByTestId('scan-start').click();await advanceUntil(page,async()=>(await saved(page)).scans[0]?.status==='completed');
  await page.getByTestId('scan-import').click();await expect.poll(async()=>(await saved(page)).assets.length).toBe(16);
  await dashboard(page);await metrics(page,4,16,6);
  await page.getByTestId('coverage-P-DEMO-004-image').click();expect(await detailAssetIds(page)).toEqual(['F-016']);
  await page.getByTestId('processing-text-pending').click();expect(await detailAssetIds(page)).toEqual(['F-001','F-002','F-006','F-009','F-011','F-013']);
  expect((await saved(page)).fields).toEqual(initial.fields);
  await navigate(page,'数据处理');await page.getByRole('button',{name:/创建处理任务/}).click();await page.getByRole('button',{name:'确认计划并执行',exact:true}).click();
  await advanceUntil(page,async()=>(await saved(page)).runs[0]?.status==='completed');
  await dashboard(page);await metrics(page,4,16,6);
  await expect(page.getByTestId('processing-text-pending')).toHaveCount(0);
  await page.getByTestId('processing-text-processed').click();expect(await detailAssetIds(page)).toEqual(['F-001','F-002','F-006','F-009','F-011','F-013']);
  await page.getByTestId('field-R-02').click();await page.getByTestId('dashboard-detail').getByRole('button',{name:'前往核验 R-02',exact:true}).click();
  await page.getByLabel('演示角色').selectOption('reviewer');
  const decisionTab=page.getByRole('button',{name:'核验与修订',exact:true});if(await decisionTab.isVisible())await decisionTab.click();
  await page.getByTestId('review-decision').selectOption('correct');await page.getByTestId('review-value').fill('2026-03-05');
  await page.getByTestId('review-note').fill('已核查合成报告：2026年03月05日为签发日期，03月03日为采集日期。');await page.getByTestId('review-submit').click();
  await expect(page.getByTestId('review-field-R-02')).toContainText('已修正');
  await dashboard(page);await metrics(page,4,16,6);await page.getByTestId('field-R-02').click();
  const detail=page.getByTestId('dashboard-detail');await expect(detail).toContainText('2026-03-05');await expect(detail).toContainText('2026-03-03');await expect(detail).toContainText('已修正');
  expect((await saved(page)).fields).toHaveLength(6);expect((await saved(page)).patients).toHaveLength(4);expect(errors).toEqual([]);
});

for(const width of [520,1050,1600]){
  test(`dashboard charts, filtering and details are keyboard usable in light and dark at ${width}px`,async({page})=>{
    const errors=watchErrors(page);await page.setViewportSize({width,height:1000});await page.goto('/#/overview');
    await expect(page.getByTestId('research-dashboard')).toBeVisible();await metrics(page,4,12,6);
    await noOverflow(page,width);await screenshot(page,`dashboard-${width}-light`);
    const coverage=page.getByTestId('coverage-P-DEMO-001-text');await coverage.focus();await coverage.press('Enter');
    expect(await detailAssetIds(page)).toEqual(['F-001','F-002']);await noOverflow(page,width);
    await page.getByLabel('切换深色主题',{exact:true}).click();await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
    await page.getByTestId('dashboard-patient-filter').selectOption('P-DEMO-003');await metrics(page,1,2,1);
    await page.getByTestId('coverage-P-DEMO-003-3d').click();expect(await detailAssetIds(page)).toEqual(['F-010']);
    await noOverflow(page,width);await screenshot(page,`dashboard-${width}-dark`);
    await page.getByTestId('dashboard-patient-filter').selectOption('all');await page.getByTestId('coverage-P-DEMO-004-image').click();
    await expect(page.getByTestId('dashboard-detail')).toContainText('该研究对象本批未提供此类资料');await noOverflow(page,width);
    await page.getByRole('button',{name:'处理与运行',exact:true}).click();await expect(page.getByTestId('inventory-total')).toHaveText('12');
    await page.getByRole('button',{name:'数据仪表台',exact:true}).click();await expect(page.getByTestId('research-dashboard')).toBeVisible();
    await metrics(page,4,12,6);expect(errors).toEqual([]);
  });
}


test('iframe reload and A/B session switches clear local dashboard filters while preserving each session inventory',async({page})=>{
  const errors=watchErrors(page);
  let state=createInitialState('demo-host-001');state=transition(state,{type:'SET_ROLE',role:'manager'});
  state=transition(state,{type:'CONFIGURE_SCAN',config:{directoryId:'inbox',recursive:true}});
  state=transition(state,{type:'START_SCAN'});
  for(let step=0;step<4;step++)state=transition(state,{type:'ADVANCE_SCAN',scanId:state.scans[0].id});
  state=transition(state,{type:'IMPORT_SCAN',scanId:state.scans[0].id});
  expect(state.assets).toHaveLength(16);
  await page.addInitScript(({key,state})=>{if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify(state));},{key:'xiaoyi-data-demo:v1:demo-host-001',state});
  await page.goto('/host.html');
  const frame=page.frameLocator('#frame-slot iframe');
  await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  await expect(frame.getByTestId('research-dashboard')).toBeVisible();await metrics(frame,4,16,6);
  await frame.getByTestId('dashboard-patient-filter').selectOption('P-DEMO-003');await metrics(frame,1,3,1);
  await page.locator('#toggle').click();await expect(page.locator('#frame-slot iframe')).toHaveCount(0);
  await page.locator('#toggle').click();await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  await expect(frame.getByTestId('dashboard-patient-filter')).toHaveValue('all');await metrics(frame,4,16,6);
  await frame.getByTestId('dashboard-patient-filter').selectOption('P-DEMO-003');await metrics(frame,1,3,1);
  await page.locator('#session').click();await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-002');
  await expect(frame.getByTestId('dashboard-patient-filter')).toHaveValue('all');await metrics(frame,4,12,6);
  await frame.getByTestId('dashboard-patient-filter').selectOption('P-DEMO-001');await metrics(frame,1,5,3);
  await page.locator('#previous-session').click();await expect(frame.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  await expect(frame.getByTestId('dashboard-patient-filter')).toHaveValue('all');await metrics(frame,4,16,6);
  expect(errors).toEqual([]);
});
