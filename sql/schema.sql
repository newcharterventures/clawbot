-- Clawbot Supabase schema. Run once in the SQL editor.

CREATE TABLE IF NOT EXISTS clawbot_prompt_templates (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  key           TEXT UNIQUE NOT NULL,
  system_prompt TEXT NOT NULL,
  user_template TEXT NOT NULL,
  model         TEXT DEFAULT 'claude-sonnet-4-6',
  max_tokens    INT  DEFAULT 2000,
  updated_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS clawbot_resource_run_log (
  id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_date           DATE NOT NULL UNIQUE,
  resources_found    INT,
  resources_selected INT,
  selected_urls      TEXT[],
  draft_post         TEXT,
  draft_x_post       TEXT,
  approval_status    TEXT DEFAULT 'pending',
  approved_at        TIMESTAMPTZ,
  whop_post_id       TEXT,
  error_msg          TEXT,
  created_at         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_run_log_date ON clawbot_resource_run_log (run_date DESC);

CREATE TABLE IF NOT EXISTS clawbot_pending_approval (
  id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id          UUID REFERENCES clawbot_resource_run_log(id) ON DELETE CASCADE,
  telegram_msg_id TEXT,
  status          TEXT DEFAULT 'waiting',
  edit_notes      TEXT,
  revision_count  INT  DEFAULT 0,
  created_at      TIMESTAMPTZ DEFAULT NOW()
);
