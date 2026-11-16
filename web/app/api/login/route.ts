import { NextRequest, NextResponse } from "next/server";
import { COOKIE, SESSION_HOURS, check, makeToken } from "@/lib/auth";

export async function POST(req: NextRequest) {
  const { name = "", password = "" } = await req.json().catch(() => ({}));
  const who = check(String(name), String(password));
  if (!who) {
    await new Promise((r) => setTimeout(r, 800));                       // slows down password guessing
    return NextResponse.json({ ok: false, error: "Wrong name or password" }, { status: 401 });
  }
  const res = NextResponse.json({ ok: true, role: who.role });
  res.cookies.set(COOKIE, await makeToken(who.user, who.role), {
    httpOnly: true, secure: process.env.NODE_ENV === "production", sameSite: "lax", path: "/", maxAge: SESSION_HOURS * 3600 });
  return res;
}
