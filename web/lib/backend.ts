import type { ConsoleData } from "./types";
import { SAMPLE } from "./sample";

/** Server-side only. Falls back to sample data when BACKEND_URL is not set (preview mode). */
export async function loadConsole(): Promise<{ data: ConsoleData; demo: boolean; error?: string }> {
  const base = process.env.BACKEND_URL;
  if (!base) return { data: SAMPLE, demo: true };
  try {
    const r = await fetch(`${base.replace(/\/$/, "")}/api/console`, {
      headers: { "X-Webhook-Secret": process.env.WEBHOOK_SECRET ?? "" }, cache: "no-store",
    });
    if (!r.ok) throw new Error(`backend answered ${r.status}`);
    return { data: (await r.json()) as ConsoleData, demo: false };
  } catch (e) {
    return { data: { ...SAMPLE, leads: [] }, demo: false, error: String(e) };
  }
}
