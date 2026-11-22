# Postgres под 1.9 ГБ памяти (I4)

Сервер: 2 vCPU, 1.9 ГБ RAM, swap 2 ГБ, база `order_bot`. Настройки заданы через
`ALTER SYSTEM` (лежат в `postgresql.auto.conf`), не в `postgresql.conf`.

| Параметр | Значение | Зачем |
|---|---|---|
| `shared_buffers` | 384MB | кэш страниц базы, примерно 20% памяти |
| `effective_cache_size` | 1GB | подсказка планировщику про кэш ОС |
| `work_mem` | 8MB | сортировки и хеши на один шаг запроса |
| `maintenance_work_mem` | 64MB | вакуум и индексы |
| `max_connections` | 50 | бот с пулом 1–8, остальное запас |
| `shared_preload_libraries` | `pg_stat_statements` | статистика запросов |
| `pg_stat_statements.max` / `track` | 1000 / all | видеть все запросы |
| `log_min_duration_statement` | 500 мс | в журнал попадают только медленные |

Пул приложения: `POOL_MIN = 1`, `POOL_MAX = 8` (`orderbot/pg_psycopg.py`).

## Как менять

```
su postgres -c "psql -d order_bot -c \"ALTER SYSTEM SET work_mem = '8MB'\""
systemctl restart postgresql
systemctl restart order_bot order_bot_web
```

## Раз в месяц

```
cd /opt/order_bot
venv/bin/python scripts/slow_queries.py            # топ-5 по суммарному времени
venv/bin/python scripts/slow_queries.py --by mean  # топ-5 по среднему
su postgres -c "psql -d order_bot -c 'SELECT pg_stat_statements_reset()'"
```

Смотреть на `calls` и `mean_ms`: частый запрос с большим средним — первый кандидат на индекс
или на кэш в приложении. Настройки меняются без деплоя кода, но с перезапуском Postgres
(миграции и бэкапы при этом не трогаем).
