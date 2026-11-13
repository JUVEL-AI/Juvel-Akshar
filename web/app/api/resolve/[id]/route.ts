import { NextRequest, NextResponse } from "next/server";

/** Browser -> this route -> backend. The backend secret never reaches the browser. */
export async function POST(_req: NextRequest, ctx: { params: Promise<{ id: string }> }) {
  const { id } = await ctx.params;
  const base = process.env.BACKEND_URL;
  if (!base || !/^\d+$/.test(id)) return NextResponse.json({ ok: false }, { status: 400 });
  const r = await fetch(`${base.replace(/\/$/, "")}/api/lead/${id}/resolve`, {
    method: "POST",
    headers: { "X-Webhook-Secret": process.env.WEBHOOK_SECRET ?? "", "Content-Type": "application/json" },
    body: JSON.stringify({ note: "marked done from the console" }),
  });
  return NextResponse.json({ ok: r.ok }, { status: r.ok ? 200 : 502 });
}
