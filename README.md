# Order bot — заказы для оптового поставщика продуктов (HoReCa)

Telegram-бот на Python 3.14 / aiogram 3: повар пишет заказ текстом, голосом,
фото списка или через мини-приложение каталога; бот разбирает позиции по прайсу
Excel, уточняет непонятное кнопками, считает сумму, передаёт заказ упаковщику,
курьеру и плательщику, ведёт деньги (книга расчётов, долги, кредит-ноты),
склад (остатки, приход, списания, закупка) и качество разбора.

---

## Что умеет

**Клиент**
- Заказ текстом, голосом, фото списка, из мини-приложения; повтор «как обычно».
- Уточнения кнопками: количество, единица, фасовка, выбор позиции из кандидатов, пакетом.
- Прайс со скрытием позиций без остатка, стоп-лист, правила замены.
- Приём заявки на позицию, которой нет в прайсе (`/missing`, спрос без предложения).
- Несколько точек у сети: `/venue_add Название | адрес`, выбор точки кнопками при заказе.
- Геопин 📍 в любой момент — точное окно прибытия.
- Оценка после доставки, опрос через сутки с претензией и фото, ответ менеджера `/reply`.

**Упаковщик и менеджер**
- Карточка заказа с кнопками статуса, чек-лист сборки, передача заказа `/assign`.
- Деньги: `/debt`, `/settle`, `/creditnote`, `/act` (акт сверки PDF), накладная PDF
  с отметкой «оплачено/остаток» и историей зачётов.
- Цены: `/price`, `/price_set`, `/cost`, `/costvoice`, `/pricenew`, `/markup`, `/margin`.
- Клиенты: карточка `/client`, скидки `/discount`, особые цены `/cprice`, плательщик `/payer`,
  точки `/venues`, оценки бригады `/team`.
- Склад: `/stock`, `/lowstock`, `/income`, `/arrivals`, `/writeoff`, `/losses`,
  `/buylist` (закупка по поставщикам + прогноз по дню недели), `/supplier`, `/labels`.
- Качество: `/quality` (вопросы, источники, распознавание голоса/фото, теневые промпты),
  `/aliases`, `/alias_add`, метрики и расход ИИ в `/health`.

**Курьер**
- `/deliveries`, `/route`, `/handover`, кнопки «Выехал», «Наличные», «Перевод по чеку»,
  «Доставлено», «Не доставлено» с причиной и переносом на дату.
- Доставка списывает остатки и отправляет накладную клиенту, менеджеру и плательщику точки.
- Чек по фото распознаётся моделью: сумма сверяется с заказом, спор решает менеджер кнопкой.

---

## Как устроено

```
main.py                    # запуск, фоновые циклы, меню команд по ролям
orderbot/
├── handlers.py            # ядро: разбор, черновик, вопросы, подтверждение, панели
├── handlers_client.py     # заказ клиента, повторы, точки, правки
├── handlers_reports.py    # отчёты, деньги, клиенты, тарифы
├── handlers_stock.py      # склад, стоп-лист, качество разбора, словарь
├── handlers_courier.py    # доставки, маршрут, окно прибытия, чек
├── handlers_prices.py     # цены от закупки и наценки
├── handlers_settings.py / handlers_tail.py
├── db.py                  # слой хранения (PostgreSQL через psycopg)
├── nlu.py, ai.py, drafts.py, draftprep.py, units.py, catalog.py
├── ledger.py, statement.py, invoice.py, margins.py, costvoice.py, receipts.py
├── stock.py, buylist.py, forecast.py, writeoff.py, arrivals.py, labels.py
├── clientcard.py, churn.py, habit.py, payer.py, replies.py, survey.py, locations.py
├── shadow.py, aicost.py, summary.py, pricecast.py, subreplace.py, staffrating.py
├── route.py, eta.py, notify.py, pending.py, watch.py, golden.py, onboarding.py
└── webapp/                # мини-приложение каталога (aiohttp, https://…:8443)
```

**Разбор заказа**: локальный детерминированный парсер → DeepSeek JSON → Gemini JSON
(провайдер задаётся `AI_PROVIDER`), плюс распознавание речи (Gemini, офлайн Vosk) и
чтение фото чеков (Gemini Vision). Локальный парсер работает всегда и не требует API.

**Данные**: PostgreSQL (`payments`, `allocations`, `credit_notes`, `credit_applications`,
`locations`, `parse_feedback`, `shadow_parses`, `receipt_checks`, `writeoffs`, `arrivals`,
`client_discounts`, `client_prices`, `llm_usage` и др.), схема — версия **42**,
миграции в `migrations/`, применяются `scripts/migrate.py` после репетиции на копии базы.

---

## Качество и проверки

- `bash scripts/test_all.sh` → **819 / 0**, **84 / 0**, **16 / 0** (самопроверка,
  сквозной сценарий, поток доставки), `ruff` по неопределённым именам — чисто.
- `scripts/eval_parse.py --check` — точность разбора золотого набора **100 %**,
  падение больше 2 п. блокирует деплой.
- `scripts/buttons_check.py` — у каждой кнопки есть обработчик.
- `scripts/doctor.py` — проверка окружения, прав `.env`, бэкапа, портов, миграций.
- `scripts/compare_models.py` — сравнение моделей ИИ по точности и скорости на золотом наборе.
- `scripts/slow_queries.py` — топ медленных запросов (pg_stat_statements).

---

## Эксплуатация

- systemd: `order_bot` (бот), `order_bot_web` (каталог), `order_bot_backup.timer`
  (часовой `pg_dump`), `order_bot_verify.timer` (еженедельная проверка восстановления).
- `scripts/rehearse.sh` — репетиция миграций на копии боевой базы (без риска).
- `scripts/backup_db.py` / `scripts/verify_backup.py` — бэкап и проверка читаемости.
- `scripts/bootstrap.sh` — развёртывание с нуля, `scripts/seed_demo.py` — демо-данные.
- Документы: `docs/AGENT.md` (правила работы), `docs/MONITORING.md`,
  `docs/OFFSITE_BACKUP.md`, `docs/POSTGRES_TUNING.md`, `RUNBOOK_MANAGER.md`.
- Порядок деплоя: тесты → репетиция миграции → бэкап → миграция → проверка журнала и
  `doctor.py` → CHANGELOG → коммит и тег `deploy-ГГГГММДД-N`.

---

## Безопасность

- Токены и ключи только в `.env` (права 600), в чат и логи не выводятся.
- `MANAGER_CHAT_ID` и `COURIER_CHAT_ID` — из `.env`; роли проверяются на каждом действии.
- Персональные данные: имена, телефоны, адреса — только в базе бота, наружу не уходят.
- USDT/TON заморожены до юридического заключения; Payme/Click — только по договору.
