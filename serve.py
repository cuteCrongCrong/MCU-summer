"""
배포용 실행 진입점 — waitress WSGI 서버.

app.py 의 개발 서버(app.run)는 한 번에 한 요청만 안정적으로 처리하고 디버거가 붙어 있어
외부 공개용으로 쓰면 안 된다. 이 파일이 그 자리를 대신한다.

waitress를 쓰는 이유:
  - 순수 파이썬이라 Windows에서도 그대로 설치·실행된다 (gunicorn은 Windows 미지원).
    → 팀원 PC와 배포 서버가 같은 방식으로 돌아간다.
  - 멀티 프로세스가 아닌 멀티 스레드라 SQLite와 궁합이 좋다 (파일 잠금 충돌이 적음).

실행:
    python serve.py

환경변수(모두 선택, 기본값은 config.py 참고):
    APP_ENV=production      배포 모드 (디버그 차단·보안 쿠키·시크릿 키 필수)
    PORT=5000               수신 포트 (Render 등은 자동 주입)
    HOST=0.0.0.0            수신 주소
    DB_PATH=/var/data/sessions.db   영구 디스크상의 DB 경로
    SERVER_THREADS=16       동시 처리 스레드 수
자세한 배포 절차는 배포.md 참고.
"""

from waitress import serve

import config
from app import app          # import 시 config 검증 + init_db()가 실행된다
from db import DB_PATH
# 다중 프로바이더 지원으로 이 상수들이 llm.py → providers/jbnu_gateway.py 로 옮겨졌다.
# (app.py도 같은 경로에서 가져온다 — 시작 배너 표시용)
from providers.jbnu_gateway import GATEWAY_BASE_URL, DEFAULT_MODEL


def main():
    print("=" * 60)
    print("  MCU 학습 플랫폼 — waitress 서버")
    print(f"  모드      : {config.APP_ENV}")
    print(f"  수신      : http://{config.HOST}:{config.PORT}")
    print(f"  스레드    : {config.SERVER_THREADS}")
    print(f"  DB 파일   : {DB_PATH}")
    print(f"  게이트웨이: {GATEWAY_BASE_URL}")
    print(f"  기본 모델 : {DEFAULT_MODEL}")
    print(f"  프록시 신뢰: {'예' if config.TRUST_PROXY else '아니오'}")
    if not config.IS_PRODUCTION:
        print("  [참고] APP_ENV=production 이 아닙니다. 외부 배포라면 지정하세요.")
    print("=" * 60)

    opts = {}
    if config.TRUST_PROXY:
        # waitress는 trusted_proxy가 지정돼 있지 않으면 X-Forwarded-* 를 환경에서 제거한다.
        # (스푸핑 방지 기본 동작) 그래서 이걸 켜주지 않으면 앱단의 ProxyFix가 헤더를
        # 아예 못 보고, Google OAuth 콜백이 http:// 로 만들어져 로그인이 실패한다.
        opts.update(
            trusted_proxy=config.TRUSTED_PROXY,
            trusted_proxy_count=1,
            trusted_proxy_headers={
                "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host"
            },
            clear_untrusted_proxy_headers=True,
        )

    # channel_timeout: 문제 생성은 LLM 호출이 연달아 일어나 수 분이 걸린다.
    #                  기본값(120초)이면 응답 전에 연결이 끊기므로 넉넉히 늘린다.
    serve(
        app,
        host=config.HOST,
        port=config.PORT,
        threads=config.SERVER_THREADS,
        channel_timeout=900,
        ident="mcu-question-generator",
        **opts,
    )


if __name__ == "__main__":
    main()
