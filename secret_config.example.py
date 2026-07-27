"""
[템플릿] 이 파일을 secret_config.py 로 복사한 뒤 값을 채우세요.
  - secret_config.py 는 .gitignore로 제외되어 커밋되지 않습니다. (비밀값 보호)
  - 또는 같은 이름의 환경변수로 넣어도 됩니다. (config.py가 환경변수 우선)

발급 방법:
  Google Cloud Console → API 및 서비스 → 사용자 인증 정보
    → 사용자 인증 정보 만들기 → OAuth 클라이언트 ID → 애플리케이션 유형: 웹 애플리케이션
  승인된 리디렉션 URI 에 정확히 추가:
    http://localhost:5000/login/google/callback

⚠️ 실제 값(client secret 등)은 절대 이 example 파일에 넣지 마세요 — 이 파일은 커밋됩니다.
"""

GOOGLE_CLIENT_ID     = ""   # 예: 1234567890-abcd.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET = ""   # 예: GOCSPX-xxxxxxxxxxxxxxxx
FLASK_SECRET_KEY     = ""   # 아무 긴 랜덤 문자열 (예: python -c "import secrets;print(secrets.token_hex(32))")
