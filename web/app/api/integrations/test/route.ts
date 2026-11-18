import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needSuper } from "@/lib/server";

export async function POST(req: NextRequest) {
  const no = needSuper(whoFromReq(req)); if (no) return no;
  if (!base()) return NextResponse.json({ ok: false, message: "Backend not set" });
  const r = await fetch(`${base()}/api/integrations/test`, { method: "POST", headers: secretHeaders(), cache: "no-store" });
  return NextResponse.json(await r.json().catch(() => ({ ok: false, message: "Backend did not answer" })));
}
