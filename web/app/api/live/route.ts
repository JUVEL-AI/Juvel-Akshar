import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq } from "@/lib/server";
import { campusScope } from "@/lib/auth";

/** The calling card only: small, asked for every few seconds so it never lags behind the phone. */
export async function GET(req: NextRequest) {
  if (!base()) return NextResponse.json({ live: [] });
  const who = whoFromReq(req);
  const only = campusScope(who.user, who.role);                  // a campus-limited counselor never receives the other campus's card
  try {
    const r = await fetch(`${base()}/api/live`, { headers: secretHeaders(), cache: "no-store" });
    if (!r.ok) return NextResponse.json({ live: null }, { status: 502 });
    const j = await r.json();
    const live = (j.live ?? []).filter((l: { campus: string }) => !only || l.campus === only);
    return NextResponse.json({ live }, { headers: { "Cache-Control": "no-store" } });
  } catch {
    return NextResponse.json({ live: null }, { status: 502 });
  }
}
