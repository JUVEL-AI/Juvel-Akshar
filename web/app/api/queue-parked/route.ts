import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";

/** Admin only: put back the enquiries that a Stop moved to Needs review. */
export async function POST(req: NextRequest) {
  const who = whoFromReq(req); const no = needAdmin(who); if (no) return no;
  if (!base()) return NextResponse.json({ ok: false, error: "Backend not set" }, { status: 400 });
  const body = await req.json().catch(() => ({}));
  const r = await fetch(`${base()}/api/queue-parked`, { method: "POST", headers: secretHeaders(), cache: "no-store",
    body: JSON.stringify({ by: who.user, campus: String(body.campus ?? "") }) });
  const j = await r.json().catch(() => ({}));
  return NextResponse.json({ ok: r.ok, queued: j.queued ?? 0, error: r.ok ? "" : (j.detail ?? "Could not do that") }, { status: r.ok ? 200 : r.status });
}
