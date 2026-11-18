import { headers } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

export const base = () => (process.env.BACKEND_URL ?? "").replace(/\/$/, "");
export const secretHeaders = (): Record<string, string> => ({ "X-Webhook-Secret": process.env.WEBHOOK_SECRET ?? "", "Content-Type": "application/json" });

export type Role = "super" | "admin" | "staff";
export type Who = { user: string; role: Role };
const asRole = (r: string | null, fallback: Role): Role => (r === "super" || r === "admin" || r === "staff" ? r : fallback);
export function whoFromReq(req: NextRequest): Who {
  return { user: req.headers.get("x-juvel-user") ?? "unknown", role: asRole(req.headers.get("x-juvel-role"), "staff") };
}
export async function whoFromPage(): Promise<Who> {
  const h = await headers();
  return { user: h.get("x-juvel-user") ?? "dashboard", role: asRole(h.get("x-juvel-role"), "super") };
}
/** admin and super may do admin things (settings, lead sheets, call now, delete, recordings). */
export const isAdmin = (w: Who) => w.role === "admin" || w.role === "super";
export const needAdmin = (w: Who) => (isAdmin(w) ? null : NextResponse.json({ ok: false, error: "Only an admin can do this" }, { status: 403 }));
/** Only the super admin may see or change the Vapi / Vobiz connections and keys. */
export const needSuper = (w: Who) => (w.role === "super" ? null : NextResponse.json({ ok: false, error: "Only the super admin can do this" }, { status: 403 }));
