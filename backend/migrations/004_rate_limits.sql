-- v0.4.0: shared fixed-window request counters across API replicas.
-- All request keys are SHA-256 hashed by application code; raw IPs are not stored.
CREATE TABLE IF NOT EXISTS rate_limit_counters (
  key_hash text NOT NULL,
  window_id bigint NOT NULL,
  hits bigint NOT NULL CHECK (hits > 0),
  PRIMARY KEY (key_hash,window_id)
);
