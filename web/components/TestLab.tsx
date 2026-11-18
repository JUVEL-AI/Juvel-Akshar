"use client";
import { useCallback, useEffect, useState } from "react";
import type { Lead } from "@/lib/types";

export default function TestLab({ campuses }: { campuses: Record<string, string> }) {
  const [f, setF] = useState({ name: "Test Parent", student: "Test Child", grade: "LKG", parent_phone: "", counselor_phone: "", campus: Object.keys(campuses)[0] ?? "" });
  const [leads, setLeads] = useState<Lead[]>([]);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const r = await fetch("/api/test", { cache: "no-store" });
    if (r.ok) setLeads(((await r.json()).leads ?? []) as Lead[]);
  }, []);
  useEffect(() => { load(); const t = setInterval(load, 8000); return () => clearInterval(t); }, [load]);

  const start = async () => {
    setBusy(true); setMsg(null);
    const r = await fetch("/api/test/lead", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(f) });
    const j = await r.json().catch(() => ({}));
    setMsg(r.ok ? { ok: true, text: "Test call started. Your 'parent' phone should ring in a few seconds." } : { ok: false, text: j.error || "Could not start the test" });
    setBusy(false); load();
  };
  const clear = async () => {
    if (!confirm("Delete all test enquiries and their recordings? Real data is not touched.")) return;
    const r = await fetch("/api/test", { method: "DELETE" });
    const j = await r.json().catch(() => ({}));
    setMsg({ ok: r.ok, text: r.ok ? `Cleared ${j.deleted ?? 0} test enquiries.` : "Could not clear" }); load();
  };
  const field = (k: keyof typeof f, label: string, ph = "") => (
    <label className="set-col" style={{ flex: 1, minWidth: 180 }}>{label}
      <input value={f[k]} placeholder={ph} onChange={(e) => setF({ ...f, [k]: e.target.value })} /></label>);

  return (
    <>
      <section className="card set">
        <h2>Safe test call</h2>
        <p className="sub">Uses <b>your own two phones</b>. The first plays the parent and gets the call. The second plays the counselor and gets the transfer.
          Nothing reaches real parents or counselors, no email goes to the school, and test calls never show in the dashboard, reports or counts.</p>
        <div className="set-row">{field("parent_phone", "Phone that plays the parent", "e.g. 9940063093")}{field("counselor_phone", "Phone that plays the counselor", "e.g. 9566191956")}</div>
        <div className="set-row">{field("name", "Parent name")}{field("student", "Student name")}{field("grade", "Class", "LKG, grade-3, elc-1")}
          <label className="set-col" style={{ flex: 1, minWidth: 180 }}>Campus
            <select value={f.campus} onChange={(e) => setF({ ...f, campus: e.target.value })} style={{ minHeight: 44, borderRadius: 10, background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line)", padding: "0 10px" }}>
              {Object.entries(campuses).map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select></label></div>
        <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
          <button className="cbtn go" disabled={busy || !f.parent_phone || !f.counselor_phone} onClick={start}>{busy ? "Starting…" : "Place test call"}</button>
          <button className="cbtn" onClick={load}>Refresh</button>
          <button className="cbtn" onClick={clear}>Clear all test data</button>
        </div>
        {msg && <div className={`chip ${msg.ok ? "" : "bad"}`} role="status" style={{ marginTop: 10 }}>{msg.text}</div>}
        <p className="sub">The test has its own start button. It works even while real calling is <b>paused or stopped</b>, and at any time of day. A test is never retried.</p>
      </section>

      <section className="card set">
        <h2>Test results</h2>
        {leads.length === 0 && <p className="sub">No test calls yet.</p>}
        {leads.map((l) => (
          <div key={l.id} style={{ borderTop: "1px solid var(--line2)", padding: "12px 0" }}>
            <div><b>{l.parent}</b> · {l.student || "no student"} · {l.class || "no class"} · {campuses[l.campus] ?? l.campus} · <span className="chip">{l.status}</span></div>
            <div className="sub">{l.note}</div>
            {(l.calls ?? []).map((c, i) => (
              <div key={i} style={{ marginTop: 8 }}>
                <div className="sub">Attempt {c.attempt} · {c.at} · {c.outcome || "in progress"}{c.seconds != null ? ` · ${c.seconds}s` : ""}{c.reason ? ` · ${c.reason}` : ""}</div>
                {c.recording && <audio controls preload="none" src={c.recording} style={{ width: "100%", marginTop: 4 }} />}
                {c.summary && <div className="sub"><b>Summary:</b> {c.summary}</div>}
                {c.transcript && <details><summary style={{ cursor: "pointer" }}>Transcript</summary><pre className="mono sub" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{c.transcript}</pre></details>}
              </div>))}
          </div>))}
      </section>
    </>
  );
}
