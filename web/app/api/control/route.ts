import { NextRequest, NextResponse } from "next/server";

/** Browser -> this route -> backend. The backend secret never reaches the browser.
 *  If CONTROL_PIN is set in Vercel, Start / Pause / Stop also need that PIN. */
export async function POST(req: NextRequest) {
  const base = process.env.BACKEND_URL;
  const body = await req.json().catch(() => ({}));
  const action = String(body.action ?? "");
  if (!base || !["start", "pause", "stop"].includes(action)) return NextResponse.json({ ok: false, error: "bad request" }, { status: 400 });
  const need = process.env.CONTROL_PIN;
  if (need && String(body.pin ?? "") !== need) return NextResponse.json({ ok: false, error: "Wrong PIN" }, { status: 403 });
  const r = await fetch(`${base.replace(/\/$/, "")}/api/control`, {
    method: "POST",
    headers: { "X-Webhook-Secret": process.env.WEBHOOK_SECRET ?? "", "Content-Type": "application/json" },
    body: JSON.stringify({ action, by: "dashboard" }),
    cache: "no-store",
  });
  return NextResponse.json({ ok: r.ok }, { status: r.ok ? 200 : 502 });
}
