-- 14. Гигиена: дефолт оплаты и CHECK-ограничения
ALTER TABLE orders ALTER COLUMN payment_method SET DEFAULT 'cash';

ALTER TABLE orders ADD CONSTRAINT orders_status_chk CHECK
  (status IN ('new','accepted','ready','delivering','done','cancelled','failed')) NOT VALID;
ALTER TABLE orders ADD CONSTRAINT orders_pay_chk CHECK
  (payment_status IN ('unpaid','paid','credit')) NOT VALID;
ALTER TABLE orders ADD CONSTRAINT orders_rating_chk CHECK
  (rating IS NULL OR rating BETWEEN 1 AND 5) NOT VALID;
ALTER TABLE orders ADD CONSTRAINT orders_pin_chk CHECK
  (delivery_pin IS NULL OR delivery_pin ~ '^\d{4}$') NOT VALID;
ALTER TABLE order_items ADD CONSTRAINT items_qty_chk CHECK (qty IS NULL OR qty > 0) NOT VALID;

ALTER TABLE orders VALIDATE CONSTRAINT orders_status_chk;
ALTER TABLE orders VALIDATE CONSTRAINT orders_pay_chk;
ALTER TABLE orders VALIDATE CONSTRAINT orders_rating_chk;
ALTER TABLE orders VALIDATE CONSTRAINT orders_pin_chk;
ALTER TABLE order_items VALIDATE CONSTRAINT items_qty_chk;
