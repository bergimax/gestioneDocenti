import { createFileRoute, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, isMockApi, session } from "@/services";

export const Route = createFileRoute("/")({
  head: () => ({
    meta: [
      { title: "Accesso — Registro Orari" },
      { name: "description", content: "Accedi alla piattaforma di gestione degli orari dei docenti." },
      { property: "og:title", content: "Accesso — Registro Orari" },
      { property: "og:description", content: "Piattaforma per generare e approvare l'orario settimanale dei docenti." },
    ],
  }),
  component: Login,
});

export function Login() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const r = await api.auth.login(email, password);
      session.set(r.token, r.role, r.email);
      navigate({ to: "/menu" });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Errore");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="grid min-h-screen lg:grid-cols-2">
      <div className="paper-grid hidden flex-col justify-between bg-secondary p-12 lg:flex">
        <div className="font-display text-2xl font-bold text-primary">Registro Orari</div>
        <div>
          <h1 className="text-5xl font-bold leading-tight text-primary">
            L'orario della settimana,<br />
            <span className="text-accent">senza incastri.</span>
          </h1>
          <p className="mt-4 max-w-md text-muted-foreground">
            Disponibilità dei docenti, generazione automatica, conflitti e approvazione in un solo posto.
          </p>
        </div>
        <div className="text-xs text-muted-foreground">Agenzia Formativa · Anno 2026-2027</div>
      </div>
      <div className="flex items-center justify-center p-8">
        <form onSubmit={submit} className="w-full max-w-sm space-y-5">
          <h2 className="text-3xl font-bold">Accedi</h2>
          <div className="space-y-2">
            <Label htmlFor="email">Email</Label>
            <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
          </div>
          <div className="space-y-2">
            <Label htmlFor="password">Password</Label>
            <Input id="password" type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
          </div>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          <Button type="submit" className="w-full" disabled={loading}>
            {loading ? "Accesso…" : "Entra"}
          </Button>
          {isMockApi && (
            <div className="rounded-md border border-border bg-card p-3 text-xs text-muted-foreground">
              Demo: <b>admin@scuola.it</b> o <b>segreteria@scuola.it</b>, password <b>password123</b>
            </div>
          )}
        </form>
      </div>
    </div>
  );
}
