export const GATEWAY_URL =
  process.env.NEXT_PUBLIC_GATEWAY_URL || "http://localhost:8000";

// ---- auth/session helpers (browser-only localStorage) --------------------

export interface SessionUser {
  id: string | number;
  username: string;
  role: "borrower" | "csr" | "underwriter" | "admin" | string;
  name: string;
}

const TOKEN_KEY = "meridian.token";
const USER_KEY = "meridian.user";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function getUser(): SessionUser | null {
  if (typeof window === "undefined") return null;
  const raw = window.localStorage.getItem(USER_KEY);
  if (!raw) return null;
  try {
    return JSON.parse(raw) as SessionUser;
  } catch {
    return null;
  }
}

export function setSession(token: string, user: SessionUser) {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(TOKEN_KEY, token);
  window.localStorage.setItem(USER_KEY, JSON.stringify(user));
}

export function clearSession() {
  if (typeof window === "undefined") return;
  window.localStorage.removeItem(TOKEN_KEY);
  window.localStorage.removeItem(USER_KEY);
}

/**
 * Role -> landing route after login. Used by login redirect + nav.
 * UI-only routing convenience. The gateway/API still accept ANY authenticated
 * caller — server-side authz is intentionally absent (debt D8, fixed in W6).
 * A role can still navigate anywhere by URL; this only sets the default landing.
 */
export function roleHome(role: string | null | undefined): string {
  switch (role) {
    case "csr":
      return "/servicing";
    case "underwriter":
      return "/underwriting";
    case "admin":
      return "/admin";
    case "borrower":
    default:
      return "/";
  }
}

// ---- fetch helpers -------------------------------------------------------

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail || `Request failed (${status})`);
    this.status = status;
    this.detail = detail;
  }
}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function parse(res: Response) {
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    const detail =
      data && typeof data === "object" && "detail" in data
        ? String((data as { detail: unknown }).detail)
        : typeof data === "string" && data
          ? data
          : `Request failed (${res.status})`;
    throw new ApiError(res.status, detail);
  }
  return data;
}

// credentials:"include" sends the gateway's HttpOnly resume cookie (ADR 0010 Phase B):
// the anonymous continuation token is no longer a client-held header, so the browser must
// carry the cookie for the gateway to re-attach the token server-side.
export async function apiGet(path: string, headers?: Record<string, string>) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    cache: "no-store",
    credentials: "include",
    headers: { ...authHeaders(), ...(headers || {}) },
  });
  return parse(res);
}

export async function apiPost(
  path: string,
  body?: unknown,
  headers?: Record<string, string>,
) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method: "POST",
    cache: "no-store",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(headers || {}),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return parse(res);
}

export async function apiDelete(path: string) {
  const res = await fetch(`${GATEWAY_URL}${path}`, {
    method: "DELETE",
    cache: "no-store",
    credentials: "include",
    headers: { ...authHeaders() },
  });
  return parse(res);
}
