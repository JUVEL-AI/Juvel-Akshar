import Link from "next/link";
import TestLab from "@/components/TestLab";
import LogoutButton from "@/components/LogoutButton";
import { loadConsole } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";

export const dynamic = "force-dynamic";

export default async function TestPage() {
  const who = await whoFromPage();
  const { data } = await loadConsole();
  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><div className="logo">V</div>
          <div><h1>Test lab</h1><div className="sub">Try the whole call without touching real data · signed in as {who.user}</div></div></div>
        <div className="chips"><Link href="/" className="cbtn" style={{ textDecoration: "none" }}>← Back to dashboard</Link><LogoutButton /></div>
      </header>
      {who.role !== "super" ? <div className="card">Only the super admin can use the Test lab.</div>
        : <TestLab campuses={data.campuses} />}
    </div>
  );
}
