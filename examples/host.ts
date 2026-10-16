import type { PageId, Theme } from '../src/domain/types';
interface HostEvent {type:string;sessionId:string;instanceId:string;payload:unknown}
interface HostClient {
 navigate(page:PageId,selection?:{fieldId?:string;patientId?:string}):boolean;
 setTheme(theme:Theme):boolean;reset():boolean;switchSession(sessionId:string):boolean;
 getState():{ready:boolean;sessionId:string;instanceId:string|null};dispose():void;
}
interface HostOptions {iframe:HTMLIFrameElement;frameOrigin:string;sessionId:string;initialPage:PageId;theme:Theme;onEvent(event:HostEvent):void}
const helperUrl = new URL('host-bridge.js',document.baseURI).href;
const {createDataDemoHost} = await import(/* @vite-ignore */ helperUrl) as {createDataDemoHost(options:HostOptions):HostClient};
const get = <T extends HTMLElement>(id:string) => document.getElementById(id) as T;
const slot = get<HTMLDivElement>('frame-slot');
const status = get<HTMLSpanElement>('status');
const toggle = get<HTMLButtonElement>('toggle');
const page = get<HTMLSelectElement>('page');
const theme = get<HTMLSelectElement>('theme');
const connection = get<HTMLDivElement>('connection');
const exportOutput = get<HTMLTextAreaElement>('export-output');
const downloadExport = get<HTMLButtonElement>('download-export');
let exportData:unknown = null;
function clearExport() {exportData = null;exportOutput.value = '';downloadExport.disabled = true;}
let bridge:HostClient|null = null;
let iframe:HTMLIFrameElement|null = null;
let sessionCounter = 1;
let sessionId = 'demo-host-001';
let previousSessionId:string|null = null;
function record(type:string,payload?:unknown) {
 const item = document.createElement('article'); item.className = 'event';
 const time = document.createElement('time'); time.textContent = new Date().toLocaleTimeString('zh-CN',{hour12:false});
 const title = document.createElement('b'); title.textContent = type;
 item.append(time,title);
 if (payload !== undefined) { const pre = document.createElement('pre'); pre.textContent = JSON.stringify(payload,null,2); item.append(pre); }
 const events = get<HTMLDivElement>('events'); events.prepend(item);
 while (events.children.length > 35) events.lastElementChild?.remove();
}
function updateConnection() {
 const state = bridge?.getState();
 connection.textContent = state ? `会话：${state.sessionId}\n实例：${state.instanceId ?? '等待握手'}\n状态：${state.ready ? '已就绪' : '等待工具确认'}` : `会话：${sessionId}\n工具已关闭`;
 connection.style.whiteSpace = 'pre-wrap';
}
function close() {
 bridge?.dispose();bridge = null;iframe?.remove();iframe = null;
 const empty = document.createElement('div');empty.className = 'empty';empty.textContent = '工具已关闭。点击“打开工具”重新建立独立实例。';slot.replaceChildren(empty);
 toggle.textContent = '打开工具';status.textContent = '已关闭';updateConnection();record('宿主关闭工具');
}
function mount() {
 bridge?.dispose();iframe?.remove();slot.replaceChildren();
 iframe = document.createElement('iframe');iframe.title = '小亿数据演示工作台';iframe.referrerPolicy = 'strict-origin';
 const url = new URL('index.html',document.baseURI);url.searchParams.set('mode','demo');url.searchParams.set('embed','1');url.searchParams.set('hostOrigin',location.origin);iframe.src = url.href;
 bridge = createDataDemoHost({iframe,frameOrigin:url.origin,sessionId,initialPage:page.value as PageId,theme:theme.value as Theme,onEvent(event) {
   record(event.type,event.payload);
   if (event.type === 'ready') status.textContent = '工具已就绪 · 仅合成演示数据';
   if (event.type === 'export') {
     exportOutput.value = JSON.stringify(event.payload,null,2);
     const payload = event.payload as {data?:unknown}|null;
     exportData = payload && typeof payload === 'object' && payload.data && typeof payload.data === 'object' ? payload.data : null;
     downloadExport.disabled = exportData === null;
   }
   if (event.type === 'error') status.textContent = '工具报告错误，请查看事件记录';
   updateConnection();
   if (event.type === 'closeRequested') close();
 }});
 slot.append(iframe);toggle.textContent = '关闭工具';status.textContent = '正在建立新实例…';updateConnection();record('宿主装载 iframe',{sessionId});
}
function result(sent:boolean|undefined,action:string) {if (!sent) {status.textContent = '请等待工具就绪后再试';return;}record(action);updateConnection();}
toggle.addEventListener('click',() => iframe ? close() : mount());
get('reopen').addEventListener('click',mount);
theme.addEventListener('change',() => result(bridge?.setTheme(theme.value as Theme),'宿主切换主题'));
get('navigate').addEventListener('click',() => result(bridge?.navigate(page.value as PageId),'宿主定位页面'));
get('field').addEventListener('click',() => {page.value = 'review';result(bridge?.navigate('review',{fieldId:'R-01',patientId:'P-DEMO-001'}),'宿主定位字段 R-01');});
get('session').addEventListener('click',() => {
 previousSessionId = sessionId;sessionId = 'demo-host-' + String(++sessionCounter).padStart(3,'0');clearExport();
 if (bridge) {bridge.switchSession(sessionId);status.textContent = '正在切换会话…';} else mount();
 updateConnection();record('宿主切换新会话',{sessionId});
});
get('previous-session').addEventListener('click',() => {
 if (!previousSessionId) {status.textContent = '请先切换一次新会话';return;}
 const next = previousSessionId;previousSessionId = sessionId;sessionId = next;clearExport();
 if (bridge) {bridge.switchSession(sessionId);status.textContent = '正在恢复上一会话…';} else mount();
 updateConnection();record('宿主恢复上一会话',{sessionId});
});
get('reset').addEventListener('click',() => {if (bridge?.reset()) {clearExport();record('宿主重置演示');} else status.textContent = '请先打开工具并等待就绪';});
downloadExport.addEventListener('click',() => {
 if (exportData === null) return;
 const blob = new Blob([JSON.stringify(exportData,null,2)],{type:'application/json;charset=utf-8'});
 const url = URL.createObjectURL(blob);const link = document.createElement('a');
 link.href = url;link.download = 'xiaoyi-demo-' + sessionId + '.json';document.body.append(link);link.click();link.remove();
 window.setTimeout(()=>URL.revokeObjectURL(url),1000);record('宿主下载演示 JSON',{sessionId});
});
get('clear-log').addEventListener('click',() => get('events').replaceChildren());
window.addEventListener('pagehide',() => bridge?.dispose(),{once:true});
mount();
