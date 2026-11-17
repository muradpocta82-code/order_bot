-- 0021: заказы, собранные в мини-приложении каталога.

CREATE TABLE IF NOT EXISTS webapp_orders (
    id serial PRIMARY KEY,
    user_id bigint NOT NULL,
    chat_id bigint NOT NULL,
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    error text
);

CREATE INDEX IF NOT EXISTS idx_webapp_orders_pending
    ON webapp_orders(id) WHERE processed_at IS NULL;
