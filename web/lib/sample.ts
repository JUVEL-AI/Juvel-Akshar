import type { ConsoleData, Lead } from "./types";

const L = (id: number, time: string, parent: string, student: string, cls: string, campus: string, phone: string,
  status: string, attempts: number, sec: number | null, note = ""): Lead =>
  ({ id, time, parent, student, class: cls, campus, phone, status, attempts, first_call_seconds: sec, note, transferred_to: "" });

export const SAMPLE: ConsoleData = {
  generated_at: new Date().toISOString(), day: "sample",
  campuses: { ecr: "ECR", t_nagar: "T Nagar" },
  control: { state: "running", changed_at: "", by: "" },
  hours_label: "Mon–Fri 8:30–2:30 · Sat 9:00–2:00",
  campus_status: {
    ecr: { label: "ECR", on_duty: "Vidya", counselors: [{ name: "Vidya", number: "+917550033275", on_duty: true }] },
    t_nagar: { label: "T Nagar", on_duty: "Viji", counselors: [{ name: "Viji", number: "+917305972752", on_duty: true }] } },
  leads: [
    L(12, "13:55", "Harini V", "Myra", "LKG", "ecr", "+91 99••• ••412", "RETRY", 2, 80),
    L(11, "13:22", "Nithya P", "Aadhi", "2nd grade", "t_nagar", "+91 97••• ••859", "TRANSFERRED", 1, 69),
    L(10, "12:40", "Vimal R", "", "", "", "+91 95••• ••204", "NEEDS_REVIEW", 0, null, "unknown campus ''"),
    L(9, "12:03", "Priya M", "Vihaan", "UKG", "ecr", "+91 98••• ••776", "TRANSFERRED", 2, 77),
    L(8, "11:38", "Arun J", "Nila", "1st grade", "t_nagar", "+91 96••• ••318", "TRANSFERRED", 1, 64),
    L(7, "11:14", "Lakshmi N", "Sana", "Nursery", "ecr", "+91 93••• ••540", "NO_RESPONSE", 4, 91),
    L(6, "10:52", "Suresh B", "Rohan", "LKG", "t_nagar", "+91 94••• ••067", "TRANSFERRED", 3, 83),
    L(5, "10:26", "Divya Prakash", "Ishita", "5th grade", "ecr", "+91 98••• ••902", "TRANSFERRED", 1, 58),
    L(4, "10:05", "Ravi Kumar", "", "", "t_nagar", "+91 90••• ••125", "RETRY", 2, 102),
    L(3, "09:41", "Meena K", "Kabir", "UKG", "ecr", "+91 99••• ••733", "ANSWERED_NOT_TRANSFERRED", 1, 88),
    L(2, "09:17", "Karthik S", "Diya", "3rd grade", "t_nagar", "+91 97••• ••481", "TRANSFERRED", 1, 71),
    L(1, "09:02", "Anitha Rao", "Aarav", "LKG", "ecr", "+91 98••• ••210", "TRANSFERRED", 1, 94),
  ],
};
