-- 002: invalidate sessions when passwords change or user requests logout-all.
-- Safe to reapply on databases created by the Docker init scripts.
ALTER TABLE users ADD COLUMN IF NOT EXISTS token_version integer NOT NULL DEFAULT 0;
