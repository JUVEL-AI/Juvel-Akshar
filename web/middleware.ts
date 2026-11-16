import { NextRequest, NextResponse } from "next/server";
import { COOKIE, authOn, readToken } from "@/lib/auth";

/** Login gate. Everything except the login page needs a valid session cookie.
 *  Users: CONSOLE_USERS="name:password:admin,name2:password2:staff". Open (preview) if no users are set. */
export async function middleware(req: NextRequest) {
  const headers = new Headers(req.headers);
  headers.delete("x-juvel-user"); headers.delete("x-juvel-role");           // never trust these from the browser
  const path = req.nextUrl.pathname;
  if (!authOn()) {
    headers.set("x-juvel-user", "dashboard"); headers.set("x-juvel-role", "admin");
    return NextResponse.next({ request: { headers } });
  }
  if (path === "/login" || path === "/api/login" || path === "/api/logout") return NextResponse.next({ request: { headers } });
  const who = await readToken(req.cookies.get(COOKIE)?.value);
  if (who) {
    headers.set("x-juvel-user", who.user); headers.set("x-juvel-role", who.role);
    return NextResponse.next({ request: { headers } });
  }
  if (path.startsWith("/api/")) return NextResponse.json({ ok: false, error: "Please sign in again" }, { status: 401 });
  const to = req.nextUrl.clone(); to.pathname = "/login"; to.search = path === "/" ? "" : `?next=${encodeURIComponent(path)}`;
  return NextResponse.redirect(to);
}

export const config = { matcher: "/((?!_next/static|_next/image|favicon.ico).*)" };
