import Link from "next/link";
import SourcesForm from "@/components/SourcesForm";
import LogoutButton from "@/components/LogoutButton";
import { loadSources } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";

export const dynamic = "force-dynamic";

export default async function SourcesPage() {
  const who = await whoFromPage();
  const { data, error } = await loadSources();
  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><div className="logo">V</div>
          <div><h1>Lead sheets</h1><div className="sub">Which sheets the system reads · signed in as {who.user}</div></div></div>
        <div className="chips"><Link href="/" className="cbtn" style={{ textDecoration: "none" }}>← Back to dashboard</Link><LogoutButton /></div>
      </header>
      {who.role !== "admin" ? <div className="card">Only an admin can choose the lead sheets.</div>
        : error ? <div className="card"><span className="chip bad">Backend not reachable</span></div>
        : <SourcesForm initial={data} />}
    </div>
  );
}
