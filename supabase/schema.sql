-- Supabase schema + RLS for Asmi Eval UI
-- Apply in Supabase SQL editor (or via migrations) once per project.

create extension if not exists "pgcrypto";

-- ─────────────────────────────────────────────────────────────────────────────
-- Tables
-- ─────────────────────────────────────────────────────────────────────────────

create table if not exists public.test_cases (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  external_id text not null,
  category text,
  name text,
  type text,
  enabled boolean not null default true,
  definition jsonb not null default '{}'::jsonb,
  latest_version integer not null default 1,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (owner_user_id, external_id)
);

create table if not exists public.test_case_versions (
  id uuid primary key default gen_random_uuid(),
  test_case_id uuid not null references public.test_cases(id) on delete cascade,
  version integer not null,
  definition jsonb not null default '{}'::jsonb,
  editor_user_id uuid references auth.users(id) on delete set null,
  change_note text,
  created_at timestamptz not null default now(),
  unique (test_case_id, version)
);

create table if not exists public.runs (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'queued', -- queued|running|done|failed|canceled|stopped
  trigger text not null default 'manual', -- manual|daemon
  started_at timestamptz,
  ended_at timestamptz,
  asmi_target text,
  asmi_handle text,
  selection jsonb not null default '{}'::jsonb, -- {ids, id, category, categories}
  test_cases_snapshot jsonb not null default '[]'::jsonb,
  output text not null default '',
  report_html text not null default '',
  results jsonb not null default '[]'::jsonb,
  progress jsonb not null default '{}'::jsonb,
  summary jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.artifacts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.runs(id) on delete cascade,
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  kind text not null, -- report_html|results_json|overall_analysis_json|log_txt
  bucket text not null default 'artifacts',
  path text not null,
  content_type text,
  bytes bigint,
  sha256 text,
  created_at timestamptz not null default now()
);

-- Minimal table to allow the daemon to post results to the server
create table if not exists public.relay_devices (
  id uuid primary key default gen_random_uuid(),
  owner_user_id uuid not null references auth.users(id) on delete cascade,
  device_name text not null,
  token_sha256 text not null,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  unique (owner_user_id, device_name)
);

-- ─────────────────────────────────────────────────────────────────────────────
-- Updated-at trigger helper
-- ─────────────────────────────────────────────────────────────────────────────

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists trg_test_cases_updated_at on public.test_cases;
create trigger trg_test_cases_updated_at
before update on public.test_cases
for each row execute function public.set_updated_at();

drop trigger if exists trg_runs_updated_at on public.runs;
create trigger trg_runs_updated_at
before update on public.runs
for each row execute function public.set_updated_at();

-- ─────────────────────────────────────────────────────────────────────────────
-- RLS
-- ─────────────────────────────────────────────────────────────────────────────

alter table public.test_cases enable row level security;
alter table public.test_case_versions enable row level security;
alter table public.runs enable row level security;
alter table public.artifacts enable row level security;
alter table public.relay_devices enable row level security;

-- test_cases: owner only
drop policy if exists "test_cases_select_own" on public.test_cases;
create policy "test_cases_select_own" on public.test_cases
for select using (owner_user_id = auth.uid());

drop policy if exists "test_cases_insert_own" on public.test_cases;
create policy "test_cases_insert_own" on public.test_cases
for insert with check (owner_user_id = auth.uid());

drop policy if exists "test_cases_update_own" on public.test_cases;
create policy "test_cases_update_own" on public.test_cases
for update using (owner_user_id = auth.uid()) with check (owner_user_id = auth.uid());

drop policy if exists "test_cases_delete_own" on public.test_cases;
create policy "test_cases_delete_own" on public.test_cases
for delete using (owner_user_id = auth.uid());

-- test_case_versions: enforce via join to test_cases owner
drop policy if exists "tc_versions_select_own" on public.test_case_versions;
create policy "tc_versions_select_own" on public.test_case_versions
for select using (
  exists (
    select 1 from public.test_cases tc
    where tc.id = test_case_versions.test_case_id
      and tc.owner_user_id = auth.uid()
  )
);

drop policy if exists "tc_versions_insert_own" on public.test_case_versions;
create policy "tc_versions_insert_own" on public.test_case_versions
for insert with check (
  exists (
    select 1 from public.test_cases tc
    where tc.id = test_case_versions.test_case_id
      and tc.owner_user_id = auth.uid()
  )
);

-- runs: owner only
drop policy if exists "runs_select_own" on public.runs;
create policy "runs_select_own" on public.runs
for select using (owner_user_id = auth.uid());

drop policy if exists "runs_insert_own" on public.runs;
create policy "runs_insert_own" on public.runs
for insert with check (owner_user_id = auth.uid());

drop policy if exists "runs_update_own" on public.runs;
create policy "runs_update_own" on public.runs
for update using (owner_user_id = auth.uid()) with check (owner_user_id = auth.uid());

drop policy if exists "runs_delete_own" on public.runs;
create policy "runs_delete_own" on public.runs
for delete using (owner_user_id = auth.uid());

-- artifacts: owner only
drop policy if exists "artifacts_select_own" on public.artifacts;
create policy "artifacts_select_own" on public.artifacts
for select using (owner_user_id = auth.uid());

drop policy if exists "artifacts_insert_own" on public.artifacts;
create policy "artifacts_insert_own" on public.artifacts
for insert with check (owner_user_id = auth.uid());

drop policy if exists "artifacts_delete_own" on public.artifacts;
create policy "artifacts_delete_own" on public.artifacts
for delete using (owner_user_id = auth.uid());

-- relay_devices: owner only (device tokens are managed server-side with service role)
drop policy if exists "relay_devices_select_own" on public.relay_devices;
create policy "relay_devices_select_own" on public.relay_devices
for select using (owner_user_id = auth.uid());

