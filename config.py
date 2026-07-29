"""
설정 로더 — 비밀값 + 배포 설정.

비밀값(구글 OAuth·세션키): 환경변수 우선, 없으면 gitignore된 secret_config.py 에서 폴백.
  값이 없어도 앱은 정상 실행되며(구글 로그인만 비활성화), 값이 채워지면 로그인이 켜진다.
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

# client id/secret이 모두 있을 때만 구글 로그인 활성화
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
# 안 켜면 url_for(_external=True)가 http:// 를 만들어 구글 OAuth 콜백이 불일치로 실패한다.
TRUST_PROXY = _flag("TRUST_PROXY", default=IS_PRODUCTION)

# 프록시로 신뢰할 접속 IP. waitress는 이 값이 없으면 X-Forwarded-* 를 아예 제거한다.
# Render처럼 플랫폼 프록시를 통해서만 접근 가능한 환경은 "*" 로 둔다.
# 직접 만든 VM에서 nginx를 앞에 두는 경우엔 nginx의 IP(보통 127.0.0.1)로 좁히는 게 안전하다.
TRUSTED_PROXY = os.environ.get("TRUSTED_PROXY") or "*"

# 동시 처리 스레드 수. 문제 생성 요청 하나가 수 분 걸리므로 넉넉히 잡는다.
SERVER_THREADS = _int("SERVER_THREADS", 16)

# 업로드 용량 상한(MB). 공개 서버에서 거대 파일 업로드를 막는다.
MAX_UPLOAD_MB = _int("MAX_UPLOAD_MB", 100)

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
