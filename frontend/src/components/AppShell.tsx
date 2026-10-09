import { Link, useNavigate } from "@tanstack/react-router";
import { CalendarDays, Database, KeyRound, LayoutGrid, LogOut, UserCheck } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { api, isMockApi, session, type Role } from "@/services";

const NAV = [
  { to: "/menu", label: "Panoramica", icon: LayoutGrid, admin: false },
  { to: "/disponibilita", label: "Disponibilità", icon: UserCheck, admin: false },
  { to: "/orario", label: "Orario", icon: CalendarDays, admin: false },
  { to: "/dati", label: "Dati scuola", icon: Database, admin: true },
] as const;

/** Client-side guard: redirects to login when no session token. */
export function useSessionRole(): Role | null {
  const navigate = useNavigate();
  const [role, setRole] = useState<Role | null>(null);
  useEffect(() => {
    const r = session.getRole();
    if (!session.getToken() || !r) navigate({ to: "/" });
    else setRole(r);
  }, [navigate]);
  return role;
}

export function AppShell({ title, subtitle, actions, children }: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: (role: Role) => ReactNode;
}) {
  const role = useSessionRole();
  const navigate = useNavigate();
  if (!role) return <div className="min-h-screen bg-background" />;
  const seg = role === "SEGRETERIA";

  async function logout() {
    await api.auth.logout().catch(() => {});
    session.clear();
    navigate({ to: "/" });
  }

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="sticky top-0 flex h-screen w-60 shrink-0 flex-col bg-sidebar text-sidebar-foreground">
        <div className="border-b border-sidebar-border px-5 py-6">
          <div className="font-display text-xl font-bold">Registro Orari</div>
          <div className="mt-1 text-xs opacity-70">Agenzia Formativa · 2026-27</div>
        </div>
        <nav className="flex-1 space-y-1 p-3">
          {NAV.filter((n) => !(seg && n.admin)).map((n) => (
            <Link
              key={n.to}
              to={n.to}
              className="flex items-center gap-3 rounded-md px-3 py-2 text-sm transition-colors hover:bg-sidebar-accent"
              activeProps={{ className: "bg-sidebar-accent text-sidebar-primary font-semibold" }}
            >
              <n.icon className="h-4 w-4" /> {n.label}
            </Link>
          ))}
        </nav>
        <div className="space-y-1 border-t border-sidebar-border p-3 text-sm">
          <div className="px-3 pb-2 text-xs opacity-70">
            {session.getEmail()}
            <div className="font-semibold text-sidebar-primary">
              {seg ? "Segreteria / Sola lettura" : "Amministratore"}
            </div>
          </div>
          <Link to="/account" className="flex items-center gap-3 rounded-md px-3 py-2 hover:bg-sidebar-accent">
            <KeyRound className="h-4 w-4" /> Cambia password
          </Link>
          <button onClick={logout} className="flex w-full items-center gap-3 rounded-md px-3 py-2 hover:bg-sidebar-accent">
            <LogOut className="h-4 w-4" /> Esci
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <header className="flex flex-wrap items-end justify-between gap-4 border-b border-border px-8 py-6">
          <div>
            <h1 className="text-3xl font-bold text-foreground">{title}</h1>
            {subtitle && <p className="mt-1 text-sm text-muted-foreground">{subtitle}</p>}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {isMockApi && (
              <span className="rounded-full border border-accent px-2 py-0.5 text-xs text-accent">Dati simulati</span>
            )}
            {actions}
          </div>
        </header>
        <div className="px-8 py-6">{children(role)}</div>
      </main>
    </div>
  );
}
