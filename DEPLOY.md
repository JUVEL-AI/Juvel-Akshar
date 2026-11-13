# Deploying with Vercel + Supabase only (no Render)

```
Lead sheet / form -> Vercel API (Python) -> Vapi -> Vobiz line -> parent
                         ^   |
 Supabase pg_cron every minute   Vapi posts results to /webhooks/vapi
                         |
                  Supabase Postgres  <-  Vercel dashboard (web/)
```

Why Render is not needed: nothing has to stay running. All state is in Supabase, so the API can be serverless.
Supabase `pg_cron` wakes the API every minute for retries, the safety net and the sheet. A new lead also triggers a
call immediately through the lead webhook.

## Steps
1. **Supabase**: new project (use a paid plan for production: free projects can pause). Copy the **transaction pooler**
   connection string (Project Settings > Database), port 6543. Tables are created automatically.
2. **Vercel project 1, the API**: import this repo with Root Directory = the repo root (this folder).
   Environment variables: DATABASE_URL, WEBHOOK_SECRET (any long random string), VAPI_API_KEY, VAPI_PHONE_NUMBER_ID,
   VAPI_SERVER_SECRET (random string), SMTP_PASSWORD, GOOGLE_SERVICE_ACCOUNT_JSON, SHEET_ID, VOLANT_SYNC_URL.
   Edit `config.yaml` first (numbers, hours, emails, sheet columns). Check: open `<api-url>/health`.
3. **Supabase cron**: open `supabase/cron.sql`, put your API URL and WEBHOOK_SECRET in, run it in the SQL Editor.
4. **Vapi**: assistant server URL = `<api-url>/webhooks/vapi`, secret = VAPI_SERVER_SECRET. Add the Vobiz SIP trunk/number
   (BYO SIP) and copy its phone number id into VAPI_PHONE_NUMBER_ID.
5. **Vercel project 2, the dashboard**: import the same repo with Root Directory = `web`. Environment:
   BACKEND_URL = the API URL, WEBHOOK_SECRET (same), CONSOLE_PASSWORD (turn the login on: the page shows parent details).
6. **Google Sheet**: create a service account, share the sheet with its email, paste the JSON key into
   GOOGLE_SERVICE_ACCOUNT_JSON. Rows older than 48 hours are never called.
7. Test: `python -m juvelcall test-call +91YOURNUMBER ecr` (with the same environment variables set locally).

## Things to know
- Vercel's free Hobby plan is for personal, non-commercial use. A paid school service should be on a Pro plan.
- The calling heartbeat is once a minute, so retry timing is accurate to about a minute.
- If you would rather have one always-on server, `render.yaml` and the `Dockerfile` still work (optional).
