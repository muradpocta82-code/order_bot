-- 0041: теневой режим промптов (E10).
-- Новый промпт разбирает часть боевых заказов, результат только сравнивается
-- с рабочим и записывается. Клиент и менеджер его не видят.

CREATE TABLE IF NOT EXISTS shadow_parses (
    id serial PRIMARY KEY,
    order_id integer,
    chef_id bigint,
    provider text,
    items_main integer NOT NULL DEFAULT 0,
    items_shadow integer NOT NULL DEFAULT 0,
    matched integer NOT NULL DEFAULT 0,
    match_pct numeric(5,2),
    details jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_shadow_parses_created ON shadow_parses(created_at DESC);
