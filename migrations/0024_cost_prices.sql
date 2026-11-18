-- 0024: закупочные цены по позициям с датой действия.
-- Нужны для настоящей маржи: продажа в прайсе, закупка — здесь.
-- Самая свежая запись с valid_from <= дата заказа считается действующей.

CREATE TABLE IF NOT EXISTS cost_prices (
    id bigserial PRIMARY KEY,
    catalog_id integer NOT NULL,
    cost numeric(12,4) NOT NULL,
    valid_from date NOT NULL DEFAULT current_date,
    source text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cost_prices_item
    ON cost_prices(catalog_id, valid_from DESC, id DESC);
