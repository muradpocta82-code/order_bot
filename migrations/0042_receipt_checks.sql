-- 0042: проверка чека по фото (G3).
-- Vision-модель вытаскивает сумму и дату, бот сверяет с заказом,
-- при расхождении решение принимает менеджер кнопкой.

CREATE TABLE IF NOT EXISTS receipt_checks (
    id serial PRIMARY KEY,
    order_id integer NOT NULL,
    chef_id bigint,
    amount numeric(12,2),
    currency text,
    receipt_date text,
    verdict text NOT NULL,
    provider text,
    raw jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_receipt_checks_order ON receipt_checks(order_id, created_at DESC);
