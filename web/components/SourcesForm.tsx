"use client";
import { useState } from "react";
import type { SourcesData, SheetCfg } from "@/lib/backend";

type Preview = { title: string; tabs: string[]; worksheet: string; headers: string[]; row_count: number; sample: Record<string, string>[]; suggested: Record<string, string> };
type Row = SheetCfg & { spreadsheet: string; preview?: Preview; busy?: boolean; err?: string };

const blank = (n: number): Row => ({ id: "", label: `Sheet ${n}`, spreadsheet_id: "", spreadsheet: "", worksheet: "", enabled: true, columns: {} });

export default function SourcesForm({ initial }: { initial: SourcesData | null }) {
  const [d, setD] = useState<SourcesData | null>(initial);
  const [rows, setRows] = useState<Row[]>(() => (initial?.sheets ?? []).map((s) => ({ ...s, spreadsheet: s.spreadsheet_id })));
  const [dc, setDc] = useState(initial?.default_campus ?? "");
  const [order, setOrder] = useState(initial?.date_order ?? "mdy");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  if (!d) return <div className="card"><span className="chip warn">Preview mode: connect the backend to choose sheets</span></div>;

  const upd = (i: number, p: Partial<Row>) => setRows(rows.map((r, k) => (k === i ? { ...r, ...p } : r)));
  const check = async (i: number) => {
    const r = rows[i]; upd(i, { busy: true, err: "" });
    const res = await fetch("/api/sources/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ spreadsheet: r.spreadsheet, worksheet: r.worksheet }) });
    const j = await res.json().catch(() => ({}));
    if (!res.ok) { upd(i, { busy: false, err: j.error || "Could not read the sheet" }); return; }
    const p: Preview = j.preview;
    const cols = Object.keys(r.columns).length ? r.columns : p.suggested;
    upd(i, { busy: false, preview: p, worksheet: p.worksheet, columns: cols, spreadsheet_id: j.preview.spreadsheet_id });
  };
  const save = async () => {
    setBusy(true); setMsg(null);
    const res = await fetch("/api/sources", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      sheets: rows.map((r) => ({ id: r.id, label: r.label, spreadsheet: r.spreadsheet, worksheet: r.worksheet, columns: r.columns, enabled: r.enabled })), default_campus: dc, date_order: order }) });
    const j = await res.json().catch(() => ({}));
    if (res.ok) { setD(j.view); setMsg({ ok: true, text: "Saved. A sheet added now is read once to note who is already in it. Only enquiries that arrive after that are called." }); }
    else setMsg({ ok: false, text: j.error || "Could not save" });
    setBusy(false);
  };

  return (
    <>
      <section className="card set">
        <h2>Google access</h2>
        {d.has_credentials
          ? <p className="sub">Share each lead sheet with this address, as a <b>Viewer</b>: <span className="mono">{d.service_email || "(key saved)"}</span></p>
          : <p className="sub">No Google key yet. Add the <b>Google service account key</b> on the <a href="/integrations" style={{ color: "var(--blue)" }}>Connections</a> page first.</p>}
        {d.from_server && <p className="sub">Now reading the sheet from the server settings. Saving a sheet below replaces it.</p>}
      </section>

      {rows.map((r, i) => (
        <section className="card set" key={i}>
          <div className="set-row">
            <label className="set-col" style={{ flex: 1 }}>Name
              <input value={r.label} onChange={(e) => upd(i, { label: e.target.value })} /></label>
            <label className="set-col"><span>Reading</span>
              <span><input type="checkbox" checked={r.enabled} onChange={(e) => upd(i, { enabled: e.target.checked })} /> On</span></label>
            <button className="cbtn" onClick={() => setRows(rows.filter((_, k) => k !== i))}>Remove</button>
          </div>
          <div className="set-row">
            <label className="set-col" style={{ flex: 2 }}>Google Sheet link
              <input value={r.spreadsheet} placeholder="https://docs.google.com/spreadsheets/d/…" onChange={(e) => upd(i, { spreadsheet: e.target.value })} /></label>
            <label className="set-col" style={{ flex: 1 }}>Tab name
              {r.preview ? <select value={r.worksheet} onChange={(e) => upd(i, { worksheet: e.target.value })} style={{ minHeight: 44, borderRadius: 10, background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line)" }}>
                {r.preview.tabs.map((t) => <option key={t}>{t}</option>)}</select>
                : <input value={r.worksheet} placeholder="Sheet1" onChange={(e) => upd(i, { worksheet: e.target.value })} />}</label>
            <button className="cbtn go" disabled={r.busy || !r.spreadsheet} onClick={() => check(i)}>{r.busy ? "Reading…" : "Check sheet"}</button>
          </div>
          {r.err && <div className="chip bad" role="alert">{r.err}</div>}
          {r.preview && <p className="sub">Found “{r.preview.title}”: {r.preview.row_count} enquiries so far. Pick which column holds each detail the call needs.</p>}
          {(r.preview || Object.keys(r.columns).length > 0) && (
            <div>
              {d.fields.map((f) => {
                const heads = r.preview?.headers ?? Object.values(r.columns);
                return (
                  <div className="int-row" key={f.key}>
                    <label><b>{f.label}</b>{f.required ? " *" : ""}</label>
                    <select value={r.columns[f.key] ?? ""} onChange={(e) => upd(i, { columns: { ...r.columns, [f.key]: e.target.value } })}
                      style={{ minHeight: 44, borderRadius: 10, background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line)", padding: "0 10px" }}>
                      <option value="">{f.required ? "Choose a column…" : "Not used"}</option>
                      {Array.from(new Set([...(r.columns[f.key] ? [r.columns[f.key]] : []), ...heads])).map((h) => <option key={h} value={h}>{h}</option>)}
                    </select>
                  </div>);
              })}
              {r.preview && r.preview.sample.length > 0 && <details style={{ marginTop: 10 }}><summary style={{ cursor: "pointer" }}>First rows of the sheet</summary>
                <pre className="mono sub" style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{JSON.stringify(r.preview.sample, null, 1)}</pre></details>}
            </div>)}
        </section>
      ))}
      <button className="cbtn" onClick={() => setRows([...rows, blank(rows.length + 1)])}>+ Add another sheet</button>

      <section className="card set">
        <h2>Rules for all sheets</h2>
        <div className="set-row">
          <label className="set-col" style={{ flex: 1 }}>If a parent answers “either” or leaves the campus blank, call
            <select value={dc} onChange={(e) => setDc(e.target.value)} style={{ minHeight: 44, borderRadius: 10, background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line)", padding: "0 10px" }}>
              <option value="">Nobody: a person checks it</option>
              {Object.entries(d.campuses).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
            </select></label>
          <label className="set-col" style={{ flex: 1 }}>Dates like 03/04/2026 mean
            <select value={order} onChange={(e) => setOrder(e.target.value)} style={{ minHeight: 44, borderRadius: 10, background: "var(--bg)", color: "var(--text)", border: "1px solid var(--line)", padding: "0 10px" }}>
              <option value="mdy">Month first (March 4)</option><option value="dmy">Day first (3 April)</option>
            </select></label>
        </div>
      </section>

      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <button className="cbtn go" disabled={busy} onClick={save}>Save sheets</button>
      </div>
      {msg && <div className={`chip ${msg.ok ? "" : "bad"}`} role="status">{msg.text}</div>}
    </>
  );
}
