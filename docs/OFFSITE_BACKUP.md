# Копия бэкапа вне сервера (A2) — что нужно сделать владельцу

Сейчас бэкапы базы делаются каждый час **на самом сервере** (`/var/backups/order_bot`).
Если сервер потеряется целиком, потеряются и бэкапы. Нужна копия в облаке.

Подготовка уже сделана в коде:
- `scripts/offsite_backup.sh` — берёт свежий дамп, отправляет в облако через `rclone`,
  сверяет размер, чистит старое в облаке, пингует Healthchecks (если настроен);
- `deploy/order_bot_offsite.service` + `.timer` — запуск раз в сутки в 04:30 с разбросом;
- скрипт ничего не делает, пока в `.env` не появится `OFFSITE_REMOTE` — безвреден по умолчанию.

## Шаги (10–15 минут, нужны данные аккаунта)

1. Завести хранилище. Подойдёт любое из:
   - **Backblaze B2** (10 ГБ бесплатно) — рекомендую;
   - **Cloudflare R2** (10 ГБ бесплатно, нужен аккаунт Cloudflare);
   - Яндекс.Диск (проще всего, если он уже есть).
2. На сервере поставить rclone: `apt install -y rclone`.
3. Настроить удалённое хранилище: `rclone config` → `n` (новое) → имя `b2` → выбрать провайдера →
   вставить ключи.
4. Сделать шифрованный слой (ключ хранится **не на сервере** — запишите его в менеджер паролей):
   `rclone config` → `n` → имя `b2crypt` → тип `crypt` → remote `b2:имя-бакета` →
   `encrypt filenames: true` → пароль и соль (сохранить!).
5. Проверить: `rclone lsd b2crypt:` показывает пустой список без ошибок.
6. В `/opt/order_bot/.env` добавить одну строку: `OFFSITE_REMOTE=b2crypt:orderbot-backups`
   (и при желании `OFFSITE_KEEP_DAYS=30`).
7. Поставить таймер:
   ```bash
   cp /opt/order_bot/deploy/order_bot_offsite.service /etc/systemd/system/
   cp /opt/order_bot/deploy/order_bot_offsite.timer /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now order_bot_offsite.timer
   systemctl start order_bot_offsite.service      # первый прогон руками
   journalctl -u order_bot_offsite -n 20 --no-pager
   ```
8. Проверка «настоящая»: в облаке появился файл `orderbot-pg-…dump`, размер совпадает с файлом
   на сервере. Раз в месяц полезно скачать копию и прогнать `scripts/restore.py` на тестовой базе.

## Важно
- **Ключ шифрования нельзя держать только на сервере**: при его потере копии бесполезны.
- Пароль от облака и rclone-конфиг не пересылать в чат.
- Если Healthchecks настроен (`HEARTBEAT_URL` в `.env`), скрипт сам сообщит туда об успехе
  или пришлёт `/fail` при ошибке.

## Вариант без облака: копия в Telegram (уже работает)

Бот раз в сутки (цикл `_send_daily_backup` в `main.py`) отправляет свежий дамп
менеджеру в Telegram — это уже копия **вне сервера**: файл лежит в облаке Telegram,
а не на диске. Проверка: `venv/bin/python scripts/doctor.py` пишет
«копия бэкапа вне сервера: ежедневно в Telegram менеджеру».

Плюсы: ничего настраивать не нужно, нет ключей и квот.
Минус: файл лежит в переписке (его видно в чате менеджера).

Облако (`OFFSITE_REMOTE`) — необязательный **второй** канал: стоит заводить, если нужна
независимая от Telegram копия. Тогда doctor пишет «облако и Telegram».

## Что уже сделано на сервере (27.11)

- `rclone` установлен (`rclone v1.60.1`).
- Скрипт и цепочка проверены целиком: копия отправилась, размеры совпали,
  **дамп из «облака» восстановился** (`verify_backup.py`: 22 заказа).
- Настройка сводится к одной команде (сама правит `.env` и включает таймер):

```bash
cd /opt/order_bot
bash scripts/set_external.sh OFFSITE_REMOTE b2crypt:orderbot-backups
```

- Проверить состояние без вывода секретов: `bash scripts/set_external.sh --show`.

## Проверка без облака (если хочется убедиться заранее)

`rclone` умеет работать с обычной папкой — годится для репетиции:

```bash
mkdir -p /tmp/offsite_test
OFFSITE_REMOTE=/tmp/offsite_test bash scripts/offsite_backup.sh
ls -l /tmp/offsite_test
```

Так проверяется вся цепочка, кроме самого облака. Файл из `/tmp/offsite_test` можно
положить в `data/backups/` и прогнать `venv/bin/python scripts/verify_backup.py` —
убедиться, что копия восстанавливается.
