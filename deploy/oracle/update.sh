#!/usr/bin/env bash
#
# 코드 업데이트 — GitHub에 push한 내용을 서버에 반영한다.
#
# 사용법:
#   sudo bash /opt/mcu/deploy/oracle/update.sh
#
# 하는 일: git pull → 패키지 갱신 → 서비스 재시작 → 상태 확인
# DB(/var/lib/mcu)와 설정(/etc/mcu/mcu.env)은 건드리지 않는다.
#
set -euo pipefail

APP_USER=mcu
APP_DIR=/opt/mcu
BRANCH="${BRANCH:-main}"

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "sudo로 실행하세요."; exit 1; }

say "현재 버전"
sudo -H -u "$APP_USER" git -C "$APP_DIR" log -1 --oneline

say "코드 내려받기"
sudo -H -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
sudo -H -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"

say "패키지 갱신"
sudo -H -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

say "서비스 재시작"
# systemd 유닛 파일 자체가 바뀌었을 수 있으므로 함께 갱신
install -m 644 "$APP_DIR/deploy/oracle/mcu.service" /etc/systemd/system/mcu.service
systemctl daemon-reload
systemctl restart mcu

sleep 3
say "결과"
sudo -H -u "$APP_USER" git -C "$APP_DIR" log -1 --oneline
systemctl --no-pager --lines=10 status mcu

cat <<'EOF'

문제가 있으면 로그를 확인하세요:
    sudo journalctl -u mcu -n 100 --no-pager
EOF
