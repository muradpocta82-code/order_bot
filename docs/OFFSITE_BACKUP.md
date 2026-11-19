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
