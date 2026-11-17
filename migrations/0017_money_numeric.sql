-- 0017: деньги в точном формате numeric вместо double precision.
-- Цены/суммы — 2 знака (деньги), цена за единицу и количество — 3–4 знака.

ALTER TABLE orders
    ALTER COLUMN total TYPE numeric(12,2) USING round(total::numeric, 2),
    ALTER COLUMN paid_amount TYPE numeric(12,2) USING round(paid_amount::numeric, 2);

ALTER TABLE order_items
    ALTER COLUMN price TYPE numeric(12,4) USING round(price::numeric, 4),
    ALTER COLUMN qty TYPE numeric(12,3) USING round(qty::numeric, 3),
    ALTER COLUMN total TYPE numeric(12,2) USING round(total::numeric, 2);

ALTER TABLE stock_items
    ALTER COLUMN stock_left TYPE numeric(12,3) USING round(stock_left::numeric, 3);
