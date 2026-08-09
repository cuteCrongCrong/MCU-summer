"""
설정 로더 — 비밀값 + 배포 설정.

비밀값(Google OAuth·세션키): 환경변수 우선, 없으면 gitignore된 secret_config.py 에서 폴백.
  값이 없어도 앱은 정상 실행되며(Google 로그인만 비활성화), 값이 채워지면 로그인이 켜진다.
  → 팀원이 OAuth 자격증명을 아직 안 만들었어도 앱의 나머지 기능은 그대로 쓸 수 있다.

배포 설정(호스트·포트·DB 경로 등): 비밀값이 아니므로 환경변수만 사용.
  아무것도 지정하지 않으면 지금까지와 똑같이 로컬 개발 모드로 동작한다.
  배포 시에는 APP_ENV=production 을 지정한다. (배포.md 참고)
"""

import os

try:
    import secret_config as _sc   # gitignore된 로컬 파일 (없어도 됨)
except ImportError:
    _sc = None


def _get(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    if v:
        return v
    if _sc is not None:
        v = getattr(_sc, name, None)
        if v:
            return v
    return default


GOOGLE_CLIENT_ID     = _get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _get("GOOGLE_CLIENT_SECRET")
# 세션 서명 키: 없으면 개발용 임시 키. 실제 배포/공유 시 반드시 지정할 것.
#   이 키로 로그인 세션과 게스트 익명 id가 모두 서명된다. 기본값 그대로 배포하면
#   누구나 쿠키를 위조해 남의 데이터를 볼 수 있다.
_DEFAULT_SECRET_KEY  = "dev-insecure-secret-change-me"
FLASK_SECRET_KEY     = _get("FLASK_SECRET_KEY", _DEFAULT_SECRET_KEY)
FLASK_SECRET_KEY_IS_DEFAULT = (FLASK_SECRET_KEY == _DEFAULT_SECRET_KEY)

# client id/secret이 모두 있을 때만 Google 로그인 활성화
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# ──────────────────────────────────────────────
# 배포 설정 (비밀값 아님 → 환경변수만 읽는다)
# 기본값 = 지금까지의 로컬 개발 동작. 배포 시에만 환경변수를 지정하면 된다.
# ──────────────────────────────────────────────

def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


APP_ENV       = (os.environ.get("APP_ENV") or "development").strip().lower()
IS_PRODUCTION = APP_ENV == "production"

# 배포 모드에서는 외부 접속을 받아야 하므로 0.0.0.0, 로컬은 기존대로 127.0.0.1
HOST  = os.environ.get("HOST") or ("0.0.0.0" if IS_PRODUCTION else "127.0.0.1")
PORT  = _int("PORT", 5000)              # Render 등은 PORT를 자동으로 넣어준다
DEBUG = _flag("FLASK_DEBUG", default=not IS_PRODUCTION)

# 리버스 프록시(Render/nginx) 뒤에 있으면 켠다.
# 안 켜면 url_for(_external=True)가 http:// 를 만들어 Google OAuth 콜백이 불일치로 실패한다.
TRUST_PROXY = _flag("TRUST_PROXY", default=IS_PRODUCTION)

# 프록시로 신뢰할 접속 IP. waitress는 이 값이 없으면 X-Forwarded-* 를 아예 제거한다.
# Render처럼 플랫폼 프록시를 통해서만 접근 가능한 환경은 "*" 로 둔다.
# 직접 만든 VM에서 nginx를 앞에 두는 경우엔 nginx의 IP(보통 127.0.0.1)로 좁히는 게 안전하다.
TRUSTED_PROXY = os.environ.get("TRUSTED_PROXY") or "*"

# 동시 처리 스레드 수. 문제 생성 요청 하나가 수 분 걸리므로 넉넉히 잡는다.
SERVER_THREADS = _int("SERVER_THREADS", 16)

# 업로드 용량 상한(MB) — 요청 하나 전체 기준(강의록+기출 합).
#   이 값은 이제 RAM이 아니라 디스크에 걸린다. 예전에는 업로드를 bytes로 읽고 렌더한
#   PNG도 힙에 들고 있어서 상한이 곧 메모리였지만, 지금은 둘 다 디스크로 내려간다
#   (llm.spill_upload / llm._spill_png). 받는 단계도 werkzeug가 500KB 넘는 파일을
#   디스크로 스풀하므로, 요청 하나가 쥐는 메모리가 업로드 크기와 무관해졌다.
#   남는 제약은 스필 디렉터리(DB 옆)가 쓰는 디스크와 추출에 걸리는 시간뿐이다.
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 200)

# Vision으로 읽을 이미지 페이지 상한 — **한쪽(강의록 전체 / 기출 전체) 기준**, 파일당이 아니다.
#   ⚠️ 예전에는 이 값이 곧 메모리였다(렌더한 PNG를 설명이 끝날 때까지 다 들고 있었다).
#      지금은 아니다 — PNG는 디스크에 있고 보내기 직전에만 올라오므로 상주 메모리는
#      IMAGE_WORKERS × PNG 한 장이다. 상한과 무관하다. (llm.py의 'spill' 절 참고)
#      실측: e2-micro(RAM 1GB)가 이 기본값 그대로 100/60/8로 돌아도 OOM이 안 난다.
#   그래서 남는 제약은 셋이다:
#     시간   — 렌더는 read_pdf_pages 루프 안에서 **한 장씩 직렬**이다. 병렬로 도는 것은
#              그 뒤의 LLM 호출뿐이라, 공유 vCPU에서는 상한이 곧 대기 시간이 된다.
#     전송량 — PNG를 base64로 부풀려 요청 본문에 싣는다(providers/openai_compatible.py).
#              상한을 다 채운 회차가 스캔 기출이면 500MB를 넘길 수 있다. GCP 무료 티어의
#              월 1GB egress가 여기서 닳는다 — 배포-GCP.md의 '무료 조건' 절 참고.
#     요금   — 한 쪽이 곧 Vision 호출 한 번. (같은 PDF 재분석은 image_desc_cache로 0회)
#   (150DPI A4 PNG ≈ 벡터 슬라이드 0.3~0.6MB · 스캔 페이지 2~3MB)
#
# 값을 정한 근거 (100/60 → 300/200, 2026-08) — 천장은 **글자 예산**이다.
#   전사는 쪽당 약 800자다(providers/jbnu_gateway.py ①의 실측 776~806).
#   주제 분석 한쪽 몫이 45만 자(llm.TOPIC_SIDE_CHAR_BUDGET)이므로
#     강의록 300쪽 → 24만 자 = 몫의 53%. 나머지 21만 자가 본문에 남는다.
#     560쪽쯤에서 전사만으로 몫을 다 먹는다 — 그 위로는 올려봐야 truncate가 도로 버린다.
#   ⚠️ 검증 안 된 벽이 하나 남아 있다: 요청 전체가 **900초**를 넘으면 waitress와 Caddy가
#      연결을 끊는다(serve.py channel_timeout · deploy/gcp/Caddyfile). 넘기면 이미지값은
#      다 낸 채로 결과를 못 받는다. 렌더가 직렬이라 상한을 올릴수록 여기에 가까워지므로,
#      공유 vCPU 서버(e2-micro 등)에서는 첫 회차의 총 소요 시간을 꼭 확인할 것.
IMAGE_CAP_LECTURE = _int("IMAGE_CAP_LECTURE", 300)   # 강의록: 그림 속 글자 전사
IMAGE_CAP_EXAM    = _int("IMAGE_CAP_EXAM", 200)      # 기출: 그림 설명

# 이미지 호출 동시 실행 수. 네트워크 대기가 대부분이라 CPU보다 상한·지연에 걸린다.
#   상한을 올리면 이 값도 같이 올려야 체감이 유지된다 (상한 ÷ 워커 = 대기 라운드 수).
#   상한을 100/60 → 300/200 으로 올리면서 8 → 12 로 맞췄다. 900초 벽이 있어서
#   대기 라운드가 그만큼 늘면 안 된다 (500쪽 ÷ 8 = 63라운드 → ÷12 = 42라운드).
#   메모리는 이 값에만 걸린다 — 동시에 뜨는 pixmap(장당 ~6.5MB) + base64 사본.
#   12개면 100~150MB 남짓이라 RAM 1GB에서도 견딘다. 더 올리려면 SERVER_THREADS와
#   곱해서 봐야 한다 (동시 요청 수 × 워커 수가 실제 동시 장수다).
IMAGE_WORKERS = _int("IMAGE_WORKERS", 12)

# DB 파일 경로. 배포 시 반드시 영구 디스크 경로를 지정할 것 (예: /var/data/sessions.db).
# 비워두면 프로젝트 폴더의 sessions.db 를 쓴다(로컬 개발 기본값).
DB_PATH = os.environ.get("DB_PATH") or ""


def check_production_ready():
    """배포 모드가 위험한 기본값으로 뜨는 것을 막는다. app.py가 시작 시 호출."""
    if not IS_PRODUCTION:
        return
    if FLASK_SECRET_KEY_IS_DEFAULT:
        raise RuntimeError(
            "APP_ENV=production 인데 FLASK_SECRET_KEY가 기본값입니다.\n"
            "  → 고유한 값을 환경변수로 지정하세요.\n"
            '  → 생성: python -c "import secrets;print(secrets.token_urlsafe(48))"\n'
            "  (기본값 그대로 배포하면 누구나 쿠키를 위조해 남의 데이터를 볼 수 있습니다.)"
        )
    if DEBUG:
        raise RuntimeError(
            "APP_ENV=production 에서는 디버그 모드를 켤 수 없습니다.\n"
            "  → 디버거가 노출되면 원격 코드 실행이 가능합니다. FLASK_DEBUG를 끄세요."
        )
