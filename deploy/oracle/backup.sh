#!/usr/bin/env bash
#
# DB 백업 — 하루치씩 보관하고 오래된 것은 지운다.
#
# ⚠️ WAL 모드를 쓰므로 sessions.db 파일만 복사하면 안 된다.
#    -wal 파일에 아직 반영되지 않은 내용이 있어 깨진 백업이 만들어진다.
#    반드시 sqlite3 의 .backup 명령을 써야 한다.
#
# 수동 실행:
#   sudo bash /opt/mcu/deploy/oracle/backup.sh
#
# 매일 새벽 4시 자동 백업 등록:
#   sudo crontab -e
#   0 4 * * * /bin/bash /opt/mcu/deploy/oracle/backup.sh >> /var/log/mcu-backup.log 2>&1
#
set -euo pipefail

DB=/var/lib/mcu/sessions.db
DEST=/var/lib/mcu/backups
KEEP_DAYS=14

[ "$(id -u)" -eq 0 ] || { echo "sudo로 실행하세요."; exit 1; }
command -v sqlite3 >/dev/null 2>&1 || { echo "sqlite3 설치: sudo apt-get install -y sqlite3"; exit 1; }
[ -f "$DB" ] || { echo "DB가 없습니다: $DB"; exit 1; }

mkdir -p "$DEST"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="$DEST/sessions-$STAMP.db"

sqlite3 "$DB" ".backup '$OUT'"
gzip -f "$OUT"
chown -R mcu:mcu "$DEST"

echo "백업 완료: $OUT.gz ($(du -h "$OUT.gz" | cut -f1))"

# 오래된 백업 정리
find "$DEST" -name 'sessions-*.db.gz' -mtime "+$KEEP_DAYS" -print -delete

echo "보관 중인 백업: $(find "$DEST" -name 'sessions-*.db.gz' | wc -l)개"
