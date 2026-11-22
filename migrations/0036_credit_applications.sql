-- 0036: зачёт кредит-нот по заказам (B3-остаток).
-- Кредит-нота — один источник зачёта; здесь видно, в какой счёт она ушла.
-- Неприменённый остаток остаётся авансом и уходит в следующий заказ.

CREATE TABLE IF NOT EXISTS credit_applications (
    id serial PRIMARY KEY,
    credit_note_id integer NOT NULL REFERENCES credit_notes(id) ON DELETE CASCADE,
    order_id integer NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    amount numeric(12,2) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (credit_note_id, order_id)
);

CREATE INDEX IF NOT EXISTS idx_credit_applications_order ON credit_applications(order_id);
