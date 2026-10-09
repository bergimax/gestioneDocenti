import { createFileRoute } from "@tanstack/react-router";
import { useState } from "react";
import { toast } from "sonner";
import { AppShell } from "@/components/AppShell";
import { errMsg } from "@/components/WeekSelect";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api } from "@/services";

export const Route = createFileRoute("/account")({
  head: () => ({
    meta: [
      { title: "Cambia password — Registro Orari" },
      { name: "description", content: "Aggiorna la password del tuo account." },
      { property: "og:title", content: "Cambia password — Registro Orari" },
      { property: "og:description", content: "Aggiorna la password del tuo account." },
    ],
  }),
  component: Account,
});

function Account() {
  const [cur, setCur] = useState("");
  const [next, setNext] = useState("");
  const [conf, setConf] = useState("");
  const [err, setErr] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setErr(null);
    if (next.length < 8) return setErr("La nuova password deve avere almeno 8 caratteri");
    if (next !== conf) return setErr("Le password non coincidono");
    try {
      await api.auth.changePassword(cur, next);
      toast.success("Password aggiornata");
      setCur(""); setNext(""); setConf("");
    } catch (e) {
      setErr(errMsg(e));
    }
  }

  return (
    <AppShell title="Cambia password">
      {() => (
        <form onSubmit={submit} className="max-w-sm space-y-4">
          {[["Password attuale", cur, setCur], ["Nuova password", next, setNext], ["Conferma", conf, setConf]].map(([l, v, s]) => (
            <div key={l as string} className="space-y-2">
              <Label htmlFor={l as string}>{l as string}</Label>
              <Input id={l as string} type="password" value={v as string} onChange={(e) => (s as (x: string) => void)(e.target.value)} required />
            </div>
          ))}
          {err && <p role="alert" className="text-sm text-destructive">{err}</p>}
          <Button type="submit">Aggiorna</Button>
        </form>
      )}
    </AppShell>
  );
}
