"""
MCU 학습 플랫폼 — Flask 진입점 (전북대 LLM 플랫폼)

앱 생성 + Blueprint 등록 + 서버 실행만 담당한다. (거의 바뀔 일 없음)
기능별 로직은 아래에 있음:
  - db.py                     : DB 연결·스키마
  - llm.py                    : PDF 추출·프롬프트·파싱·분석 파이프라인
  - providers/                : LLM 제공사별 호출 (전북대 게이트웨이·OpenAI·Anthropic·Gemini)
  - features/question_gen.py  : 문제 생성 라우트 (/generate, /sessions, /models ...)
  - features/wrong_note.py    : 오답 노트 라우트 (/wrong-folders ...)
  - features/topic_analysis.py: 기출 주제 분석 라우트 (/analyze-topics)
  - features/auth.py          : Google 로그인 + 게스트 익명 id
  - 정적 파일(css/js)          : static/ , 화면(HTML): index.html

로컬 실행: python app.py  /  py app.py   (또는 실행.bat)   접속: http://localhost:5000
배포 실행: python serve.py               (waitress WSGI 서버 — 배포.md 참고)
필요 패키지: pip install -r requirements.txt
"""

import os
import threading
import webbrowser
from datetime import timedelta

from flask import Flask, send_from_directory, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

import config
from db import init_db, DB_PATH
from providers.jbnu_gateway import GATEWAY_BASE_URL, DEFAULT_MODEL
from features.question_gen import gen_bp
from features.wrong_note import wrong_bp
from features.topic_analysis import topic_bp
from features.auth import auth_bp, init_auth

# 배포 모드인데 시크릿 키가 기본값이거나 디버그가 켜져 있으면 여기서 즉시 중단한다.
config.check_production_ready()

app = Flask(__name__)                 # 정적 파일: /static → ./static
app.secret_key = config.FLASK_SECRET_KEY   # 로그인 세션 + 게스트 익명 id(서명 쿠키)용
# 게스트의 익명 소유자 id가 쿠키에 담기므로, 브라우저를 닫아도 데이터를 잃지 않도록 길게 잡는다.
app.permanent_session_lifetime = timedelta(days=365)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,               # JS에서 쿠키를 읽지 못하게
    SESSION_COOKIE_SAMESITE="Lax",              # OAuth 리다이렉트는 통과, CSRF는 완화
    SESSION_COOKIE_SECURE=config.IS_PRODUCTION,  # 배포(https)에서는 https로만 전송
    MAX_CONTENT_LENGTH=config.MAX_UPLOAD_MB * 1024 * 1024,
)

# 리버스 프록시 뒤에 있으면 X-Forwarded-* 를 신뢰해야 https·호스트를 올바로 인식한다.
# (안 켜면 Google OAuth 콜백 주소가 http:// 로 만들어져 리다이렉트 URI 불일치로 실패)
if config.TRUST_PROXY:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

init_auth(app)                        # Authlib(Google OAuth) 초기화
app.register_blueprint(gen_bp)
app.register_blueprint(wrong_bp)
app.register_blueprint(topic_bp)
app.register_blueprint(auth_bp)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


@app.route("/guide")
def guide():
    """사용 가이드 — 정적 HTML 한 장.

    index.html 안의 탭이 아니라 별도 페이지인 이유: 가이드를 보지 않는 사람도
    첫 접속에 통째로 내려받게 되고(index.html 이 그만큼 커진다), 탭 전환 로직과도
    얽힌다. 파일로 떼어 두면 누른 사람만 받고, 앱 코드와 닿는 곳이 없다.
    """
    return send_from_directory("static", "guide.html")


@app.route("/healthz")
def healthz():
    """배포 플랫폼의 헬스체크용 (LLM/DB를 건드리지 않는 가벼운 응답)."""
    return jsonify({"status": "ok"})


@app.errorhandler(413)
def too_large(_e):
    """업로드 용량 초과 — 프런트가 JSON을 기대하므로 HTML 대신 JSON으로 응답."""
    return jsonify({
        "error": f"업로드 파일이 너무 큽니다. 최대 {config.MAX_UPLOAD_MB}MB까지 가능합니다."
    }), 413


init_db()  # 앱 로드 시 DB 테이블 보장 (serve.py / flask run / gunicorn 포함)


if __name__ == "__main__":
    # ── 로컬 개발 전용 실행 경로 (배포는 serve.py를 쓴다) ──
    # 리로더를 켜면 이 파일이 부모(파일 변경 감시)와 자식(실제 서버) 두 프로세스에서
    # 실행된다. 자식에만 WERKZEUG_RUN_MAIN이 붙으므로 그걸로 구분해, 안내문·브라우저
    # 열기를 부모에서만 한다 — 안 그러면 저장할 때마다 배너가 찍히고 탭이 새로 열린다.
    is_reload_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"

    if not is_reload_child:
        print("=" * 55)
        print("  MCU 학습 플랫폼 서버 시작")
        print(f"  접속 주소: http://localhost:{config.PORT}")
        print(f"  LLM 게이트웨이: {GATEWAY_BASE_URL}")
        print(f"  기본 모델: {DEFAULT_MODEL}")
        print(f"  DB 파일: {DB_PATH}")
        if config.FLASK_SECRET_KEY_IS_DEFAULT:
            print("  [경고] FLASK_SECRET_KEY가 기본값입니다. 로컬 사용은 괜찮지만,")
            print("         외부에 배포할 때는 반드시 고유한 값을 지정하세요.")
            print("         (지정 안 하면 게스트/로그인 데이터 분리가 무의미해집니다)")
        if config.DEBUG:
            print("  코드를 수정하고 저장하면 서버가 자동으로 다시 읽습니다.")
        print("  브라우저가 자동으로 열립니다. (창을 닫으면 서버 종료)")
        print("=" * 55)
        threading.Timer(
            1.5, lambda: webbrowser.open(f"http://localhost:{config.PORT}")
        ).start()
    try:
        # 개발 중에는 리로더를 켠다 — 끄면 .py를 고쳐도 서버가 옛 코드를 들고 있어서
        # "저장했는데 반영이 안 된다"로 보인다. (HTML·CSS·JS는 요청마다 디스크에서
        # 읽으므로 영향 없다) 배포 모드에서는 config.DEBUG가 False라 함께 꺼진다.
        app.run(host=config.HOST, port=config.PORT,
                debug=config.DEBUG, use_reloader=config.DEBUG)
    except OSError as e:
        print("\n" + "=" * 55)
        print(f"  [오류] 포트 {config.PORT}을(를) 사용할 수 없습니다.")
        print("  이미 서버가 실행 중일 수 있습니다.")
        print(f"  → 열려 있는 브라우저 탭(http://localhost:{config.PORT})을 먼저 확인하세요.")
        print("  → 그래도 안 되면 작업 관리자에서 'python.exe'를 모두 종료 후 다시 실행하세요.")
        print(f"  (상세: {e})")
        print("=" * 55)
