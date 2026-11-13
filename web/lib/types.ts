export type Lead = {
  id: number; time: string; parent: string; student: string; class: string; campus: string; phone: string;
  status: string; attempts: number; first_call_seconds: number | null; note: string; transferred_to: string;
};
export type ConsoleData = {
  generated_at: string; day: string; campuses: Record<string, string>;
  leads: Lead[]; hours_label?: string;
  campus_status?: Record<string, { label: string; on_duty: string | null; counselors: { name: string; number: string; on_duty: boolean }[] }>; control?: { state: "running" | "paused" | "stopped"; changed_at: string; by: string }; kpis?: Record<string, number>; outcomes?: Record<string, number>; by_hour?: Record<string, number>;
};
