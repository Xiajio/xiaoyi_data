import { describe, expect, it, vi } from 'vitest';
import { createFrameBridge, parseFrameCommand, resolveEmbedConfig, PROTOCOL, VERSION, type FrameCallbacks, type FrameEnvironment } from '../src/bridge/frame';
const origin = 'https://host.example';
const ownOrigin = 'https://demo.example';
const instanceId = 'frame-unit-001';
const sessionId = 'session-001';
function message(type:string,payload:unknown = {},overrides:Record<string,unknown> = {}) {
 return {protocol:PROTOCOL,version:VERSION,type,instanceId,sessionId,seq:1,payload,...overrides};
}
function harness() {
 const parent = {};
 let listener:((event:{source:unknown;origin:string;data:unknown})=>void)|null = null;
 const post = vi.fn(); const unsubscribe = vi.fn();
 const callbacks:FrameCallbacks = {onInit:vi.fn(),onNavigate:vi.fn(),onTheme:vi.fn(),onReset:vi.fn()};
 const env:FrameEnvironment = {parent,instanceId,post,subscribe(next) {listener = next;return unsubscribe;}};
 const bridge = createFrameBridge({embedded:true,hostOrigin:origin,error:null},callbacks,env);
 return {bridge,post,callbacks,unsubscribe,receive(data:unknown,source:unknown = parent,eventOrigin = origin){listener?.({data,source,origin:eventOrigin});}};
}
describe('embedding configuration',() => {
 it('remains standalone without an explicit embed request',() => expect(resolveEmbedConfig('',ownOrigin,false,undefined)).toEqual({embedded:false,hostOrigin:null,error:null}));
 it('defaults to exactly same-origin hosts',() => {
  expect(resolveEmbedConfig('?embed=1&hostOrigin='+encodeURIComponent(ownOrigin),ownOrigin,true,undefined).error).toBeNull();
  expect(resolveEmbedConfig('?embed=1&hostOrigin='+encodeURIComponent(origin),ownOrigin,true,undefined).error).not.toBeNull();
 });
 it('allows explicitly configured cross-origin embedding',() => expect(resolveEmbedConfig('?embed=1&hostOrigin='+encodeURIComponent(origin),ownOrigin,true,origin+',http://localhost:5173')).toEqual({embedded:true,hostOrigin:origin,error:null}));
 it.each(['*','https://host.example/','https://host.example/path','https://user@host.example','null','javascript:alert(1)'])('rejects ambiguous origin %s',value => {
  expect(resolveEmbedConfig('?embed=1&hostOrigin='+encodeURIComponent(value),ownOrigin,true,origin).error).not.toBeNull();
 });
 it('rejects duplicated parameters, invalid allowlist entries and top-level embed',() => {
  expect(resolveEmbedConfig('?embed=1&embed=1&hostOrigin='+origin,ownOrigin,true,origin).error).not.toBeNull();
  expect(resolveEmbedConfig('?embed=1&hostOrigin='+origin+'&hostOrigin='+origin,ownOrigin,true,origin).error).not.toBeNull();
  expect(resolveEmbedConfig('?embed=1&hostOrigin='+origin,ownOrigin,true,origin+',*').error).not.toBeNull();
  expect(resolveEmbedConfig('?embed=1&hostOrigin='+origin,ownOrigin,false,origin).error).not.toBeNull();
 });
});
describe('strict iframe command schema',() => {
 it('accepts only enumerated references, never arbitrary case data',() => {
  expect(parseFrameCommand(message('navigate',{page:'review',fieldId:'R-01',patientId:'P-DEMO-001'}),instanceId,sessionId)?.type).toBe('navigate');
  for (const payload of [{page:'admin'},{page:'review',fieldId:'R-99'},{page:'cases',patientId:'real-patient'},{page:'review',data:{name:'patient'}},{page:'review',fields:[]}]) expect(parseFrameCommand(message('navigate',payload),instanceId,sessionId)).toBeNull();
 });
 it('rejects unknown fields, protocol/version drift and malformed sessions',() => {
  for (const overrides of [{protocol:'other'},{version:2},{extra:true},{seq:0},{seq:1.5},{seq:undefined},{seq:Number.MAX_SAFE_INTEGER+1},{sessionId:''},{sessionId:'real patient'},{instanceId:'retired-instance'}]) expect(parseFrameCommand(message('init',{},overrides),instanceId,null)).toBeNull();
  expect(parseFrameCommand(message('init',{theme:'auto'}),instanceId,null)).toBeNull();
  expect(parseFrameCommand(message('init',{initialPage:'bad'}),instanceId,null)).toBeNull();
  expect(parseFrameCommand(message('init',{records:[]}),instanceId,null)).toBeNull();
  expect(parseFrameCommand(message('reset',{force:true}),instanceId,sessionId)).toBeNull();
 });
 it('does not execute session commands before init or for the previous session',() => {
  expect(parseFrameCommand(message('reset'),instanceId,null)).toBeNull();
  expect(parseFrameCommand(message('reset'),instanceId,'session-new')).toBeNull();
 });
});
describe('frame lifecycle',() => {
 it('bootstraps without assuming a persisted session',() => {
  const h = harness();
  expect(h.post).toHaveBeenCalledWith({protocol:PROTOCOL,version:VERSION,type:'bootstrap',instanceId,payload:{challenge:null}},origin);
  h.bridge.emit('stateChanged',{page:'overview'});
  h.receive(message('navigate',{page:'review'}));
  expect(h.post).toHaveBeenCalledTimes(1);expect(h.callbacks.onNavigate).not.toHaveBeenCalled();
 });
 it('binds both parent window and exact origin before processing even discovery',() => {
  const h = harness();h.receive(message('init'),{},origin);h.receive(message('init'),undefined,'https://evil.example');
  expect(h.callbacks.onInit).not.toHaveBeenCalled();
  h.receive({protocol:PROTOCOL,version:VERSION,type:'discover',challenge:'challenge-1'});
  expect(h.post).toHaveBeenLastCalledWith({protocol:PROTOCOL,version:VERSION,type:'bootstrap',instanceId,payload:{challenge:'challenge-1'}},origin);
 });
 it('initializes once, acknowledges duplicate init without resetting progress',() => {
  const h = harness();h.receive(message('init',{initialPage:'review',theme:'dark'}));h.receive(message('init',{initialPage:'overview'},{seq:2}));
  expect(h.callbacks.onInit).toHaveBeenCalledTimes(1);
  expect(h.callbacks.onInit).toHaveBeenCalledWith({sessionId,initialPage:'review',theme:'dark'});
  { const hb = harness(); hb.receive(message('init',{theme:'blue'})); expect(hb.callbacks.onInit).toHaveBeenCalledWith({sessionId,initialPage:undefined,theme:'blue'}); hb.receive(message('theme',{theme:'blue'},{seq:2})); expect(hb.callbacks.onTheme).toHaveBeenCalledWith('blue'); }
  expect(h.post.mock.calls.filter(([data])=>data.type === 'ready')).toHaveLength(2);
 });
 it('handles valid commands and attaches the current binding to outbound events',() => {
  const h = harness();h.receive(message('init'));h.receive(message('navigate',{page:'review',fieldId:'R-06'},{seq:2}));h.receive(message('theme',{theme:'dark'},{seq:3}));h.receive(message('reset',{}, {seq:4}));
  expect(h.callbacks.onNavigate).toHaveBeenCalledWith({page:'review',fieldId:'R-06'});expect(h.callbacks.onTheme).toHaveBeenCalledWith('dark');expect(h.callbacks.onReset).toHaveBeenCalledOnce();
  h.bridge.emit('export',{demoOnly:true});expect(h.post).toHaveBeenLastCalledWith({protocol:PROTOCOL,version:VERSION,type:'export',instanceId,sessionId,payload:{demoOnly:true}},origin);
 });
 it('switches to a new session and rejects delayed previous-session init and commands',() => {
  const h = harness();h.receive(message('init'));h.receive(message('init',{}, {sessionId:'session-002',seq:2}));h.receive(message('init'));h.receive(message('reset'));
  expect(h.callbacks.onInit).toHaveBeenCalledTimes(2);expect(h.callbacks.onReset).not.toHaveBeenCalled();
  h.bridge.emit('stateChanged',{});expect(h.post).toHaveBeenLastCalledWith({protocol:PROTOCOL,version:VERSION,type:'stateChanged',instanceId,sessionId:'session-002',payload:{}},origin);
  h.receive(message('init',{}, {seq:3}));expect(h.callbacks.onInit).toHaveBeenCalledTimes(3);
  h.bridge.emit('stateChanged',{});expect(h.post).toHaveBeenLastCalledWith({protocol:PROTOCOL,version:VERSION,type:'stateChanged',instanceId,sessionId,payload:{}},origin);
 });
 it('rejects same-sequence replay and does not advance sequence for invalid payloads',() => {
  const h = harness();h.receive(message('init'));h.receive(message('reset',{}, {seq:2}));h.receive(message('reset',{}, {seq:2}));
  h.receive(message('navigate',{page:'invalid'},{seq:99}));h.receive(message('theme',{theme:'dark'},{seq:3}));
  expect(h.callbacks.onReset).toHaveBeenCalledOnce();expect(h.callbacks.onTheme).toHaveBeenCalledWith('dark');
 });
 it('a new mount rejects old-instance init regardless of matching session',() => {
  const h = harness();h.receive(message('init',{}, {instanceId:'previous-mount'}));expect(h.callbacks.onInit).not.toHaveBeenCalled();
 });
 it('disposal cancels listeners and blocks late calls and outgoing events',() => {
  const h = harness();h.receive(message('init'));const sent = h.post.mock.calls.length;h.bridge.dispose();h.bridge.dispose();h.receive(message('reset'));h.bridge.emit('export',{});
  expect(h.unsubscribe).toHaveBeenCalledOnce();expect(h.callbacks.onReset).not.toHaveBeenCalled();expect(h.post).toHaveBeenCalledTimes(sent);
 });
 it('does not install a bridge for invalid or standalone config',() => {
  const subscribe = vi.fn();const callbacks = {onInit:vi.fn(),onNavigate:vi.fn(),onTheme:vi.fn(),onReset:vi.fn()};
  for (const config of [{embedded:false,hostOrigin:null,error:null},{embedded:true,hostOrigin:origin,error:'blocked'}]) createFrameBridge(config,callbacks,{instanceId,parent:{},post:vi.fn(),subscribe});
  expect(subscribe).not.toHaveBeenCalled();
 });
});
