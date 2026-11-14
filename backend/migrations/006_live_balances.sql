-- v0.6.0: partial indexes for live reservation/blocked balance scans.
-- Re-run safe; first build on a large database should be planned for low traffic.
CREATE INDEX IF NOT EXISTS idx_reservations_live_stock
  ON reservations(product_id,warehouse_id)
  INCLUDE (qty,shipped_qty)
  WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_blocks_live_stock
  ON stock_blocks(product_id,warehouse_id)
  INCLUDE (qty)
  WHERE active;
