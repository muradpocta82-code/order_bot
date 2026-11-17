-- 0022: хронология заказов — кто и когда что изменил.
-- Нужна для споров с клиентами, замеров «принят → собран → доставлен» и причин отмен.

CREATE TABLE IF NOT EXISTS order_events (
    id bigserial PRIMARY KEY,
    order_id integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL,
    actor_id bigint,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_order_events_order
    ON order_events(order_id, id DESC);

CREATE INDEX IF NOT EXISTS idx_order_events_kind_time
    ON order_events(kind, created_at DESC);
