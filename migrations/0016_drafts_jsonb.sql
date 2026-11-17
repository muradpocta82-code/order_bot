-- 0016: содержимое черновика — настоящий JSON, отметка эскалации — в заказе.

-- 1. drafts.payload: text(JSON) -> jsonb. Некорректный JSON не пройдёт,
--    поэтому миграция сначала проверяет данные и падает с понятной ошибкой.
DO $$
DECLARE
    bad integer;
BEGIN
    SELECT count(*) INTO bad
    FROM drafts
    WHERE payload IS NOT NULL AND payload <> ''
      AND NOT (payload ~ '^\s*[\{\[]');
    IF bad > 0 THEN
        RAISE EXCEPTION 'drafts.payload: % строк не похожи на JSON', bad;
    END IF;
END
$$;

ALTER TABLE drafts ALTER COLUMN payload TYPE jsonb USING payload::jsonb;
ALTER TABLE drafts ALTER COLUMN payload SET DEFAULT '{}'::jsonb;

-- 2. Отметка «упаковщику уже напомнили» — в самом заказе, а не в settings.
--    Время пока текстом, как и остальные отметки времени.
ALTER TABLE orders ADD COLUMN IF NOT EXISTS escalated_at text;
