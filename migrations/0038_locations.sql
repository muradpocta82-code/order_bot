-- 0038: точки одного клиента (C11).
-- У сети несколько заведений: заказ везётся на конкретную точку,
-- счёт и долг считаются на клиента целиком.

CREATE TABLE IF NOT EXISTS locations (
    id serial PRIMARY KEY,
    client_id bigint NOT NULL,
    name text NOT NULL,
    address text,
    lat double precision,
    lon double precision,
    payer_id bigint,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_locations_client_name
    ON locations(client_id, lower(name));
CREATE INDEX IF NOT EXISTS idx_locations_client ON locations(client_id);

-- Снимок точки в заказе: прайс и карточки не зависят от правок в списке точек.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS location_id integer;
ALTER TABLE orders ADD COLUMN IF NOT EXISTS location_name text;
