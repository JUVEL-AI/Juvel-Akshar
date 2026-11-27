import { NextRequest, NextResponse } from "next/server";
import { base, secretHeaders, whoFromReq, needAdmin } from "@/lib/server";
import { campusScope } from "@/lib/auth";

/** Download one day's list as a CSV (opens in Excel). Contains full phone numbers, so it is logged. Admins get everything; a counselor
 *  limited to one campus gets only that campus, and only while an admin allows it in Settings. */
export async function GET(req: NextRequest) {
  const who = whoFromReq(req);
  const only = campusScope(who.user, who.role);
  if (who.role === "staff") { if (!only) return NextResponse.json({ ok: false, error: "Only an admin can download" }, { status: 403 }); }
  else { const no = needAdmin(who); if (no) return no; }
  const day = req.nextUrl.searchParams.get("day") ?? "";
  const kind = req.nextUrl.searchParams.get("kind") === "calls" ? "calls" : "enquiries";
  if (!base() || (day && !/^\d{4}-\d{2}-\d{2}$/.test(day))) return new NextResponse("bad request", { status: 400 });
  const scope = who.role === "staff" && only ? `&campus=${encodeURIComponent(only)}&staff=1` : "";
  const r = await fetch(`${base()}/api/export?by=${encodeURIComponent(who.user)}${day ? `&day=${day}` : ""}&kind=${kind}${scope}`, { headers: secretHeaders(), cache: "no-store" });
  if (r.status === 403) return NextResponse.json({ ok: false, error: "Downloads are switched off for staff. Ask an admin." }, { status: 403 });
  return new NextResponse(await r.text(), { status: r.ok ? 200 : 502, headers: {
    "Content-Type": "text/csv; charset=utf-8", "Content-Disposition": `attachment; filename="${kind === "calls" ? "call-log" : "enquiries"}-${only ?? "all"}-${day || "today"}.csv"` } });
}
