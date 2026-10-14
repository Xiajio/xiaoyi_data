/** Framework-free host client for xiaoyi.data-demo/v1. No dependency on React. */
const PROTOCOL = 'xiaoyi.data-demo/v1';
const VERSION = 1;
const PAGES = ['overview','intake','processing','review','cases','publish','audit'];
const EVENTS = ['ready','stateChanged','export','closeRequested','error'];
const FIELDS = ['R-01','R-02','R-03','R-04','R-05','R-06'];
const PATIENTS = ['P-DEMO-001','P-DEMO-002','P-DEMO-003','P-DEMO-004'];
const object = value => !!value && typeof value === 'object' && !Array.isArray(value);
const keys = (value, allowed) => Object.keys(value).every(key => allowed.includes(key));
const token = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$/.test(value);
const newToken = () => { const bytes = new Uint32Array(4); crypto.getRandomValues(bytes); return 'host-' + Array.from(bytes, n => n.toString(16)).join('-'); };
function origin(value) { try { const url = new URL(value); return ['http:','https:'].includes(url.protocol) && url.origin === value; } catch { return false; } }
function checkSession(value) { if (!token(value)) throw new Error('sessionId 必须是 1–96 位字母、数字或 . _ : -。'); }
/**
 * @param {{iframe:HTMLIFrameElement,frameOrigin:string,sessionId:string,initialPage?:string,theme?:'light'|'dark'|'blue',onEvent?:(event:object)=>void}} options
 */
export function createDataDemoHost(options) {
  if (!origin(options.frameOrigin)) throw new Error('frameOrigin 必须为精确 HTTP(S) origin。');
  checkSession(options.sessionId);
  if (options.initialPage !== undefined && !PAGES.includes(options.initialPage)) throw new Error('初始页面无效。');
  if (options.theme !== undefined && !['light','dark','blue'].includes(options.theme)) throw new Error('主题无效。');
  let sessionId = options.sessionId;
  let selectedTheme = options.theme;
  let initialPage = options.initialPage;
  let instanceId = null;
  let challenge = null;
  let ready = false;
  let disposed = false;
  const retiredInstances = new Set();
  let sequence = 0;
  function post(message) { if (!disposed) options.iframe.contentWindow?.postMessage(message,options.frameOrigin); }
  function discover() {
    if (disposed) return;
    challenge = newToken();
    post({protocol:PROTOCOL,version:VERSION,type:'discover',challenge});
  }
  function initialize() {
    if (!instanceId) return;
    const payload = {};
    if (initialPage !== undefined) payload.initialPage = initialPage;
    if (selectedTheme !== undefined) payload.theme = selectedTheme;
    post({protocol:PROTOCOL,version:VERSION,type:'init',sessionId,instanceId,seq:++sequence,payload});
  }
  function command(type,payload) {
    if (disposed || !ready || !instanceId) return false;
    post({protocol:PROTOCOL,version:VERSION,type,sessionId,instanceId,seq:++sequence,payload});
    return true;
  }
  function onMessage(event) {
    if (disposed || event.source !== options.iframe.contentWindow || event.origin !== options.frameOrigin || !object(event.data)) return;
    const data = event.data;
    if (data.protocol !== PROTOCOL || data.version !== VERSION || !token(data.instanceId)) return;
    if (data.type === 'bootstrap') {
      if (!keys(data,['protocol','version','type','instanceId','payload']) || !object(data.payload) || !keys(data.payload,['challenge']) || !(data.payload.challenge === null || token(data.payload.challenge)) || retiredInstances.has(data.instanceId)) return;
      if (data.payload.challenge === null) { if (data.instanceId !== instanceId || !ready) discover(); return; }
      if (!challenge || data.payload.challenge !== challenge) return;
      if (instanceId && instanceId !== data.instanceId) retiredInstances.add(instanceId);
      if (instanceId !== data.instanceId) sequence = 0;
      instanceId = data.instanceId;
      ready = false;
      initialize();
      return;
    }
    if (!keys(data,['protocol','version','type','instanceId','sessionId','payload']) || !EVENTS.includes(data.type) || data.instanceId !== instanceId || data.sessionId !== sessionId) return;
    if (data.type === 'ready') ready = true;
    if (ready) options.onEvent?.(data);
  }
  function onLoad() { ready = false; discover(); }
  window.addEventListener('message',onMessage);
  options.iframe.addEventListener('load',onLoad);
  discover();
  return {
    navigate(page, selection = {}) {
      if (!PAGES.includes(page) || !object(selection) || !keys(selection,['fieldId','patientId']) || ('fieldId' in selection && !FIELDS.includes(selection.fieldId)) || ('patientId' in selection && !PATIENTS.includes(selection.patientId))) throw new Error('只能定位演示页面与固定种子 ID。');
      initialPage = page;
      return command('navigate',{page,...selection});
    },
    setTheme(theme) { if (!['light','dark','blue'].includes(theme)) throw new Error('主题无效。'); selectedTheme = theme; return command('theme',{theme}); },
    reset() { return command('reset',{}); },
    switchSession(nextSessionId) {
      checkSession(nextSessionId);
      if (nextSessionId === sessionId) return false;
      sessionId = nextSessionId;
      ready = false;
      initialize();
      return true;
    },
    getState() { return {ready,sessionId,instanceId}; },
    dispose() {
      if (disposed) return;
      disposed = true; ready = false; instanceId = null; challenge = null;
      window.removeEventListener('message',onMessage);
      options.iframe.removeEventListener('load',onLoad);
    },
  };
}
