-- 0034: правила замены позиций (C8). «Заменить на …, не заменять, спросить».

CREATE TABLE IF NOT EXISTS substitute_rules (
    catalog_id integer PRIMARY KEY,
    mode text NOT NULL DEFAULT 'ask',
    substitute_id integer,
    updated_at timestamptz NOT NULL DEFAULT now()
);
