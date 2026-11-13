import { headers } from "next/headers";
import { NextRequest, NextResponse } from "next/server";

export const base = () => (process.env.BACKEND_URL ?? "").replace(/\/$/, "");
export const secretHeaders = (): Record<string, string> => ({ "X-Webhook-Secret": process.env.WEBHOOK_SECRET ?? "", "Content-Type": "application/json" });

export type Who = { user: string; role: "admin" | "staff" };
export function whoFromReq(req: NextRequest): Who {
  return { user: req.headers.get("x-juvel-user") ?? "unknown", role: req.headers.get("x-juvel-role") === "admin" ? "admin" : "staff" };
}
export async function whoFromPage(): Promise<Who> {
  const h = await headers();
  return { user: h.get("x-juvel-user") ?? "dashboard", role: h.get("x-juvel-role") === "staff" ? "staff" : "admin" };
}
export const needAdmin = (w: Who) => (w.role === "admin" ? null : NextResponse.json({ ok: false, error: "Only an admin can do this" }, { status: 403 }));
