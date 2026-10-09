// Pure ISO-date helpers (UTC arithmetic, timezone-safe).

export function addDays(iso: string, days: number): string {
  const d = new Date(`${iso}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() + days);
  return d.toISOString().slice(0, 10);
}

export function mondayOf(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  const dow = (d.getUTCDay() + 6) % 7; // 0 = Monday
  return addDays(iso, -dow);
}

export function firstMondayOnOrAfter(iso: string): string {
  const m = mondayOf(iso);
  return m < iso ? addDays(m, 7) : m;
}

export function formatShort(iso: string): string {
  const [, m, d] = iso.split("-");
  return `${d}/${m}`;
}

export function hourLabel(h: number): string {
  return `${String(h).padStart(2, "0")}:00`;
}
