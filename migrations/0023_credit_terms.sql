-- 0023: условия отсрочки по клиенту — лимит долга и срок оплаты.
-- Пустые значения означают «без ограничения» (решает менеджер).

ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_limit numeric(12,2);
ALTER TABLE users ADD COLUMN IF NOT EXISTS credit_days integer;

CREATE INDEX IF NOT EXISTS idx_orders_credit_age
    ON orders(chef_id, created_at) WHERE payment_status = 'credit';
