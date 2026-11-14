-- v0.5.0: speed up partial SKU/name/barcode search on large catalogs.
-- For a high-volume production catalog, schedule the first index build in a maintenance window.
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_products_sku_search
  ON products USING gin (sku gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_name_search
  ON products USING gin (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_products_barcode_search
  ON products USING gin (barcode gin_trgm_ops);
