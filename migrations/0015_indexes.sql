-- 18. Индексы под реальные запросы
CREATE INDEX IF NOT EXISTS idx_orders_chef        ON orders (chef_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_orders_active      ON orders (delivery_date, id)
  WHERE status NOT IN ('done','cancelled','failed');
CREATE INDEX IF NOT EXISTS idx_orders_created     ON orders (created_at);
CREATE INDEX IF NOT EXISTS idx_orders_unnotified  ON orders (id) WHERE notified_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_orders_courier_unn ON orders (id)
  WHERE courier_chat_id IS NOT NULL AND courier_notified_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_orders_debt        ON orders (chef_id)
  WHERE payment_status IN ('unpaid','credit');
CREATE UNIQUE INDEX IF NOT EXISTS uq_ton_one_pending ON ton_payments (order_id)
  WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_ton_poll           ON ton_payments (created_at)
  WHERE status IN ('pending','expired');
CREATE INDEX IF NOT EXISTS idx_claims_order       ON claims (order_id);
CREATE INDEX IF NOT EXISTS idx_drafts_updated     ON drafts (updated_at);
