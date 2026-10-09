import { afterEach, describe, expect, it, vi } from "vitest";
import { createHttpApi } from "@/services/http";
import { session } from "@/services/session";

function mockFetch(status: number, body: unknown) {
  const fn = vi.fn(async () => new Response(JSON.stringify(body), { status }));
  vi.stubGlobal("fetch", fn);
  return fn;
}

describe("http api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    session.clear();
  });

  it("sends bearer token and JSON body to the right endpoint", async () => {
    session.set("tok", "ADMIN", "a@b.it");
    const f = mockFetch(200, { status: "generated" });
    await createHttpApi("/api").schedule.generate("2026-09-14");
    const [url, init] = f.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/schedule/generate");
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Authorization"]).toBe("Bearer tok");
    expect(JSON.parse(init.body as string)).toEqual({ week_start: "2026-09-14", include_preferences: true });
  });

  it("clears session on 401 and calls onUnauthorized", async () => {
    session.set("tok", "ADMIN", "a@b.it");
    mockFetch(401, { detail: "expired" });
    const cb = vi.fn();
    await expect(createHttpApi("/api", cb).teachers.list()).rejects.toMatchObject({ status: 401, message: "expired" });
    expect(session.getToken()).toBeNull();
    expect(cb).toHaveBeenCalled();
  });

  it("returns null for a missing schedule (404)", async () => {
    mockFetch(404, { detail: "not found" });
    expect(await createHttpApi().schedule.get("2026-09-14")).toBeNull();
  });
});
