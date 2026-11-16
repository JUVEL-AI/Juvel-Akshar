import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";

export async function POST(req: NextRequest) {
  const no = needAdmin(whoFromReq(req)); if (no) return no;
  if (!base()) return NextResponse.json({ ok: false, error: "Backend not set" }, { status: 400 });
  const body = await req.json().catch(() => ({}));
  const r = await fetch(`${base()}/api/sources/preview`, { method: "POST", headers: secretHeaders(), cache: "no-store", body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) return NextResponse.json({ ok: false, error: j.detail || "Could not read the sheet" }, { status: r.status });
  return NextResponse.json({ ok: true, preview: j });
}
