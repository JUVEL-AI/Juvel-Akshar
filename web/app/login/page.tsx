"use client";
import { useState, FormEvent } from "react";

export default function Login() {
  const [name, setName] = useState(""); const [password, setPassword] = useState("");
  const [show, setShow] = useState(false);
  const [err, setErr] = useState(""); const [busy, setBusy] = useState(false);
  const go = async (e: FormEvent) => {
    e.preventDefault(); setBusy(true); setErr("");
    const r = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, password }) });
    if (r.ok) {
      const next = new URLSearchParams(window.location.search).get("next") ?? "/";
      window.location.href = next.startsWith("/") && !next.startsWith("//") ? next : "/";
    } else { const j = await r.json().catch(() => ({})); setErr(j.error || "Could not sign in"); setBusy(false); }
  };
  return (
    <div className="shell" style={{ alignItems: "center", justifyContent: "center" }}>
      <form className="card set" onSubmit={go} style={{ width: "min(400px,100%)", display: "flex", flexDirection: "column", gap: 14 }}>
        <div className="brand"><div className="logo">V</div>
          <div><h1>Juvel Calling</h1><div className="sub">Admissions call console · sign in</div></div></div>
        <label className="sub" htmlFor="n">Name</label>
        <input id="n" type="text" autoComplete="username" autoCapitalize="none" value={name} onChange={(e) => setName(e.target.value)} required />
        <label className="sub" htmlFor="p">Password</label>
        <div style={{ position: "relative" }}>
          <input id="p" type={show ? "text" : "password"} autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required style={{ width: "100%", boxSizing: "border-box", paddingRight: 70 }} />
          <button type="button" className="cbtn" onClick={() => setShow(!show)} aria-pressed={show} aria-label={show ? "Hide password" : "Show password"}
            style={{ position: "absolute", right: 6, top: 6, minHeight: 32, padding: "0 10px", fontSize: 13 }}>{show ? "Hide" : "Show"}</button>
        </div>
        {err && <div className="chip bad" role="alert">{err}</div>}
        <button className="cbtn go" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        <a className="sub" href="/privacy" style={{ textAlign: "center" }}>Privacy notice</a>
      </form>
    </div>
  );
}
