import Link from "next/link";
import SettingsForm from "@/components/SettingsForm";
import { loadSettings } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const who = await whoFromPage();
  const { settings, audit, error } = await loadSettings();
  return (
    <div className="shell">
      <header className="top">
        <div className="brand"><div className="logo">V</div>
          <div><h1>Settings</h1><div className="sub">Juvel Calling · signed in as {who.user}</div></div></div>
        <Link href="/" className="cbtn" style={{ textDecoration: "none" }}>← Back to dashboard</Link>
      </header>
      {who.role !== "admin" ? <div className="card">Only an admin can change settings. Ask the principal or Juvel.</div>
        : error ? <div className="card"><span className="chip bad">Backend not reachable</span></div>
        : <SettingsForm initial={settings} audit={audit} />}
    </div>
  );
}
