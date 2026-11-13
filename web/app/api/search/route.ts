import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders } from "@/lib/server";

export async function GET(req: NextRequest) {
  const q = req.nextUrl.searchParams.get("q") ?? "";
  if (!base() || q.trim().length < 3) return NextResponse.json([]);
  const r = await fetch(`${base()}/api/search?q=${encodeURIComponent(q)}`, { headers: secretHeaders(), cache: "no-store" });
  return NextResponse.json(r.ok ? await r.json() : []);
}
