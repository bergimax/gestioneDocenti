import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { AppShell } from "@/components/AppShell";
import { errMsg, useWeeks, WeekSelect } from "@/components/WeekSelect";
import { Button } from "@/components/ui/button";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { api, GIORNI_LOWER, type GiorniFasce } from "@/services";
import { addDays } from "@/services/dates";

export const Route = createFileRoute("/disponibilita")({
  head: () => ({
    meta: [
      { title: "Disponibilità docenti — Registro Orari" },
      { name: "description", content: "Griglia settimanale delle disponibilità di ogni docente." },
      { property: "og:title", content: "Disponibilità docenti — Registro Orari" },
      { property: "og:description", content: "Griglia settimanale delle disponibilità di ogni docente." },
    ],
  }),
  component: Page,
});

function Page() {
  const [week, setWeek] = useState<string | null>(null);
  return (
    <AppShell title="Disponibilità" subtitle="Fasce orarie in cui ogni docente può insegnare" actions={<WeekSelect value={week} onChange={setWeek} />}>
      {() => (week ? <Availability week={week} /> : null)}
    </AppShell>
  );
}

function Availability({ week }: { week: string }) {
  const qc = useQueryClient();
  const { data: weeks = [] } = useWeeks();
  const teachers = useQuery({ queryKey: ["teachers"], queryFn: () => api.teachers.list() });
  const status = useQuery({ queryKey: ["av-status", week], queryFn: () => api.availability.status(week) });
  const [tid, setTid] = useState<string | null>(null);
  useEffect(() => {
    if (!tid && teachers.data?.length) setTid(teachers.data[0]!.id);
  }, [tid, teachers.data]);

  const prev = addDays(week, -7);
  const hasPrev = weeks.some((w) => w.start === prev);
  const copy = useMutation({
    mutationFn: () => api.availability.copyFrom(week, prev),
    onSuccess: (r) => {
      toast.success(`Copiate ${r.copied} griglie`);
      qc.invalidateQueries({ queryKey: ["av-status"] });
      qc.invalidateQueries({ queryKey: ["availability"] });
    },
    onError: (e) => toast.error(errMsg(e)),
  });

  return (
    <div className="grid gap-6 lg:grid-cols-[260px_1fr]">
      <div className="space-y-4">
        {status.data && (
          <div className="rounded-lg border border-border bg-card p-4 shadow-card">
            <div className="text-sm font-semibold">Completamento {status.data.completion_percentage}%</div>
            <Progress value={status.data.completion_percentage} className="mt-2" />
            <div className="mt-2 text-xs text-muted-foreground">
              {status.data.teachers_with_availability}/{status.data.total_teachers} docenti ·{" "}
              {status.data.ready_to_generate ? "pronto per generare" : "mancano contrattisti"}
            </div>
          </div>
        )}
        <Button variant="outline" className="w-full" disabled={!hasPrev || copy.isPending} onClick={() => copy.mutate()}>
          Copia dalla settimana precedente
        </Button>
        <ul className="divide-y divide-border rounded-lg border border-border bg-card">
          {teachers.data?.map((t) => (
            <li key={t.id}>
              <button
                onClick={() => setTid(t.id)}
                className={cn("flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-muted", tid === t.id && "bg-muted font-semibold")}
              >
                {t.nome}
                <span className={cn("text-[10px] uppercase", t.tipo === "CONTRATTO" ? "text-accent" : "text-muted-foreground")}>{t.tipo}</span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      {tid && <Grid key={`${week}-${tid}`} week={week} teacherId={tid} />}
    </div>
  );
}

function Grid({ week, teacherId }: { week: string; teacherId: string }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["availability", week, teacherId], queryFn: () => api.availability.get(week, teacherId) });
  const [grid, setGrid] = useState<GiorniFasce | null>(null);
  useEffect(() => {
    if (q.data) setGrid(q.data.giorni_fasce);
  }, [q.data]);
  const save = useMutation({
    mutationFn: (g: GiorniFasce) => api.availability.save(week, teacherId, g),
    onSuccess: () => {
      toast.success("Disponibilità salvata");
      qc.invalidateQueries({ queryKey: ["av-status", week] });
      qc.invalidateQueries({ queryKey: ["availability", week, teacherId] });
    },
    onError: (e) => toast.error(errMsg(e)),
  });
  if (!grid) return <div className="text-muted-foreground">Caricamento…</div>;
  const toggle = (d: (typeof GIORNI_LOWER)[number], i: number) =>
    setGrid({ ...grid, [d]: grid[d].map((f, j) => (j === i ? { ...f, disponibile: !f.disponibile } : f)) });

  return (
    <div className="rounded-lg border border-border bg-card p-5 shadow-card">
      <div className="mb-4 flex items-center justify-between">
        <div className="text-sm text-muted-foreground">
          {q.data?.is_default ? "Griglia predefinita (tutto disponibile)" : q.data?.inherited_from ? `Ereditata dalla settimana del ${q.data.inherited_from}` : "Griglia salvata"}
        </div>
        <Button onClick={() => save.mutate(grid)} disabled={save.isPending}>Salva</Button>
      </div>
      <table className="w-full border-separate border-spacing-1 text-sm">
        <thead>
          <tr>
            <th />
            {GIORNI_LOWER.map((d) => <th key={d} className="pb-1 font-medium capitalize">{d}</th>)}
          </tr>
        </thead>
        <tbody>
          {grid.lunedi.map((f, i) => (
            <tr key={i}>
              <td className="pr-2 text-xs text-muted-foreground">{f.ora_inizio}</td>
              {GIORNI_LOWER.map((d) => (
                <td key={d}>
                  <button
                    aria-label={`${d} ${grid[d][i]!.ora_inizio}`}
                    aria-pressed={grid[d][i]!.disponibile}
                    onClick={() => toggle(d, i)}
                    className={cn(
                      "h-10 w-full rounded-md border text-xs transition",
                      grid[d][i]!.disponibile ? "border-success bg-success text-success-foreground" : "border-border bg-muted text-muted-foreground",
                    )}
                  >
                    {grid[d][i]!.disponibile ? "Sì" : "No"}
                  </button>
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
