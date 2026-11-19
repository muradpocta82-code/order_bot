-- 0030: списания и усушка (B9). Потери уменьшают склад и маржу.

CREATE TABLE IF NOT EXISTS writeoffs (
    id serial PRIMARY KEY,
    catalog_id integer,
    name text,
    qty numeric(12,3) NOT NULL,
    unit text,
    reason text,
    cost numeric(12,4),
    actor_id bigint,
    ts timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_writeoffs_ts ON writeoffs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_writeoffs_catalog ON writeoffs(catalog_id);
