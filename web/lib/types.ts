export type CallRec = { attempt: number; at: string; outcome: string; seconds: number | null; reason: string; recording: string; summary: string; transcript: string; detail: string };
export type Lead = {
  id: number; time: string; parent: string; student: string; class: string; campus: string; phone: string;
  status: string; attempts: number; first_call_seconds: number | null; note: string; transferred_to: string; outcome?: string; calls?: CallRec[];
};
export type Live = { id: number; parent: string; student: string; class: string; campus: string; attempt: number; since: string; last4?: string; source?: string; phase?: "calling" | "recent" | "next"; status?: string };
export type ConsoleData = {
  live?: Live[]; recall?: { waiting: number };
  generated_at: string; day: string; campuses: Record<string, string>;
  leads: Lead[]; hours_label?: string; records_staff?: boolean; records_hidden?: boolean;
  campus_status?: Record<string, { label: string; on_duty: string | null; counselors: { name: string; number: string; on_duty: boolean }[] }>; control?: { state: "running" | "paused" | "stopped"; changed_at: string; by: string; campuses?: Record<string, { state: "running" | "paused" | "stopped"; changed_at: string; by: string }> }; kpis?: Record<string, number>; outcomes?: Record<string, number>; by_hour?: Record<string, number>;
};
