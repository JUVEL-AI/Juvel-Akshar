import Link from "next/link";
import IntegrationsForm from "@/components/IntegrationsForm";
import LogoutButton from "@/components/LogoutButton";
import { loadIntegrations } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";

export const dynamic = "force-dynamic";

export default async function IntegrationsPage() {
  const who = await whoFromPage();
  const { data, error } = await loadIntegrations();
  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><div className="logo">V</div>
          <div><h1>Connections</h1><div className="sub">Vapi and Vobiz · signed in as {who.user}</div></div></div>
        <div className="chips"><Link href="/" className="cbtn" style={{ textDecoration: "none" }}>← Back to dashboard</Link><LogoutButton /></div>
      </header>
      {who.role !== "super" ? <div className="card">Only the super admin can see or change the phone connections.</div>
        : error ? <div className="card"><span className="chip bad">Backend not reachable</span></div>
        : <IntegrationsForm initial={data} />}
    </div>
  );
}
