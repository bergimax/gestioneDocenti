import { useQuery } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { CalendarDays, Database, UserCheck } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { useWeeks } from "@/components/WeekSelect";
import { api } from "@/services";

export const Route = createFileRoute("/menu")({
  head: () => ({
    meta: [
      { title: "Panoramica — Registro Orari" },
      { name: "description", content: "Stato della settimana: disponibilità dei docenti e orario." },
      { property: "og:title", content: "Panoramica — Registro Orari" },
      { property: "og:description", content: "Stato della settimana corrente." },
    ],
  }),
  component: Menu,
});

function Menu() {
  return (
    <AppShell title="Panoramica" subtitle="Lo stato della prima settimana dell'anno formativo">
      {(role) => <Overview admin={role === "ADMIN"} />}
    </AppShell>
  );
}

function Overview({ admin }: { admin: boolean }) {
  const { data: weeks } = useWeeks();
  const week = weeks?.[0]?.start;
  const status = useQuery({ queryKey: ["av-status", week], queryFn: () => api.availability.status(week!), enabled: !!week });
  const sched = useQuery({ queryKey: ["schedule", week], queryFn: () => api.schedule.get(week!), enabled: !!week });
  const cards = [
    { to: "/disponibilita", icon: UserCheck, title: "Disponibilità", text: status.data ? `${status.data.completion_percentage}% dei docenti compilati` : "…" },
    { to: "/orario", icon: CalendarDays, title: "Orario", text: sched.data ? `${sched.data.stato} · qualità ${sched.data.quality_level}` : "Non ancora generato" },
    ...(admin ? [{ to: "/dati", icon: Database, title: "Dati scuola", text: "Docenti e classi" }] : []),
  ] as const;
  return (
    <div className="grid gap-5 md:grid-cols-3">
      {cards.map((c) => (
        <Link key={c.to} to={c.to} className="group rounded-lg border border-border bg-card p-6 shadow-card transition hover:-translate-y-0.5">
          <c.icon className="h-6 w-6 text-accent" />
          <h3 className="mt-4 text-xl font-bold">{c.title}</h3>
          <p className="mt-1 text-sm text-muted-foreground">{c.text}</p>
        </Link>
      ))}
    </div>
  );
}
