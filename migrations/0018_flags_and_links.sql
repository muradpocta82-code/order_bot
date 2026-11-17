-- 0018: булевы флаги, недостающие пользователи и внешние ключи.
-- Всё в одной транзакции (раннер migrate.py).

-- 1. Старые заказы ссылаются на chef_id, которых нет в users.
--    Заводим их как подтверждённых клиентов, чтобы можно было поставить FK.
INSERT INTO users(user_id, name, username, access, created_at, updated_at)
SELECT o.chef_id,
       MAX(COALESCE(NULLIF(o.chef_name, ''), 'Клиент')),
       MAX(o.chef_username),
       'approved',
       to_char(now(), 'YYYY-MM-DD HH24:MI:SS'),
       to_char(now(), 'YYYY-MM-DD HH24:MI:SS')
FROM orders o
LEFT JOIN users u ON u.user_id = o.chef_id
WHERE u.user_id IS NULL
GROUP BY o.chef_id;

-- 2. Флаги: целые 0/1 -> boolean.
ALTER TABLE staff ALTER COLUMN active DROP DEFAULT;
ALTER TABLE staff ALTER COLUMN active TYPE boolean USING (active <> 0);
ALTER TABLE staff ALTER COLUMN active SET DEFAULT true;

ALTER TABLE stock_items ALTER COLUMN available DROP DEFAULT;
ALTER TABLE stock_items ALTER COLUMN available TYPE boolean USING (available <> 0);
ALTER TABLE stock_items ALTER COLUMN available SET DEFAULT true;

-- 3. Внешние ключи: удаление заказа уносит его платежи и претензии.
ALTER TABLE claims ADD CONSTRAINT claims_order_fk
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE;

ALTER TABLE ton_payments ADD CONSTRAINT ton_payments_order_fk
    FOREIGN KEY (order_id) REFERENCES orders(id) ON DELETE CASCADE;

ALTER TABLE orders ADD CONSTRAINT orders_chef_fk
    FOREIGN KEY (chef_id) REFERENCES users(user_id) NOT VALID;

ALTER TABLE orders VALIDATE CONSTRAINT orders_chef_fk;
