import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { api } from "@/services";
import { formatShort } from "@/services/dates";

export function useWeeks() {
  return useQuery({ queryKey: ["weeks"], queryFn: () => api.availability.weeks() });
}

export function WeekSelect({ value, onChange }: { value: string | null; onChange: (w: string) => void }) {
  const { data: weeks = [] } = useWeeks();
  useEffect(() => {
    if (!value && weeks.length) onChange(weeks[0]!.start);
  }, [value, weeks, onChange]);
  return (
    <Select value={value ?? ""} onValueChange={onChange}>
      <SelectTrigger className="w-64 bg-card" aria-label="Settimana">
        <SelectValue placeholder="Settimana…" />
      </SelectTrigger>
      <SelectContent>
        {weeks.map((w) => (
          <SelectItem key={w.start} value={w.start}>
            Sett. {w.week_num} · {formatShort(w.start)} – {formatShort(w.end)}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function errMsg(e: unknown) {
  return e instanceof Error ? e.message : "Errore imprevisto";
}
