import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";

/** Download one day's list as a CSV (opens in Excel). Contains full phone numbers, so admin only and logged. */
export async function GET(req: NextRequest) {
  const who = whoFromReq(req); const no = needAdmin(who); if (no) return no;
  const day = req.nextUrl.searchParams.get("day") ?? "";
  if (!base() || (day && !/^\d{4}-\d{2}-\d{2}$/.test(day))) return new NextResponse("bad request", { status: 400 });
  const r = await fetch(`${base()}/api/export?by=${encodeURIComponent(who.user)}${day ? `&day=${day}` : ""}`, { headers: secretHeaders(), cache: "no-store" });
  return new NextResponse(await r.text(), { status: r.ok ? 200 : 502, headers: {
    "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": `attachment; filename="enquiries-${day || "today"}.csv"` } });
}
