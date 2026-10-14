import { PAGES, type PageId, type Theme } from '../domain/types';

export const PROTOCOL = 'xiaoyi.data-demo/v1';
export const VERSION = 1;
export const SEED_FIELD_IDS = ['R-01','R-02','R-03','R-04','R-05','R-06'] as const;
export const SEED_PATIENT_IDS = ['P-DEMO-001','P-DEMO-002','P-DEMO-003','P-DEMO-004'] as const;
export interface EmbedConfig { embedded: boolean; hostOrigin: string | null; error: string | null }
export interface FrameCallbacks {
  onInit(value: {sessionId:string;initialPage?:PageId;theme?:Theme}):void;
  onNavigate(value: {page:PageId;fieldId?:string;patientId?:string}):void;
  onTheme(theme:Theme):void;
  onReset():void;
}
export type FrameEvent = 'ready'|'stateChanged'|'export'|'closeRequested'|'error';
export interface FrameBridge { emit(type:FrameEvent,payload:unknown):void;dispose():void }
type RecordValue = Record<string, unknown>;
const record = (value:unknown):value is RecordValue => !!value && typeof value === 'object' && !Array.isArray(value);
const keys = (value:RecordValue, allowed:readonly string[]) => Object.keys(value).every(key => allowed.includes(key));
export const validToken = (value:unknown):value is string => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/.test(value);
const page = (value:unknown):value is PageId => typeof value === 'string' && (PAGES as readonly string[]).includes(value);
const theme = (value:unknown):value is Theme => value === 'light' || value === 'dark' || value === 'blue';
export function exactOrigin(value:unknown):string|null {
  if (typeof value !== 'string' || value.trim() !== value) return null;
  try { const url = new URL(value); return ['http:','https:'].includes(url.protocol) && url.origin === value ? value : null; } catch { return null; }
}
export function resolveEmbedConfig(search:string, ownOrigin:string, inFrame:boolean, allowedOrigins:string|undefined):EmbedConfig {
  const params = new URLSearchParams(search);
  const embedded = params.get('embed') === '1';
  const fail = (error:string):EmbedConfig => ({embedded,hostOrigin:null,error});
  if (params.getAll('embed').length > 1 || params.getAll('hostOrigin').length > 1) return fail('嵌入参数重复，请检查宿主配置。');
  if (!embedded) return {embedded:false,hostOrigin:null,error:null};
  if (!inFrame) return fail('嵌入模式需要由宿主 iframe 打开。');
  const origin = exactOrigin(params.get('hostOrigin'));
  if (!origin) return fail('宿主来源须为完整且精确的 HTTP(S) origin。');
  const entries = allowedOrigins === undefined || allowedOrigins.trim() === '' ? [ownOrigin] : allowedOrigins.split(',').map(item => item.trim());
  if (entries.some(entry => !exactOrigin(entry))) return fail('宿主白名单配置无效。');
  if (!entries.includes(origin)) return fail('该宿主来源尚未列入允许名单。');
  return {embedded:true,hostOrigin:origin,error:null};
}
export function getEmbedConfig():EmbedConfig {
  return resolveEmbedConfig(window.location.search, window.location.origin, window.parent !== window, import.meta.env.VITE_DEMO_ALLOWED_ORIGINS);
}
export type FrameCommand =
  | {type:'discover';challenge:string}
  | {type:'init';sessionId:string;payload:{initialPage?:PageId;theme?:Theme}}
  | {type:'navigate';payload:{page:PageId;fieldId?:string;patientId?:string}}
  | {type:'theme';payload:{theme:Theme}}
  | {type:'reset';payload:Record<string,never>};
/** Validate the entire envelope before handing any value to application state. */
export function parseFrameCommand(data:unknown, instanceId:string, sessionId:string|null):FrameCommand|null {
  if (!record(data) || data.protocol !== PROTOCOL || data.version !== VERSION) return null;
  if (data.type === 'discover') {
    return keys(data,['protocol','version','type','challenge']) && validToken(data.challenge) ? {type:'discover',challenge:data.challenge} : null;
  }
  if (!keys(data,['protocol','version','type','instanceId','sessionId','seq','payload']) || data.instanceId !== instanceId || !validToken(data.sessionId) || !Number.isSafeInteger(data.seq) || (data.seq as number) < 1 || !record(data.payload)) return null;
  const payload = data.payload;
  if (data.type === 'init') {
    if (!keys(payload,['initialPage','theme']) || ('initialPage' in payload && !page(payload.initialPage)) || ('theme' in payload && !theme(payload.theme))) return null;
    return {type:'init',sessionId:data.sessionId,payload:payload as {initialPage?:PageId;theme?:Theme}};
  }
  if (!sessionId || data.sessionId !== sessionId) return null;
  if (data.type === 'navigate') {
    if (!keys(payload,['page','fieldId','patientId']) || !page(payload.page) || ('fieldId' in payload && !(SEED_FIELD_IDS as readonly unknown[]).includes(payload.fieldId)) || ('patientId' in payload && !(SEED_PATIENT_IDS as readonly unknown[]).includes(payload.patientId))) return null;
    return {type:'navigate',payload:payload as {page:PageId;fieldId?:string;patientId?:string}};
  }
  if (data.type === 'theme' && keys(payload,['theme']) && theme(payload.theme)) return {type:'theme',payload:{theme:payload.theme}};
  if (data.type === 'reset' && keys(payload,[])) return {type:'reset',payload:{}};
  return null;
}
export interface FrameEnvironment {
  parent:unknown; instanceId:string;
  post(message:unknown,origin:string):void;
  subscribe(listener:(event:{source:unknown;origin:string;data:unknown})=>void):()=>void;
}
function browserEnvironment():FrameEnvironment {
  const bytes = new Uint32Array(4); crypto.getRandomValues(bytes);
  return {
    parent:window.parent, instanceId:'frame-' + Array.from(bytes, value => value.toString(16).padStart(8,'0')).join(''),
    post:(message,origin) => window.parent.postMessage(message,origin),
    subscribe(listener) { window.addEventListener('message',listener); return () => window.removeEventListener('message',listener); },
  };
}
/** The optional environment lets protocol tests exercise source/origin/lifecycle without a DOM. */
export function createFrameBridge(config:EmbedConfig,callbacks:FrameCallbacks,environment?:FrameEnvironment):FrameBridge {
  if (!config.embedded || config.error || !config.hostOrigin) return {emit(){},dispose(){}};
  const env = environment ?? browserEnvironment();
  const origin = config.hostOrigin;
  let currentSession:string|null = null;
  let disposed = false;
  let lastSeq = 0;
  const emit = (type:FrameEvent,payload:unknown) => {
    if (!disposed && currentSession) env.post({protocol:PROTOCOL,version:VERSION,type,instanceId:env.instanceId,sessionId:currentSession,payload},origin);
  };
  const bootstrap = (challenge:string|null) => env.post({protocol:PROTOCOL,version:VERSION,type:'bootstrap',instanceId:env.instanceId,payload:{challenge}},origin);
  const unsubscribe = env.subscribe(event => {
    if (disposed || event.source !== env.parent || event.origin !== origin) return;
    const command = parseFrameCommand(event.data,env.instanceId,currentSession);
    if (!command) return;
    if (command.type === 'discover') { bootstrap(command.challenge); return; }
    const sequence = (event.data as RecordValue).seq as number;
    if (sequence <= lastSeq) return;
    lastSeq = sequence;
    try {
      if (command.type === 'init') {
        if (currentSession !== command.sessionId) {
          currentSession = command.sessionId;
          callbacks.onInit({sessionId:currentSession,...command.payload});
        }
        emit('ready',{capabilities:['navigate','theme','reset','stateChanged','export','closeRequested'],demoOnly:true});
      } else if (command.type === 'navigate') callbacks.onNavigate(command.payload);
      else if (command.type === 'theme') callbacks.onTheme(command.payload.theme);
      else callbacks.onReset();
    } catch { emit('error',{code:'HOST_COMMAND_FAILED',message:'宿主指令暂时无法完成。'}); }
  });
  bootstrap(null);
  return {emit,dispose() { if (disposed) return; disposed = true; currentSession = null; unsubscribe(); }};
}
