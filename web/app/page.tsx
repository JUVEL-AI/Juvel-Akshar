import Console from "@/components/Console";
import { loadConsole } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";
import { campusScope } from "@/lib/auth";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }: { searchParams: Promise<{ day?: string }> }) {
  const { day } = await searchParams;
  const safeDay = day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : undefined;
  const { data, demo, error } = await loadConsole(safeDay);
  const who = await whoFromPage();
  // Recordings, transcripts and summaries stay with admins until an admin releases them in Settings.
  // They are removed here, on the server, so a staff browser never receives them.
  if (who.role === "staff" && !data.records_staff) {
    data.leads = data.leads.map((l) => ({ ...l, calls: (l.calls ?? []).map((c) => ({ ...c, recording: "", transcript: "", summary: "" })) }));
    data.records_hidden = true;
  }
  const only = campusScope(who.user, who.role);               // a counselor limited to one campus never receives the other campus's data
  if (only) {
    data.leads = data.leads.filter((l) => l.campus === only);
    data.live = (data.live ?? []).filter((l) => l.campus === only);
    data.campuses = Object.fromEntries(Object.entries(data.campuses).filter(([k]) => k === only));
    data.campus_status = Object.fromEntries(Object.entries(data.campus_status ?? {}).filter(([k]) => k === only));
    data.kpis = undefined; data.outcomes = undefined; data.by_hour = undefined;
  }
  return <Console data={data} demo={demo} error={error} pinRequired={!!process.env.CONTROL_PIN} role={who.role} user={who.user} day={safeDay} />;
}
