import { getInventorySummary, isReviewed } from './model';
import type { Asset, AssetKind, AssetProcessingStatus, DemoState, Field, FieldStatus, InventorySummary, Patient } from './types';

export const ASSET_KINDS = ['text', 'table', '3d', 'image'] as const satisfies readonly AssetKind[];
export const PROCESSING_STATUSES = ['pending', 'processing', 'processed', 'failed', 'restricted'] as const satisfies readonly AssetProcessingStatus[];
export const FIELD_STATUSES = ['pending', 'confirmed', 'corrected', 'missing', 'needs-info', 'rejected'] as const satisfies readonly FieldStatus[];

export interface CoverageCell { kind: AssetKind; assetIds: string[]; total: number; restricted: number }
export interface PatientCoverage { patientId: string; privacyPassed: boolean; associationPassed: boolean; cells: CoverageCell[] }
export interface TypeCoverage { kind: AssetKind; patients: number; totalPatients: number; assetIds: string[] }
export interface AssetMonth { month: string; total: number; assetIds: string[]; byKind: Record<AssetKind, number> }
export interface ResearchDashboard {
  scope: { patients: number; assets: number; fields: number };
  patients: Patient[];
  assets: Asset[];
  fields: Field[];
  inventory: InventorySummary;
  coverage: PatientCoverage[];
  typeCoverage: TypeCoverage[];
  months: AssetMonth[];
  review: {
    total: number;
    reviewed: number;
    pending: number;
    critical: number;
    missing: number;
    byStatus: Record<FieldStatus, number>;
    byPatient: { patientId: string; total: number; reviewed: number; critical: number }[];
  };
}

const emptyKinds = (): Record<AssetKind, number> => ({ text: 0, table: 0, '3d': 0, image: 0 });
const unresolvedCritical = (field: Field) => field.critical && !isReviewed(field);

/** Counts current synthetic records only; source dates are not experiment event dates. */
export function getResearchDashboard(state: DemoState, patientId?: string): ResearchDashboard {
  const patients = state.patients.filter(patient => patientId === undefined || patient.id === patientId);
  const patientIds = new Set(patients.map(patient => patient.id));
  const assets = state.assets.filter(asset => patientIds.has(asset.patientId));
  const fields = state.fields.filter(field => patientIds.has(field.patientId));
  const byStatus: Record<FieldStatus, number> = { pending: 0, confirmed: 0, corrected: 0, missing: 0, 'needs-info': 0, rejected: 0 };
  fields.forEach(field => { byStatus[field.status] += 1; });
  const reviewed = fields.filter(isReviewed).length;
  const monthMap = new Map<string, AssetMonth>();
  assets.forEach(asset => {
    const month = asset.date.slice(0, 7);
    const entry = monthMap.get(month) ?? { month, total: 0, assetIds: [], byKind: emptyKinds() };
    entry.total += 1;
    entry.assetIds.push(asset.id);
    entry.byKind[asset.kind] += 1;
    monthMap.set(month, entry);
  });

  return {
    scope: { patients: patients.length, assets: assets.length, fields: fields.length },
    patients,
    assets,
    fields,
    inventory: getInventorySummary({ ...state, assets }),
    coverage: patients.map(patient => ({
      patientId: patient.id,
      privacyPassed: patient.privacyPassed,
      associationPassed: patient.associationPassed,
      cells: ASSET_KINDS.map(kind => {
        const matching = assets.filter(asset => asset.patientId === patient.id && asset.kind === kind);
        return { kind, assetIds: matching.map(asset => asset.id), total: matching.length, restricted: matching.filter(asset => state.assetProcessing[asset.id] === 'restricted').length };
      }),
    })),
    typeCoverage: ASSET_KINDS.map(kind => {
      const matching = assets.filter(asset => asset.kind === kind);
      return { kind, patients: new Set(matching.map(asset => asset.patientId)).size, totalPatients: patients.length, assetIds: matching.map(asset => asset.id) };
    }),
    months: [...monthMap.values()].sort((a, b) => a.month.localeCompare(b.month)),
    review: {
      total: fields.length,
      reviewed,
      pending: fields.length - reviewed,
      critical: fields.filter(unresolvedCritical).length,
      missing: byStatus.missing,
      byStatus,
      byPatient: patients.map(patient => {
        const matching = fields.filter(field => field.patientId === patient.id);
        return { patientId: patient.id, total: matching.length, reviewed: matching.filter(isReviewed).length, critical: matching.filter(unresolvedCritical).length };
      }),
    },
  };
}
