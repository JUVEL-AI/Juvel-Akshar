"use client";
import { useState } from "react";
import type { IntegData } from "@/lib/backend";

const FIELDS: { k: keyof Omit<IntegData, "live">; label: string; secret?: boolean; hint: string; ph: string }[] = [
  { k: "vapi_api_key", label: "Vapi private API key", secret: true, ph: "paste the private key", hint: "Vapi dashboard > API Keys > Private Key. Never the public key." },
  { k: "vapi_phone_number_id", label: "Vapi phone number ID", ph: "e.g. 8f3c…", hint: "Vapi dashboard > Phone Numbers > click your Vobiz number > copy its ID." },
  { k: "vapi_server_secret", label: "Vapi webhook secret", secret: true, ph: "a long random value", hint: "Any long random value. Put the same value in Vapi as the x-vapi-secret header for the server URL." },
  { k: "vapi_voice_id", label: "Voice (ElevenLabs voice ID)", ph: "e.g. a voice ID from ElevenLabs", hint: "Pick a female Indian-English voice in ElevenLabs, copy its Voice ID. In Vapi, add your ElevenLabs key under Providers first. Empty = Vapi's default voice." },
  { k: "caller_id", label: "Calling number (Vobiz)", ph: "+914440001111", hint: "The Vobiz number parents see. It must not be a counselor's own number." },
];

export default function IntegrationsForm({ initial }: { initial: IntegData | null }) {
  const [d, setD] = useState<IntegData | null>(initial);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  if (!d) return <div className="card"><span className="chip warn">Preview mode: connect the backend to edit connections</span></div>;

  const save = async () => {
    setBusy(true); setMsg(null);
    const r = await fetch("/api/integrations", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(vals) });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { setD(j.view); setVals({}); setMsg({ ok: true, text: "Saved. The next call uses these." }); }
    else setMsg({ ok: false, text: j.error || "Could not save" });
    setBusy(false);
  };
  const test = async () => {
    setBusy(true); setMsg(null);
    const r = await fetch("/api/integrations/test", { method: "POST" });
    const j = await r.json().catch(() => ({}));
    setMsg({ ok: !!j.ok, text: j.message || "No answer" }); setBusy(false);
  };
  const clear = async (k: string) => {
    if (!confirm("Remove the value saved from this page? The server setting, if any, will be used instead.")) return;
    setBusy(true);
    const r = await fetch("/api/integrations", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ clear: [k] }) });
    const j = await r.json().catch(() => ({}));
    if (r.ok) setD(j.view); setBusy(false);
  };

  return (
    <section className="card set">
      <h2>Phone connections <span className={`chip ${d.live ? "" : "warn"}`} style={{ marginLeft: 8 }}>{d.live ? "Live calling is on" : "Not connected yet"}</span></h2>
      <p className="sub">Only admins can see this page. Saved keys are shown as the last 4 characters. Leave a box empty to keep what is saved.</p>
      {FIELDS.map((f) => {
        const cur = d[f.k];
        return (
          <div className="int-row" key={f.k}>
            <label htmlFor={f.k}><b>{f.label}</b><div className="sub">{cur.set ? <>Saved: <span className="mono">{cur.value}</span>{cur.from ? ` · ${cur.from}` : ""}</> : "Not set"}</div></label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <input id={f.k} type={f.secret ? "password" : "text"} autoComplete="off" placeholder={cur.set ? "enter a new value to replace" : f.ph}
                value={vals[f.k] ?? ""} onChange={(e) => setVals({ ...vals, [f.k]: e.target.value })} style={{ flex: "1 1 240px" }} />
              {cur.from === "dashboard" && <button className="cbtn" disabled={busy} onClick={() => clear(f.k)}>Remove</button>}
            </div>
            <div className="hint">{f.hint}</div>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", marginTop: 16 }}>
        <button className="cbtn go" disabled={busy || !Object.values(vals).some(Boolean)} onClick={save}>Save</button>
        <button className="cbtn" disabled={busy} onClick={test}>Test the Vapi connection</button>
      </div>
      {msg && <div className={`chip ${msg.ok ? "" : "bad"}`} role="status" style={{ marginTop: 12 }}>{msg.text}</div>}
      <p className="sub" style={{ marginTop: 14 }}>Vapi server URL to enter in Vapi: <span className="mono">{(process.env.NEXT_PUBLIC_API_URL ?? "https://juvel-akshar-api.vercel.app")}/webhooks/vapi</span></p>
    </section>
  );
}
