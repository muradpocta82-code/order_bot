-- 0039: метрика распознавания голоса и фото (E8), провайдер в заказе.
-- Клиент жмёт «Верно» или «Поправить» — видно долю правок у Gemini и Vosk.

CREATE TABLE IF NOT EXISTS parse_feedback (
    order_id integer PRIMARY KEY,
    chef_id bigint,
    provider text,
    verdict text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE orders ADD COLUMN IF NOT EXISTS parse_provider text;
