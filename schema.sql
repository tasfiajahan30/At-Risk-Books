-- ============================================================
-- ISBN Market Drop & Digital Scarcity System
-- Supabase Database Schema
-- Run this entire script in the Supabase SQL Editor
-- ============================================================

-- 1. Table definition
create table if not exists public.at_risk_books (
  id                bigint generated always as identity primary key,
  isbn              text not null unique,
  title             text not null,
  author            text default 'Unknown',
  publish_year      integer,
  has_digital_copy  boolean default false,
  library_holdings  integer default 0,
  risk_status       text default 'HIGH RISK',
  detected_at       timestamptz default now()
);

-- Helpful index for search/filter queries from the frontend
create index if not exists idx_at_risk_books_isbn on public.at_risk_books (isbn);
create index if not exists idx_at_risk_books_title on public.at_risk_books (title);

-- 2. Enable Row Level Security
alter table public.at_risk_books enable row level security;

-- 3. Policy: allow anyone (anon key) to READ the ledger
create policy "Public can read at-risk books"
  on public.at_risk_books
  for select
  to anon
  using (true);

-- 4. Policy: allow the service_role key (used only by the GitHub Action,
--    never exposed to the browser) to INSERT new rows
create policy "Service role can insert"
  on public.at_risk_books
  for insert
  to service_role
  with check (true);

-- 5. Policy: allow the service_role key to UPDATE existing rows (for upsert)
create policy "Service role can update"
  on public.at_risk_books
  for update
  to service_role
  using (true)
  with check (true);

-- ============================================================
-- NOTE ON KEYS:
-- - The frontend (index.html) should use the SUPABASE ANON (public) key.
--   That key can only SELECT, per the policy above.
-- - The tracker.py script (run only inside GitHub Actions, never in
--   the browser) should use the SUPABASE SERVICE_ROLE key, stored as
--   a GitHub Repository Secret, so it can insert/update rows.
--   Never put the service_role key in index.html or any public file.
-- ============================================================
