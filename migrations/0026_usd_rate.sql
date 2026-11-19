-- 0026: курс доллара фиксируется в момент подтверждения заказа (B10).
-- Смена курса не меняет суммы уже подтверждённых заказов.

ALTER TABLE orders ADD COLUMN IF NOT EXISTS usd_rate_at_order numeric(12,4);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS total_local numeric(14,2);

CREATE TABLE IF NOT EXISTS rate_history (
    id serial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    rate numeric(12,4) NOT NULL,
    source text
);

CREATE INDEX IF NOT EXISTS idx_rate_history_ts ON rate_history(ts DESC);
