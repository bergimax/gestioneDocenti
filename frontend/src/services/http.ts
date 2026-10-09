import type { ApiService } from "./api";
import { session } from "./session";
import { ApiError, type ExportFile, type Schedule } from "./types";

/** Real HTTP implementation of ApiService. All calls go through `request`. */
export function createHttpApi(baseUrl = "/api", onUnauthorized?: () => void): ApiService {
  async function raw(method: string, path: string, body?: unknown): Promise<Response> {
    const headers: Record<string, string> = {};
    const token = session.getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const res = await fetch(`${baseUrl}${path}`, {
      method,
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (!res.ok) {
      if (res.status === 401 && path !== "/auth/login") {
        session.clear();
        onUnauthorized?.();
      }
      let msg = res.statusText;
      try {
        const j = await res.json();
        msg = j.detail ?? j.message ?? msg;
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, msg);
    }
    return res;
  }
  async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
    const res = await raw(method, path, body);
    if (res.status === 204) return undefined as T;
    const text = await res.text();
    return (text ? JSON.parse(text) : undefined) as T;
  }
  async function file(path: string, filename: string): Promise<ExportFile> {
    const res = await raw("GET", path);
    return { filename, blob: await res.blob() };
  }
  const s = (w: string) => `/schedule/${encodeURIComponent(w)}`;

  return {
    auth: {
      login: (email, password) => request("POST", "/auth/login", { email, password }),
      logout: () => request("POST", "/auth/logout"),
      me: () => request("GET", "/auth/me"),
      changePassword: (current_password, new_password) =>
        request("POST", "/auth/change-password", { current_password, new_password }),
    },
    teachers: {
      list: () => request("GET", "/teachers"),
      create: (i) => request("POST", "/teachers", i),
      update: (id, i) => request("PUT", `/teachers/${id}`, i),
      remove: (id) => request("DELETE", `/teachers/${id}`),
    },
    classes: {
      list: () => request("GET", "/classes"),
      create: (i) => request("POST", "/classes", i),
      update: (id, i) => request("PUT", `/classes/${id}`, i),
      remove: (id) => request("DELETE", `/classes/${id}`),
    },
    subjects: { list: () => request("GET", "/subjects") },
    assignments: { list: () => request("GET", "/assignments") },
    availability: {
      weeks: () => request("GET", "/availability/weeks"),
      get: (w, t) => request("GET", `/availability/${w}/${t}`),
      save: (w, t, giorni_fasce) => request("POST", `/availability/${w}/${t}`, { giorni_fasce }),
      copyFrom: (w, p) => request("POST", `/availability/${w}/copy-from/${p}`),
      status: (w) => request("GET", `/availability/${w}/status`),
    },
    schedule: {
      async get(w) {
        try {
          return await request<Schedule>("GET", s(w));
        } catch (e) {
          if (e instanceof ApiError && e.status === 404) return null;
          throw e;
        }
      },
      generate: (week_start, include_preferences = true) =>
        request("POST", "/schedule/generate", { week_start, include_preferences }),
      regenerate: (w) => request("POST", `${s(w)}/regenerate`),
      modifySlot: (w, slot_id, changes) => request("POST", `${s(w)}/modify-slot`, { slot_id, changes }),
      assignable: (w, c) => request("GET", `${s(w)}/assignable/${c}`),
      assignSlot: (w, i) => request("POST", `${s(w)}/assign-slot`, i),
      approveConflicts: (w, chiavi) => request("POST", `${s(w)}/conflicts/approve`, { chiavi }),
      rejectConflict: (w, i) => request("POST", `${s(w)}/conflicts/reject`, i),
      applyQuickAction: (w, i) => request("POST", `${s(w)}/apply-quick-action`, i),
      approve: (w) => request("POST", `${s(w)}/approve`),
      listFeedback: (w) => request("GET", `${s(w)}/feedback`),
      sendFeedback: (w, i) => request("POST", `${s(w)}/feedback`, i),
      async exportPdf(w) {
        await request("GET", `${s(w)}/export-pdf`);
        return file(`${s(w)}/export-pdf/file`, `orario_${w}.pdf`);
      },
      async exportExcel(w) {
        await request("GET", `${s(w)}/export-excel`);
        return file(`${s(w)}/export-excel/file`, `orario_${w}.xlsx`);
      },
    },
  };
}
