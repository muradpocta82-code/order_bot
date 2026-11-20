-- 0032: запросы вне прайса с клиентом (B11). Нужны для недельной сводки и уведомлений.

CREATE TABLE IF NOT EXISTS missing_requests (
    id serial PRIMARY KEY,
    user_id bigint,
    phrase text NOT NULL,
    ts timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_missing_requests_ts ON missing_requests(ts DESC);
CREATE INDEX IF NOT EXISTS idx_missing_requests_phrase ON missing_requests(phrase);
