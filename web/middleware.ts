import { NextRequest, NextResponse } from "next/server";

/** Simple password gate (HTTP Basic). Turn on by setting CONSOLE_PASSWORD in Vercel. */
export function middleware(req: NextRequest) {
  const pw = process.env.CONSOLE_PASSWORD;
  if (!pw) return NextResponse.next();
  const h = req.headers.get("authorization") ?? "";
  if (h.startsWith("Basic ")) {
    const decoded = atob(h.slice(6));
    if (decoded.slice(decoded.indexOf(":") + 1) === pw) return NextResponse.next();
  }
  return new NextResponse("Password required", { status: 401, headers: { "WWW-Authenticate": 'Basic realm="Juvel Calling"' } });
}

export const config = { matcher: "/((?!_next/static|_next/image|favicon.ico).*)" };
