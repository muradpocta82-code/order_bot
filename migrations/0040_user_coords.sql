-- 0040: геопин клиента (C12) — точное окно прибытия и маршрут.
-- Координаты храним отдельно от текста адреса: текст остаётся для людей.

ALTER TABLE users ADD COLUMN IF NOT EXISTS lat double precision;
ALTER TABLE users ADD COLUMN IF NOT EXISTS lon double precision;
