import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";

export async function PUT(req: NextRequest) {
  const who = whoFromReq(req); const no = needAdmin(who); if (no) return no;
  if (!base()) return NextResponse.json({ ok: false, error: "Backend not set" }, { status: 400 });
  const body = await req.json().catch(() => ({}));
  const r = await fetch(`${base()}/api/settings`, { method: "PUT", headers: secretHeaders(), cache: "no-store",
    body: JSON.stringify({ ...body, by: who.user }) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return NextResponse.json({ ok: false, error: j.detail || "Could not save" }, { status: r.status });
  return NextResponse.json({ ok: true, settings: j });
}
