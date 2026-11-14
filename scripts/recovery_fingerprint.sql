-- Fingerprint of restored business entities, immutable events and physical balances.
SELECT concat_ws('|',
  (SELECT count(*) FROM users),
  (SELECT count(*) FROM products),
  (SELECT count(*) FROM warehouses),
  (SELECT count(*) FROM zones),
  (SELECT count(*) FROM bins),
  (SELECT count(*) FROM lots),
  (SELECT count(*) FROM orders),
  (SELECT count(*) FROM order_lines),
  (SELECT count(*) FROM receipts),
  (SELECT count(*) FROM receipt_lines),
  (SELECT count(*) FROM transfers),
  (SELECT count(*) FROM reservations),
  (SELECT count(*) FROM stock_blocks),
  (SELECT count(*) FROM shipments),
  (SELECT count(*) FROM pick_tasks),
  (SELECT count(*) FROM return_lines),
  (SELECT count(*) FROM inventory_counts),
  (SELECT count(*) FROM inventory_lines),
  (SELECT count(*) FROM audit_events),
  (SELECT count(*) FROM outbox_events),
  (SELECT count(*) FROM stock_movements),
  (SELECT md5(coalesce(string_agg(
      concat_ws(':',m.id,m.product_id,m.warehouse_id,m.bin_id,m.lot_id,
        m.qty,m.doc_type,m.doc_id,coalesce(m.actor_id::text,''),
        m.created_at::text),E'\n' ORDER BY m.id),'')) FROM stock_movements m),
  (SELECT md5(coalesce(string_agg(
      concat_ws(':',a.id,a.action,a.entity_type,a.entity_id,
        coalesce(a.actor_id::text,''),coalesce(a.before::text,''),
        coalesce(a.after::text,''),a.request_id,a.created_at::text),
      E'\n' ORDER BY a.id),'')) FROM audit_events a),
  (SELECT md5(coalesce(string_agg(
      concat_ws(':',x.product_id,x.warehouse_id,x.bin_id,x.lot_id,x.qty),
      E'\n' ORDER BY x.product_id,x.warehouse_id,x.bin_id,x.lot_id),'')) FROM (
        SELECT product_id,warehouse_id,bin_id,lot_id,sum(qty) AS qty
        FROM stock_movements GROUP BY product_id,warehouse_id,bin_id,lot_id
      ) x)
);
