-- 0028: смены сотрудников и назначение заказа упаковщику (D4).
-- Основные роли из .env (MANAGER_CHAT_ID, COURIER_CHAT_ID) остаются «владельцами доступа».

ALTER TABLE staff ADD COLUMN IF NOT EXISTS on_shift boolean NOT NULL DEFAULT true;
ALTER TABLE staff ADD COLUMN IF NOT EXISTS last_order_at timestamptz;

ALTER TABLE orders ADD COLUMN IF NOT EXISTS assigned_to bigint;

CREATE INDEX IF NOT EXISTS idx_orders_assigned ON orders(assigned_to, status);
