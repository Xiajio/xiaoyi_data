import { test, expect, type Page } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { createInitialState } from '../../src/domain/seed';
import { transition } from '../../src/domain/model';
import type { DemoState } from '../../src/domain/types';

const protocol = 'xiaoyi.data-demo/v1';
const storageKey = (id:string) => 'xiaoyi-data-demo:v1:' + encodeURIComponent(id);
type BridgeEnvelope = {protocol:string;version:number;type:string;instanceId:string;sessionId?:string;seq?:number;payload:Record<string,unknown>};
type ObservedWindow = Window & {__hostTestMessages?:BridgeEnvelope[]};
async function instrument(page:Page) {
  await page.addInitScript(() => {
    const scope = window as ObservedWindow;scope.__hostTestMessages = [];
    window.addEventListener('message',event => {
      if (event.data?.protocol === 'xiaoyi.data-demo/v1') scope.__hostTestMessages!.push(event.data);
    });
  });
}
async function ready(page:Page,session='demo-host-001') {
  await expect(page.frameLocator('#frame-slot iframe').getByTestId('demo-app')).toHaveAttribute('data-session',session);
  await expect(page.locator('#connection')).toContainText('状态：已就绪');
}
async function instance(page:Page):Promise<string> {
  return page.evaluate(() => (window as ObservedWindow).__hostTestMessages!.filter(event => event.type === 'ready').at(-1)!.instanceId);
}
async function navigate(page:Page,value:string) {
  await page.locator('#page').selectOption(value);await page.locator('#navigate').click();
}
async function confirmFirstField(page:Page) {
  await page.locator('#field').click();
  const frame = page.frameLocator('#frame-slot iframe');
  await expect(frame.getByRole('heading',{name:'核验工作台',exact:true})).toBeVisible();
  const decisionTab = frame.getByRole('button',{name:'核验与修订',exact:true});
  if (await decisionTab.isVisible()) await decisionTab.click();
  await frame.getByTestId('review-decision').selectOption('confirm');
  await frame.getByTestId('review-submit').click();
  await expect(frame.getByTestId('review-field-R-01')).toContainText('已确认');
}

test('host controls theme, deep links, saved A→B→A sessions, reopen, reset and close requests',async ({page}) => {
  await instrument(page);await page.goto('/host.html');await ready(page);
  const frame = page.frameLocator('#frame-slot iframe');
  await expect(frame.getByRole('heading',{name:'工作概览',exact:true})).toBeVisible();
  await page.locator('#theme').selectOption('dark');await expect(frame.locator('html')).toHaveAttribute('data-theme','dark');
  await navigate(page,'cases');await expect(frame.getByRole('heading',{name:'病例资料库',exact:true})).toBeVisible();
  await confirmFirstField(page);
  const firstInstance = await instance(page);
  await page.locator('#session').click();await ready(page,'demo-host-002');
  await page.locator('#field').click();await expect(frame.getByTestId('review-field-R-01')).toContainText('待核验');
  await expect(page.locator('#download-export')).toBeDisabled();
  await page.locator('#previous-session').click();await ready(page);
  await page.locator('#field').click();await expect(frame.getByTestId('review-field-R-01')).toContainText('已确认');
  await page.locator('#reopen').click();await ready(page);
  await expect.poll(() => instance(page)).not.toBe(firstInstance);
  await page.locator('#field').click();await expect(frame.getByTestId('review-field-R-01')).toContainText('已确认');
  await page.locator('#reset').click();
  await expect(frame.getByRole('heading',{name:'数据处理',exact:true})).toBeVisible();
  await page.locator('#field').click();await expect(frame.getByTestId('review-field-R-01')).toContainText('待核验');
  await frame.getByRole('button',{name:'关闭工具',exact:true}).click();
  await expect(page.locator('#frame-slot iframe')).toHaveCount(0);
  await expect(page.locator('#events')).toContainText('closeRequested');
  await page.locator('#toggle').click();await ready(page);
  await page.locator('#toggle').click();await expect(page.locator('#frame-slot iframe')).toHaveCount(0);
  await page.locator('#toggle').click();await ready(page);
});

test('real message transport rejects sibling sources, cross-origin senders, malformed commands and retired instances',async ({page}) => {
  await instrument(page);await page.goto('/host.html');await ready(page);
  const frameLocator = page.frameLocator('#frame-slot iframe');
  const oldInstance = await instance(page);
  await page.locator('#reopen').click();await ready(page);
  await expect.poll(() => instance(page)).not.toBe(oldInstance);
  const currentInstance = await instance(page);
  const envelope:BridgeEnvelope = {protocol,version:1,type:'navigate',instanceId:currentInstance,sessionId:'demo-host-001',seq:9000,payload:{page:'cases'}};
  // Actual postMessage from the child itself has the correct origin but the wrong source.
  const frame = page.frames().find(item => /embed=1/.test(item.url()))!;
  await frame.evaluate(data => window.postMessage(data,location.origin),envelope);
  // A distinct-origin sibling delivers an actual browser-generated MessageEvent.
  await page.evaluate(() => {
    const attacker = document.createElement('iframe');attacker.id='foreign-sender';attacker.name='foreign-sender';attacker.hidden=true;
    attacker.src='http://localhost:4173/index.html';document.body.append(attacker);
  });
  await expect.poll(() => page.frames().some(item => item.url().startsWith('http://localhost:4173'))).toBe(true);
  const attacker = page.frames().find(item => item.url().startsWith('http://localhost:4173'))!;
  await attacker.evaluate(data => window.parent.frames[0].postMessage(data,'http://127.0.0.1:4173'),envelope);
  // Exact parent, but malformed envelope, foreign session, and retired instance.
  await page.evaluate(({base,oldInstance}) => {
    const target = document.querySelector<HTMLIFrameElement>('#frame-slot iframe')!.contentWindow!;
    for (const data of [
      {...base,version:2},
      {...base,sessionId:'session-uninitialized'},
      {...base,payload:{page:'review',fieldId:'REAL-PATIENT-FIELD'}},
      {...base,payload:{page:'cases',records:[{name:'not-allowed'}]}},
      {...base,type:'init',instanceId:oldInstance,sessionId:'retired-session',payload:{initialPage:'cases'}},
      {...base,seq:0},
    ]) target.postMessage(data,location.origin);
    target.postMessage({protocol:'xiaoyi.data-demo/v1',version:1,type:'discover',challenge:'e2e-barrier'},location.origin);
  },{base:envelope,oldInstance});
  await expect.poll(() => page.evaluate(() => (window as ObservedWindow).__hostTestMessages!.some(event => event.type==='bootstrap' && event.payload.challenge==='e2e-barrier'))).toBe(true);
  await expect(frameLocator.getByTestId('demo-app')).toHaveAttribute('data-session','demo-host-001');
  await expect(frameLocator.getByRole('heading',{name:'工作概览',exact:true})).toBeVisible();
  // If invalid seq=9000 advanced the counter, this legitimate low-seq command would fail.
  await navigate(page,'audit');await expect(frameLocator.getByRole('heading',{name:'运行与审计',exact:true})).toBeVisible();
});

test('an approved synthetic snapshot reaches the host, downloads as JSON, and does not leak into a new session',async ({page}) => {
  let state:DemoState=createInitialState('demo-host-001');
  state=transition(state,{type:'SELECT_FIELDS',ids:['R-06']});
  state=transition(state,{type:'FREEZE'});
  state=transition(state,{type:'SET_ROLE',role:'lead'});
  state=transition(state,{type:'APPROVE',snapshotId:state.snapshots[0].id});
  const snapshotId=state.snapshots[0].id;
  await page.addInitScript(({key,state}) => {if(window === window.top)localStorage.setItem(key,JSON.stringify(state));},{key:storageKey(state.sessionId),state});
  await page.goto('/host.html');await ready(page);
  await expect(page.locator('#download-export')).toBeDisabled();
  await navigate(page,'publish');
  const frame=page.frameLocator('#frame-slot iframe');
  await expect(frame.getByTestId(`export-${snapshotId}`)).toBeEnabled();
  await frame.getByTestId(`export-${snapshotId}`).click();
  await expect(page.locator('#download-export')).toBeEnabled();
  await expect(page.locator('#export-output')).toHaveValue(/synthetic_field_subset/);
  const downloadPromise=page.waitForEvent('download');await page.locator('#download-export').click();const download=await downloadPromise;
  expect(download.suggestedFilename()).toBe('xiaoyi-demo-demo-host-001.json');
  const path=await download.path();expect(path).toBeTruthy();
  const exported=JSON.parse(await readFile(path!,'utf8'));
  expect(exported.snapshotId).toBe(snapshotId);expect(exported.type).toBe('synthetic_field_subset');expect(exported.fieldIds).toEqual(['R-06']);expect(exported.disclaimer).toContain('虚构');
  await page.locator('#session').click();await ready(page,'demo-host-002');
  await expect(page.locator('#download-export')).toBeDisabled();await expect(page.locator('#export-output')).toHaveValue('');
  await navigate(page,'publish');await expect(frame.getByText('还没有数据版本',{exact:true})).toBeVisible();
  await page.locator('#previous-session').click();await ready(page);
  await navigate(page,'publish');await expect(frame.getByTestId(`export-${snapshotId}`)).toBeEnabled();
  await expect(page.locator('#download-export')).toBeDisabled();
});
