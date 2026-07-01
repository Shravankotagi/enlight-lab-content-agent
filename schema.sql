-- Agent 03 - Content Generation & Curation Agent
-- New Supabase tables. Run this once in the Supabase SQL editor.
-- Does NOT touch any existing tables (organizations, courses, learners, etc.)

-- ---------------------------------------------------------------------------
-- content_sources: the uploaded PDF / video transcript / URL that content
-- gets generated FROM.
-- ---------------------------------------------------------------------------
create table if not exists content_sources (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations(id),
  course_id uuid references courses(id),
  source_type text not null check (source_type in ('pdf', 'video_transcript', 'url')),
  filename text,
  source_url text not null,
  status text not null default 'uploaded' check (status in ('uploaded', 'processing', 'ready', 'failed')),
  error_message text,
  uploaded_at timestamptz not null default now(),
  processed_at timestamptz
);

create index if not exists idx_content_sources_org on content_sources(organization_id);

-- ---------------------------------------------------------------------------
-- generated_content: the actual quizzes / flashcards / summaries / exercises
-- produced from a source. Versioned: regenerating creates a NEW row with an
-- incremented version rather than overwriting, per PRD "versioning and
-- content audit trail" requirement.
-- ---------------------------------------------------------------------------
create table if not exists generated_content (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations(id),
  source_id uuid not null references content_sources(id),
  content_type text not null check (content_type in ('quiz', 'flashcard', 'summary', 'exercise')),
  format text not null default 'na' check (format in ('mcq', 'open_ended', 'case_study', 'na')),
  bloom_level text check (bloom_level in ('remember', 'understand', 'apply', 'analyze', 'evaluate', 'create')),
  payload jsonb not null,
  quality_score numeric,
  version int not null default 1,
  status text not null default 'draft' check (status in ('draft', 'pending_review', 'approved', 'rejected')),
  reviewed_by text,
  reviewed_at timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists idx_generated_content_source on generated_content(source_id);
create index if not exists idx_generated_content_status on generated_content(organization_id, status);

-- ---------------------------------------------------------------------------
-- curated_references: external articles/videos/case studies surfaced via
-- web search, aligned to the source's learning objectives.
-- ---------------------------------------------------------------------------
create table if not exists curated_references (
  id uuid primary key default gen_random_uuid(),
  organization_id uuid not null references organizations(id),
  source_id uuid not null references content_sources(id),
  title text not null,
  url text not null,
  description text,
  relevance_score numeric,
  added_at timestamptz not null default now()
);

create index if not exists idx_curated_references_source on curated_references(source_id);

-- ---------------------------------------------------------------------------
-- content_audit_log: every state transition on a generated_content row
-- (created, regenerated, approved, rejected). PRD explicitly requires an
-- audit trail, separate from just having a status column.
-- ---------------------------------------------------------------------------
create table if not exists content_audit_log (
  id uuid primary key default gen_random_uuid(),
  content_id uuid not null references generated_content(id),
  action text not null,  -- e.g. 'generated', 'regenerated', 'approved', 'rejected'
  actor text,             -- instructor id/email, or 'system' for automated actions
  notes text,
  created_at timestamptz not null default now()
);

create index if not exists idx_audit_log_content on content_audit_log(content_id);
