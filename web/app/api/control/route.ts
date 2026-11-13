import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";

/** Browser -> this route -> backend. Admin only. If CONTROL_PIN is set, the PIN is also required. */
export async function POST(req: NextRequest) {
  const who = whoFromReq(req); const no = needAdmin(who); if (no) return no;
  const body = await req.json().catch(() => ({}));
  const action = String(body.action ?? "");
  if (!base() || !["start", "pause", "stop"].includes(action)) return NextResponse.json({ ok: false, error: "bad request" }, { status: 400 });
  const need = process.env.CONTROL_PIN;
  if (need && String(body.pin ?? "") !== need) return NextResponse.json({ ok: false, error: "Wrong PIN" }, { status: 403 });
  const r = await fetch(`${base()}/api/control`, { method: "POST", headers: secretHeaders(), cache: "no-store",
    body: JSON.stringify({ action, by: who.user }) });
  return NextResponse.json({ ok: r.ok }, { status: r.ok ? 200 : 502 });
}
