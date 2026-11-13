import type { ConsoleData } from "./types";
import { SAMPLE } from "./sample";
import { base, secretHeaders } from "./server";

/** Server-side only. Falls back to sample data when BACKEND_URL is not set (preview mode). */
export async function loadConsole(day?: string): Promise<{ data: ConsoleData; demo: boolean; error?: string }> {
  if (!base()) return { data: SAMPLE, demo: true };
  try {
    const r = await fetch(`${base()}/api/console${day ? `?day=${day}` : ""}`, { headers: secretHeaders(), cache: "no-store" });
    if (!r.ok) throw new Error(`backend answered ${r.status}`);
    return { data: (await r.json()) as ConsoleData, demo: false };
  } catch (e) {
    return { data: { ...SAMPLE, leads: [] }, demo: false, error: String(e) };
  }
}

export type SettingsData = {
  hours: Record<string, [string, string] | null>; retry_minutes: number[]; max_attempts: number;
  campuses: Record<string, { label: string; alert_emails: string[]; counselors: { name: string; number: string; on_duty: boolean }[] }>;
  admin_emails: string[];
};
export type AuditRow = { at: string; who: string; action: string; detail: string };

export async function loadSettings(): Promise<{ settings: SettingsData | null; audit: AuditRow[]; error?: string }> {
  if (!base()) return { settings: null, audit: [] };
  try {
    const [a, b] = await Promise.all([
      fetch(`${base()}/api/settings`, { headers: secretHeaders(), cache: "no-store" }),
      fetch(`${base()}/api/audit?limit=30`, { headers: secretHeaders(), cache: "no-store" })]);
    if (!a.ok) throw new Error(`backend answered ${a.status}`);
    return { settings: await a.json(), audit: b.ok ? await b.json() : [] };
  } catch (e) { return { settings: null, audit: [], error: String(e) }; }
}
