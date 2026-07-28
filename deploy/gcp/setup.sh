#!/usr/bin/env bash
#
# 의대 예상문제 생성기 — Google Cloud (Compute Engine / Ubuntu) 자동 설치 스크립트
#
# 하는 일:
#   1. 필요한 패키지 설치
#   2. swap 2GB 생성          ← e2-micro는 RAM이 1GB뿐이라 반드시 필요
#   3. Caddy(HTTPS 자동 발급) 설치
#   4. 전용 사용자(mcu)로 앱을 /opt/mcu 에 설치, DB는 /var/lib/mcu 에 분리
#   5. 환경변수 파일 생성 (FLASK_SECRET_KEY 자동 생성)
#   6. systemd 서비스 등록 → 재부팅해도 자동 시작
#   7. Caddy 리버스 프록시 설정 → https 자동 적용
#   8. LLM 게이트웨이 도달 여부 점검
#
# 사용법 (VM에 SSH 접속 후):
#   curl -fsSL https://raw.githubusercontent.com/cuteCrongCrong/MCU-summer/main/deploy/gcp/setup.sh -o setup.sh
#   sudo bash setup.sh <도메인>
#
# 예:
#   sudo bash setup.sh mcu-club.duckdns.org
#
# DuckDNS를 쓰고 토큰을 넘기면, 외부 IP가 바뀌어도 5분마다 자동으로 도메인을 갱신한다:
#   sudo DUCKDNS_TOKEN=<토큰> bash setup.sh mcu-club.duckdns.org
#
# ⚠️ 이 스크립트를 돌리기 전에 반드시:
#   - GCP VPC 방화벽에서 80·443 포트를 열어둘 것 (아래 명령을 내 PC에서 실행)
#         gcloud compute firewall-rules create mcu-allow-web \
#             --allow=tcp:80,tcp:443 --target-tags=http-server,https-server
#     (VM 생성 시 --tags=http-server,https-server 를 줬어야 한다)
#   - 도메인이 이 VM의 외부 IP를 가리키도록 설정해둘 것 (DuckDNS 등)
#   자세한 절차는 저장소의 배포-GCP.md 참고.
#
# Oracle판(deploy/oracle/setup.sh)과 다른 점:
#   - iptables를 건드리지 않는다. GCP의 Ubuntu 이미지는 OS 방화벽이 열려 있고,
#     차단은 VPC 방화벽 규칙이 담당한다. (Oracle은 그 반대라 iptables 조작이 필요했다)
#   - swap을 만든다. Oracle 무료 티어는 RAM이 최대 24GB지만 e2-micro는 1GB다.
#   - 업로드 상한·스레드 수를 낮게 잡는다. (같은 이유)
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

SWAP_FILE=/swapfile
SWAP_SIZE=2G

# e2-micro(1GB RAM) 기준 기본값. llm.py는 PDF 전체를 메모리에 올리고
# 페이지를 150DPI로 렌더하므로(A4 1장 ≈ 6.5MB), 기본값 100MB/16스레드로 두면 OOM이 난다.
DEF_MAX_UPLOAD_MB=30
DEF_SERVER_THREADS=4

# 문제 생성이 실제로 되는지는 이 게이트웨이에 닿느냐에 달려 있다. (llm.py의 GATEWAY_BASE_URL)
GATEWAY_HOST=factchat-cloud.mindlogic.ai

say() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
warn() { printf '\n\033[1;33m[주의] %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31m[오류] %s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "sudo로 실행하세요:  sudo bash setup.sh <도메인>"
[ -n "$DOMAIN" ] || die "도메인을 인자로 넘기세요.  예: sudo bash setup.sh mcu-club.duckdns.org"

# ──────────────────────────────────────────────
say "1/8  시스템 패키지 설치"
# ──────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
# sqlite3: backup.sh 의 .backup 명령에 필요하다. (Oracle판 스크립트엔 빠져 있던 항목)
# cron   : DuckDNS 자동 갱신에 쓴다.
apt-get install -y \
    python3 python3-venv python3-pip git curl ca-certificates cron sqlite3 \
    debian-keyring debian-archive-keyring apt-transport-https

# ──────────────────────────────────────────────
say "2/8  swap 2GB 생성 (e2-micro는 RAM 1GB뿐)"
# ──────────────────────────────────────────────
# swap이 없으면 큰 PDF 한 건에 OOM Killer가 프로세스를 죽인다.
# systemd의 Restart=always가 되살리긴 하지만 그 요청은 실패한다.
if swapon --show=NAME --noheadings 2>/dev/null | grep -qx "$SWAP_FILE"; then
    echo "이미 활성화되어 있음 — 건너뜀"
else
    if [ ! -f "$SWAP_FILE" ]; then
        # fallocate가 안 먹는 파일시스템도 있어 dd로 폴백
        fallocate -l "$SWAP_SIZE" "$SWAP_FILE" 2>/dev/null \
            || dd if=/dev/zero of="$SWAP_FILE" bs=1M count=2048 status=none
    fi
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" >/dev/null
    swapon "$SWAP_FILE"
    echo "생성 완료: $SWAP_FILE ($SWAP_SIZE)"
fi
# 재부팅 후에도 유지
grep -qs "^$SWAP_FILE " /etc/fstab || echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
# swap은 어디까지나 OOM 방지용 안전망이다. 평소에도 swap을 쓰면 느려지므로 낮게 잡는다.
if [ ! -f /etc/sysctl.d/99-mcu-swap.conf ]; then
    echo "vm.swappiness=10" > /etc/sysctl.d/99-mcu-swap.conf
    sysctl -q -w vm.swappiness=10
fi
free -h

# ──────────────────────────────────────────────
say "3/8  Caddy 설치 (HTTPS 인증서 자동 발급·갱신)"
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
say "4/8  앱 사용자·디렉터리 준비"
# ──────────────────────────────────────────────
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR" "$DATA_DIR" "$ENV_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR" "$DATA_DIR"
chmod 750 "$DATA_DIR"

# ──────────────────────────────────────────────
say "5/8  코드 내려받기 + 파이썬 가상환경"
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
say "6/8  환경변수 파일"
# ──────────────────────────────────────────────
# ⚠️ FLASK_SECRET_KEY는 한 번 만들면 절대 바꾸지 않는다.
#    바뀌면 기존 쿠키 서명이 전부 무효가 되어, 비로그인 사용자들이
#    자기 세션·오답노트에 접근할 수 없게 된다. → 파일이 있으면 건드리지 않는다.
if [ -f "$ENV_FILE" ]; then
    echo "$ENV_FILE 이(가) 이미 있습니다 — 기존 설정(특히 시크릿 키)을 유지합니다."
else
    SECRET="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
    cat > "$ENV_FILE" <<EOF
# 의대 예상문제 생성기 — 배포 환경변수 (GCP e2-micro)
# 값을 바꾼 뒤에는:  sudo systemctl restart mcu

APP_ENV=production
FLASK_SECRET_KEY=$SECRET
DB_PATH=$DATA_DIR/sessions.db

# Caddy가 앞단이므로 앱은 로컬에서만 듣는다.
HOST=127.0.0.1
PORT=$APP_PORT

# 프록시(Caddy)가 같은 서버에 있으므로 127.0.0.1만 신뢰한다.
TRUSTED_PROXY=127.0.0.1

# ── e2-micro(RAM 1GB) 대응 ──
# llm.py는 업로드된 PDF 전체를 메모리에 올리고(read()), 페이지를 150DPI로
# 최대 15장 렌더한다(A4 1장 raw ≈ 6.5MB). 기본값(100MB / 16스레드)이면 OOM이 난다.
# e2-small(2GB) 이상으로 올렸다면 이 두 값을 키워도 된다.
MAX_UPLOAD_MB=$DEF_MAX_UPLOAD_MB
SERVER_THREADS=$DEF_SERVER_THREADS

# 구글 로그인 — 값을 채우면 로그인 버튼이 나타난다. 비워두면 게스트 전용으로 동작.
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
EOF
    echo "새로 만들었습니다: $ENV_FILE"
fi
chmod 600 "$ENV_FILE"
chown root:root "$ENV_FILE"

# ──────────────────────────────────────────────
say "7/8  systemd 서비스 + Caddy 설정"
# ──────────────────────────────────────────────
install -m 644 "$APP_DIR/deploy/gcp/mcu.service" /etc/systemd/system/mcu.service
systemctl daemon-reload
systemctl enable mcu
systemctl restart mcu

sed -e "s|{{DOMAIN}}|$DOMAIN|g" -e "s|{{APP_PORT}}|$APP_PORT|g" \
    "$APP_DIR/deploy/gcp/Caddyfile" > /etc/caddy/Caddyfile
systemctl reload caddy || systemctl restart caddy

# DuckDNS 자동 갱신 — 토큰을 줬고 도메인이 duckdns.org일 때만.
# VM을 정지→시작하면 외부 IP(ephemeral)가 바뀌므로, 그때 도메인이 끊기지 않게 한다.
# (단순 재부팅으로는 IP가 바뀌지 않는다)
if [ -n "${DUCKDNS_TOKEN:-}" ] && [ "${DOMAIN%.duckdns.org}" != "$DOMAIN" ]; then
    SUB="${DOMAIN%.duckdns.org}"
    cat > /usr/local/bin/mcu-duckdns.sh <<EOF
#!/bin/sh
# DuckDNS에 현재 외부 IP를 알린다. ip= 를 비워 보내면 DuckDNS가 접속 IP를 자동 인식한다.
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
say "8/8  LLM 게이트웨이 도달 점검"
# ──────────────────────────────────────────────
# 이 앱의 핵심 기능(문제 생성)은 전북대 LLM 게이트웨이에 닿아야 동작한다.
# GCP 무료 티어는 미국 리전이라, 게이트웨이가 국내 IP만 허용한다면 여기서 막힌다.
# 인증은 앱이 하므로 상태 코드는 무엇이든 상관없다 — "연결 자체가 되는가"만 본다.
GW_CODE="$(curl -sS -o /dev/null -m 15 -w '%{http_code}' "https://$GATEWAY_HOST/" 2>/dev/null || echo 000)"
if [ "$GW_CODE" = "000" ]; then
    warn "게이트웨이($GATEWAY_HOST)에 연결하지 못했습니다.
      이 리전(미국)의 IP가 차단됐을 가능성이 큽니다. 이 상태로는 문제 생성이 실패합니다.
      → 배포-GCP.md의 '게이트웨이가 막힐 때' 절을 참고해 서울 리전으로 옮기세요."
else
    echo "연결 OK (HTTP $GW_CODE) — 네트워크 경로는 열려 있습니다."
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
  업로드 상한/스레드: ${DEF_MAX_UPLOAD_MB}MB / ${DEF_SERVER_THREADS} (e2-micro 기준)
────────────────────────────────────────────────────────

다음 할 일:

 1) 구글 로그인을 쓰려면 $ENV_FILE 에
    GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET 를 채우고:
        sudo systemctl restart mcu

 2) Google Cloud Console → 사용자 인증 정보 → 승인된 리디렉션 URI 에 추가:
        https://$DOMAIN/login/google/callback
    (로컬 개발용 http://localhost:5000/login/google/callback 은 지우지 마세요)

 3) 접속이 안 되면 GCP 방화벽부터 확인하세요. 내 PC에서:
        gcloud compute firewall-rules create mcu-allow-web \\
            --allow=tcp:80,tcp:443 --target-tags=http-server,https-server

자주 쓰는 명령:
    sudo systemctl status mcu      # 상태 확인
    sudo journalctl -u mcu -f      # 실시간 로그
    free -h                        # 메모리·swap 확인
    sudo bash $APP_DIR/deploy/gcp/update.sh    # 코드 업데이트

EOF
