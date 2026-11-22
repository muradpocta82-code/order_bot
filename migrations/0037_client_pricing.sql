-- 0037: цены и скидки по клиентам (B13).
-- Крупный клиент получает скидку процентом или особую цену на позицию.
-- Цена применяется в черновике заказа, базовая цена сохраняется в позиции.

CREATE TABLE IF NOT EXISTS client_discounts (
    client_id bigint PRIMARY KEY,
    discount_pct numeric(5,2) NOT NULL,
    note text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS client_prices (
    client_id bigint NOT NULL,
    catalog_id integer NOT NULL,
    price numeric(12,2) NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (client_id, catalog_id)
);

ALTER TABLE order_items ADD COLUMN IF NOT EXISTS base_price numeric(12,2);
ALTER TABLE order_items ADD COLUMN IF NOT EXISTS discount_pct numeric(5,2);
