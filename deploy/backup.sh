#!/usr/bin/env bash
# Суточная копия базы. Ставится в cron один раз:
#
#   crontab -e
#   0 4 * * * /home/aidabot/projects/Aida/deploy/backup.sh
#
# База — единственное, что не восстанавливается ниоткуда: код лежит
# в GitHub, материалы есть у самих преподавателей, векторную базу можно
# пересобрать. А привязки учеников, коды приглашений и весь прогресс
# существуют только здесь.

set -euo pipefail

PROJECT="/home/aidabot/projects/Aida"
DB="$PROJECT/data/history_ct.sqlite3"
DEST="/home/aidabot/backups"
KEEP_DAYS=7

mkdir -p "$DEST"

if [ ! -f "$DB" ]; then
    echo "backup: базы нет по пути $DB" >&2
    exit 1
fi

STAMP=$(date +%F_%H%M)
OUT="$DEST/history_ct_$STAMP.sqlite3"

# Копируем средствами самой SQLite, а не `cp`: бот пишет в базу постоянно,
# и обычное копирование на живой записи даёт битый файл, который выглядит
# целым до первой попытки его открыть.
"$PROJECT/venv/bin/python" - "$DB" "$OUT" <<'PY'
import sqlite3, sys

src, dst = sys.argv[1], sys.argv[2]
with sqlite3.connect(src) as source, sqlite3.connect(dst) as target:
    source.backup(target)
PY

# Старые копии убираем сами: иначе через год на диске лежит 365 файлов,
# и первым, что кончится, будет место — как раз в неподходящий момент.
find "$DEST" -name 'history_ct_*.sqlite3' -mtime "+$KEEP_DAYS" -delete

echo "backup: $OUT ($(du -h "$OUT" | cut -f1))"
