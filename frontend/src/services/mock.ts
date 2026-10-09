import type { ApiService } from "./api";
import { addDays, firstMondayOnOrAfter, hourLabel } from "./dates";
import { session } from "./session";
import {
  ApiError,
  GIORNI,
  GIORNI_LOWER,
  ORE,
  type Assegnazione,
  type Classe,
  type Conflict,
  type Docente,
  type Feedback,
  type GenerateResult,
  type Giorno,
  type GiorniFasce,
  type Materia,
  type QuickActionType,
  type Role,
  type Schedule,
  type Slot,
  type StageCell,
  type Week,
} from "./types";

/* ------------------------------------------------------------------ */
/* Seed data                                                           */
/* ------------------------------------------------------------------ */

export const MOCK_USERS = [
  { email: "admin@scuola.it", password: "password123", role: "ADMIN" as Role },
  { email: "segreteria@scuola.it", password: "password123", role: "SEGRETERIA" as Role },
];

export const SCHOOL_YEAR = { start: "2026-09-14", end: "2027-06-11", anno: "2026-2027" };

function seedTeachers(): Docente[] {
  const t = (id: string, nome: string, tipo: Docente["tipo"], first = false): Docente => ({
    id,
    nome,
    email: `${nome.split(" ")[0]!.toLowerCase()}@scuola.it`,
    tipo,
    active: true,
    first_time_this_year: first,
  });
  return [
    t("d1", "ROSSI MARIO", "ASSUNTO"),
    t("d2", "BIANCHI LAURA", "ASSUNTO"),
    t("d3", "VERDI GIUSEPPE", "ASSUNTO"),
    t("d4", "FERRARI ANNA", "ASSUNTO"),
    t("d5", "ESPOSITO LUCA", "CONTRATTO"),
    t("d6", "ROMANO GIULIA", "CONTRATTO"),
    t("d7", "COLOMBO MARCO", "CONTRATTO", true),
    t("d8", "RICCI SARA", "ASSUNTO"),
  ];
}

function seedClasses(): Classe[] {
  return [
    { id: "c1", nome: "I ELETTRICISTI", n_studenti: 18, gruppo: "PRIME" },
    { id: "c2", nome: "II ELETTRICISTI", n_studenti: 16, gruppo: "SECONDE" },
    { id: "c3", nome: "III MECCANICI", n_studenti: 14, gruppo: "TERZE" },
    { id: "c4", nome: "I I.T.C.", n_studenti: 20, gruppo: "PRIMA4+2" },
  ];
}

function seedSubjects(): Materia[] {
  return [
    { id: "m1", nome: "ITALIANO", tipo: "TEORIA", peso_cognitivo: "ALTO" },
    { id: "m2", nome: "MATEMATICA", tipo: "TEORIA", peso_cognitivo: "ALTO" },
    { id: "m3", nome: "INGLESE", tipo: "TEORIA", peso_cognitivo: "MEDIO" },
    { id: "m4", nome: "LAB. ELETTRICO", tipo: "PRATICA", peso_cognitivo: "BASSO" },
    { id: "m5", nome: "LAB. MECCANICO", tipo: "PRATICA", peso_cognitivo: "BASSO" },
    { id: "m6", nome: "INFORMATICA", tipo: "TEORIA", peso_cognitivo: "MEDIO" },
    { id: "m7", nome: "SCIENZE MOTORIE", tipo: "PRATICA", peso_cognitivo: "BASSO" },
    { id: "m8", nome: "DIRITTO", tipo: "TEORIA", peso_cognitivo: "MEDIO" },
  ];
}

function seedAssignments(): Assegnazione[] {
  // [docente, classe, materia, weekly hours] – each class sums to 30h/week
  const rows: [string, string, string, number][] = [
    ["d1", "c1", "m1", 6], ["d2", "c1", "m2", 5], ["d3", "c1", "m3", 4], ["d5", "c1", "m4", 10], ["d8", "c1", "m7", 5],
    ["d1", "c2", "m1", 5], ["d2", "c2", "m2", 5], ["d3", "c2", "m3", 4], ["d5", "c2", "m4", 8], ["d4", "c2", "m8", 4], ["d8", "c2", "m7", 4],
    ["d4", "c3", "m1", 6], ["d2", "c3", "m2", 4], ["d6", "c3", "m5", 12], ["d8", "c3", "m7", 2],
    ["d1", "c4", "m1", 5], ["d4", "c4", "m2", 5], ["d3", "c4", "m3", 5], ["d7", "c4", "m6", 10], ["d6", "c4", "m8", 5],
  ];
  return rows.map(([d, c, m, h], i) => ({
    id: `a${i + 1}`,
    docente_id: d,
    classe_id: c,
    materia_id: m,
    ore_settimanali: h,
    ore_totali: h * 33,
    ore_erogate: 0,
  }));
}

export function defaultGrid(available = true): GiorniFasce {
  const g = {} as GiorniFasce;
  for (const d of GIORNI_LOWER) {
    g[d] = ORE.map((h) => ({ ora_inizio: hourLabel(h), ora_fine: hourLabel(h + 1), disponibile: available }));
  }
  return g;
}

const STAGE: { classe_id: string; giorno: Giorno }[] = [{ classe_id: "c3", giorno: "GIOVEDI" }];

/* ------------------------------------------------------------------ */
/* Mock implementation                                                 */
/* ------------------------------------------------------------------ */

interface InternalSchedule {
  schedule_id: string;
  week_start: string;
  stato: Schedule["stato"];
  approved_at: string | null;
  slots: Slot[];
  approvedKeys: Set<string>;
  freed: Set<string>; // `${classe}|${giorno}|${ora}`
  deroghe: { action_type: QuickActionType; teacher_id?: string; class_id?: string }[];
}

export interface MockOptions {
  latencyMs?: number;
}

export function createMockApi(opts: MockOptions = {}): ApiService {
  const latency = opts.latencyMs ?? 0;
  const wait = <T>(v: T): Promise<T> =>
    latency ? new Promise((r) => setTimeout(() => r(v), latency)) : Promise.resolve(v);
  const clone = <T>(v: T): T => JSON.parse(JSON.stringify(v));

  let idSeq = 1000;
  const newId = (p: string) => `${p}${++idSeq}`;

  const passwords = new Map(MOCK_USERS.map((u) => [u.email, u.password]));
  const tokens = new Map<string, { email: string; role: Role }>();
  const teachers = seedTeachers();
  const classes = seedClasses();
  const subjects = seedSubjects();
  const assignments = seedAssignments();
  const availability = new Map<string, GiorniFasce>();
  const schedules = new Map<string, InternalSchedule>();
  const feedback = new Map<string, Feedback[]>();

  // Seed contractor availability for the first week
  const w0 = SCHOOL_YEAR.start;
  const g5 = defaultGrid(true);
  g5.venerdi.forEach((f) => (f.disponibile = false));
  availability.set(`${w0}|d5`, g5);
  const g6 = defaultGrid(true);
  g6.lunedi.forEach((f) => (f.disponibile = false));
  g6.mercoledi.slice(3).forEach((f) => (f.disponibile = false));
  availability.set(`${w0}|d6`, g6);

  /* ---------- auth helpers ---------- */
  function current() {
    const tok = session.getToken();
    const u = tok ? tokens.get(tok) : undefined;
    if (!u) throw new ApiError(401, "Sessione non valida o scaduta");
    return { ...u, token: tok! };
  }
  function requireAdmin() {
    const u = current();
    if (u.role !== "ADMIN") throw new ApiError(403, "Permesso negato: operazione riservata all'amministratore");
    return u;
  }

  /* ---------- lookup helpers ---------- */
  const teacher = (id: string) => {
    const t = teachers.find((x) => x.id === id);
    if (!t) throw new ApiError(404, "Docente non trovato");
    return t;
  };
  const classe = (id: string) => {
    const c = classes.find((x) => x.id === id);
    if (!c) throw new ApiError(404, "Classe non trovata");
    return c;
  };
  const materia = (id: string) => subjects.find((x) => x.id === id)!;

  function resolveAvailability(week: string, t: Docente): { grid: GiorniFasce; is_default: boolean; from: string | null } {
    const own = availability.get(`${week}|${t.id}`);
    if (own) return { grid: own, is_default: false, from: week };
    if (!t.first_time_this_year) {
      const prev = [...availability.keys()]
        .filter((k) => k.endsWith(`|${t.id}`) && k.split("|")[0]! < week)
        .sort()
        .pop();
      if (prev) return { grid: availability.get(prev)!, is_default: false, from: prev.split("|")[0] ?? null };
    }
    return { grid: defaultGrid(true), is_default: true, from: null };
  }

  function isAvailable(week: string, t: Docente, g: Giorno, h: number, ignore = false): boolean {
    if (t.tipo === "ASSUNTO" || ignore) return true;
    const { grid, is_default } = resolveAvailability(week, t);
    // a contractor with nothing recorded is unavailable (unless first year default)
    if (is_default && !t.first_time_this_year) return false;
    const day = grid[g.toLowerCase() as keyof GiorniFasce];
    return day?.[h - 8]?.disponibile ?? false;
  }

  const stageFor = (cid: string, g: Giorno) => STAGE.some((s) => s.classe_id === cid && s.giorno === g);

  function stageCells(): StageCell[] {
    return STAGE.map((s) => ({ ...s, classe_nome: classe(s.classe_id).nome }));
  }

  function makeSlot(a: Assegnazione, g: Giorno, h: number): Slot {
    const c = classe(a.classe_id);
    const t = teacher(a.docente_id);
    const m = materia(a.materia_id);
    return {
      slot_id: newId("s"),
      classe_id: c.id,
      classe_nome: c.nome,
      docente_id: t.id,
      docente_nome: t.nome,
      materia_id: m.id,
      materia_nome: m.nome,
      materia_tipo: m.tipo,
      giorno: g,
      ora_inizio: h,
      ora_fine: h + 1,
      accoppiata: false,
      classe_accoppiata_id: null,
      conflitto: false,
      conflitto_chiavi: [],
      indisponibile: false,
    };
  }

  /** Greedy deterministic generator (stand-in for the CP-SAT solver). */
  function solve(week: string, seed: number, deroghe: InternalSchedule["deroghe"]): Slot[] {
    const ignoreAvail = new Set(
      deroghe.filter((d) => d.action_type === "override_availability" && d.teacher_id).map((d) => d.teacher_id!),
    );
    const remaining = new Map(assignments.map((a) => [a.id, a.ore_settimanali]));
    const busy = new Set<string>();
    const slots: Slot[] = [];
    GIORNI.forEach((g, di) => {
      classes.forEach((c, ci) => {
        if (stageFor(c.id, g)) return;
        let lastMateria: string | null = null;
        let run = 0;
        for (const h of ORE) {
          const cand = assignments
            .filter((a) => a.classe_id === c.id && (remaining.get(a.id) ?? 0) > 0)
            .filter((a) => !busy.has(`${a.docente_id}|${g}|${h}`))
            .filter((a) => isAvailable(week, teacher(a.docente_id), g, h, ignoreAvail.has(a.docente_id)))
            .sort((a, b) => {
              // keep practice blocks together, otherwise rotate by remaining hours
              const pa = a.materia_id === lastMateria && materia(a.materia_id).tipo === "PRATICA" && run < 4 ? -100 : 0;
              const pb = b.materia_id === lastMateria && materia(b.materia_id).tipo === "PRATICA" && run < 4 ? -100 : 0;
              const ra = (remaining.get(a.id) ?? 0) + (((seed + di + ci + a.id.length) * 7) % 3);
              const rb = (remaining.get(b.id) ?? 0) + (((seed + di + ci + b.id.length) * 7) % 3);
              return pa - pb || rb - ra;
            });
          const a = cand[0];
          if (!a) {
            lastMateria = null;
            run = 0;
            continue;
          }
          remaining.set(a.id, (remaining.get(a.id) ?? 0) - 1);
          busy.add(`${a.docente_id}|${g}|${h}`);
          run = a.materia_id === lastMateria ? run + 1 : 1;
          lastMateria = a.materia_id;
          slots.push(makeSlot(a, g, h));
        }
      });
    });
    return slots;
  }

  /** Recomputes conflicts, quality and slot flags (as backend does on every read). */
  function view(s: InternalSchedule): Schedule {
    const suppressed = new Set(s.deroghe.map((d) => d.action_type));
    const ignoreAvail = new Set(
      s.deroghe.filter((d) => d.action_type === "override_availability").map((d) => d.teacher_id),
    );
    const conflicts: Conflict[] = [];
    const slotKeys = new Map<string, string[]>();
    const add = (c: Omit<Conflict, "conflict_id">, slotIds: string[] = []) => {
      if (s.approvedKeys.has(c.chiave)) return;
      if (conflicts.some((x) => x.chiave === c.chiave)) return;
      conflicts.push({ ...c, conflict_id: newId("k") });
      slotIds.forEach((id) => slotKeys.set(id, [...(slotKeys.get(id) ?? []), c.chiave]));
    };
    const slots = s.slots.map((x) => ({ ...x }));

    for (const c of classes) {
      for (const g of GIORNI) {
        if (stageFor(c.id, g)) continue;
        const day = slots.filter((x) => x.classe_id === c.id && x.giorno === g).sort((a, b) => a.ora_inizio - b.ora_inizio);
        const missing = ORE.filter((h) => !day.some((x) => x.ora_inizio === h) && !s.freed.has(`${c.id}|${g}|${h}`));
        if (missing.length) {
          add({
            chiave: `classe_unfilled|${c.id}||${g}`,
            description: `${c.nome}: ${missing.length} ore non coperte ${g.toLowerCase()} (${missing.map(hourLabel).join(", ")})`,
            suggested_action: null,
            classe_id: c.id,
            docente_id: null,
            giorno: g,
            ore: missing.length,
          });
        }
        if (!suppressed.has("force_3_hours_theory")) {
          let run: Slot[] = [];
          for (const x of [...day, null]) {
            if (x && x.materia_tipo === "TEORIA" && (!run.length || run.at(-1)!.ora_inizio === x.ora_inizio - 1)) {
              run.push(x);
              continue;
            }
            if (run.length > 3) {
              add(
                {
                  chiave: `teoria_consecutive|${c.id}||${g}`,
                  description: `${c.nome}: ${run.length} ore di teoria consecutive ${g.toLowerCase()}`,
                  suggested_action: { action_type: "force_3_hours_theory", label: "Limita a 3 ore teoria" },
                  classe_id: c.id,
                  docente_id: null,
                  giorno: g,
                },
                run.map((r) => r.slot_id),
              );
            }
            run = x && x.materia_tipo === "TEORIA" ? [x] : [];
          }
        }
      }
    }

    for (const x of slots) {
      const t = teacher(x.docente_id);
      const doubles = slots.filter(
        (y) => y.docente_id === x.docente_id && y.giorno === x.giorno && y.ora_inizio === x.ora_inizio && !y.accoppiata,
      );
      if (doubles.length > 1) {
        add(
          {
            chiave: `double_booking||${t.id}|${x.giorno}|${x.ora_inizio}`,
            description: `${t.nome} è in ${doubles.length} classi alle ${hourLabel(x.ora_inizio)} di ${x.giorno.toLowerCase()}`,
            suggested_action: null,
            classe_id: x.classe_id,
            docente_id: t.id,
            giorno: x.giorno,
          },
          doubles.map((d) => d.slot_id),
        );
      }
      if (!isAvailable(s.week_start, t, x.giorno, x.ora_inizio, ignoreAvail.has(t.id))) {
        x.indisponibile = true;
        add(
          {
            chiave: `unavailable|${x.classe_id}|${t.id}|${x.giorno}|${x.ora_inizio}`,
            description: `${t.nome} non è disponibile alle ${hourLabel(x.ora_inizio)} di ${x.giorno.toLowerCase()}`,
            suggested_action: { action_type: "override_availability", label: "Ignora disponibilità" },
            classe_id: x.classe_id,
            docente_id: t.id,
            giorno: x.giorno,
          },
          [x.slot_id],
        );
      }
    }

    if (!suppressed.has("authorize_early_exit")) {
      for (const t of teachers.filter((t) => t.tipo === "CONTRATTO")) {
        for (const g of GIORNI) {
          const hrs = slots.filter((x) => x.docente_id === t.id && x.giorno === g).map((x) => x.ora_inizio).sort();
          if (hrs.length >= 2 && hrs.at(-1)! - hrs[0]! + 1 > hrs.length) {
            add({
              chiave: `contractor_gap||${t.id}|${g}`,
              description: `${t.nome} ha ore buche ${g.toLowerCase()}`,
              suggested_action: { action_type: "authorize_early_exit", label: "Autorizza ore buche" },
              classe_id: null,
              docente_id: t.id,
              giorno: g,
            });
          }
        }
      }
    }

    for (const x of slots) {
      const keys = slotKeys.get(x.slot_id) ?? [];
      x.conflitto_chiavi = keys;
      x.conflitto = keys.length > 0;
    }
    const hard = conflicts.filter((c) => /^(double_booking|unavailable|classe_unfilled)/.test(c.chiave)).length;
    const soft = conflicts.length - hard;
    const score = Math.max(0, Math.round((100 - hard * 6 - soft * 2) * 10) / 10);
    return {
      schedule_id: s.schedule_id,
      week_start: s.week_start,
      stato: s.stato,
      approved_at: s.approved_at,
      quality_score: score,
      quality_level: score >= 85 ? "A" : score >= 65 ? "B" : "C",
      n_soft_conflicts: soft,
      slots,
      conflicts,
      stage_cells: stageCells(),
    };
  }

  function getInternal(week: string) {
    const s = schedules.get(week);
    if (!s) throw new ApiError(404, "Orario non trovato per la settimana");
    return s;
  }
  function editable(week: string) {
    const s = getInternal(week);
    if (s.stato === "APPROVATO") throw new ApiError(400, "Orario approvato: non modificabile");
    return s;
  }

  let seedCounter = 0;
  function runGeneration(week: string, deroghe: InternalSchedule["deroghe"]): GenerateResult {
    if (!assignments.length) return { status: "error", message: "Nessuna assegnazione presente" };
    const existing = schedules.get(week);
    if (existing?.stato === "APPROVATO") throw new ApiError(400, "La settimana è già approvata");
    const s: InternalSchedule = {
      schedule_id: existing?.schedule_id ?? newId("o"),
      week_start: week,
      stato: "BOZZA",
      approved_at: null,
      slots: solve(week, seedCounter++, deroghe),
      approvedKeys: new Set(),
      freed: new Set(),
      deroghe,
    };
    schedules.set(week, s);
    const v = view(s);
    const unfilled = v.conflicts.some((c) => c.chiave.startsWith("classe_unfilled"));
    return {
      status: "generated",
      ...v,
      ...(unfilled ? { message: "Nessun orario completo possibile: restituito il miglior orario possibile" } : {}),
    };
  }

  function weeks(): Week[] {
    const out: Week[] = [];
    let m = firstMondayOnOrAfter(SCHOOL_YEAR.start);
    let n = 1;
    while (m <= SCHOOL_YEAR.end) {
      out.push({ start: m, end: addDays(m, 4), week_num: n++ });
      m = addDays(m, 7);
    }
    return out;
  }

  function exportRows(week: string): string[][] {
    const v = view(getInternal(week));
    const header = ["Giorno", "Ora", ...classes.map((c) => c.nome)];
    const rows = [header];
    for (const g of GIORNI) {
      for (const h of ORE) {
        rows.push([
          g,
          `${hourLabel(h)}-${hourLabel(h + 1)}`,
          ...classes.map((c) => {
            if (stageFor(c.id, g)) return "STAGE";
            const sl = v.slots.find((x) => x.classe_id === c.id && x.giorno === g && x.ora_inizio === h);
            return sl ? `${sl.materia_nome} (${sl.docente_nome})` : "";
          }),
        ]);
      }
    }
    return rows;
  }

  return {
    auth: {
      async login(email, password) {
        const u = MOCK_USERS.find((x) => x.email === email.trim().toLowerCase());
        if (!u || passwords.get(u.email) !== password) throw new ApiError(401, "Email o password non corretti");
        const token = `mock_${Math.random().toString(36).slice(2)}${Date.now()}`;
        tokens.set(token, { email: u.email, role: u.role });
        return wait({ status: "ok" as const, token, email: u.email, role: u.role });
      },
      async logout() {
        const tok = session.getToken();
        if (tok) tokens.delete(tok);
        return wait(undefined);
      },
      async me() {
        const u = current();
        return wait({ email: u.email, role: u.role });
      },
      async changePassword(cur, next) {
        const u = current();
        if (passwords.get(u.email) !== cur) throw new ApiError(400, "Password attuale errata");
        if (next.length < 8) throw new ApiError(400, "La nuova password deve avere almeno 8 caratteri");
        if (next === cur) throw new ApiError(400, "La nuova password deve essere diversa dall'attuale");
        passwords.set(u.email, next);
        for (const [k, v] of tokens) if (v.email === u.email && k !== u.token) tokens.delete(k);
        return wait(undefined);
      },
    },
    teachers: {
      async list() {
        current();
        return wait(clone(teachers));
      },
      async create(input) {
        requireAdmin();
        if (!input.nome.trim()) throw new ApiError(400, "Nome obbligatorio");
        const t = { ...input, id: newId("d") };
        teachers.push(t);
        return wait(clone(t));
      },
      async update(id, input) {
        requireAdmin();
        Object.assign(teacher(id), input);
        return wait(clone(teacher(id)));
      },
      async remove(id) {
        requireAdmin();
        teacher(id);
        teachers.splice(teachers.findIndex((t) => t.id === id), 1);
        for (let i = assignments.length - 1; i >= 0; i--) if (assignments[i]!.docente_id === id) assignments.splice(i, 1);
        return wait(undefined);
      },
    },
    classes: {
      async list() {
        current();
        return wait(clone(classes));
      },
      async create(input) {
        requireAdmin();
        if (!input.nome.trim()) throw new ApiError(400, "Nome obbligatorio");
        const c = { ...input, id: newId("c") };
        classes.push(c);
        return wait(clone(c));
      },
      async update(id, input) {
        requireAdmin();
        Object.assign(classe(id), input);
        return wait(clone(classe(id)));
      },
      async remove(id) {
        requireAdmin();
        classe(id);
        classes.splice(classes.findIndex((c) => c.id === id), 1);
        for (let i = assignments.length - 1; i >= 0; i--) if (assignments[i]!.classe_id === id) assignments.splice(i, 1);
        return wait(undefined);
      },
    },
    subjects: {
      async list() {
        current();
        return wait(clone(subjects));
      },
    },
    assignments: {
      async list() {
        current();
        return wait(clone(assignments));
      },
    },
    availability: {
      async weeks() {
        current();
        return wait(weeks());
      },
      async get(week, teacherId) {
        current();
        const r = resolveAvailability(week, teacher(teacherId));
        return wait({
          docente_id: teacherId,
          settimana_inizio: week,
          giorni_fasce: clone(r.grid),
          is_default: r.is_default,
          inherited_from: r.from && r.from !== week ? r.from : null,
        });
      },
      async save(week, teacherId, grid) {
        current();
        teacher(teacherId);
        for (const d of GIORNI_LOWER) {
          if (!Array.isArray(grid[d]) || grid[d].length === 0) throw new ApiError(422, `Giorno mancante: ${d}`);
        }
        availability.set(`${week}|${teacherId}`, clone(grid));
        return wait(undefined);
      },
      async copyFrom(week, prev) {
        current();
        let copied = 0;
        for (const t of teachers) {
          const src = availability.get(`${prev}|${t.id}`);
          if (src && !availability.has(`${week}|${t.id}`)) {
            availability.set(`${week}|${t.id}`, clone(src));
            copied++;
          }
        }
        return wait({ copied });
      },
      async status(week) {
        current();
        const active = teachers.filter((t) => t.active);
        const missing = active.filter((t) => !availability.has(`${week}|${t.id}`));
        const contractMissing = missing.filter((t) => t.tipo === "CONTRATTO");
        const withAv = active.length - missing.length;
        return wait({
          week_complete: missing.length === 0,
          total_teachers: active.length,
          teachers_with_availability: withAv,
          teachers_missing: missing.map((t) => t.nome),
          ready_to_generate: contractMissing.length === 0,
          completion_percentage: active.length ? Math.round((withAv / active.length) * 100) : 100,
        });
      },
    },
    schedule: {
      async get(week) {
        current();
        const s = schedules.get(week);
        return wait(s ? view(s) : null);
      },
      async generate(week) {
        requireAdmin();
        return wait(runGeneration(week, []));
      },
      async regenerate(week) {
        requireAdmin();
        editable(week);
        return wait(runGeneration(week, []));
      },
      async modifySlot(week, slotId, changes) {
        requireAdmin();
        const s = editable(week);
        const sl = s.slots.find((x) => x.slot_id === slotId);
        if (!sl) throw new ApiError(404, "Slot non trovato");
        if (changes.ora_inizio !== undefined && (changes.ora_inizio < 8 || changes.ora_inizio > 13))
          throw new ApiError(400, "ora_inizio deve essere tra 8 e 13");
        if (changes.giorno && !GIORNI.includes(changes.giorno)) throw new ApiError(400, "Giorno non valido");
        const g = changes.giorno ?? sl.giorno;
        const h = changes.ora_inizio ?? sl.ora_inizio;
        if (stageFor(sl.classe_id, g)) throw new ApiError(400, "La classe è in stage quel giorno");
        // swap with the class's lesson already occupying the target cell
        const other = s.slots.find((x) => x !== sl && x.classe_id === sl.classe_id && x.giorno === g && x.ora_inizio === h);
        if (other) {
          other.giorno = sl.giorno;
          other.ora_inizio = sl.ora_inizio;
          other.ora_fine = sl.ora_inizio + 1;
        }
        sl.giorno = g;
        sl.ora_inizio = h;
        sl.ora_fine = h + 1;
        if (changes.docente_id) {
          const t = teacher(changes.docente_id);
          sl.docente_id = t.id;
          sl.docente_nome = t.nome;
        }
        s.freed.delete(`${sl.classe_id}|${g}|${h}`);
        return wait(view(s));
      },
      async assignable(week, classeId) {
        current();
        getInternal(week);
        return wait(
          assignments
            .filter((a) => a.classe_id === classeId)
            .map((a) => ({
              docente_id: a.docente_id,
              docente_nome: teacher(a.docente_id).nome,
              materia_id: a.materia_id,
              materia_nome: materia(a.materia_id).nome,
            })),
        );
      },
      async assignSlot(week, input) {
        requireAdmin();
        const s = editable(week);
        if (stageFor(input.classe_id, input.giorno)) throw new ApiError(400, "La classe è in stage quel giorno");
        if (s.slots.some((x) => x.classe_id === input.classe_id && x.giorno === input.giorno && x.ora_inizio === input.ora_inizio))
          throw new ApiError(400, "Ora già occupata");
        const a =
          assignments.find(
            (x) => x.classe_id === input.classe_id && x.docente_id === input.docente_id && x.materia_id === input.materia_id,
          ) ?? { id: "manual", ...input, ore_settimanali: 0, ore_totali: 0, ore_erogate: 0 };
        s.slots.push(makeSlot(a, input.giorno, input.ora_inizio));
        s.freed.delete(`${input.classe_id}|${input.giorno}|${input.ora_inizio}`);
        return wait(view(s));
      },
      async approveConflicts(week, chiavi) {
        requireAdmin();
        const s = editable(week);
        chiavi.forEach((k) => s.approvedKeys.add(k));
        return wait(view(s));
      },
      async rejectConflict(week, input) {
        requireAdmin();
        const s = editable(week);
        s.slots = s.slots.filter(
          (x) => !(x.classe_id === input.classe_id && x.giorno === input.giorno && x.ora_inizio === input.ora_inizio),
        );
        s.freed.add(`${input.classe_id}|${input.giorno}|${input.ora_inizio}`);
        return wait(view(s));
      },
      async applyQuickAction(week, input) {
        requireAdmin();
        editable(week);
        return wait(runGeneration(week, [{ action_type: input.action_type, ...(input.teacher_id ? { teacher_id: input.teacher_id } : {}), ...(input.class_id ? { class_id: input.class_id } : {}) }]));
      },
      async approve(week) {
        requireAdmin();
        const s = getInternal(week);
        if (s.stato === "APPROVATO") {
          return wait({
            status: "approved" as const,
            schedule_id: s.schedule_id,
            stato: "APPROVATO" as const,
            ore_scalate: 0,
            assegnazioni_aggiornate: 0,
            senza_monte_ore: 0,
          });
        }
        let ore = 0;
        let senza = 0;
        const touched = new Set<string>();
        for (const sl of s.slots) {
          const a = assignments.find(
            (x) => x.classe_id === sl.classe_id && x.docente_id === sl.docente_id && x.materia_id === sl.materia_id,
          );
          if (!a) {
            senza++;
            continue;
          }
          a.ore_erogate += 1;
          ore++;
          touched.add(a.id);
        }
        s.stato = "APPROVATO";
        s.approved_at = new Date().toISOString();
        return wait({
          status: "approved" as const,
          schedule_id: s.schedule_id,
          stato: "APPROVATO" as const,
          ore_scalate: ore,
          assegnazioni_aggiornate: touched.size,
          senza_monte_ore: senza,
        });
      },
      async listFeedback(week) {
        current();
        return wait(clone(feedback.get(week) ?? []));
      },
      async sendFeedback(week, input) {
        requireAdmin();
        if (input.voto < 1 || input.voto > 5) throw new ApiError(422, "Voto tra 1 e 5");
        if ((input.nota ?? "").length > 2000) throw new ApiError(422, "Nota troppo lunga");
        const v = view(getInternal(week));
        const f: Feedback = {
          ...input,
          id: newId("f"),
          created_at: new Date().toISOString(),
          quality_score: v.quality_score,
          quality_level: v.quality_level,
        };
        feedback.set(week, [...(feedback.get(week) ?? []), f]);
        return wait(clone(f));
      },
      async exportPdf(week) {
        current();
        const rows = exportRows(week);
        const html = `<!doctype html><html><head><meta charset="utf-8"><title>Orario ${week}</title>
<style>@page{size:A4 landscape}body{font-family:sans-serif;font-size:10px}table{border-collapse:collapse;width:100%}td,th{border:1px solid #333;padding:3px}td.stage{background:#fde047}</style></head>
<body><h2>Orario settimana ${week} (mock)</h2><table>${rows
          .map(
            (r, i) =>
              `<tr>${r.map((c) => (i === 0 ? `<th>${c}</th>` : `<td${c === "STAGE" ? ' class="stage"' : ""}>${c}</td>`)).join("")}</tr>`,
          )
          .join("")}</table><script>print()</script></body></html>`;
        return wait({ filename: `orario_${week}.html`, blob: new Blob([html], { type: "text/html" }) });
      },
      async exportExcel(week) {
        current();
        const csv = exportRows(week)
          .map((r) => r.map((c) => `"${c.replace(/"/g, '""')}"`).join(";"))
          .join("\n");
        return wait({ filename: `orario_${week}.csv`, blob: new Blob([csv], { type: "text/csv" }) });
      },
    },
  };
}
