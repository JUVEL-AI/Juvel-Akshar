import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needSuper } from "@/lib/server";

export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  const no = needSuper(whoFromReq(req)); if (no) return no;
  if (!base()) return NextResponse.json({ ok: false, error: "Backend not set" }, { status: 400 });
  const r = await fetch(`${base()}/api/test`, { headers: secretHeaders(), cache: "no-store" });
  const j = await r.json().catch(() => ({}));
  return NextResponse.json(j, { status: r.status });
}

export async function DELETE(req: NextRequest) {
  const who = whoFromReq(req); const no = needSuper(who); if (no) return no;
  if (!base()) return NextResponse.json({ ok: false, error: "Backend not set" }, { status: 400 });
  const r = await fetch(`${base()}/api/test?by=${encodeURIComponent(who.user)}`, { method: "DELETE", headers: secretHeaders(), cache: "no-store" });
  const j = await r.json().catch(() => ({}));
  return NextResponse.json(j, { status: r.status });
}
