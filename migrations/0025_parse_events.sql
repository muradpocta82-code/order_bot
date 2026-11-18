-- 0025: журнал разбора заказов — чтобы качество мерилось цифрами, а не на глаз.
-- Пишется один раз на каждый присланный заказ: источник, путь разбора, сколько позиций,
-- сколько вопросов, сколько заняло времени.

CREATE TABLE IF NOT EXISTS parse_events (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    chef_id bigint,
    chat_id bigint,
    source text,
    path text,
    items integer NOT NULL DEFAULT 0,
    questions integer NOT NULL DEFAULT 0,
    latency_ms integer,
    text_len integer,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_parse_events_time
    ON parse_events(created_at DESC);

CREATE INDEX IF NOT EXISTS idx_parse_events_source
    ON parse_events(source, created_at DESC);
