# Карта проекта для агента (краткая)

Читай это вместо полного аудита: 5 КБ вместо 30. Подробности — `CHANGELOG.md`,
`docs/АУДИТ_order_bot_2026-11-17.md` (если положили), `README.md`.

## Что это

Telegram-бот приёма заказов для оптового поставщика продуктов (HoReCa, Ташкент).
Цикл: заказ клиента (текст/голос/фото/мини-апп) → разбор по прайсу → упаковщик →
доставщик → оплата (нал / перевод / USDT-TON / долг) → закрыт.

## Где что лежит

| Что | Где |
|---|---|
| Запуск, 13 фоновых циклов, меню команд | `main.py` |
| Ядро обработчиков (1584 строки) | `orderbot/handlers.py` |
| Статусы заказа, списки доставок | `orderbot/handlers_status.py` |
| Оплата USDT в TON, /pay | `orderbot/handlers_ton.py` |
| Панели менеджера и клиента | `orderbot/handlers_panels.py` |
| Команды (/start, /help, /order, /health …) | `orderbot/handlers_commands.py` |
| Приём: довес, адрес, телефон, геопин | `orderbot/handlers_intake.py` |
| Фото, чеки, претензии, горячее предложение | `orderbot/handlers_photos.py` |
| Касса, сотрудники, смены | `orderbot/handlers_shift.py` |
| Оценки, опрос, курс валюты | `orderbot/handlers_feedback.py` |
| Склад, качество, словарь | `orderbot/handlers_stock.py` |
| Цены от закупки, наценки | `orderbot/handlers_prices.py` |
| Заказ клиента | `orderbot/handlers_client.py` |
| Панель доставщика | `orderbot/handlers_courier.py` |
| Отчёты, деньги, рассылка | `orderbot/handlers_reports.py` |
| Широкие фильтры (**подключается последним**) | `orderbot/handlers_tail.py` |
| Слой данных (всё в одном классе) | `orderbot/db.py` |
| Разбор текста без AI | `orderbot/nlu.py` |
| AI: Gemini (текст+голос), локальный план Б | `orderbot/ai.py`, `orderbot/stt.py` |
| Черновик заказа, прайс, единицы | `orderbot/drafts.py`, `catalog.py`, `units.py` |
| Счётчик ошибок для тревог | `orderbot/watch.py` |
| Тексты для нескольких модулей | `orderbot/texts.py` |
| Время старта и последняя ошибка | `orderbot/runtime.py` |
| USDT в сети TON | `orderbot/tonflow.py`, `tonpay.py` |
| Мини-приложение каталога (порт 8443) | `webapp/server.py`, `webapp/index.html` |
| Бэкап базы (отдельная служба) | `scripts/backup_db.py` + `deploy/order_bot_backup.*` |
| Диагностика развёртывания | `scripts/doctor.py` |
| Тесты | `scripts/test_all.sh` (selftest 147, integration 82, delivery 16) |

## Службы

- `order_bot` — бот, пользователь **orderbot**, `MemoryMax=900M`.
- `order_bot_web` — каталог, порт 8443, `GET /health` для мониторинга.
- `order_bot_backup.timer` — часовой `pg_dump` от postgres в `/var/backups/order_bot`.
- `order_bot_verify.timer` — вс 03:30 проверка восстановления бэкапа.

## Обязательный порядок изменений

1. `cd /opt/order_bot && bash scripts/test_all.sh` → 147/0 · 82/0 · 16/0 + ruff без замечаний.
2. Миграция → сначала репетиция на `order_bot_rehearsal`, потом боевая (`scripts/migrate.py`).
3. Деплой: `systemctl restart order_bot`, затем `journalctl -u order_bot -n 30 --no-pager`.
4. `venv/bin/python scripts/doctor.py` — не должно быть ❌.
5. Изменение записать в `CHANGELOG.md` и закоммитить в git (`deploy-ГГГГММДД-N` тег).

## Жёсткие запреты

- Порт **443** и всё про VPN владельца (wstunnel, xray, x-ui, vpn.sh, webroot) — не трогать.
- `.env` целиком не читать и не печатать; секреты в чат не выводить.
- Широкие фильтры aiogram — только в `handlers_tail.py`.
- Локальные файлы ноутбука владельца не менять.

## Полезное

```bash
systemctl is-active order_bot order_bot_web order_bot_backup.timer
journalctl -u order_bot -n 50 --no-pager
ls -lt /var/backups/order_bot | head
cd /opt/order_bot && venv/bin/python scripts/verify_backup.py
```
