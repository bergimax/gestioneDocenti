import { beforeEach, describe, expect, it } from "vitest";
import { createMockApi, SCHOOL_YEAR, defaultGrid } from "@/services/mock";
import { session } from "@/services/session";
import { addDays, firstMondayOnOrAfter, mondayOf } from "@/services/dates";
import { ApiError, type ApiService } from "@/services";

const W = SCHOOL_YEAR.start;

async function loginAs(api: ApiService, email: string) {
  const r = await api.auth.login(email, "password123");
  session.set(r.token, r.role, r.email);
  return r;
}

describe("dates", () => {
  it("computes mondays", () => {
    expect(mondayOf("2026-09-17")).toBe("2026-09-14");
    expect(firstMondayOnOrAfter("2026-09-15")).toBe("2026-09-21");
    expect(addDays("2026-12-31", 1)).toBe("2027-01-01");
  });
});

describe("mock api", () => {
  let api: ApiService;
  beforeEach(() => {
    session.clear();
    api = createMockApi();
  });

  it("rejects wrong credentials and unauthenticated calls", async () => {
    await expect(api.auth.login("admin@scuola.it", "nope")).rejects.toMatchObject({ status: 401 });
    await expect(api.teachers.list()).rejects.toBeInstanceOf(ApiError);
  });

  it("logs in and returns role", async () => {
    const r = await loginAs(api, "admin@scuola.it");
    expect(r.role).toBe("ADMIN");
    expect(await api.auth.me()).toEqual({ email: "admin@scuola.it", role: "ADMIN" });
  });

  it("validates password change", async () => {
    await loginAs(api, "admin@scuola.it");
    await expect(api.auth.changePassword("wrong", "newpassword")).rejects.toMatchObject({ status: 400 });
    await expect(api.auth.changePassword("password123", "short")).rejects.toMatchObject({ status: 400 });
    await api.auth.changePassword("password123", "newpassword1");
    await expect(api.auth.login("admin@scuola.it", "newpassword1")).resolves.toMatchObject({ status: "ok" });
  });

  it("forbids schedule writes for segreteria but allows availability", async () => {
    await loginAs(api, "segreteria@scuola.it");
    await expect(api.schedule.generate(W)).rejects.toMatchObject({ status: 403 });
    await expect(api.teachers.create({ nome: "X", tipo: "ASSUNTO", email: null, active: true, first_time_this_year: true })).rejects.toMatchObject({ status: 403 });
    await api.availability.save(W, "d1", defaultGrid());
    expect((await api.availability.get(W, "d1")).is_default).toBe(false);
  });

  it("lists weeks of the school year starting on mondays", async () => {
    await loginAs(api, "admin@scuola.it");
    const weeks = await api.availability.weeks();
    expect(weeks[0]).toEqual({ start: W, end: addDays(W, 4), week_num: 1 });
    expect(weeks.at(-1)!.start <= SCHOOL_YEAR.end).toBe(true);
  });

  it("inherits availability from previous week and copies grids", async () => {
    await loginAs(api, "admin@scuola.it");
    const next = addDays(W, 7);
    const inherited = await api.availability.get(next, "d5");
    expect(inherited.inherited_from).toBe(W);
    expect(inherited.giorni_fasce.venerdi.every((f) => !f.disponibile)).toBe(true);
    const r = await api.availability.copyFrom(next, W);
    expect(r.copied).toBe(2);
    const st = await api.availability.status(next);
    expect(st.teachers_with_availability).toBe(2);
  });

  it("generates a schedule respecting hard constraints", async () => {
    await loginAs(api, "admin@scuola.it");
    const r = await api.schedule.generate(W);
    expect(r.status).toBe("generated");
    if (r.status !== "generated") return;
    // no teacher double-booked
    const keys = r.slots.map((s) => `${s.docente_id}|${s.giorno}|${s.ora_inizio}`);
    expect(new Set(keys).size).toBe(keys.length);
    // no lessons for class in stage
    expect(r.slots.some((s) => s.classe_id === "c3" && s.giorno === "GIOVEDI")).toBe(false);
    expect(r.stage_cells).toContainEqual(expect.objectContaining({ classe_id: "c3", giorno: "GIOVEDI" }));
    // contractor d5 never on friday (unavailable)
    expect(r.slots.some((s) => s.docente_id === "d5" && s.giorno === "VENERDI")).toBe(false);
    expect(["A", "B", "C"]).toContain(r.quality_level);
  });

  it("approves conflicts, frees slots and locks after approval", async () => {
    await loginAs(api, "admin@scuola.it");
    const r = await api.schedule.generate(W);
    if (r.status !== "generated") throw new Error();
    if (r.conflicts.length) {
      const after = await api.schedule.approveConflicts(W, [r.conflicts[0]!.chiave]);
      expect(after.conflicts.find((c) => c.chiave === r.conflicts[0]!.chiave)).toBeUndefined();
    }
    const sl = r.slots[0]!;
    const freed = await api.schedule.rejectConflict(W, { classe_id: sl.classe_id, giorno: sl.giorno, ora_inizio: sl.ora_inizio });
    expect(freed.slots.find((s) => s.slot_id === sl.slot_id)).toBeUndefined();

    const before = (await api.assignments.list()).reduce((a, x) => a + x.ore_erogate, 0);
    const ap = await api.schedule.approve(W);
    expect(ap.stato).toBe("APPROVATO");
    const after = (await api.assignments.list()).reduce((a, x) => a + x.ore_erogate, 0);
    expect(after - before).toBe(ap.ore_scalate);
    // idempotent
    expect((await api.schedule.approve(W)).ore_scalate).toBe(0);
    await expect(api.schedule.regenerate(W)).rejects.toMatchObject({ status: 400 });
  });

  it("moves a slot by swapping and validates hour", async () => {
    await loginAs(api, "admin@scuola.it");
    const r = await api.schedule.generate(W);
    if (r.status !== "generated") throw new Error();
    const sl = r.slots.find((s) => s.giorno === "LUNEDI" && s.ora_inizio === 8)!;
    await expect(api.schedule.modifySlot(W, sl.slot_id, { ora_inizio: 14 })).rejects.toMatchObject({ status: 400 });
    const m = await api.schedule.modifySlot(W, sl.slot_id, { ora_inizio: 9 });
    expect(m.slots.find((s) => s.slot_id === sl.slot_id)!.ora_inizio).toBe(9);
  });

  it("stores feedback and validates voto", async () => {
    await loginAs(api, "admin@scuola.it");
    await api.schedule.generate(W);
    await expect(api.schedule.sendFeedback(W, { voto: 6, motivi: [] })).rejects.toMatchObject({ status: 422 });
    await api.schedule.sendFeedback(W, { voto: 2, motivi: ["contractor_gap"] });
    expect(await api.schedule.listFeedback(W)).toHaveLength(1);
  });

  it("exports files", async () => {
    await loginAs(api, "admin@scuola.it");
    await api.schedule.generate(W);
    const f = await api.schedule.exportExcel(W);
    expect(f.filename).toContain(W);
    const text = await new Promise<string>((res) => {
      const r = new FileReader();
      r.onload = () => res(String(r.result));
      r.readAsText(f.blob);
    });
    expect(text).toContain("STAGE");
  });
});
