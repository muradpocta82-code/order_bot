#!/usr/bin/env bash
# Статус и последние логи бота.
systemctl --no-pager --full status order_bot || true
echo "============================================================"
journalctl -u order_bot -n 50 --no-pager || true
