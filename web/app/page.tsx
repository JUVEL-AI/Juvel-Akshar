import Console from "@/components/Console";
import { loadConsole } from "@/lib/backend";

export const dynamic = "force-dynamic";

export default async function Page() {
  const { data, demo, error } = await loadConsole();
  return <Console data={data} demo={demo} error={error} pinRequired={!!process.env.CONTROL_PIN} />;
}
