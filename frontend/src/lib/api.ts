export class ApiError extends Error {
  status: number;
  code: string;
  data: any;
  constructor(status: number, code: string, message: string, data: any) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

// The account this tab shows, sent with every request. Tabs share the sign-in cookie, so after signing in as someone
// else in another tab the server refuses this tab's requests instead of acting as the new account.
let signedInAs: number | null = null;
export const setSignedInAs = (id: number | null) => { signedInAs = id; };
export const SIGNED_IN_ELSEWHERE = "vob:signed-in-elsewhere";

export async function api<T = any>(path: string, opts: { method?: string; body?: any; form?: FormData } = {}): Promise<T> {
  const headers: Record<string, string> = signedInAs === null ? {} : { "X-Signed-In-As": String(signedInAs) };
  const init: RequestInit = { method: opts.method || (opts.body || opts.form ? "POST" : "GET"), credentials: "same-origin", headers };
  if (opts.form) init.body = opts.form;
  else if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch("/api" + path, init);
  const text = await res.text();
  let data: any = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { message: text }; }
  // the demo access cookie expired or the code changed: reload, which shows the access-code screen
  if (res.status === 401 && data?.error === "access_required" && path !== "/access") window.location.reload();
  if (res.status === 409 && data?.error === "signed_in_elsewhere") window.dispatchEvent(new Event(SIGNED_IN_ELSEWHERE));
  if (!res.ok) throw new ApiError(res.status, data?.error || "error", data?.message || res.statusText, data);
  return data as T;
}

export const opId = () => (crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2) + Date.now());
