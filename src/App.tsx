import { useCallback, useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, Database, FileInput, Files, LayoutDashboard, Moon, PanelLeftClose, RotateCcw, ShieldCheck, Sun, Workflow, X } from 'lucide-react';
import { Pages } from './components/Pages';
import { createInitialState } from './domain/seed';
import { exportSnapshot, getSummary, transition } from './domain/model';
import { loadSession, resetSession, saveSession } from './domain/persistence';
import { PAGE_LABELS, PAGES, ROLE_LABELS, type DemoAction, type DemoState, type PageId, type Role } from './domain/types';
import { createFrameBridge, getEmbedConfig, type FrameBridge } from './bridge/frame';

const ICONS = {overview:LayoutDashboard,intake:FileInput,processing:Workflow,review:ShieldCheck,cases:Files,publish:Database,audit:Activity};
type Loaded = { state:DemoState; notice:string|null; blocked:boolean };
const successText: Partial<Record<DemoAction['type'],string>> = {REVIEW:'核验结果已记录，原始依据保持不变。',FREEZE:'候选版本已冻结，请切换项目负责人视角进行审批。',APPROVE:'演示版本已批准，可以查看和导出。',INTAKE:'合成资料批次已接入。',START_RUN:'模拟任务已开始，既有核验结果将保留。',START_SCAN:'目录扫描已开始，扫描结果可确认加入资料库。',IMPORT_SCAN:'新发现的合成资料已加入待处理队列。',STOP_SCAN:'本次目录扫描已停止。'};
function fromHash():PageId|undefined { const page=window.location.hash.replace(/^#\/?/,''); return PAGES.includes(page as PageId)?page as PageId:undefined; }

export function App() {
  const [config] = useState(getEmbedConfig);
  const [loaded,setLoaded] = useState<Loaded|null>(()=>{
    if(config.embedded) return null;
    const result=loadSession('standalone'); const page=fromHash();
    if(page) result.state={...result.state,page};
    return result;
  });
  const current=useRef(loaded); current.current=loaded;
  const bridge=useRef<FrameBridge|null>(null);
  const lifecycle=useRef(0);
  const [message,setMessage]=useState('');
  const [saveNotice,setSaveNotice]=useState<string|null>(null);
  const [mobileNav,setMobileNav]=useState(false);
  const [viewEpoch,setViewEpoch]=useState(0);
  const resetDialog=useRef<HTMLDialogElement>(null);
  const notify=useCallback((text:string)=>setMessage(text),[]);
  const apply=useCallback((next:Loaded)=>{current.current=next;setLoaded(next);},[]);
  const dispatch=useCallback((action:DemoAction)=>{
    const value=current.current;
    if(!value) return;
    if(value.blocked && action.type!=='NAVIGATE' && action.type!=='SET_THEME') { notify('当前缓存无法读取。请先重置演示，再进行操作。');return; }
    try {
      const state=transition(value.state,action);
      apply({...value,state});
      if(action.type==='NAVIGATE')setMobileNav(false);
      if(successText[action.type])notify(successText[action.type]!);
    }catch(error){notify(error instanceof Error?error.message:'操作未完成，请重试。');}
  },[apply,notify]);
  const reset=useCallback(()=>{
    const value=current.current;if(!value)return;
    lifecycle.current += 1;
    const notice=resetSession(value.state.sessionId);
    const state={...createInitialState(value.state.sessionId),theme:value.state.theme};
    apply({state,notice,blocked:false});setSaveNotice(null);setMobileNav(false);
    setViewEpoch(epoch=>epoch+1);resetDialog.current?.close();notify('演示已恢复初始状态。');
  },[apply,notify]);

  useEffect(()=>{
    if(!config.embedded || config.error) return;
    const instance=createFrameBridge(config,{
      onInit(params){
        lifecycle.current += 1;
        const previous=current.current;
        if(previous && !previous.blocked) saveSession(transition(previous.state,{type:"INTERRUPT_RUNS"}));
        const result=loadSession(params.sessionId);
        if(params.initialPage)result.state={...result.state,page:params.initialPage};
        if(params.theme)result.state={...result.state,theme:params.theme};
        resetDialog.current?.close();setMobileNav(false);setViewEpoch(epoch=>epoch+1);setSaveNotice(null);setMessage('');apply(result);
      },
      onNavigate(params){dispatch({type:'NAVIGATE',...params});},
      onTheme(theme){dispatch({type:'SET_THEME',theme});},
      onReset(){reset();},
    });
    bridge.current=instance;
    return ()=>{instance.dispose();if(bridge.current===instance)bridge.current=null;};
  },[config,apply,dispatch,reset]);

  useEffect(()=>{
    if(!loaded || current.current!==loaded)return;
    document.documentElement.dataset.theme=loaded.state.theme;
    document.documentElement.style.colorScheme=loaded.state.theme;
    document.title=`${PAGE_LABELS[loaded.state.page]} · 小亿数据`;
    if(!config.embedded) history.replaceState(null,'',`${location.pathname}${location.search}#/${loaded.state.page}`);
    if(!loaded.blocked) setSaveNotice(saveSession(loaded.state));
    bridge.current?.emit('stateChanged',{page:loaded.state.page,patientId:loaded.state.selectedPatientId,fieldId:loaded.state.selectedFieldId,summary:getSummary(loaded.state)});
  },[loaded,config.embedded]);
  useEffect(()=>{
    if(config.embedded)return;
    const navigate=()=>{const page=fromHash();if(page && page!==current.current?.state.page)dispatch({type:'NAVIGATE',page});};
    window.addEventListener('hashchange',navigate);return ()=>window.removeEventListener('hashchange',navigate);
  },[config.embedded,dispatch]);
  const activeRun=loaded?.state.runs.find(run=>run.status==='running');
  useEffect(()=>{
    if(!activeRun || loaded?.blocked)return;
    const runId=activeRun.id;const sessionId=loaded!.state.sessionId;
    const timer=window.setTimeout(()=>{
      if(current.current?.state.sessionId===sessionId && current.current.state.runs.some(run=>run.id===runId && run.status==='running' && run.step===activeRun.step))dispatch({type:'ADVANCE_RUN',runId});
    },1100);
    return ()=>window.clearTimeout(timer);
  },[activeRun?.id,activeRun?.step,loaded?.state.sessionId,loaded?.blocked,dispatch]);
  const activeScan=loaded?.state.scans.find(scan=>scan.status==='running');
  useEffect(()=>{
    if(!activeScan || loaded?.blocked)return;
    const scanId=activeScan.id;const sessionId=loaded!.state.sessionId;
    const generation=lifecycle.current;
    const timer=window.setTimeout(()=>{
      const value=current.current;
      if(lifecycle.current===generation && value?.state.sessionId===sessionId && value.state.scans.some(scan=>scan.id===scanId && scan.status==='running' && scan.step===activeScan.step))dispatch({type:'ADVANCE_SCAN',scanId});
    },900);
    return ()=>window.clearTimeout(timer);
  },[activeScan?.id,activeScan?.step,loaded?.state.sessionId,loaded?.blocked,dispatch]);
  const autoEnabled=loaded?.state.scanConfig.autoEnabled;
  const nextScanAt=loaded?.state.scanConfig.nextScanAt;
  useEffect(()=>{
    if(!loaded || loaded.blocked || !autoEnabled || !nextScanAt || activeScan)return;
    const sessionId=loaded.state.sessionId;const generation=lifecycle.current;
    const timer=window.setTimeout(()=>{
      const value=current.current;
      if(lifecycle.current===generation && value?.state.sessionId===sessionId && !value.blocked && value.state.scanConfig.autoEnabled && value.state.scanConfig.nextScanAt===nextScanAt && !value.state.scans.some(scan=>scan.status==='running'))dispatch({type:'START_SCAN',trigger:'automatic'});
    },Math.max(0,Date.parse(nextScanAt)-Date.now()));
    return ()=>window.clearTimeout(timer);
  },[autoEnabled,nextScanAt,activeScan?.id,loaded?.state.sessionId,loaded?.blocked,dispatch]);
  useEffect(()=>{if(!message)return;const timer=window.setTimeout(()=>setMessage(''),5500);return ()=>clearTimeout(timer);},[message]);

  const onExport=useCallback((snapshotId:string)=>{
    const value=current.current;if(!value || value.blocked)return;
    try {
      const data=exportSnapshot(value.state,snapshotId);
      if(config.embedded)bridge.current?.emit('export',{snapshotId,data});
      else {
        const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));
        const a=document.createElement('a');a.href=url;a.download=`xiaoyi-demo-${snapshotId}.json`;a.click();
        window.setTimeout(()=>URL.revokeObjectURL(url),1000);
      }
      dispatch({type:'EXPORT_SNAPSHOT',snapshotId});
      notify(config.embedded?'演示版本已发送给宿主。':'合成数据版本已导出为 JSON。');
    }catch(error){notify(error instanceof Error?error.message:'无法导出版本。');}
  },[config.embedded,dispatch,notify]);

  if(!loaded) return <main className="connection-screen"><div className="brand-emblem"><Database size={26}/></div><h1>{config.error?'无法连接宿主':'等待宿主连接'}</h1><p>{config.error || '宿主初始化后，将恢复对应会话的演示进度。'}</p><span className="badge">小亿数据 · 合成资料演示</span></main>;
  const {state}=loaded;const summary=getSummary(state);
  const notice=loaded.notice || saveNotice;
  return <div className={`app-shell ${config.embedded?'embedded':''}`} data-testid="demo-app" data-session={state.sessionId}>
    <a className="skip-link" href="#workspace-content">跳到工作区</a>
    <aside className={`sidebar ${mobileNav?'is-open':''}`} aria-label="工具导航">
      {!config.embedded && <a href="#/overview" className="brand" onClick={event=>{event.preventDefault();dispatch({type:'NAVIGATE',page:'overview'});}}><span className="brand-emblem"><Database size={22}/></span><span><strong>小亿数据</strong><small>科研资料工作台</small></span></a>}
      <div className="project-card"><span className="project-mark">肠</span><div><strong>肠道肿瘤科研</strong><small>首期数据整理 · DEMO-01</small></div></div>
      <div className="nav-caption">工作空间</div>
      <nav>{PAGES.map((page,index)=>{const Icon=ICONS[page];return <button key={page} className={`nav-item ${state.page===page?'active':''} ${index===6?'nav-separated':''}`} aria-label={PAGE_LABELS[page]} aria-current={state.page===page?'page':undefined} onClick={()=>dispatch({type:'NAVIGATE',page})}><Icon size={18}/><span>{PAGE_LABELS[page]}</span>{page==='review'&&summary.pending>0?<span className="nav-count">{summary.pending}</span>:null}</button>;})}</nav>
      <div className="sidebar-bottom"><div className="local-indicator"><i/>合成资料 · 本地演示</div><p>从资料到证据<br/>每一步都有记录</p>{!config.embedded && <a href="./host.html" target="_blank" rel="noreferrer">查看宿主接入示例 <ArrowUpRight size={14}/></a>}</div>
    </aside>
    <div className="main-shell">
      <header className="topbar">
        <div className="breadcrumb"><button className="btn icon nav-toggle" aria-label="切换导航" onClick={()=>setMobileNav(!mobileNav)}><PanelLeftClose size={18}/></button><span>科研工作空间</span><span className="crumb-slash">/</span><strong>{PAGE_LABELS[state.page]}</strong></div>
        <div className="top-actions"><label className="role-control"><span>演示视角</span><select aria-label="演示角色" value={state.role} onChange={event=>dispatch({type:'SET_ROLE',role:event.target.value as Role})}>{Object.entries(ROLE_LABELS).map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label><button className="btn icon" aria-label={state.theme==='light'?'切换深色主题':'切换浅色主题'} onClick={()=>dispatch({type:'SET_THEME',theme:state.theme==='light'?'dark':'light'})}>{state.theme==='light'?<Moon size={17}/>:<Sun size={17}/>}</button><button className="btn quiet reset-button" onClick={()=>resetDialog.current?.showModal()}><RotateCcw size={15}/><span>重置演示</span></button>{config.embedded&&<button className="btn icon" aria-label="关闭工具" onClick={()=>bridge.current?.emit('closeRequested',{})}><X size={18}/></button>}</div>
      </header>
      <div className="demo-strip"><span className="demo-tag">演示环境</span><span>全部资料均为虚构示例</span><span className="demo-strip-detail">用于演示整理、核验与版本发布流程</span><span className="session-status"><i/> {saveNotice?'仅本次有效':loaded.blocked?'缓存待重置':'进度自动保存在本机'}</span></div>
      {notice && <div className="storage-notice" role="alert">{notice}{loaded.blocked&&<button className="btn small" onClick={()=>resetDialog.current?.showModal()}>重置此演示会话</button>}</div>}
      <main id="workspace-content" tabIndex={-1} className="workspace-content"><Pages key={`${state.sessionId}:${viewEpoch}`} state={state} dispatch={dispatch} onExport={onExport}/></main>
      <footer className="workspace-footer"><span>小亿数据 <span className="version">v0.3</span></span><span>DEMO-B01 · {state.patients.length} 名研究对象 · {state.assets.length} 项合成资料</span><span>本地演示，未连接医院或模型服务</span></footer>
    </div>
    <div className={`toast ${message?'visible':''}`} role="status" aria-live="polite">{message}</div>
    <dialog className="reset-dialog" ref={resetDialog}><form method="dialog"><div className="dialog-icon"><RotateCcw size={23}/></div><h2>重新开始这次演示？</h2><p>当前会话的核验、模拟任务和发布版本将恢复到初始示例。其他演示会话不受影响。</p><div className="dialog-actions"><button className="btn" value="cancel">保留进度</button><button className="btn primary" type="button" onClick={reset}>确认重置</button></div></form></dialog>
  </div>;
}
