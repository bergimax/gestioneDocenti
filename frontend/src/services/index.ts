import type { ApiService } from "./api";
import { createHttpApi } from "./http";
import { createMockApi } from "./mock";

export * from "./types";
export type { ApiService } from "./api";
export { session } from "./session";

/**
 * The one entry point the UI uses for backend calls.
 * Mock is used unless VITE_USE_MOCK is explicitly "false".
 */
const useMock = import.meta.env["VITE_USE_MOCK"] !== "false";

export const api: ApiService = useMock
  ? createMockApi({ latencyMs: 250 })
  : createHttpApi(import.meta.env["VITE_API_BASE_URL"] ?? "/api", () => {
      if (typeof window !== "undefined") window.location.href = "/";
    });

export const isMockApi = useMock;
