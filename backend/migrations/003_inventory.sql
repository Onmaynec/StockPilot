-- v0.3.0: record the ledger high-water mark at the start of a zone count.
-- Nullable for in-flight counts created by earlier releases; old counts retain timestamp fallback.
ALTER TABLE inventory_counts ADD COLUMN IF NOT EXISTS snapshot_movement_id bigint;
CREATE INDEX IF NOT EXISTS idx_movements_zone_fresh ON stock_movements(bin_id,id);
