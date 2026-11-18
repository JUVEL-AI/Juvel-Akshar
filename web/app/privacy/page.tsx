export const metadata = { title: "Privacy notice · Juvel Calling" };

const Row = ({ t, children }: { t: string; children: React.ReactNode }) => (
  <section style={{ marginBottom: 22 }}><h2 style={{ fontSize: 17, margin: "0 0 6px" }}>{t}</h2><div style={{ lineHeight: 1.6, opacity: 0.9 }}>{children}</div></section>
);

export default function Privacy() {
  const contact = process.env.PRIVACY_CONTACT_EMAIL || "the school office";
  return (
    <main style={{ maxWidth: 720, margin: "0 auto", padding: "32px 20px" }}>
      <h1 style={{ fontSize: 24 }}>Privacy notice: admissions calls</h1>
      <p className="sub">Akshar Arbol International School (the school) uses Juvel Calling, provided by Juvel AI Technologies Pvt Ltd, to return admission enquiries.</p>
      <Row t="What we collect">Parent name, phone number and email, student name and class, the campus chosen, and the details of the call: time, result, a recording and a transcript. We do not collect dates of birth, identity documents or payment details.</Row>
      <Row t="Why">Only to call you about the admission enquiry you submitted, connect you to the campus admissions counselor, and follow up. We do not use it for advertising or sell it.</Row>
      <Row t="Calls are automated and may be recorded">The first voice you hear is an automated assistant. The call may be recorded and transcribed so the counselor has your details and the school can check quality. Recordings are visible only to authorised school staff.</Row>
      <Row t="Who handles the data (processors)">Juvel AI Technologies (service provider); Vapi (calling), ElevenLabs (voice), Vobiz (phone network), Supabase and Vercel (storage and hosting), Google (spreadsheets, email). Some of these servers are outside India.</Row>
      <Row t="How long we keep it">Recordings, transcripts and summaries are erased after 30 days. The enquiry itself is erased after 180 days. These periods may be changed by the school.</Row>
      <Row t="Your choices">You may ask to see, correct or delete your data, to stop calls, or to withdraw consent at any time. Contact {contact}. Deleting an enquiry also asks the calling provider to erase its recording.</Row>
      <Row t="Children">Student details are given by the parent or guardian and used only for this admission enquiry.</Row>
    </main>
  );
}
