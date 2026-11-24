import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq } from "@/lib/server";
import { campusScope } from "@/lib/auth";

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q") ?? "";
  if (!base() || q.trim().length < 3) return NextResponse.json([]);
  const who = whoFromReq(req);
  const only = campusScope(who.user, who.role) ?? "";            // a campus-limited counselor searches only their own campus
  const r = await fetch(`${base()}/api/search?q=${encodeURIComponent(q)}${only ? `&campus=${encodeURIComponent(only)}` : ""}`, { headers: secretHeaders(), cache: "no-store" });
  return NextResponse.json(r.ok ? await r.json() : []);
}
