-- 0029: книга расчётов (B3, шаг 1). Пока только чтение: старые поля заказа остаются главными.
-- Клиент в опте платит одной суммой за неделю, частично, с зачётом претензии.

CREATE TABLE IF NOT EXISTS payments (
    id serial PRIMARY KEY,
    client_id bigint NOT NULL,
    amount numeric(12,2) NOT NULL,
    currency text NOT NULL DEFAULT 'USD',
    method text,
    ts timestamptz NOT NULL DEFAULT now(),
    note text,
    source text
);

CREATE TABLE IF NOT EXISTS allocations (
    id serial PRIMARY KEY,
    payment_id integer NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
    order_id integer NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    amount numeric(12,2) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (payment_id, order_id)
);

CREATE TABLE IF NOT EXISTS credit_notes (
    id serial PRIMARY KEY,
    client_id bigint NOT NULL,
    order_id integer REFERENCES orders(id) ON DELETE SET NULL,
    amount numeric(12,2) NOT NULL,
    reason text,
    ts timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_payments_client_ts ON payments(client_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_allocations_order ON allocations(order_id);
CREATE INDEX IF NOT EXISTS idx_credit_notes_client ON credit_notes(client_id, ts DESC);
