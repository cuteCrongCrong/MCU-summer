"""
비밀값 로더 — 환경변수 우선, 없으면 gitignore된 secret_config.py 에서 폴백.

값이 없어도 앱은 정상 실행되며(구글 로그인만 비활성화), 값이 채워지면 로그인이 켜진다.
→ 팀원이 OAuth 자격증명을 아직 안 만들었어도 앱의 나머지 기능은 그대로 쓸 수 있다.
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
FLASK_SECRET_KEY     = _get("FLASK_SECRET_KEY", "dev-insecure-secret-change-me")

# client id/secret이 모두 있을 때만 구글 로그인 활성화
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
