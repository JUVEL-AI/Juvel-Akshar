import Console from "@/components/Console";
import { loadConsole } from "@/lib/backend";
import { whoFromPage } from "@/lib/server";

export const dynamic = "force-dynamic";

export default async function Page({ searchParams }: { searchParams: Promise<{ day?: string }> }) {
  const { day } = await searchParams;
  const safeDay = day && /^\d{4}-\d{2}-\d{2}$/.test(day) ? day : undefined;
  const { data, demo, error } = await loadConsole(safeDay);
  const who = await whoFromPage();
  return <Console data={data} demo={demo} error={error} pinRequired={!!process.env.CONTROL_PIN} role={who.role} user={who.user} day={safeDay} />;
}
