// Client for the local engine. The per-launch session token arrives in the URL fragment (#token=…), is kept
// in memory/sessionStorage only, and is sent as a custom header — which also forces a CORS preflight that the
// engine never grants, so other websites cannot call the API.

const TOKEN_KEY = "sextant-token";

function readToken(): string {
  const m = /token=([A-Za-z0-9_-]+)/.exec(window.location.hash);
  if (m) {
    try {
      sessionStorage.setItem(TOKEN_KEY, m[1]);
    } catch {
      /* storage may be unavailable */
    }
    const go = /[&?]go=([^&]+)/.exec(window.location.hash);
    const next = go ? decodeURIComponent(go[1]) : "/";
    history.replaceState(null, "", window.location.pathname + window.location.search + "#" + (next.startsWith("/") ? next : "/"));
    return m[1];
  }
  try {
    return sessionStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

const token = readToken();

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, message: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "X-Sextant-Token": token, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
    body: body !== undefined ? JSON.stringify(body) : undefined,
    credentials: "omit",
    cache: "no-store",
  });
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    let detail: unknown;
    try {
      const j = await res.json();
      detail = j?.detail;
      if (typeof detail === "string") msg = detail;
      else if (Array.isArray(detail)) msg = detail.map((d) => d?.msg ?? JSON.stringify(d)).join("; ");
      else if (detail && typeof detail === "object" && "message" in detail) msg = String((detail as { message: unknown }).message);
      else if (detail) msg = JSON.stringify(detail);
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, msg, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  hasToken: () => token.length > 0,
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body ?? {}),
  put: <T>(path: string, body: unknown) => request<T>("PUT", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  del: <T>(path: string) => request<T>("DELETE", path),
  async download(path: string): Promise<Blob> {
    const res = await fetch(path, { headers: { "X-Sextant-Token": token }, credentials: "omit", cache: "no-store" });
    if (!res.ok) throw new ApiError(res.status, `${res.status} ${res.statusText}`);
    return await res.blob();
  },
};

export function inv(id: string, rest = ""): string {
  return `/api/investigations/${id}${rest}`;
}
