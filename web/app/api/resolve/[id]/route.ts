import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq } from "@/lib/server";

/** Browser -> this route -> backend. The backend secret never reaches the browser. */
export async function POST(req: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  if (!base() || !/^\d+$/.test(id)) return NextResponse.json({ ok: false }, { status: 400 });
  const who = whoFromReq(req);
  const r = await fetch(`${base()}/api/lead/${id}/resolve`, { method: "POST", headers: secretHeaders(),
    body: JSON.stringify({ note: `marked done by ${who.user}`, by: who.user }) });
  return NextResponse.json({ ok: r.ok }, { status: r.ok ? 200 : 502 });
}
