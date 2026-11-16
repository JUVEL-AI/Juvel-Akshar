"use client";
import { useState, FormEvent } from "react";

export default function Login() {
  const [name, setName] = useState(""); const [password, setPassword] = useState("");
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
        <input id="p" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        {err && <div className="chip bad" role="alert">{err}</div>}
        <button className="cbtn go" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
