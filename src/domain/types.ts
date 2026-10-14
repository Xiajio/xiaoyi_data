export const PAGES = ['overview','intake','processing','review','cases','publish','audit'] as const;
export type PageId = typeof PAGES[number];
export type Theme = 'light' | 'dark';
export type Role = 'reviewer' | 'manager' | 'lead' | 'operator';
export const ROLE_LABELS: Record<Role,string> = { reviewer:'临床审核员',manager:'数据管理员',lead:'项目负责人',operator:'技术运维' };
export const PAGE_LABELS: Record<PageId,string> = {overview:'工作概览',intake:'数据接入',processing:'数据处理',review:'核验工作台',cases:'病例资料库',publish:'数据集与发布',audit:'运行与审计'};
export type AssetKind = 'text'|'table'|'wsi'|'image';
export type AssetProcessingStatus = 'pending'|'processing'|'processed'|'failed'|'restricted';
export type AssetFilter = AssetProcessingStatus|'all';
export interface InventoryCounts { total:number;pending:number;processing:number;processed:number;failed:number;restricted:number }
export interface InventorySummary extends InventoryCounts { byKind:Record<AssetKind,InventoryCounts> }
export interface Patient { id:string; label:string; description:string; privacyPassed:boolean; associationPassed:boolean; events:{date:string;label:string;detail:string}[] }
export interface Asset { id:string; patientId:string; name:string; kind:AssetKind; format:string; size:string; date:string; status:'indexed'|'restricted'|'failed'; detail:string }
export type FieldStatus = 'pending'|'confirmed'|'corrected'|'missing'|'needs-info'|'rejected';
export type ReviewDecision = 'confirm'|'correct'|'missing'|'needs-info'|'reject';
export interface ReviewEntry { id:string; at:string; role:Role; decision:ReviewDecision; previousValue:string; value:string; note:string }
export interface Field { id:string;patientId:string;assetId:string;relatedAssetIds?:string[];label:string;originalValue:string;value:string;status:FieldStatus;critical:boolean;issue:string;location:string;evidence:string;eventTime:string;recordTime:string;reviewNote:string;history:ReviewEntry[] }
export interface Run { id:string;status:'running'|'completed'|'failed'|'interrupted';step:number;fail:boolean;startedAt:string;finishedAt?:string;retryOf?:string;assetIds:string[];legacy?:true }
export type ScanDirectoryId = 'inbox'|'archive';
export interface ScanConfig { directoryId:ScanDirectoryId;recursive:boolean;autoEnabled:boolean;intervalSeconds:15|30|60;nextScanAt?:string }
export interface ScanEntry { path:string;status:'new'|'duplicate'|'unsupported'|'unreadable';assetId?:string;reason:string }
export interface ScanRun { id:string;status:'running'|'completed'|'interrupted';step:0|1|2|3|4;trigger:'manual'|'automatic';directoryId:ScanDirectoryId;recursive:boolean;startedAt:string;finishedAt?:string;entries:ScanEntry[];importedAssetIds:string[] }
export interface AuditEntry { id:string;at:string;role:Role;action:string;detail:string }
export interface Snapshot { id:string;version:number;status:'pending'|'approved'|'invalidated';createdAt:string;approvedAt?:string;fieldIds:string[];fields:Field[];note:string }
export interface DemoState { schemaVersion:2;sessionId:string;revision:number;page:PageId;theme:Theme;role:Role;selectedFieldId:string;selectedPatientId:string;patients:Patient[];assets:Asset[];assetProcessing:Record<string,AssetProcessingStatus>;assetFilter:AssetFilter;fields:Field[];selectedFieldIds:string[];snapshots:Snapshot[];runs:Run[];audit:AuditEntry[];intakeDone:boolean;scanConfig:ScanConfig;scans:ScanRun[] }
export type DemoAction =
 | {type:'NAVIGATE';page:PageId;fieldId?:string;patientId?:string;assetStatus?:AssetFilter}
 | {type:'SET_ROLE';role:Role}
 | {type:'SET_THEME';theme:Theme}
 | {type:'REVIEW';fieldId:string;decision:ReviewDecision;value?:string;note?:string}
 | {type:'SELECT_FIELDS';ids:string[]}
 | {type:'FREEZE'}
 | {type:'APPROVE';snapshotId:string}
 | {type:'EXPORT_SNAPSHOT';snapshotId:string}
 | {type:'INTAKE'}
 | {type:'START_RUN';fail?:boolean;retryOf?:string}
 | {type:'ADVANCE_RUN';runId:string}
 | {type:'INTERRUPT_RUNS'}
 | {type:'CONFIGURE_SCAN';config:Partial<Omit<ScanConfig,'nextScanAt'>>}
 | {type:'START_SCAN';trigger?:'manual'|'automatic'}
 | {type:'ADVANCE_SCAN';scanId:string}
 | {type:'IMPORT_SCAN';scanId:string}
 | {type:'STOP_SCAN';scanId:string};
export interface DemoSummary { pending:number;critical:number;reviewed:number;publishable:number;approved:number;running:boolean;inventory:InventorySummary }
