import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq } from "@/lib/server";
import { campusScope } from "@/lib/auth";

/** Call-back list: start calling these parents back one at a time, or cancel the list. Counselors may do this for their own campus. */
export async function POST(req: NextRequest) {
  const who = whoFromReq(req);
  if (!base()) return NextResponse.json({ ok: false, error: "Not connected" }, { status: 400 });
  const b = await req.json().catch(() => ({}));
  const ids = Array.isArray(b.ids) ? b.ids.filter((x: unknown) => Number.isInteger(x)).slice(0, 200) : [];
  const body = b.action === "cancel" ? { action: "cancel", by: who.user } : { ids, by: who.user, campus: campusScope(who.user, who.role) ?? "" };
  const r = await fetch(`${base()}/api/recall`, { method: "POST", headers: secretHeaders(), cache: "no-store", body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  return NextResponse.json(r.ok ? { ok: true, ...j } : { ok: false, error: j.detail || "Could not start the call backs" }, { status: r.ok ? 200 : r.status });
}
