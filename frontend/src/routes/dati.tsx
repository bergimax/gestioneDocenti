import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { Trash2 } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { AppShell } from "@/components/AppShell";
import { errMsg } from "@/components/WeekSelect";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { api, type TipoDocente } from "@/services";

export const Route = createFileRoute("/dati")({
  head: () => ({
    meta: [
      { title: "Dati scuola — Registro Orari" },
      { name: "description", content: "Docenti, classi e assegnazioni con monte ore." },
      { property: "og:title", content: "Dati scuola — Registro Orari" },
      { property: "og:description", content: "Docenti, classi e assegnazioni con monte ore." },
    ],
  }),
  component: Page,
});

function Page() {
  return (
    <AppShell title="Dati scuola" subtitle="Anagrafiche e monte ore">
      {(role) => (
        <Tabs defaultValue="docenti">
          <TabsList>
            <TabsTrigger value="docenti">Docenti</TabsTrigger>
            <TabsTrigger value="classi">Classi</TabsTrigger>
            <TabsTrigger value="assegnazioni">Assegnazioni</TabsTrigger>
          </TabsList>
          <TabsContent value="docenti"><Docenti admin={role === "ADMIN"} /></TabsContent>
          <TabsContent value="classi"><Classi admin={role === "ADMIN"} /></TabsContent>
          <TabsContent value="assegnazioni"><Assegnazioni /></TabsContent>
        </Tabs>
      )}
    </AppShell>
  );
}

const box = "rounded-lg border border-border bg-card shadow-card overflow-hidden";
const th = "px-3 py-2 text-left font-medium bg-muted";

function Docenti({ admin }: { admin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["teachers"], queryFn: () => api.teachers.list() });
  const [nome, setNome] = useState("");
  const [tipo, setTipo] = useState<TipoDocente>("ASSUNTO");
  const inv = () => qc.invalidateQueries({ queryKey: ["teachers"] });
  const add = useMutation({
    mutationFn: () => api.teachers.create({ nome: nome.toUpperCase(), tipo, email: null, active: true, first_time_this_year: true }),
    onSuccess: () => { setNome(""); inv(); toast.success("Docente aggiunto"); },
    onError: (e) => toast.error(errMsg(e)),
  });
  const del = useMutation({ mutationFn: (id: string) => api.teachers.remove(id), onSuccess: inv, onError: (e) => toast.error(errMsg(e)) });
  return (
    <div className="space-y-4">
      {admin && (
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          <Input placeholder="COGNOME NOME" value={nome} onChange={(e) => setNome(e.target.value)} className="max-w-xs bg-card" required />
          <Select value={tipo} onValueChange={(v) => setTipo(v as TipoDocente)}>
            <SelectTrigger className="w-40 bg-card"><SelectValue /></SelectTrigger>
            <SelectContent><SelectItem value="ASSUNTO">Assunto</SelectItem><SelectItem value="CONTRATTO">Contratto</SelectItem></SelectContent>
          </Select>
          <Button type="submit">Aggiungi</Button>
        </form>
      )}
      <div className={box}>
        <table className="w-full text-sm">
          <thead><tr><th className={th}>Nome</th><th className={th}>Tipo</th><th className={th}>Email</th><th className={th} /></tr></thead>
          <tbody>
            {q.data?.map((t) => (
              <tr key={t.id} className="border-t border-border">
                <td className="px-3 py-2 font-medium">{t.nome}</td>
                <td className="px-3 py-2">{t.tipo}</td>
                <td className="px-3 py-2 text-muted-foreground">{t.email}</td>
                <td className="px-3 py-2 text-right">
                  {admin && <Button size="icon" variant="ghost" aria-label={`Elimina ${t.nome}`} onClick={() => del.mutate(t.id)}><Trash2 className="h-4 w-4" /></Button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Classi({ admin }: { admin: boolean }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["classes"], queryFn: () => api.classes.list() });
  const [nome, setNome] = useState("");
  const inv = () => qc.invalidateQueries({ queryKey: ["classes"] });
  const add = useMutation({
    mutationFn: () => api.classes.create({ nome: nome.toUpperCase(), n_studenti: 0, gruppo: null }),
    onSuccess: () => { setNome(""); inv(); },
    onError: (e) => toast.error(errMsg(e)),
  });
  const del = useMutation({ mutationFn: (id: string) => api.classes.remove(id), onSuccess: inv, onError: (e) => toast.error(errMsg(e)) });
  return (
    <div className="space-y-4">
      {admin && (
        <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
          <Input placeholder="es. II MECCANICI" value={nome} onChange={(e) => setNome(e.target.value)} className="max-w-xs bg-card" required />
          <Button type="submit">Aggiungi</Button>
        </form>
      )}
      <div className={box}>
        <table className="w-full text-sm">
          <thead><tr><th className={th}>Classe</th><th className={th}>Gruppo</th><th className={th}>Studenti</th><th className={th} /></tr></thead>
          <tbody>
            {q.data?.map((c) => (
              <tr key={c.id} className="border-t border-border">
                <td className="px-3 py-2 font-medium">{c.nome}</td>
                <td className="px-3 py-2">{c.gruppo ?? "—"}</td>
                <td className="px-3 py-2">{c.n_studenti}</td>
                <td className="px-3 py-2 text-right">
                  {admin && <Button size="icon" variant="ghost" aria-label={`Elimina ${c.nome}`} onClick={() => del.mutate(c.id)}><Trash2 className="h-4 w-4" /></Button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Assegnazioni() {
  const a = useQuery({ queryKey: ["assignments"], queryFn: () => api.assignments.list() });
  const t = useQuery({ queryKey: ["teachers"], queryFn: () => api.teachers.list() });
  const c = useQuery({ queryKey: ["classes"], queryFn: () => api.classes.list() });
  const m = useQuery({ queryKey: ["subjects"], queryFn: () => api.subjects.list() });
  const n = <T extends { id: string; nome: string }>(l: T[] | undefined, id: string) => l?.find((x) => x.id === id)?.nome ?? id;
  return (
    <div className={box}>
      <table className="w-full text-sm">
        <thead><tr><th className={th}>Docente</th><th className={th}>Classe</th><th className={th}>Materia</th><th className={th}>Ore/sett.</th><th className={th}>Erogate / Totali</th></tr></thead>
        <tbody>
          {a.data?.map((x) => (
            <tr key={x.id} className="border-t border-border">
              <td className="px-3 py-2">{n(t.data, x.docente_id)}</td>
              <td className="px-3 py-2">{n(c.data, x.classe_id)}</td>
              <td className="px-3 py-2">{n(m.data, x.materia_id)}</td>
              <td className="px-3 py-2">{x.ore_settimanali}</td>
              <td className="px-3 py-2">{x.ore_erogate} / {x.ore_totali}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
