import Console from "@/components/Console";
import { loadConsole } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }: { searchParams: Promise<{ day?: string }> }) {
  const { day } = await searchParams;
  const safeDay = day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : undefined;
  const { data, demo, error } = await loadConsole(safeDay);
  const who = await whoFromPage();
  // Recordings, transcripts and summaries stay with admins until an admin releases them in Settings.
  // They are removed here, on the server, so a staff browser never receives them.
  if (who.role !== "admin" && !data.records_staff) {
    data.leads = data.leads.map((l) => ({ ...l, calls: (l.calls ?? []).map((c) => ({ ...c, recording: "", transcript: "", summary: "" })) }));
    data.records_hidden = true;
  }
  return <Console data={data} demo={demo} error={error} pinRequired={!!process.env.CONTROL_PIN} role={who.role} user={who.user} day={safeDay} />;
}
