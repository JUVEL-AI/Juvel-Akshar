"use client";
import { useState } from "react";
import type { SettingsData, AuditRow } from "@/lib/backend";

const DAYS: [string, string][] = [["mon", "Monday"], ["tue", "Tuesday"], ["wed", "Wednesday"], ["thu", "Thursday"], ["fri", "Friday"], ["sat", "Saturday"], ["sun", "Sunday"]];

export default function SettingsForm({ initial, audit }: { initial: SettingsData | null; audit: AuditRow[] }) {
  const [s, setS] = useState<SettingsData | null>(initial);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  if (!s) return <div className="card"><span className="chip warn">Preview mode: connect the backend to edit settings</span></div>;

  const setHour = (d: string, i: 0 | 1, v: string) => setS({ ...s, hours: { ...s.hours, [d]: [i === 0 ? v : s.hours[d]?.[0] ?? "09:00", i === 1 ? v : s.hours[d]?.[1] ?? "14:00"] } });
  const setCounselor = (k: string, idx: number, patch: Partial<{ name: string; number: string; on_duty: boolean }>) =>
    setS({ ...s, campuses: { ...s.campuses, [k]: { ...s.campuses[k], counselors: s.campuses[k].counselors.map((c, i) => (i === idx ? { ...c, ...patch } : c)) } } });

  const save = async () => {
    setBusy(true); setMsg(null);
    const r = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({
      hours: s.hours, max_attempts: s.max_attempts, retry_minutes: s.retry_minutes,
      campuses: Object.fromEntries(Object.entries(s.campuses).map(([k, c]) => [k, { alert_emails: c.alert_emails, counselors: c.counselors }])) }) });
    const j = await r.json().catch(() => ({}));
    if (r.ok) { setS(j.settings); setMsg({ ok: true, text: "Saved. New settings apply to the next call." }); }
    else setMsg({ ok: false, text: j.error || "Could not save" });
    setBusy(false);
  };

  return (
    <>
      <section className="card set">
        <h2>Calling days and hours</h2>
        <p className="sub">Parents are only called inside these hours. Tick Closed for a day with no calls.</p>
        {DAYS.map(([d, name]) => {
          const closed = !s.hours[d];
          return (
            <div className="set-row" key={d}>
              <b>{name}</b>
              <label><input type="checkbox" checked={closed} onChange={(e) => setS({ ...s, hours: { ...s.hours, [d]: e.target.checked ? null : ["09:00", "14:00"] } })} /> Closed</label>
              <input type="time" disabled={closed} value={s.hours[d]?.[0] ?? ""} onChange={(e) => setHour(d, 0, e.target.value)} aria-label={`${name} opens`} />
              <span>to</span>
              <input type="time" disabled={closed} value={s.hours[d]?.[1] ?? ""} onChange={(e) => setHour(d, 1, e.target.value)} aria-label={`${name} closes`} />
            </div>
          );
        })}
      </section>

      {Object.entries(s.campuses).map(([k, c]) => (
        <section className="card set" key={k}>
          <h2>{c.label} campus</h2>
          <p className="sub">The first counselor marked On duty takes the calls. If nobody is on duty, enquiries wait and the team is emailed after 10 minutes.</p>
          {c.counselors.map((x, i) => (
            <div className="set-row" key={i}>
              <input value={x.name} onChange={(e) => setCounselor(k, i, { name: e.target.value })} aria-label="Counselor name" placeholder="Name" />
              <input value={x.number} onChange={(e) => setCounselor(k, i, { number: e.target.value })} aria-label="Counselor phone" placeholder="+91…" inputMode="tel" />
              <label><input type="checkbox" checked={x.on_duty} onChange={(e) => setCounselor(k, i, { on_duty: e.target.checked })} /> On duty</label>
            </div>
          ))}
          <label className="set-col">Alert emails (separate with commas)
            <input value={c.alert_emails.join(", ")} onChange={(e) => setS({ ...s, campuses: { ...s.campuses, [k]: { ...c, alert_emails: e.target.value.split(",").map((m) => m.trim()) } } })} />
          </label>
        </section>
      ))}

      <section className="card set">
        <h2>Retries</h2>
        <div className="set-row">
          <label className="set-col">Attempts per parent
            <input type="number" min={1} max={6} value={s.max_attempts} onChange={(e) => setS({ ...s, max_attempts: Number(e.target.value) })} />
          </label>
          <label className="set-col" style={{ flex: 1 }}>Gap before each retry, in minutes
            <input value={s.retry_minutes.join(", ")} onChange={(e) => setS({ ...s, retry_minutes: e.target.value.split(",").map((m) => Number(m.trim())).filter((n) => !Number.isNaN(n) && n > 0) })} />
          </label>
        </div>
      </section>

      <div className="row-between">
        <div>{msg && <span className={`chip ${msg.ok ? "" : "bad"}`}>{msg.text}</span>}</div>
        <button type="button" className="cbtn go" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save settings"}</button>
      </div>

      <section className="card set">
        <h2>Recent changes</h2>
        {audit.length === 0 && <p className="sub">Nothing yet.</p>}
        {audit.map((a, i) => (
          <div className="audit" key={i}><span className="mono">{new Date(a.at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
            <b>{a.who}</b><span>{a.action.replace(/_/g, " ")}</span><span className="sub">{a.detail}</span></div>
        ))}
      </section>
    </>
  );
}
