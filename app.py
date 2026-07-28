"""
의대 예상문제 생성기 — Flask 진입점 (전북대 LLM 플랫폼)

앱 생성 + Blueprint 등록 + 서버 실행만 담당한다. (거의 바뀔 일 없음)
기능별 로직은 아래에 있음:
  - db.py                     : DB 연결·스키마
  - llm.py                    : LLM 호출·PDF 추출·프롬프트·파싱·분석
  - features/question_gen.py  : 문제 생성 라우트 (/generate, /sessions, /models ...)
  - features/wrong_note.py    : 오답 노트 라우트 (/wrong-folders ...)
  - 정적 파일(css/js)          : static/ , 화면(HTML): index.html

실행: python app.py  /  py app.py     접속: http://localhost:5000
필요 패키지: pip install flask pymupdf openai
"""

import threading
import webbrowser

from flask import Flask, send_from_directory

import config
from db import init_db
from llm import GATEWAY_BASE_URL, DEFAULT_MODEL
from features.question_gen import gen_bp
from features.wrong_note import wrong_bp
from features.bone_ocr import bone_bp
from features.auth import auth_bp, init_auth

app = Flask(__name__)                 # 정적 파일: /static → ./static
app.secret_key = config.FLASK_SECRET_KEY   # 로그인 세션(서명 쿠키)용

init_auth(app)                        # Authlib(구글 OAuth) 초기화
app.register_blueprint(gen_bp)
app.register_blueprint(wrong_bp)
app.register_blueprint(bone_bp)
app.register_blueprint(auth_bp)


@app.route("/")
def index():
    return send_from_directory(".", "index.html")


init_db()  # 앱 로드 시 DB 테이블 보장 (flask run / gunicorn 포함)


if __name__ == "__main__":
    print("=" * 55)
    print("  의대 예상문제 생성기 서버 시작")
    print("  접속 주소: http://localhost:5000")
    print(f"  LLM 게이트웨이: {GATEWAY_BASE_URL}")
    print(f"  기본 모델: {DEFAULT_MODEL}")
    print("  브라우저가 자동으로 열립니다. (창을 닫으면 서버 종료)")
    print("=" * 55)
    # 브라우저 자동 열기 (리로더를 끄므로 단일 프로세스 → 중복/좀비 없음)
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:5000")).start()
    try:
        # use_reloader=False: 프로세스 2중 실행·좀비 프로세스로 포트가 붙잡히는 문제 방지
        app.run(host="127.0.0.1", port=5000, debug=True, use_reloader=False)
    except OSError as e:
        print("\n" + "=" * 55)
        print("  [오류] 포트 5000을 사용할 수 없습니다.")
        print("  이미 서버가 실행 중일 수 있습니다.")
        print("  → 열려 있는 브라우저 탭(http://localhost:5000)을 먼저 확인하세요.")
        print("  → 그래도 안 되면 작업 관리자에서 'python.exe'를 모두 종료 후 다시 실행하세요.")
        print(f"  (상세: {e})")
        print("=" * 55)
