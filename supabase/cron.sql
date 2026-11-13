-- Run once in Supabase > SQL Editor. Replace the two placeholders first.
-- API_URL    = your Vercel API project URL, e.g. https://juvel-calling-api.vercel.app
-- THE_SECRET = the same WEBHOOK_SECRET you set in Vercel (both projects)
create extension if not exists pg_cron;
create extension if not exists pg_net;

-- every minute: place due calls, retry, run the safety net, read the Google Sheet
select cron.schedule(
  'juvel-tick', '* * * * *',
  $$ select net.http_post(
       url     := 'API_URL/admin/tick',
       headers := '{"X-Webhook-Secret": "THE_SECRET", "Content-Type": "application/json"}'::jsonb,
       body    := '{}'::jsonb,
       timeout_milliseconds := 55000
     ); $$
);

-- to stop it:  select cron.unschedule('juvel-tick');
-- to see runs: select * from cron.job_run_details order by start_time desc limit 20;
