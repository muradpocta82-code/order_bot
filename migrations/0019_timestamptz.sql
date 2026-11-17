-- 0019: время — настоящий timestamptz вместо текста.
-- Старые значения трактуются как местное время Asia/Tashkent (там работает бизнес),
-- поэтому результат не зависит от часового пояса сессии.

ALTER TABLE aliases
    ALTER COLUMN updated_at TYPE timestamptz
    USING (NULLIF(updated_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE claims
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE drafts
    ALTER COLUMN updated_at TYPE timestamptz
    USING (NULLIF(updated_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE missing_items
    ALTER COLUMN last_seen TYPE timestamptz
    USING (NULLIF(last_seen, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE orders
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE orders
    ALTER COLUMN packed_at TYPE timestamptz
    USING (NULLIF(packed_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN delivering_at TYPE timestamptz
    USING (NULLIF(delivering_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN delivered_at TYPE timestamptz
    USING (NULLIF(delivered_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN paid_at TYPE timestamptz
    USING (NULLIF(paid_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE orders
    ALTER COLUMN notified_at TYPE timestamptz
    USING (NULLIF(notified_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN courier_notified_at TYPE timestamptz
    USING (NULLIF(courier_notified_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN escalated_at TYPE timestamptz
    USING (NULLIF(escalated_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE price_versions
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE question_log
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE staff
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE stock_items
    ALTER COLUMN updated_at TYPE timestamptz
    USING (NULLIF(updated_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE ton_payments
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN expires_at TYPE timestamptz
    USING (NULLIF(expires_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN paid_at TYPE timestamptz
    USING (NULLIF(paid_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');

ALTER TABLE users
    ALTER COLUMN created_at TYPE timestamptz
    USING (NULLIF(created_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent'),
    ALTER COLUMN updated_at TYPE timestamptz
    USING (NULLIF(updated_at, '')::timestamp AT TIME ZONE 'Asia/Tashkent');
