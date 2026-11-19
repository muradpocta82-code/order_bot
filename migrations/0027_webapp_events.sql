-- 0027: счётчик открытий мини-приложения каталога (G5).
-- Нужен, чтобы решить по фактам, оставлять мини-апп или отключить.

CREATE TABLE IF NOT EXISTS webapp_events (
    id serial PRIMARY KEY,
    user_id bigint NOT NULL,
    ts timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_webapp_events_ts ON webapp_events(ts DESC);
CREATE INDEX IF NOT EXISTS idx_webapp_events_user ON webapp_events(user_id, kind, ts DESC);
