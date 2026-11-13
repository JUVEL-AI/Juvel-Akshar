"use client";
import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import type { ConsoleData, Lead } from "@/lib/types";

const META: Record<string, { label: string; tone: string; bg: string; group: string }> = {
  TRANSFERRED: { label: "Transferred", tone: "#3DDC97", bg: "rgba(61,220,151,.14)", group: "transferred" },
  ANSWERED_NOT_TRANSFERRED: { label: "Call back needed", tone: "#FF8A8A", bg: "rgba(255,107,107,.15)", group: "callback" },
  RETRY: { label: "Retrying", tone: "#FFB547", bg: "rgba(255,181,71,.14)", group: "retry" },
  CALLING: { label: "Calling now", tone: "#3DDC97", bg: "rgba(61,220,151,.14)", group: "retry" },
  QUEUED: { label: "Waiting to call", tone: "#8FA1B5", bg: "rgba(143,161,181,.12)", group: "retry" },
  NO_RESPONSE: { label: "No response", tone: "#C4D0DD", bg: "rgba(143,161,181,.16)", group: "noresp" },
  NEEDS_REVIEW: { label: "Needs review", tone: "#6AA7FF", bg: "rgba(106,167,255,.15)", group: "review" },
  INVALID_PHONE: { label: "Bad phone number", tone: "#6AA7FF", bg: "rgba(106,167,255,.15)", group: "review" },
  STALE: { label: "Too old to call", tone: "#6AA7FF", bg: "rgba(106,167,255,.15)", group: "review" },
  DUPLICATE: { label: "Duplicate", tone: "#8FA1B5", bg: "rgba(143,161,181,.12)", group: "other" },
  RESOLVED: { label: "Done", tone: "#3DDC97", bg: "rgba(61,220,151,.10)", group: "other" },
  DO_NOT_CALL: { label: "Do not call", tone: "#8FA1B5", bg: "rgba(143,161,181,.12)", group: "other" },
};
const meta = (s: string) => META[s] ?? { label: s, tone: "#8FA1B5", bg: "rgba(143,161,181,.12)", group: "other" };
const FILTERS: [string, string][] = [["all", "All"], ["transferred", "Transferred"], ["retry", "Retrying"], ["callback", "Call back"], ["review", "Review"]];
const mmss = (s: number | null) => (s === null ? "—" : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`);

const STAGES = [
  { label: "Greeting the parent", says: "PARENT HEARS", text: "Hello, calling from Akshar Arbol International School regarding your registration. Your counselor from the admissions team will speak with you now. Please stay on the line." },
  { label: "Ringing the counselor", says: "COUNSELOR PHONE", text: "Rings for 25 seconds. If nobody answers, the backup number is tried next." },
  { label: "Whisper to counselor", says: "COUNSELOR HEARS", text: "Admissions inquiry. Parent Suresh, student Rohan, class LKG, T Nagar campus. Connecting now." },
  { label: "Connected", says: "LIVE", text: "Parent and counselor are talking. The result is saved automatically." },
];

function whisperFor(l: Lead, campusLabel: string) {
  if (!l.campus) return "No call is made. The campus could not be read, so an alert asks a person to follow up.";
  if (l.student && l.class) return `Admissions inquiry. Parent ${l.parent || "name not given"}, student ${l.student}, class ${l.class.replace(" grade", "")}, ${campusLabel} campus. Connecting now.`;
  if (l.student) return `Admissions inquiry. Parent ${l.parent || "name not given"}, student ${l.student}, ${campusLabel} campus. Connecting now.`;
  return `Admissions inquiry. Parent ${l.parent || "name not given"}, ${campusLabel} campus. Connecting now.`;
}

function timeline(l: Lead, campusLabel: string) {
  const t: { title: string; sub: string; color: string }[] = [{ title: "Form submitted", sub: `${l.time} · enquiry received`, color: "#6AA7FF" }];
  if (l.first_call_seconds !== null) t.push({ title: "Parent called", sub: `After ${mmss(l.first_call_seconds)} from the ${campusLabel} number`, color: "#3DDC97" });
  switch (l.status) {
    case "TRANSFERRED": t.push({ title: "Whisper played to the counselor", sub: "Student and class announced", color: "#3DDC97" }, { title: "Connected", sub: "Result saved", color: "#3DDC97" }); break;
    case "ANSWERED_NOT_TRANSFERRED": t.push({ title: "Parent answered", sub: "Counselor and backup did not pick up", color: "#FFB547" }, { title: "Fallback message played", sub: "Parent was told a counselor will call back", color: "#FFB547" }, { title: "Email sent: call back needed", sub: "Parent details included", color: "#FF8A8A" }); break;
    case "RETRY": t.push({ title: "No answer", sub: `Attempt ${l.attempts} of 4. Next try inside calling hours`, color: "#FFB547" }); break;
    case "NO_RESPONSE": t.push({ title: "All 4 attempts used", sub: "Parent never answered", color: "#8FA1B5" }, { title: "Email sent: please try by hand", sub: "Parent details included", color: "#FF8A8A" }); break;
    default: if (l.note) t.push({ title: meta(l.status).label, sub: l.note, color: "#6AA7FF" });
  }
  return t;
}

export default function Console({ data, demo, error, pinRequired = false }: { data: ConsoleData; demo: boolean; error?: string; pinRequired?: boolean }) {
  const router = useRouter();
  const [campus, setCampus] = useState("all");
  const [filter, setFilter] = useState("all");
  const [picked, setPicked] = useState<number | null>(null);
  const [stage, setStage] = useState(0);
  const [ask, setAsk] = useState<"start" | "pause" | "stop" | null>(null);
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [ctlErr, setCtlErr] = useState("");
  const ctl = data.control?.state ?? "running";

  useEffect(() => { const t = setInterval(() => setStage((s) => (s + 1) % 4), 2600); return () => clearInterval(t); }, []);
  useEffect(() => { if (demo) return; const t = setInterval(() => router.refresh(), 15000); return () => clearInterval(t); }, [demo, router]);

  const label = (k: string) => data.campuses[k] ?? (k ? k : "Campus unknown");
  const inCampus = useMemo(() => data.leads.filter((l) => campus === "all" || l.campus === campus), [data, campus]);
  const rows = inCampus.filter((l) => filter === "all" || meta(l.status).group === filter);
  const sel = data.leads.find((l) => l.id === picked) ?? rows[0] ?? data.leads[0];

  const called = inCampus.filter((l) => l.first_call_seconds !== null);
  const fast = called.filter((l) => (l.first_call_seconds ?? 0) <= 300).length;
  const pct = called.length ? Math.round((fast * 100) / called.length) : 0;
  const secs = called.map((l) => l.first_call_seconds as number).sort((a, b) => a - b);
  const med = secs.length ? secs[Math.floor(secs.length / 2)] : 0;
  const tr = inCampus.filter((l) => l.status === "TRANSFERRED").length;
  const cb = inCampus.filter((l) => l.status === "ANSWERED_NOT_TRANSFERRED").length;
  const share = inCampus.length ? Math.round((tr * 100) / inCampus.length) : 0;
  const kpis = [
    { l: "Enquiries today", v: String(inCampus.length), n: "from the registration form", c: "#E8EEF5", f: 100 },
    { l: "Called within 5 minutes", v: `${pct}%`, n: `median first call ${mmss(med)}`, c: "#3DDC97", f: pct },
    { l: "Connected to a counselor", v: String(tr), n: `${share}% of enquiries`, c: "#6AA7FF", f: share },
    { l: "Call-backs needed", v: String(cb), n: cb ? "counselor must ring these parents" : "all clear", c: cb ? "#FF8A8A" : "#3DDC97", f: cb ? 100 : 8 },
  ];
  const attn = inCampus.filter((l) => ["ANSWERED_NOT_TRANSFERRED", "NEEDS_REVIEW", "NO_RESPONSE", "INVALID_PHONE", "STALE"].includes(l.status));
  const outcomeKeys = ["TRANSFERRED", "RETRY", "ANSWERED_NOT_TRANSFERRED", "NO_RESPONSE", "NEEDS_REVIEW"];
  const total = Math.max(inCampus.length, 1);
  const byHour: Record<number, number> = {};
  inCampus.forEach((l) => { const h = parseInt(l.time.slice(0, 2), 10); byHour[h] = (byHour[h] ?? 0) + 1; });
  const hours = Array.from({ length: 10 }, (_, i) => 9 + i);
  const maxH = Math.max(1, ...hours.map((h) => byHour[h] ?? 0));
  const markDone = async (id: number) => {
    if (demo) return;
    await fetch(`/api/resolve/${id}`, { method: "POST" });
    router.refresh();
  };
  const sendControl = async () => {
    if (!ask) return;
    if (demo) { setAsk(null); return; }
    setBusy(true); setCtlErr("");
    try {
      const r = await fetch("/api/control", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action: ask, pin }) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { setCtlErr(j.error || "Could not change the setting. Try again."); setBusy(false); return; }
      setAsk(null); setPin(""); router.refresh();
    } catch { setCtlErr("Could not reach the server."); }
    setBusy(false);
  };
  const live = data.leads.find((l) => l.status === "CALLING");
  const st = STAGES[stage];

  return (
    <div className="shell">
      <header className="top">
        <div className="brand">
          <div className="logo">V</div>
          <div><h1>Juvel Calling</h1><div className="sub">Akshar Arbol International School · Admissions</div></div>
        </div>
        <div className="chips">
          {demo && <span className="chip warn">Sample data for preview</span>}
          {error && <span className="chip bad">Backend not reachable</span>}
          <span className="chip"><span className="dot" />Mon–Fri 8:30–2:30 · Sat 9:00–2:00</span>
        </div>
      </header>

      <section className={`ctl ${ctl}`} aria-label="Calling control">
        <div className="ctl-state">
          <span className="ctl-dot" />
          <div>
            <b>{ctl === "running" ? "Calling is ON" : ctl === "paused" ? "Calling is PAUSED" : "Calling is STOPPED"}</b>
            <div className="sub">
              {ctl === "running" && "New enquiries are called within minutes, inside calling hours."}
              {ctl === "paused" && "No calls are going out. Enquiries are still saved and will be called when you press Start."}
              {ctl === "stopped" && "No calls are going out. Waiting enquiries were moved to Needs review for your team to call by hand."}
            </div>
          </div>
        </div>
        <div className="ctl-btns">
          <button type="button" className="cbtn go" disabled={ctl === "running"} onClick={() => { setCtlErr(""); setAsk("start"); }}>▶ Start</button>
          <button type="button" className="cbtn pause" disabled={ctl !== "running"} onClick={() => { setCtlErr(""); setAsk("pause"); }}>⏸ Pause</button>
          <button type="button" className="cbtn stop" disabled={ctl === "stopped"} onClick={() => { setCtlErr(""); setAsk("stop"); }}>⏹ Stop</button>
        </div>
      </section>

      {ask && (
        <div className="modal-bg" role="dialog" aria-modal="true" aria-label="Confirm" onClick={() => !busy && setAsk(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h2>{ask === "start" ? "Start calling?" : ask === "pause" ? "Pause calling?" : "Stop calling?"}</h2>
            <p>
              {ask === "start" && "Calls go out again. Enquiries that arrived while paused are called now (anything older than 48 hours is skipped)."}
              {ask === "pause" && "No new calls will start. Calls already ringing finish normally. Enquiries are still saved and called when you press Start."}
              {ask === "stop" && "No new calls will start, and every enquiry waiting to be called is moved to Needs review for your team to call by hand. Calls already ringing finish normally. New enquiries are also parked until you press Start."}
            </p>
            {pinRequired && <label className="pin">PIN<input type="password" inputMode="numeric" autoComplete="off" value={pin} onChange={(e) => setPin(e.target.value)} /></label>}
            {ctlErr && <div className="err">{ctlErr}</div>}
            {demo && <div className="sub">Preview mode: nothing will change.</div>}
            <div className="modal-btns">
              <button type="button" className="cbtn" disabled={busy} onClick={() => setAsk(null)}>Cancel</button>
              <button type="button" className={`cbtn ${ask === "start" ? "go" : ask === "pause" ? "pause" : "stop"}`} disabled={busy || (pinRequired && !pin)} onClick={sendControl}>
                {busy ? "Please wait…" : ask === "start" ? "Yes, start" : ask === "pause" ? "Yes, pause" : "Yes, stop"}
              </button>
            </div>
          </div>
        </div>
      )}

      <nav className="tabs" aria-label="Campus">
        {[["all", "All campuses"], ...Object.entries(data.campuses)].map(([k, name]) => (
          <button key={k} type="button" className="tab" aria-pressed={campus === k} onClick={() => setCampus(k)}>
            {name}<span className="n">{data.leads.filter((l) => k === "all" || l.campus === k).length}</span>
          </button>
        ))}
      </nav>

      <section className="kpis">
        {kpis.map((k) => (
          <div className="card kpi" key={k.l}>
            <div className="l">{k.l}</div><div className="v" style={{ color: k.c }}>{k.v}</div><div className="sub">{k.n}</div>
            <div className="bar" style={{ width: `${k.f}%`, background: k.c }} />
          </div>
        ))}
      </section>

      <div className="grid">
        <div className="main">
          <section className="card live">
            <div className="row-between">
              <div style={{ display: "flex", gap: 10, alignItems: "center" }}><span className="dot" /><h2>{live ? "Call in progress" : "How every call runs"}</h2></div>
              <span className="mono sub">{live ? `${live.parent} · ${label(live.campus)} campus` : "Preview of the four steps"}</span>
            </div>
            <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 22 }}>
              <div className="wave" aria-hidden="true">{Array.from({ length: 16 }, (_, i) => <span key={i} style={{ animationDelay: `${(i * 0.09).toFixed(2)}s` }} />)}</div>
              <div className="stages">
                {STAGES.map((s, i) => {
                  const done = i < stage, now = i === stage;
                  return (
                    <div className="stage" key={s.label}>
                      <div className="r">
                        <span className="c" style={{ background: done || now ? "#3DDC97" : "#1B2531", borderColor: now ? "rgba(61,220,151,.4)" : "transparent" }}>{i + 1}</span>
                        <span className="ln" style={{ background: done ? "#3DDC97" : "#243040" }} />
                      </div>
                      <div className="t" style={{ color: now ? "#E8EEF5" : done ? "#C4D0DD" : "#8FA1B5" }}>{s.label}</div>
                    </div>
                  );
                })}
              </div>
            </div>
            <div className="say"><b>{st.says}</b><br />{st.text}</div>
          </section>

          <section className="card table">
            <div className="row-between" style={{ padding: "0 18px" }}>
              <h2>Today&apos;s enquiries</h2>
            </div>
            <div className="fchips">
              {FILTERS.map(([k, name]) => <button key={k} type="button" className="fchip" aria-pressed={filter === k} onClick={() => setFilter(k)}>{name}</button>)}
            </div>
            <div className="scroll"><div className="t-in">
              <div className="cols th"><span>Time</span><span>Parent / student</span><span>Campus · class</span><span>First call</span><span>Status</span><span>Attempts</span></div>
              {rows.map((l) => {
                const m = meta(l.status);
                return (
                  <button key={l.id} type="button" className="cols tr" aria-pressed={sel?.id === l.id} onClick={() => setPicked(l.id)}>
                    <span className="mono sub">{l.time}</span>
                    <span style={{ display: "flex", flexDirection: "column", gap: 2, minWidth: 0 }}><b>{l.parent || "—"}</b><span className="sub">{l.student ? `Student ${l.student}` : "Student name not given"}</span></span>
                    <span style={{ fontSize: 13.5, color: "#C4D0DD" }}>{label(l.campus)} · {l.class || "Class unclear"}</span>
                    <span className="mono" style={{ fontSize: 13, color: (l.first_call_seconds ?? 9999) <= 300 ? "#3DDC97" : "#8FA1B5" }}>{mmss(l.first_call_seconds)}</span>
                    <span><span className="pill" style={{ color: m.tone, background: m.bg }}>{l.status === "RETRY" ? `Retry ${l.attempts} of 4` : m.label}</span></span>
                    <span className="mono sub">{l.attempts} / 4</span>
                  </button>
                );
              })}
              {rows.length === 0 && <div style={{ padding: "28px 18px", color: "#8FA1B5", fontSize: 14 }}>No enquiries match this filter.</div>}
            </div></div>
          </section>

          {sel && (
            <section className="card detail">
              <div>
                <div><div className="eyebrow">Selected enquiry</div>
                  <div style={{ fontSize: 22, fontWeight: 800, marginTop: 4 }}>{sel.parent || "—"}</div>
                  <div className="mono sub" style={{ marginTop: 2 }}>{sel.phone} · {label(sel.campus)}</div></div>
                <div>
                  {timeline(sel, label(sel.campus)).map((e, i) => (
                    <div className="tl" key={i}>
                      <div className="rail"><i style={{ background: e.color }} /><s /></div>
                      <div className="body"><div style={{ fontSize: 14, fontWeight: 700 }}>{e.title}</div><div className="sub" style={{ marginTop: 2 }}>{e.sub}</div></div>
                    </div>
                  ))}
                </div>
              </div>
              <div>
                <div className="eyebrow">What each person hears</div>
                <div className="say2"><b style={{ color: "#6AA7FF" }}>PARENT</b><p>Hello, calling from Akshar Arbol International School regarding your registration. Your counselor from the admissions team will speak with you now. Please stay on the line.</p></div>
                <div className="say2"><b style={{ color: "#3DDC97" }}>COUNSELOR (BEFORE CONNECTING)</b><p>{whisperFor(sel, label(sel.campus))}</p></div>
                <div className="say2"><b style={{ color: "#FFB547" }}>IF NOBODY PICKS UP</b><p>Thank you for staying on the call. Our admissions counselor will call you back shortly. We look forward to speaking with you soon.</p></div>
              </div>
            </section>
          )}
        </div>

        <aside className="side">
          <section className="card attn">
            <div className="row-between"><h2 style={{ color: "#FF8A8A" }}>Needs your attention</h2>
              <span className="mono" style={{ fontSize: 13, fontWeight: 600, color: "#06231A", background: "#FF8A8A", borderRadius: 999, padding: "3px 10px" }}>{attn.length}</span></div>
            {attn.map((l) => (
              <div className="it" key={l.id}>
                <div style={{ fontSize: 14, fontWeight: 700 }}>{l.status === "ANSWERED_NOT_TRANSFERRED" ? `Call back ${l.parent}` : l.status === "NO_RESPONSE" ? `${l.parent} did not answer` : `Check ${l.parent || "this"} enquiry`}</div>
                <div className="sub" style={{ lineHeight: 1.5 }}>{label(l.campus)} · {l.phone} · {l.status === "ANSWERED_NOT_TRANSFERRED" ? "parent answered but no counselor was free" : l.note || meta(l.status).label}</div>
                {!demo && <button type="button" className="fchip" style={{ marginTop: 8 }} onClick={() => markDone(l.id)}>Mark as done</button>}
              </div>
            ))}
            {attn.length === 0 && <div className="sub" style={{ fontSize: 13.5 }}>Nothing waiting for this campus.</div>}
          </section>

          <section className="card" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <h2>Where enquiries ended up</h2>
            {outcomeKeys.map((k) => {
              const n = inCampus.filter((l) => l.status === k).length;
              return (
                <div className="out" key={k}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13 }}><b style={{ fontWeight: 600 }}>{meta(k).label}</b><span className="mono sub">{n}</span></div>
                  <div className="trk"><i style={{ width: `${Math.round((n * 100) / total)}%`, background: meta(k).tone }} /></div>
                </div>
              );
            })}
          </section>

          <section className="card" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
            <h2>Enquiries by hour</h2>
            <div className="bars">
              {hours.map((h) => (
                <div key={h}><i style={{ height: `${Math.max(4, Math.round(((byHour[h] ?? 0) / maxH) * 100))}px` }} /><span className="mono sub" style={{ fontSize: 10.5 }}>{h}</span></div>
              ))}
            </div>
          </section>

          <section className="card" style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <h2>Safety net</h2>
            {["Inquiry arrives but no call is placed: you get an email within 10 minutes.", "Counselor busy: the backup number rings automatically.", "Nobody reachable: the parent hears a callback promise and you get the details.", "Calls that get stuck are closed and retried, never left hanging."].map((t) => (
              <div className="safe" key={t}><u>✓</u><span>{t}</span></div>
            ))}
          </section>
        </aside>
      </div>
    </div>
  );
}
