import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq } from "@/lib/server";
import { campusScope } from "@/lib/auth";

/** A counselor records how the conversation ended (admitted, not admitted, interested, do not disturb). */
export async function POST(req: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!base() || !/^\d+$/.test(id)) return NextResponse.json({ ok: false }, { status: 400 });
  const who = whoFromReq(req);
  const body = await req.json().catch(() => ({}));
  const outcome = String(body.outcome ?? "");
  if (!["admitted", "not_admitted", "interested", "dnd"].includes(outcome)) return NextResponse.json({ ok: false, error: "Pick an outcome" }, { status: 400 });
  const r = await fetch(`${base()}/api/lead/${id}/outcome`, { method: "POST", headers: secretHeaders(),
    body: JSON.stringify({ outcome, by: who.user, campus: campusScope(who.user, who.role) ?? "" }) });
  const j = await r.json().catch(() => ({}));
  return NextResponse.json({ ok: r.ok, error: r.ok ? "" : (j.detail ?? "Could not save") }, { status: r.ok ? 200 : r.status });
}
