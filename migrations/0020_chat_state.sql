-- 0020: состояния чата — отдельная таблица вместо ключей вида "weigh:<chat_id>" в settings.

CREATE TABLE IF NOT EXISTS chat_state (
    chat_id bigint NOT NULL,
    name text NOT NULL,
    value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, name)
);

CREATE INDEX IF NOT EXISTS idx_chat_state_updated ON chat_state(updated_at);

-- Старые ключи состояний больше не читаются — убираем, чтобы settings не пух.
DELETE FROM settings
WHERE key ~ '^(retry|onboard|acc_notified|qty_input|date_input|weigh|cashwait|pinwait|pinok|edit|receipt|claim|broadcast|unit_habit|remind_off|last_remind|escalated|err_alert):';
