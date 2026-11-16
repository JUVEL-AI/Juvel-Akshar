/** Session cookie for the dashboard. Works in the Edge middleware and in route handlers (Web Crypto only). */
export type Role = "admin" | "staff";
export type U = { name: string; pw: string; role: Role };
export const COOKIE = "juvel_session";
export const SESSION_HOURS = 12;

export function users(): U[] {
  return (process.env.CONSOLE_USERS ?? "").split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
    const [name, pw, role] = s.split(":");
    return { name, pw, role: role === "admin" ? "admin" : "staff" } as U;
  }).filter((u) => u.name && u.pw);
}
export const authOn = () => users().length > 0 || !!process.env.CONSOLE_PASSWORD;

const secret = () => process.env.SESSION_SECRET || process.env.WEBHOOK_SECRET || "";
const enc = new TextEncoder();
const b64u = (b: ArrayBuffer | Uint8Array) => btoa(String.fromCharCode(...new Uint8Array(b as ArrayBuffer))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const fromB64u = (s: string) => atob(s.replace(/-/g, "+").replace(/_/g, "/"));

async function sign(data: string): Promise<string> {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret()), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return b64u(await crypto.subtle.sign("HMAC", key, enc.encode(data)));
}
export function same(a: string, b: string): boolean {              // constant-time compare
  const x = enc.encode(a), y = enc.encode(b);
  let d = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) d |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return d === 0;
}

export async function makeToken(name: string, role: Role): Promise<string> {
  const body = b64u(enc.encode(JSON.stringify({ u: name, r: role, e: Date.now() + SESSION_HOURS * 3600_000 })));
  return `${body}.${await sign(body)}`;
}
export async function readToken(tok: string | undefined): Promise<{ user: string; role: Role } | null> {
  if (!tok || !secret()) return null;
  const [body, sig] = tok.split(".");
  if (!body || !sig || !same(sig, await sign(body))) return null;
  try {
    const p = JSON.parse(fromB64u(body));
    if (typeof p.e !== "number" || p.e < Date.now()) return null;
    return { user: String(p.u), role: p.r === "admin" ? "admin" : "staff" };
  } catch { return null; }
}

/** Checks a name and password. Returns the person, or null. */
export function check(name: string, pw: string): { user: string; role: Role } | null {
  const list = users();
  const hit = list.find((u) => u.name.toLowerCase() === name.trim().toLowerCase() && same(u.pw, pw));
  if (hit) return { user: hit.name, role: hit.role };
  const shared = process.env.CONSOLE_PASSWORD;
  if (!list.length && shared && same(shared, pw)) return { user: name.trim() || "admin", role: "admin" };
  return null;
}
