import type {
  ApproveResult,
  Assegnazione,
  AssignSlotInput,
  Assignable,
  Availability,
  AvailabilityStatus,
  Classe,
  ClasseInput,
  Docente,
  DocenteInput,
  ExportFile,
  Feedback,
  FeedbackInput,
  GenerateResult,
  GiorniFasce,
  LoginResponse,
  Materia,
  Me,
  QuickActionInput,
  Schedule,
  SlotChanges,
  Week,
} from "./types";

/**
 * Single contract for every frontend → backend call.
 * Implemented by the HTTP client (real backend) and by the in-memory mock.
 */
export interface ApiService {
  auth: {
    login(email: string, password: string): Promise<LoginResponse>;
    logout(): Promise<void>;
    me(): Promise<Me>;
    changePassword(current_password: string, new_password: string): Promise<void>;
  };
  teachers: {
    list(): Promise<Docente[]>;
    create(input: DocenteInput): Promise<Docente>;
    update(id: string, input: Partial<DocenteInput>): Promise<Docente>;
    remove(id: string): Promise<void>;
  };
  classes: {
    list(): Promise<Classe[]>;
    create(input: ClasseInput): Promise<Classe>;
    update(id: string, input: Partial<ClasseInput>): Promise<Classe>;
    remove(id: string): Promise<void>;
  };
  subjects: { list(): Promise<Materia[]> };
  assignments: { list(): Promise<Assegnazione[]> };
  availability: {
    weeks(): Promise<Week[]>;
    get(week: string, teacherId: string): Promise<Availability>;
    save(week: string, teacherId: string, giorni_fasce: GiorniFasce): Promise<void>;
    copyFrom(week: string, prevWeek: string): Promise<{ copied: number }>;
    status(week: string): Promise<AvailabilityStatus>;
  };
  schedule: {
    get(week: string): Promise<Schedule | null>;
    generate(week: string, include_preferences?: boolean): Promise<GenerateResult>;
    regenerate(week: string): Promise<GenerateResult>;
    modifySlot(week: string, slotId: string, changes: SlotChanges): Promise<Schedule>;
    assignable(week: string, classeId: string): Promise<Assignable[]>;
    assignSlot(week: string, input: AssignSlotInput): Promise<Schedule>;
    approveConflicts(week: string, chiavi: string[]): Promise<Schedule>;
    rejectConflict(
      week: string,
      input: { classe_id: string; giorno: string; ora_inizio: number },
    ): Promise<Schedule>;
    applyQuickAction(week: string, input: QuickActionInput): Promise<GenerateResult>;
    approve(week: string): Promise<ApproveResult>;
    listFeedback(week: string): Promise<Feedback[]>;
    sendFeedback(week: string, input: FeedbackInput): Promise<Feedback>;
    exportPdf(week: string): Promise<ExportFile>;
    exportExcel(week: string): Promise<ExportFile>;
  };
}
