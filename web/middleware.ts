import { NextRequest, NextResponse } from "next/server";

/** Login gate (HTTP Basic) with named users and roles.
 *  CONSOLE_USERS = "priya:pw1:admin,viji:pw2:staff"  (admin can Start/Pause/Stop, change Settings, export; staff can view and mark done)
 *  If CONSOLE_USERS is empty but CONSOLE_PASSWORD is set, any user name + that password gets admin.
 *  If neither is set the dashboard is open (preview only). */
type U = { name: string; pw: string; role: "admin" | "staff" };

function users(): U[] {
  return (process.env.CONSOLE_USERS ?? "").split(",").map((s) => s.trim()).filter(Boolean).map((s) => {
    const [name, pw, role] = s.split(":");
    return { name, pw, role: role === "admin" ? "admin" : "staff" } as U;
  }).filter((u) => u.name && u.pw);
}

export function middleware(req: NextRequest) {
  const headers = new Headers(req.headers);
  headers.delete("x-juvel-user"); headers.delete("x-juvel-role");           // never trust these from the browser
  const list = users(), shared = process.env.CONSOLE_PASSWORD;
  if (!list.length && !shared) {
    headers.set("x-juvel-user", "dashboard"); headers.set("x-juvel-role", "admin");
    return NextResponse.next({ request: { headers } });
  }
  const h = req.headers.get("authorization") ?? "";
  if (h.startsWith("Basic ")) {
    const decoded = atob(h.slice(6));
    const i = decoded.indexOf(":");
    const name = decoded.slice(0, i), pw = decoded.slice(i + 1);
    const hit = list.find((u) => u.name.toLowerCase() === name.toLowerCase() && u.pw === pw);
    if (hit) { headers.set("x-juvel-user", hit.name); headers.set("x-juvel-role", hit.role); return NextResponse.next({ request: { headers } }); }
    if (!list.length && shared && pw === shared) { headers.set("x-juvel-user", name || "admin"); headers.set("x-juvel-role", "admin"); return NextResponse.next({ request: { headers } }); }
  }
  return new NextResponse("Password required", { status: 401, headers: { "WWW-Authenticate": 'Basic realm="Juvel Calling"' } });
}

export const config = { matcher: "/((?!_next/static|_next/image|favicon.ico).*)" };
