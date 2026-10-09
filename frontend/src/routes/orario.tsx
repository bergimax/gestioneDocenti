import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { AlertTriangle, Check, Download, Star, Wand2, X } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { AppShell } from "@/components/AppShell";
import { errMsg, WeekSelect } from "@/components/WeekSelect";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { api, GIORNI, ORE, type Conflict, type ExportFile, type GenerateResult, type Giorno, type Motivo, type Schedule, type Slot } from "@/services";
import { hourLabel } from "@/services/dates";

export const Route = createFileRoute("/orario")({
  head: () => ({
    meta: [
      { title: "Orario settimanale — Registro Orari" },
      { name: "description", content: "Genera, modifica e approva l'orario settimanale dei docenti." },
      { property: "og:title", content: "Orario settimanale — Registro Orari" },
      { property: "og:description", content: "Genera, modifica e approva l'orario settimanale dei docenti." },
    ],
  }),
  component: Page,
});

function Page() {
  const [week, setWeek] = useState<string | null>(null);
  return (
    <AppShell title="Orario" subtitle="Tabellone classi × ore" actions={<WeekSelect value={week} onChange={setWeek} />}>
      {(role) => (week ? <Orario key={week} week={week} admin={role === "ADMIN"} /> : null)}
    </AppShell>
  );
}

const MOTIVI: { code: Motivo; label: string }[] = [
  { code: "classe_late_start", label: "Ingressi posticipati" },
  { code: "friday_late_start", label: "Venerdì posticipato" },
  { code: "teoria_consecutive", label: "Troppa teoria di fila" },
  { code: "pratica_block", label: "Blocchi pratica spezzati" },
  { code: "contractor_gap", label: "Buchi contrattisti" },
  { code: "single_classe_day", label: "Docente in una sola classe" },
  { code: "ore_target_deviation", label: "Ore lontane dal target" },
  { code: "classe_unfilled", label: "Ore scoperte" },
  { code: "altro", label: "Altro" },
];

function download(f: ExportFile) {
  const url = URL.createObjectURL(f.blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = f.filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function Orario({ week, admin }: { week: string; admin: boolean }) {
  const qc = useQueryClient();
  const key = ["schedule", week];
  const q = useQuery({ queryKey: key, queryFn: () => api.schedule.get(week) });
  const [day, setDay] = useState<Giorno>("LUNEDI");
  const [hl, setHl] = useState<string | null>(null);
  const [edit, setEdit] = useState<{ slot: Slot | undefined; classe_id: string; ora: number } | null>(null);

  const setData = (s: Schedule) => qc.setQueryData(key, s);
  const onGen = (r: GenerateResult) => {
    if (r.status === "generated") {
      setData(r);
      r.message ? toast.warning(r.message) : toast.success("Orario generato");
    } else if (r.status === "infeasible") toast.error("Orario impossibile: " + r.conflicting_constraints.join(", "));
    else toast.error(r.message);
  };
  const m = <T,>(fn: () => Promise<T>, ok: (v: T) => void) =>
    useMutation({ mutationFn: fn, onSuccess: ok, onError: (e) => toast.error(errMsg(e)) });

  const generate = m(() => api.schedule.generate(week), onGen);
  const regenerate = m(() => api.schedule.regenerate(week), onGen);
  const approve = m(() => api.schedule.approve(week), (r) => {
    toast.success(`Settimana approvata · ${r.ore_scalate} ore scalate dal monte ore`);
    qc.invalidateQueries({ queryKey: key });
    qc.invalidateQueries({ queryKey: ["assignments"] });
  });

  const s = q.data;
  const locked = s?.stato === "APPROVATO";
  const canEdit = admin && !!s && !locked;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        {s && <Quality s={s} />}
        <div className="flex-1" />
        {admin && !locked && (
          <>
            <Button onClick={() => (s ? regenerate.mutate() : generate.mutate())} disabled={generate.isPending || regenerate.isPending}>
              <Wand2 className="mr-1 h-4 w-4" /> {s ? "Genera nuovo orario" : "Genera orario"}
            </Button>
            {s && <Button variant="outline" onClick={() => approve.mutate()} disabled={approve.isPending}><Check className="mr-1 h-4 w-4" /> Approva settimana</Button>}
          </>
        )}
        {s && (
          <>
            <Button variant="ghost" onClick={() => api.schedule.exportPdf(week).then(download).catch((e) => toast.error(errMsg(e)))}><Download className="mr-1 h-4 w-4" /> PDF</Button>
            <Button variant="ghost" onClick={() => api.schedule.exportExcel(week).then(download).catch((e) => toast.error(errMsg(e)))}><Download className="mr-1 h-4 w-4" /> Excel</Button>
          </>
        )}
      </div>

      {q.isLoading && <p className="text-muted-foreground">Caricamento…</p>}
      {q.isSuccess && !s && (
        <div className="rounded-lg border border-dashed border-border p-12 text-center text-muted-foreground">
          Nessun orario per questa settimana. {admin ? "Premi «Genera orario»." : "L'amministratore non l'ha ancora generato."}
        </div>
      )}

      {s && (
        <div className={cn("grid gap-5", canEdit && "xl:grid-cols-[1fr_340px]")}>
          <div className="space-y-3">
            <Tabs value={day} onValueChange={(v) => setDay(v as Giorno)}>
              <TabsList>{GIORNI.map((g) => <TabsTrigger key={g} value={g} className="capitalize">{g.toLowerCase()}</TabsTrigger>)}</TabsList>
            </Tabs>
            <Board s={s} day={day} hl={hl} setHl={setHl} onCell={canEdit ? (c, ora, slot) => setEdit({ classe_id: c, ora, slot }) : undefined} />
            <Legend />
            {admin && <FeedbackBox week={week} />}
          </div>
          {canEdit && <ConflictPanel week={week} s={s} setData={setData} onGen={onGen} />}
        </div>
      )}
      {edit && s && <EditDialog week={week} s={s} day={day} {...edit} onClose={() => setEdit(null)} onDone={setData} />}
    </div>
  );
}

function Quality({ s }: { s: Schedule }) {
  return (
    <div className="flex items-center gap-4 rounded-lg border border-border bg-card px-4 py-2 shadow-card">
      <div className={cn("font-display text-3xl font-bold", s.quality_level === "A" ? "text-success" : s.quality_level === "B" ? "text-accent" : "text-destructive")}>
        {s.quality_level}
      </div>
      <div className="text-sm">
        <div className="font-semibold">Qualità {s.quality_score}/100</div>
        <div className="text-muted-foreground">{s.conflicts.length} conflitti · {s.stato}</div>
      </div>
    </div>
  );
}

function Board({ s, day, hl, setHl, onCell }: {
  s: Schedule; day: Giorno; hl: string | null; setHl: (t: string | null) => void;
  onCell: ((classe_id: string, ora: number, slot?: Slot) => void) | undefined;
}) {
  const classes = [...new Map(s.slots.map((x) => [x.classe_id, x.classe_nome])).entries()];
  for (const st of s.stage_cells) if (!classes.some(([id]) => id === st.classe_id)) classes.push([st.classe_id, st.classe_nome]);
  return (
    <div className="overflow-x-auto rounded-lg border border-border bg-card shadow-card">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-border bg-muted">
            <th className="w-40 px-3 py-2 text-left font-medium">Classe</th>
            {ORE.map((h) => <th key={h} className="px-2 py-2 font-medium">{hourLabel(h)}</th>)}
          </tr>
        </thead>
        <tbody>
          {classes.map(([cid, nome]) => {
            const stage = s.stage_cells.some((x) => x.classe_id === cid && x.giorno === day);
            return (
              <tr key={cid} className="border-b border-border last:border-0">
                <td className="px-3 py-2 font-semibold">{nome}</td>
                {stage ? (
                  <td colSpan={ORE.length} className="p-1">
                    <div className="rounded-md border-2 border-stage-border bg-stage py-3 text-center font-bold tracking-widest text-stage-foreground">STAGE</div>
                  </td>
                ) : (
                  ORE.map((h) => {
                    const sl = s.slots.find((x) => x.classe_id === cid && x.giorno === day && x.ora_inizio === h);
                    return (
                      <td key={h} className="p-1">
                        <div
                          role={onCell ? "button" : undefined}
                          tabIndex={onCell ? 0 : undefined}
                          onClick={() => onCell?.(cid, h, sl)}
                          data-testid={`cell-${cid}-${h}`}
                          className={cn(
                            "relative min-h-14 rounded-md border p-1.5 text-xs leading-tight",
                            !sl && "border-dashed border-border text-muted-foreground",
                            sl?.materia_tipo === "TEORIA" && "border-transparent bg-teoria text-teoria-foreground",
                            sl?.materia_tipo === "PRATICA" && "border-transparent bg-pratica text-pratica-foreground",
                            sl?.conflitto && "ring-2 ring-conflict",
                            hl && sl && sl.docente_id !== hl && "opacity-30",
                            hl && sl?.docente_id === hl && "ring-2 ring-highlight",
                            onCell && "cursor-pointer hover:brightness-95",
                          )}
                        >
                          {sl ? (
                            <>
                              <div className="font-semibold">{sl.materia_nome}</div>
                              <button
                                className="mt-0.5 text-left underline-offset-2 hover:underline"
                                onClick={(e) => { e.stopPropagation(); setHl(hl === sl.docente_id ? null : sl.docente_id); }}
                              >
                                {sl.docente_nome}
                              </button>
                              {sl.conflitto && <AlertTriangle className="absolute right-1 top-1 h-3 w-3 text-conflict" />}
                            </>
                          ) : (
                            "libera"
                          )}
                        </div>
                      </td>
                    );
                  })
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-4 text-xs text-muted-foreground">
      <span className="flex items-center gap-1"><i className="h-3 w-3 rounded bg-teoria" /> Teoria</span>
      <span className="flex items-center gap-1"><i className="h-3 w-3 rounded bg-pratica" /> Pratica</span>
      <span className="flex items-center gap-1"><i className="h-3 w-3 rounded bg-stage" /> Stage</span>
      <span className="flex items-center gap-1"><i className="h-3 w-3 rounded ring-2 ring-conflict" /> Conflitto</span>
      <span>Clic sul nome del docente per evidenziarne le ore</span>
    </div>
  );
}

function ConflictPanel({ week, s, setData, onGen }: { week: string; s: Schedule; setData: (s: Schedule) => void; onGen: (r: GenerateResult) => void }) {
  const ok = { onSuccess: setData, onError: (e: unknown) => toast.error(errMsg(e)) };
  const approveC = useMutation({ mutationFn: (k: string[]) => api.schedule.approveConflicts(week, k), ...ok });
  const quick = useMutation({
    mutationFn: (c: Conflict) =>
      api.schedule.applyQuickAction(week, {
        action_type: c.suggested_action!.action_type,
        ...(c.docente_id ? { teacher_id: c.docente_id } : {}),
        ...(c.classe_id ? { class_id: c.classe_id } : {}),
        ...(c.giorno ? { giorno: c.giorno } : {}),
      }),
    onSuccess: onGen,
    onError: (e) => toast.error(errMsg(e)),
  });
  return (
    <aside className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-bold">Conflitti ({s.conflicts.length})</h3>
        {s.conflicts.length > 0 && (
          <Button size="sm" variant="ghost" onClick={() => approveC.mutate(s.conflicts.map((c) => c.chiave))}>Accetta tutti</Button>
        )}
      </div>
      {s.conflicts.length === 0 && <p className="text-sm text-muted-foreground">Nessun conflitto. Ottimo lavoro.</p>}
      <ul className="max-h-[70vh] space-y-2 overflow-y-auto pr-1">
        {s.conflicts.map((c) => (
          <li key={c.chiave} className="rounded-md border border-border bg-card p-3 text-sm">
            <p>{c.description}</p>
            <div className="mt-2 flex flex-wrap gap-2">
              <Button size="sm" variant="outline" onClick={() => approveC.mutate([c.chiave])}>Accetta</Button>
              {c.suggested_action && (
                <Button size="sm" variant="secondary" onClick={() => quick.mutate(c)} disabled={quick.isPending}>
                  {c.suggested_action.label}
                </Button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </aside>
  );
}

function EditDialog({ week, s, day, slot, classe_id, ora, onClose, onDone }: {
  week: string; s: Schedule; day: Giorno; slot: Slot | undefined; classe_id: string; ora: number;
  onClose: () => void; onDone: (s: Schedule) => void;
}) {
  const assignable = useQuery({ queryKey: ["assignable", week, classe_id], queryFn: () => api.schedule.assignable(week, classe_id) });
  const [giorno, setGiorno] = useState<Giorno>(day);
  const [h, setH] = useState(String(ora));
  const [pair, setPair] = useState<string>(slot ? `${slot.docente_id}|${slot.materia_id}` : "");
  const done = { onSuccess: (r: Schedule) => { onDone(r); onClose(); toast.success("Orario aggiornato"); }, onError: (e: unknown) => toast.error(errMsg(e)) };
  const save = useMutation({
    mutationFn: () => {
      const [docente_id = "", materia_id = ""] = pair.split("|");
      if (slot) return api.schedule.modifySlot(week, slot.slot_id, { giorno, ora_inizio: Number(h), docente_id });
      return api.schedule.assignSlot(week, { classe_id, giorno: day, ora_inizio: ora, docente_id, materia_id });
    },
    ...done,
  });
  const free = useMutation({ mutationFn: () => api.schedule.rejectConflict(week, { classe_id, giorno: day, ora_inizio: ora }), ...done });
  const nome = s.slots.find((x) => x.classe_id === classe_id)?.classe_nome ?? "";
  const pairs = assignable.data ?? [];
  const opts = slot ? pairs.filter((p) => p.materia_id === slot.materia_id) : pairs;

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>{slot ? "Sposta lezione" : "Assegna ora libera"} · {nome}, {day.toLowerCase()} {hourLabel(ora)}</DialogTitle>
        </DialogHeader>
        <div className="space-y-3">
          <Select value={pair} onValueChange={setPair}>
            <SelectTrigger aria-label="Docente e materia"><SelectValue placeholder="Docente / materia" /></SelectTrigger>
            <SelectContent>
              {opts.map((p) => <SelectItem key={`${p.docente_id}|${p.materia_id}`} value={`${p.docente_id}|${p.materia_id}`}>{p.materia_nome} — {p.docente_nome}</SelectItem>)}
            </SelectContent>
          </Select>
          {slot && (
            <div className="grid grid-cols-2 gap-3">
              <Select value={giorno} onValueChange={(v) => setGiorno(v as Giorno)}>
                <SelectTrigger aria-label="Giorno"><SelectValue /></SelectTrigger>
                <SelectContent>{GIORNI.map((g) => <SelectItem key={g} value={g}>{g}</SelectItem>)}</SelectContent>
              </Select>
              <Select value={h} onValueChange={setH}>
                <SelectTrigger aria-label="Ora"><SelectValue /></SelectTrigger>
                <SelectContent>{ORE.map((o) => <SelectItem key={o} value={String(o)}>{hourLabel(o)}</SelectItem>)}</SelectContent>
              </Select>
            </div>
          )}
          <div className="flex justify-between gap-2 pt-2">
            {slot ? <Button variant="ghost" onClick={() => free.mutate()}><X className="mr-1 h-4 w-4" /> Libera ora</Button> : <span />}
            <Button onClick={() => save.mutate()} disabled={!pair || save.isPending}>Conferma</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function FeedbackBox({ week }: { week: string }) {
  const qc = useQueryClient();
  const list = useQuery({ queryKey: ["feedback", week], queryFn: () => api.schedule.listFeedback(week) });
  const [voto, setVoto] = useState(0);
  const [motivi, setMotivi] = useState<Motivo[]>([]);
  const [nota, setNota] = useState("");
  const send = useMutation({
    mutationFn: () => api.schedule.sendFeedback(week, { voto, motivi, nota }),
    onSuccess: () => {
      toast.success("Valutazione registrata");
      setVoto(0); setMotivi([]); setNota("");
      qc.invalidateQueries({ queryKey: ["feedback", week] });
    },
    onError: (e) => toast.error(errMsg(e)),
  });
  return (
    <div className="rounded-lg border border-border bg-card p-4 shadow-card">
      <div className="flex items-center justify-between">
        <h3 className="text-lg font-bold">Valuta l'orario</h3>
        <span className="text-xs text-muted-foreground">{list.data?.length ?? 0} valutazioni</span>
      </div>
      <div className="mt-2 flex gap-1">
        {[1, 2, 3, 4, 5].map((n) => (
          <button key={n} aria-label={`${n} stelle`} onClick={() => setVoto(n)}>
            <Star className={cn("h-6 w-6", n <= voto ? "fill-accent text-accent" : "text-border")} />
          </button>
        ))}
      </div>
      {voto > 0 && voto <= 3 && (
        <div className="mt-3 flex flex-wrap gap-2">
          {MOTIVI.map((mo) => (
            <button
              key={mo.code}
              onClick={() => setMotivi((x) => (x.includes(mo.code) ? x.filter((y) => y !== mo.code) : [...x, mo.code]))}
              className={cn("rounded-full border px-3 py-1 text-xs", motivi.includes(mo.code) ? "border-primary bg-primary text-primary-foreground" : "border-border")}
            >
              {mo.label}
            </button>
          ))}
        </div>
      )}
      {voto > 0 && (
        <div className="mt-3 space-y-2">
          <Textarea maxLength={2000} placeholder="Nota (facoltativa)" value={nota} onChange={(e) => setNota(e.target.value)} />
          <Button size="sm" onClick={() => send.mutate()} disabled={send.isPending}>Invia valutazione</Button>
        </div>
      )}
    </div>
  );
}
