-- 0033: роль «плательщик» (C6). Бухгалтер точки получает накладные и напоминания о долге.

CREATE TABLE IF NOT EXISTS venue_contacts (
    user_id bigint PRIMARY KEY,
    venue_id bigint NOT NULL,
    role text NOT NULL DEFAULT 'payer',
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_venue_contacts_venue ON venue_contacts(venue_id);
