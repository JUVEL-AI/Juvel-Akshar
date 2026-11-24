import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq } from "@/lib/server";
import { campusScope } from "@/lib/auth";

/** Browser -> this route -> backend. Start / pause / stop per campus: a counselor may change their own campus,
 *  admin and super admin may change any campus and the master switch for the whole school. If CONTROL_PIN is set, the PIN is also required. */
export async function POST(req: NextRequest) {
  const who = whoFromReq(req);
  const body = await req.json().catch(() => ({}));
  const action = String(body.action ?? "");
  const campus = String(body.campus ?? "");
  if (!base() || !["start", "pause", "stop"].includes(action) || !/^[a-z0-9_]*$/.test(campus)) return NextResponse.json({ ok: false, error: "bad request" }, { status: 400 });
  const admin = who.role !== "staff";
  const scope = campusScope(who.user, who.role);
  if (!campus && !admin) return NextResponse.json({ ok: false, error: "Only an admin can change calling for the whole school" }, { status: 403 });
  if (campus && !admin && scope && scope !== campus) return NextResponse.json({ ok: false, error: "You can only change your own campus" }, { status: 403 });
  const need = process.env.CONTROL_PIN;
  if (need && String(body.pin ?? "") !== need) return NextResponse.json({ ok: false, error: "Wrong PIN" }, { status: 403 });
  const r = await fetch(`${base()}/api/control`, { method: "POST", headers: secretHeaders(), cache: "no-store",
    body: JSON.stringify({ action, by: who.user, ...(campus ? { campus } : {}) }) });
  return NextResponse.json({ ok: r.ok }, { status: r.ok ? 200 : 502 });
}
