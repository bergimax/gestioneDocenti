import type { Role } from "./types";

const TOKEN_KEY = "authToken";
const ROLE_KEY = "authRole";
const EMAIL_KEY = "authEmail";

const store = () => (typeof window === "undefined" ? null : window.sessionStorage);

export const session = {
  getToken: () => store()?.getItem(TOKEN_KEY) ?? null,
  getRole: () => (store()?.getItem(ROLE_KEY) as Role | null) ?? null,
  getEmail: () => store()?.getItem(EMAIL_KEY) ?? null,
  set(token: string, role: Role, email: string) {
    store()?.setItem(TOKEN_KEY, token);
    store()?.setItem(ROLE_KEY, role);
    store()?.setItem(EMAIL_KEY, email);
  },
  clear() {
    store()?.removeItem(TOKEN_KEY);
    store()?.removeItem(ROLE_KEY);
    store()?.removeItem(EMAIL_KEY);
  },
  isSegreteria: () => session.getRole() === "SEGRETERIA",
};
