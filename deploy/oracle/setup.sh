#!/usr/bin/env bash
#
# MCU 학습 플랫폼 — Oracle Cloud (Ubuntu) 자동 설치 스크립트
#
# 하는 일:
#   1. 필요한 패키지 + Caddy(HTTPS 자동 발급) 설치
#   2. 전용 사용자(mcu)로 앱을 /opt/mcu 에 설치, DB는 /var/lib/mcu 에 분리
#   3. 환경변수 파일 생성 (FLASK_SECRET_KEY 자동 생성)
#   4. systemd 서비스 등록 → 재부팅해도 자동 시작
#   5. Caddy 리버스 프록시 설정 → https 자동 적용
#   6. 인스턴스 방화벽(iptables)에 80/443 개방
#   7. (토큰을 주면) DuckDNS 자동 갱신 등록
#
# 사용법 (서버에 SSH 접속 후):
#   curl -fsSL https://raw.githubusercontent.com/cuteCrongCrong/MCU-summer/main/deploy/oracle/setup.sh -o setup.sh
#   sudo bash setup.sh <도메인>
#
# 예:
#   sudo bash setup.sh mcu-club.duckdns.org
#
# DuckDNS를 쓰고 토큰을 넘기면, 공인 IP가 바뀌어도 5분마다 자동으로 도메인을 갱신한다:
#   sudo env DUCKDNS_TOKEN=<토큰> bash setup.sh mcu-club.duckdns.org
#
# (sudo VAR=값 이 아니라 sudo env VAR=값 을 쓰는 이유: 전자는 sudoers의 setenv
#  정책에 따라 변수가 걸러질 수 있다. env를 거치면 정책과 무관하게 전달된다)
#
# ⚠️ 이 스크립트를 돌리기 전에 반드시:
#   - Oracle 콘솔의 보안 목록(Security List)에서 80·443 포트를 열어둘 것
#   - 도메인이 이 서버의 공인 IP를 가리키도록 설정해둘 것 (DuckDNS 등)
#   자세한 절차는 저장소의 배포-오라클.md 참고.
#
set -euo pipefail

DOMAIN="${1:-}"
REPO_URL="${REPO_URL:-https://github.com/cuteCrongCrong/MCU-summer.git}"
BRANCH="${BRANCH:-main}"

APP_USER=mcu
APP_DIR=/opt/mcu
DATA_DIR=/var/lib/mcu
ENV_DIR=/etc/mcu
ENV_FILE="$ENV_DIR/mcu.env"
APP_PORT=8000

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[주의] %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[오류] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "sudo로 실행하세요:  sudo bash setup.sh <도메인>"
[ -n "$DOMAIN" ] || die "도메인을 인자로 넘기세요.  예: sudo bash setup.sh mcu-club.duckdns.org"

# ──────────────────────────────────────────────
say "1/7  시스템 패키지 설치"
# ──────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# sqlite3: backup.sh 의 .backup 명령에 필요하다 (WAL 때문에 파일 복사로는 안 된다).
# cron   : DuckDNS 자동 갱신에 쓴다.
apt-get install -y \
    python3 python3-venv python3-pip git curl ca-certificates cron sqlite3 \
    debian-keyring debian-archive-keyring apt-transport-https \
    iptables-persistent

# ──────────────────────────────────────────────
say "2/7  Caddy 설치 (HTTPS 인증서 자동 발급·갱신)"
# ──────────────────────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -y
    apt-get install -y caddy
else
    echo "이미 설치되어 있음 — 건너뜀"
fi

# ──────────────────────────────────────────────
say "3/7  앱 사용자·디렉터리 준비"
# ──────────────────────────────────────────────
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ENV_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"

# ──────────────────────────────────────────────
say "4/7  코드 내려받기 + 파이썬 가상환경"
# ──────────────────────────────────────────────
if [ -d "$APP_DIR/.git" ]; then
    sudo -H -u "$APP_USER" git -C "$APP_DIR" fetch --all --prune
    sudo -H -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
else
    sudo -H -u "$APP_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

sudo -H -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -H -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip
sudo -H -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ──────────────────────────────────────────────
say "5/7  환경변수 파일"
# ──────────────────────────────────────────────
# ⚠️ FLASK_SECRET_KEY는 한 번 만들면 절대 바꾸지 않는다.
#    바뀌면 기존 쿠키 서명이 전부 무효가 되어, 비로그인 사용자들이
#    자기 세션·오답노트에 접근할 수 없게 된다. → 파일이 있으면 건드리지 않는다.
if [ -f "$ENV_FILE" ]; then
    echo "$ENV_FILE 이(가) 이미 있습니다 — 기존 설정(특히 시크릿 키)을 유지합니다."
else
    SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
    cat > "$ENV_FILE" <<EOF
# MCU 학습 플랫폼 — 배포 환경변수
# 값을 바꾼 뒤에는:  sudo systemctl restart mcu

APP_ENV=production
FLASK_SECRET_KEY=$SECRET
DB_PATH=$DATA_DIR/sessions.db

# Caddy가 앞단이므로 앱은 로컬에서만 듣는다.
HOST=127.0.0.1
PORT=$APP_PORT

# 프록시(Caddy)가 같은 서버에 있으므로 127.0.0.1만 신뢰한다.
TRUSTED_PROXY=127.0.0.1

# Google 로그인 — 값을 채우면 로그인 버튼이 나타난다. 비워두면 게스트 전용으로 동작.
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
EOF
    echo "새로 만들었습니다: $ENV_FILE"
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ──────────────────────────────────────────────
say "6/7  systemd 서비스 등록"
# ──────────────────────────────────────────────
install -m 644 "$APP_DIR/deploy/oracle/mcu.service" /etc/systemd/system/mcu.service
systemctl daemon-reload
systemctl enable mcu
systemctl restart mcu

# ──────────────────────────────────────────────
say "7/7  Caddy 설정 + 방화벽 개방"
# ──────────────────────────────────────────────
sed -e "s|{{DOMAIN}}|$DOMAIN|g" -e "s|{{APP_PORT}}|$APP_PORT|g" \
    "$APP_DIR/deploy/oracle/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

# Oracle Cloud의 Ubuntu 이미지는 기본 iptables가 22번 외 모든 포트를 막는다.
# -I 로 맨 앞에 넣어야 아래쪽 REJECT 규칙보다 먼저 평가된다.
for port in 80 443; do
    if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
        iptables -I INPUT -p tcp --dport "$port" -j ACCEPT
        echo "iptables: $port 개방"
    else
        echo "iptables: $port 이미 열려 있음"
    fi
done
netfilter-persistent save >/dev/null

# DuckDNS 자동 갱신 — 토큰을 줬고 도메인이 duckdns.org일 때만.
# OCI의 ephemeral 공인 IP는 인스턴스 수명 동안 유지되므로, GCP와 달리 정지→시작으로는
# 바뀌지 않는다. 다만 인스턴스를 다시 만들거나 IP를 재할당하면 바뀌고, 그때 도메인이
# 어긋나면 접속뿐 아니라 Caddy의 인증서 갱신까지 막힌다 → 안전망으로 걸어둔다.
if [ -n "${DUCKDNS_TOKEN:-}" ] && [ "${DOMAIN%.duckdns.org}" != "$DOMAIN" ]; then
    SUB="${DOMAIN%.duckdns.org}"
    cat > /usr/local/bin/mcu-duckdns.sh <<EOF
#!/bin/sh
# DuckDNS에 현재 공인 IP를 알린다. ip= 를 비워 보내면 DuckDNS가 접속 IP를 자동 인식한다.
curl -fsS "https://www.duckdns.org/update?domains=$SUB&token=$DUCKDNS_TOKEN&ip=" -o /var/log/mcu-duckdns.log
EOF
    # 토큰이 들어 있으므로 root만 읽을 수 있게 한다.
    chmod 700 /usr/local/bin/mcu-duckdns.sh
    ( crontab -l 2>/dev/null | grep -v 'mcu-duckdns.sh' || true
      echo "*/5 * * * * /usr/local/bin/mcu-duckdns.sh" ) | crontab -
    /usr/local/bin/mcu-duckdns.sh || warn "DuckDNS 갱신 요청이 실패했습니다. 토큰을 확인하세요."
    echo "DuckDNS 자동 갱신 등록됨 ($SUB, 5분 주기)"
fi

# ──────────────────────────────────────────────
say "완료"
# ──────────────────────────────────────────────
sleep 2
systemctl --no-pager --lines=5 status mcu || true

cat <<EOF

────────────────────────────────────────────────────────
  접속 주소:  https://$DOMAIN
  DB 파일  :  $DATA_DIR/sessions.db
  설정 파일:  $ENV_FILE
────────────────────────────────────────────────────────

다음 할 일:

 1) Google 로그인을 쓰려면 $ENV_FILE 에
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 를 채우고:
        sudo systemctl restart mcu

 2) Google Cloud Console → 승인된 리디렉션 URI 에 추가:
        https://$DOMAIN/login/google/callback

자주 쓰는 명령:
    sudo systemctl status mcu      # 상태 확인
    sudo journalctl -u mcu -f      # 실시간 로그
    sudo bash $APP_DIR/deploy/oracle/update.sh     # 코드 업데이트
    sudo bash $APP_DIR/deploy/oracle/backup.sh     # DB 백업

EOF
