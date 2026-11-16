"use client";
export default function LogoutButton() {
  return (
    <button className="chip" style={{ cursor: "pointer", color: "inherit" }} onClick={async () => {
      await fetch("/api/logout", { method: "POST" });
      window.location.href = "/login";
    }}>Sign out</button>
  );
}
