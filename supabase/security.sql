-- Run once in Supabase > SQL Editor. Locks the tables so the public Supabase REST/anon key can never read them.
-- The backend connects with the database owner role (DATABASE_URL), which is not affected.
alter table if exists leads  enable row level security;
alter table if exists calls  enable row level security;
alter table if exists alerts enable row level security;
alter table if exists dnc    enable row level security;
alter table if exists kv     enable row level security;
alter table if exists audit  enable row level security;
revoke all on leads, calls, alerts, dnc, kv, audit from anon, authenticated;
