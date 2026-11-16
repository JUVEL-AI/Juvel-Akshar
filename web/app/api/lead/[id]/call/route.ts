import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";

export async function POST(req: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  const who = whoFromReq(req); const no = needAdmin(who); if (no) return no;
  const { id } = await ctx.params;
  if (!base() || !/^\d+$/.test(id)) return NextResponse.json({ ok: false, error: "Bad request" }, { status: 400 });
  const r = await fetch(`${base()}/api/lead/${id}/call-now`, { method: "POST", headers: secretHeaders(), cache: "no-store", body: JSON.stringify({ by: who.user }) });
  const j = await r.json().catch(() => ({}));
  return NextResponse.json(r.ok ? { ok: true } : { ok: false, error: j.detail || "Could not start the call" }, { status: r.ok ? 200 : r.status });
}
