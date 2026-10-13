import { expect, test, type Page } from '@playwright/test';
import { createInitialState } from '../../src/domain/seed';
import { transition } from '../../src/domain/model';
import { mkdir } from 'node:fs/promises';

const key='xiaoyi-data-demo:v1:standalone';
async function navigate(page:Page,label:string){await page.locator('.sidebar').getByRole('button',{name:label,exact:true}).click();}
async function review(page:Page,id:string,decision:string,value?:string){
  const queue=page.getByTestId(`review-field-${id}`);
  if(!await queue.isVisible())await page.getByRole('button',{name:/问题队列/}).click();
  await queue.click();
  await page.getByTestId('review-decision').selectOption(decision);
  if(value!==undefined)await page.getByTestId('review-value').fill(value);
  await page.getByTestId('review-note').fill('已对照本批合成原文及对应日期，保留核验依据。');
  await page.getByTestId('review-submit').click();
  await expect(page.getByRole('status').filter({hasText:'核验结果已记录'})).toBeVisible();
}
async function saved(page:Page){return page.evaluate(k=>JSON.parse(localStorage.getItem(k)!),key);}

test('complete intake, simulated execution, evidence review, frozen version and export',async({page})=>{
  const errors:string[]=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto('/');
  await page.getByLabel('演示角色').selectOption('manager');
  await navigate(page,'数据接入');
  await page.getByRole('button',{name:'接入演示批次',exact:true}).click();
  await page.getByRole('button',{name:'确认接入',exact:true}).click();
  await navigate(page,'数据处理');
  await page.getByRole('button',{name:/创建处理任务/}).click();
  await page.getByRole('button',{name:'确认计划并执行',exact:true}).click();
  await expect.poll(async()=>(await saved(page)).runs[0]?.status,{timeout:12000}).toBe('completed');
  await page.getByLabel('演示角色').selectOption('reviewer');
  await navigate(page,'核验工作台');
  await review(page,'R-01','confirm');
  await review(page,'R-02','correct','2026-03-05');
  await review(page,'R-03','correct','计划');
  await review(page,'R-04','missing');
  await review(page,'R-05','confirm');
  await navigate(page,'数据集与发布');
  await page.getByTestId('select-eligible').click();
  await expect.poll(async()=>(await saved(page)).selectedFieldIds).not.toContain('R-03');
  await page.getByTestId('freeze-snapshot').click();
  await page.getByLabel('演示角色').selectOption('lead');
  await page.getByLabel('已核对本次演示快照的范围、来源与核验记录').check();
  await page.getByTestId(/^approve-/).first().click();
  const approved=(await saved(page)).snapshots[0];
  const downloadPromise=page.waitForEvent('download');
  await page.getByTestId(/^export-/).first().click();
  const download=await downloadPromise;const stream=await download.createReadStream();let text='';for await(const chunk of stream!)text+=chunk.toString();
  const output=JSON.parse(text);
  expect(output.snapshotId).toBe(approved.id);expect(output.fields).toHaveLength(5);
  expect(output.fields.find((field:{id:string})=>field.id==='R-02').value).toBe('2026-03-05');
  expect(output.fields.find((field:{id:string})=>field.id==='R-04').status).toBe('missing');
  expect(output.disclaimer).toContain('虚构');
  await page.reload();await expect(page.getByTestId(/^export-/).first()).toBeVisible();
  expect((await saved(page)).snapshots[0]).toEqual(approved);
  await page.getByLabel('演示角色').selectOption('reviewer');
  await navigate(page,'核验工作台');await review(page,'R-01','correct','乙状结肠（原文复核）');
  expect((await saved(page)).snapshots[0]).toEqual(approved);
  await navigate(page,'数据集与发布');
  await page.getByTestId('freeze-snapshot').click();
  await navigate(page,'核验工作台');await review(page,'R-02','needs-info');
  expect((await saved(page)).snapshots[0].status).toBe('invalidated');
  expect((await saved(page)).snapshots[1]).toEqual(approved);
  await page.getByRole('button',{name:'重置演示',exact:true}).click();
  await page.getByRole('button',{name:'确认重置',exact:true}).click();
  await expect.poll(async()=>(await saved(page)).snapshots).toEqual([]);
  expect((await saved(page)).page).toBe('processing');
  expect(errors).toEqual([]);
});

test('failed task retries and refresh interrupts work without overwriting reviews',async({page})=>{
  let state=createInitialState('standalone');state=transition(state,{type:'REVIEW',fieldId:'R-01',decision:'confirm'});
  state=transition(state,{type:'SET_ROLE',role:'manager'});state=transition(state,{type:'INTAKE'});
  await page.addInitScript(({key,state})=>{if(!localStorage.getItem(key))localStorage.setItem(key,JSON.stringify(state));},{key,state});
  await page.goto('/');await page.getByRole('button',{name:/创建处理任务/}).click();
  await page.getByLabel('模拟一次整理失败，演示重试').check();
  await page.getByRole('button',{name:'确认计划并执行',exact:true}).click();
  await expect.poll(async()=>(await saved(page)).runs[0]?.status).toBe('failed');
  await page.getByRole('button',{name:'重试',exact:true}).click();
  await expect.poll(async()=>(await saved(page)).runs[0]?.status).toBe('running');
  await page.reload();
  await expect.poll(async()=>(await saved(page)).runs[0]?.status).toBe('interrupted');
  expect((await saved(page)).fields[0].status).toBe('confirmed');
  await page.locator('.pp-tabs').getByRole('button',{name:/^执行记录/}).click();
  await page.getByRole('button',{name:'重试',exact:true}).last().click();
  await expect.poll(async()=>(await saved(page)).runs[0]?.status,{timeout:12000}).toBe('completed');
  expect((await saved(page)).fields[0].status).toBe('confirmed');
});

test('corrupt cache stays untouched until explicit reset',async({page})=>{
  await page.addInitScript(k=>localStorage.setItem(k,'{broken'),key);
  await page.goto('/');await expect(page.getByRole('alert')).toContainText('已损坏');
  await page.getByLabel('演示角色').selectOption('manager');
  expect(await page.evaluate(k=>localStorage.getItem(k),key)).toBe('{broken');
  await page.getByRole('button',{name:'重置此演示会话'}).click();
  await page.getByRole('button',{name:'确认重置'}).click();
  await expect(page.getByRole('alert')).toHaveCount(0);
  expect((await saved(page)).schemaVersion).toBe(1);
});

test('unavailable browser storage runs in memory with a clear notice',async({page})=>{
  await page.addInitScript(()=>Object.defineProperty(window,'localStorage',{get(){throw new DOMException('Storage denied','SecurityError');}}));
  await page.goto('/');await expect(page.getByRole('alert')).toContainText('存储不可用');
  await navigate(page,'核验工作台');await review(page,'R-01','confirm');
  await page.getByRole('button',{name:'重置演示',exact:true}).click();await page.getByRole('button',{name:'确认重置'}).click();
  await expect(page.getByRole('heading',{name:'数据处理',exact:true})).toBeVisible();
});

for(const viewport of [{width:1600,height:1000},{width:1050,height:900},{width:520,height:900}]){
 test(`seven pages and keyboard review at ${viewport.width}px`,async({page})=>{
  await page.setViewportSize(viewport);await page.goto('/');
  const requests:string[]=[];page.on('request',request=>{if(!request.url().startsWith('http://127.0.0.1:4173'))requests.push(request.url());});
  for(const label of ['工作概览','数据接入','数据处理','核验工作台','病例资料库','数据集与发布','运行与审计']){
    if(!await page.locator('.sidebar').isVisible())await page.getByLabel('切换导航').click();
    await navigate(page,label);await expect(page.getByRole('heading',{name:label,exact:true})).toBeVisible();
    const overflow=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,elements:[...document.querySelectorAll('*')].filter(el=>el.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(el=>({tag:el.tagName,cls:el.className,right:el.getBoundingClientRect().right}))}));
    expect(overflow.scroll,JSON.stringify({label,...overflow})).toBeLessThanOrEqual(viewport.width+1);
  }
  if(!await page.locator('.sidebar').isVisible())await page.getByLabel('切换导航').click();
  await navigate(page,'核验工作台');
  if(viewport.width<700)await page.getByRole('button',{name:'核验与修订',exact:true}).click();
  await page.getByTestId('review-decision').focus();await page.keyboard.press('Tab');
  expect(await page.evaluate(()=>document.activeElement?.tagName)).not.toBe('BODY');
  await page.getByLabel('切换深色主题').click();
  await expect(page.locator('html')).toHaveAttribute('data-theme','dark');
  await mkdir('output/playwright',{recursive:true});
  await page.screenshot({path:`output/playwright/review-${viewport.width}-dark.png`,fullPage:true,animations:'disabled'});
  await page.getByLabel('切换浅色主题').click();
  await page.screenshot({path:`output/playwright/review-${viewport.width}-light.png`,fullPage:true,animations:'disabled'});
  expect(requests).toEqual([]);
 });
}





test('conflicting source tables remain discoverable in issue filters and locate their evidence',async({page})=>{
  await page.goto('/');
  await page.getByLabel('资料类型').selectOption('table');
  await page.getByRole('button',{name:'仅看问题',exact:true}).click();
  await page.getByRole('button',{name:'科研登记表_001.csv',exact:true}).click();
  await expect(page.getByRole('heading',{name:'病理报告日期',exact:true})).toBeVisible();
  await expect(page.locator('.pp-evidence')).toContainText('Sheet1 / D2');
  await navigate(page,'数据处理');
  await page.getByLabel('资料类型').selectOption('table');
  await page.getByRole('button',{name:'仅看问题',exact:true}).click();
  await page.getByRole('button',{name:'随访登记表_004.xlsx',exact:true}).click();
  await expect(page.getByRole('heading',{name:'随访复发信息状态',exact:true})).toBeVisible();
  await expect(page.locator('.pp-evidence')).toContainText('Sheet1 / E2');
});
