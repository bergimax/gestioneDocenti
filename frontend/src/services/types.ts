// DTOs mirroring the backend API contract (see project spec §3–§12).

export type Role = "ADMIN" | "SEGRETERIA";
export type Giorno = "LUNEDI" | "MARTEDI" | "MERCOLEDI" | "GIOVEDI" | "VENERDI";
export type GiornoLower = "lunedi" | "martedi" | "mercoledi" | "giovedi" | "venerdi";
export type TipoDocente = "ASSUNTO" | "CONTRATTO";
export type TipoMateria = "TEORIA" | "PRATICA";
export type StatoOrario = "BOZZA" | "APPROVATO";
export type QualityLevel = "A" | "B" | "C";

export const GIORNI: Giorno[] = ["LUNEDI", "MARTEDI", "MERCOLEDI", "GIOVEDI", "VENERDI"];
export const GIORNI_LOWER: GiornoLower[] = ["lunedi", "martedi", "mercoledi", "giovedi", "venerdi"];
export const ORE = [8, 9, 10, 11, 12, 13] as const;

export interface LoginResponse {
  status: "ok";
  token: string;
  email: string;
  role: Role;
}
export interface Me {
  email: string;
  role: Role;
}

export interface Docente {
  id: string;
  nome: string;
  email: string | null;
  tipo: TipoDocente;
  active: boolean;
  first_time_this_year: boolean;
}
export type DocenteInput = Omit<Docente, "id">;

export interface Classe {
  id: string;
  nome: string;
  n_studenti: number;
  gruppo: string | null;
}
export type ClasseInput = Omit<Classe, "id">;

export interface Materia {
  id: string;
  nome: string;
  tipo: TipoMateria;
  peso_cognitivo: "ALTO" | "MEDIO" | "BASSO";
}

export interface Assegnazione {
  id: string;
  docente_id: string;
  classe_id: string;
  materia_id: string;
  ore_totali: number;
  ore_erogate: number;
  ore_settimanali: number;
}

export interface Fascia {
  ora_inizio: string; // "08:00"
  ora_fine: string;
  disponibile: boolean;
}
export type GiorniFasce = Record<GiornoLower, Fascia[]>;

export interface Availability {
  docente_id: string;
  settimana_inizio: string;
  giorni_fasce: GiorniFasce;
  is_default: boolean;
  inherited_from?: string | null;
}

export interface AvailabilityStatus {
  week_complete: boolean;
  total_teachers: number;
  teachers_with_availability: number;
  teachers_missing: string[];
  ready_to_generate: boolean;
  completion_percentage: number;
}

export interface Week {
  start: string;
  end: string;
  week_num: number;
}

export interface Slot {
  slot_id: string;
  classe_id: string;
  classe_nome: string;
  docente_id: string;
  docente_nome: string;
  materia_id: string;
  materia_nome: string;
  materia_tipo: TipoMateria;
  giorno: Giorno;
  ora_inizio: number;
  ora_fine: number;
  accoppiata: boolean;
  classe_accoppiata_id: string | null;
  conflitto: boolean;
  conflitto_chiavi: string[];
  indisponibile: boolean;
}

export interface Conflict {
  conflict_id: string;
  chiave: string;
  description: string;
  suggested_action: { action_type: QuickActionType; label: string } | null;
  classe_id: string | null;
  docente_id: string | null;
  giorno: Giorno | null;
  ore?: number;
}

export interface StageCell {
  classe_id: string;
  classe_nome: string;
  giorno: Giorno;
}

export interface Schedule {
  schedule_id: string;
  week_start: string;
  stato: StatoOrario;
  quality_score: number;
  quality_level: QualityLevel;
  n_soft_conflicts: number;
  slots: Slot[];
  conflicts: Conflict[];
  stage_cells: StageCell[];
  approved_at: string | null;
}

export type GenerateResult =
  | ({ status: "generated"; message?: string } & Schedule)
  | { status: "infeasible"; conflicting_constraints: string[]; suggested_deroghe: string[] }
  | { status: "timeout" | "error"; message: string };

export interface SlotChanges {
  docente_id?: string;
  giorno?: Giorno;
  ora_inizio?: number;
}

export interface AssignSlotInput {
  classe_id: string;
  giorno: Giorno;
  ora_inizio: number;
  docente_id: string;
  materia_id: string;
}

export interface Assignable {
  docente_id: string;
  docente_nome: string;
  materia_id: string;
  materia_nome: string;
}

export type QuickActionType =
  | "force_3_hours_theory"
  | "reduce_contract_hours"
  | "authorize_early_exit"
  | "override_availability"
  | "authorize_single_classe_day"
  | "authorize_friday_late_start"
  | "authorize_short_pratica_block";

export interface QuickActionInput {
  action_type: QuickActionType;
  class_id?: string;
  teacher_id?: string;
  giorno?: Giorno;
  max_hours?: number;
}

export interface ApproveResult {
  status: "approved";
  schedule_id: string;
  stato: "APPROVATO";
  ore_scalate: number;
  assegnazioni_aggiornate: number;
  senza_monte_ore: number;
}

export type Motivo =
  | "classe_late_start"
  | "friday_late_start"
  | "teoria_consecutive"
  | "pratica_block"
  | "contractor_gap"
  | "single_classe_day"
  | "ore_target_deviation"
  | "classe_unfilled"
  | "altro";

export interface FeedbackInput {
  voto: number;
  motivi: Motivo[];
  nota?: string;
}
export interface Feedback extends FeedbackInput {
  id: string;
  created_at: string;
  quality_score: number;
  quality_level: QualityLevel;
}

export interface ExportFile {
  filename: string;
  blob: Blob;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
