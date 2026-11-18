import { NextRequest, NextResponse } from "next/server";
import { COOKIE, SESSION_HOURS, check, makeToken } from "@/lib/auth";

// Lockout: 5 wrong tries per name+address -> locked 15 minutes. Kept in memory, so it is per server instance
// (good against casual guessing; use a strong password, and a shared store if you need it airtight).
const fails = new Map<string, { n: number; until: number }>();
const MAX = 5, LOCK_MS = 15 * 60_000;

export async function POST(req: NextRequest) {
  const { name = "", password = "" } = await req.json().catch(() => ({}));
  const ip = (req.headers.get("x-forwarded-for") || "").split(",")[0].trim() || "?";
  const key = `${String(name).toLowerCase().slice(0, 40)}|${ip}`;
  const f = fails.get(key);
  if (f && f.until > Date.now()) {
    return NextResponse.json({ ok: false, error: "Too many wrong tries. Try again in 15 minutes." }, { status: 429 });
  }
  const who = check(String(name), String(password));
  if (!who) {
    const cur = f && f.until === 0 ? f.n + 1 : 1;
    fails.set(key, { n: cur, until: cur >= MAX ? Date.now() + LOCK_MS : 0 });
    if (fails.size > 500) fails.clear();
    await new Promise((r) => setTimeout(r, 800));                       // slows down password guessing
    return NextResponse.json({ ok: false, error: "Wrong name or password" }, { status: 401 });
  }
  fails.delete(key);
  const res = NextResponse.json({ ok: true, role: who.role });
  res.cookies.set(COOKIE, await makeToken(who.user, who.role), {
    httpOnly: true, secure: process.env.NODE_ENV === "production", sameSite: "lax", path: "/", maxAge: SESSION_HOURS * 3600 });
  return res;
}
