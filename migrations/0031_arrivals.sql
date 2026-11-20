-- 0031: приход товара (G7). Приход пополняет остаток и фиксирует закупочную цену.

CREATE TABLE IF NOT EXISTS arrivals (
    id serial PRIMARY KEY,
    catalog_id integer,
    name text,
    qty numeric(12,3),
    unit text,
    cost numeric(12,4),
    actor_id bigint,
    ts timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_arrivals_ts ON arrivals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_arrivals_catalog ON arrivals(catalog_id);
