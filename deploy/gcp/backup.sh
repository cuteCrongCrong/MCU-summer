#!/usr/bin/env bash
#
# DB 백업 — 하루치씩 보관하고 오래된 것은 지운다.
#
# ⚠️ WAL 모드를 쓰므로 sessions.db 파일만 복사하면 안 된다.
#    -wal 파일에 아직 반영되지 않은 내용이 있어 깨진 백업이 만들어진다.
#    반드시 sqlite3 의 .backup 명령을 써야 한다.
#
# 수동 실행:
#   sudo bash /opt/mcu/deploy/gcp/backup.sh
#
# 매일 새벽 4시 자동 백업 등록:
#   sudo crontab -e
#   0 4 * * * /bin/bash /opt/mcu/deploy/gcp/backup.sh >> /var/log/mcu-backup.log 2>&1
#
# ── Cloud Storage에도 올리려면 (선택) ──
#   VM이 죽거나 디스크가 날아가도 백업이 남는다. US 리전 5GB까지 무료다.
#
#   1) 버킷 만들기 (내 PC에서, 무료 조건상 리전은 US여야 한다):
#        gcloud storage buckets create gs://<버킷이름> --location=us-west1
#   2) VM에 쓰기 권한 주기 — 기본 스코프는 읽기 전용이라 그냥은 실패한다.
#        gcloud compute instances stop mcu-app --zone=us-west1-b
#        gcloud compute instances set-service-account mcu-app --zone=us-west1-b \
#            --scopes=https://www.googleapis.com/auth/devstorage.read_write
#        gcloud compute instances start mcu-app --zone=us-west1-b
#   3) VM에 gcloud 설치 (Ubuntu 이미지에는 없다):
#        sudo snap install google-cloud-cli --classic
#   4) 이 스크립트를 GCS_BUCKET 과 함께 실행:
#        sudo GCS_BUCKET=gs://<버킷이름> bash /opt/mcu/deploy/gcp/backup.sh
#
#   cron에 넣을 때는 crontab 줄 맨 앞에 변수를 붙인다:
#        0 4 * * * GCS_BUCKET=gs://<버킷이름> /bin/bash /opt/mcu/deploy/gcp/backup.sh >> /var/log/mcu-backup.log 2>&1
#
set -euo pipefail

DB=/var/lib/mcu/sessions.db
DEST=/var/lib/mcu/backups
KEEP_DAYS=14
GCS_BUCKET="${GCS_BUCKET:-}"   # 비어 있으면 로컬 보관만 한다

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

# ── Cloud Storage 업로드 (GCS_BUCKET 이 지정됐을 때만) ──
if [ -n "$GCS_BUCKET" ]; then
    if ! command -v gcloud >/dev/null 2>&1; then
        # 로컬 백업은 이미 성공했으므로 여기서 죽이지 않고 경고만 남긴다.
        echo "[주의] GCS_BUCKET이 지정됐지만 gcloud가 없어 업로드를 건너뜁니다."
        echo "       설치: sudo snap install google-cloud-cli --classic"
    elif gcloud storage cp "$OUT.gz" "$GCS_BUCKET/"; then
        echo "업로드 완료: $GCS_BUCKET/$(basename "$OUT.gz")"
    else
        echo "[주의] 업로드 실패 — VM의 스토리지 권한(스코프)을 확인하세요."
        echo "       이 스크립트 상단의 'Cloud Storage에도 올리려면' 2번 참고."
    fi
fi

# 오래된 백업 정리 (로컬만. GCS는 버킷의 수명 주기 규칙으로 관리하는 편이 낫다)
find "$DEST" -name 'sessions-*.db.gz' -mtime "+$KEEP_DAYS" -print -delete

echo "보관 중인 백업: $(find "$DEST" -name 'sessions-*.db.gz' | wc -l)개"
