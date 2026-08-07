"""
기출 주제 분석의 '분량 초과 → 확인 → 진행' 2단계 경로를 라우트째로 흘려본다.

왜 필요한가 — 주제 분석은 라우트 하나가 추출·경고·LLM·보관을 다 하는데, 확인 경로는
파일 대신 토큰으로 들어와 앞부분을 통째로 건너뛴다. 두 갈래가 같은 응답을 내는지는
함수를 따로 부르면 확인되지 않는다 (test_generation_flow.py 와 같은 이유).

API 키 없이 돈다 — 프로바이더와 주제 분석 LLM 호출을 가짜로 바꾼다.
"""

import io
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import fitz

import db

# 이 테스트는 라우트를 통째로 흘려보내므로 분석 결과·이미지 캐시가 DB에 실제로 쓰인다.
# 임시 파일로 돌리지 않으면 사용자의 sessions.db에 테스트 찌꺼기가 쌓이고, 이미지 캐시가
# 남아 다음 실행에서 호출 수가 0이 되어 검사가 조용히 무의미해진다.
# db.get_conn()이 호출 시점에 db.DB_PATH를 읽으므로 app 임포트 전에 바꿔치면 된다.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
db.DB_PATH = _tmp.name
db.init_db()

import app as app_module
import llm
from features import extract_cache, topic_analysis

_failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        _failures.append(name)


class FakeProvider:
    name = "fake"
    label = "가짜 제공사"
    default_model = "fake-model"
    supports_credits = False

    def complete(self, prompt, api_key, model, max_tokens=None, usage=None):
        return "{}"

    def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None,
                       max_tokens=None):
        return "그림 설명"

    def list_models(self, api_key):
        return [self.default_model]


def make_pdf(lines):
    """텍스트 레이어만 있는 PDF. 페이지 밖 글자는 추출되지 않으므로 쪽을 넘겨 그린다."""
    doc = fitz.open()
    page, y = doc.new_page(), 72
    for ln in lines:
        if y > 740:
            page, y = doc.new_page(), 72
        page.insert_text((40, y), ln, fontsize=9)
        y += 16
    data = doc.tobytes()
    doc.close()
    return data


FAKE_RESULT = {
    "topics": [{"주제": "머리뼈", "강의록": [], "강의록발췌": "",
                "기출": [], "출제형태": ""}],
    "dropped": 0,
    "total_questions": 1,
    "raw": "{}",
}


def test_topic_confirm():
    print("[주제 분석 · 분량 초과 확인 경로]")
    extract_cache.clear()

    # LLM 계층을 가짜로 — 키 없이, 돈 없이 돈다
    topic_analysis.get_provider = lambda *a, **k: FakeProvider()
    topic_analysis.run_topic_analysis = lambda *a, **k: dict(FAKE_RESULT)

    client = app_module.app.test_client()

    # 파일을 상한만큼 올리면 파일당 예산이 1/N이 된다. 하나만 크게 만들어 그 파일만 걸리게.
    per_doc = llm.TOPIC_SIDE_CHAR_BUDGET // llm.MAX_FILES_PER_SIDE
    big = make_pdf([f"OVERSIZE {i:04d} " + "가" * 60
                    for i in range(per_doc // 60 + 200)])
    small = make_pdf(["SMALL 짧은 강의록"])
    exam = make_pdf(["1. 머리뼈를 이루는 뼈는?"])

    def payload(extra=None):
        files = [("lectures", (io.BytesIO(big), "큰강의록.pdf"))]
        files += [("lectures", (io.BytesIO(small), f"작은{i}.pdf"))
                  for i in range(llm.MAX_FILES_PER_SIDE - 1)]
        files += [("exams", (io.BytesIO(exam), "기출.pdf"))]
        data = {"api_key": "test-key", "model": "fake-model", "provider": "fake",
                "title": "확인 경로 테스트"}
        for key, val in files:
            data.setdefault(key, []).append(val)
        data.update(extra or {})
        return data

    # ── 1단계: 파일 업로드 → 경고에서 멈춰야 한다 ──
    r1 = client.post("/analyze-topics", data=payload(),
                     content_type="multipart/form-data")
    check("1단계 200 응답", r1.status_code == 200, r1.status_code)
    d1 = r1.get_json()
    check("확인이 필요하다고 답한다", d1.get("needs_confirm") is True, d1.get("error"))
    if not d1.get("needs_confirm"):
        return
    check("분석 결과는 아직 없다", "topics" not in d1)

    warns = d1.get("warnings") or []
    check("경고는 초과한 1개 파일만", len(warns) == 1, [w.get("name") for w in warns])
    w = warns[0]
    check("강의록 쪽임을 알려준다", w.get("side") == "강의록", w.get("side"))
    check("어느 파일인지 알려준다", w.get("name") == "큰강의록.pdf", w.get("name"))
    check("버리기 시작하는 쪽·어절", bool(w.get("from_page")) and bool(w.get("from_words")), w)
    check("버리기 끝나는 쪽·어절", bool(w.get("to_page")) and bool(w.get("to_words")), w)
    check("시작 쪽이 끝 쪽보다 앞", w.get("from_page", 0) <= w.get("to_page", 0), w)
    check("어절에 쪽 표시가 안 섞인다",
          all("페이지" not in x and "[" not in x
              for x in (w["from_words"], w["to_words"])), w)
    token = d1.get("extract_token")
    check("토큰이 온다", bool(token))

    # ── 2단계: 토큰만 보내 진행 ──
    r2 = client.post("/analyze-topics",
                     data={"api_key": "test-key", "title": "확인 경로 테스트",
                           "extract_token": token},
                     content_type="multipart/form-data")
    check("2단계 200 응답", r2.status_code == 200, r2.status_code)
    d2 = r2.get_json()
    check("이번엔 분석 결과가 온다", d2.get("success") is True, d2.get("error"))
    check("주제가 담겨 있다", len(d2.get("topics") or []) == 1, d2.get("topics"))
    check("파일 목록이 그대로 실린다",
          len(d2.get("lecture_docs") or []) == llm.MAX_FILES_PER_SIDE,
          len(d2.get("lecture_docs") or []))

    # ── 토큰은 한 번만 ──
    r3 = client.post("/analyze-topics",
                     data={"api_key": "test-key", "extract_token": token},
                     content_type="multipart/form-data")
    check("같은 토큰은 다시 못 쓴다", r3.status_code == 400, r3.status_code)


def test_image_model_pinned():
    """
    주제 분석에서 그림 읽는 모델은 **사용자가 고르지 않는다.**

      - 제공사가 image_model을 선언했으면(게이트웨이) 그 모델로 고정
      - 선언이 없으면 화면에서 고른 메인 모델을 그대로 (모델 나누기 전과 같은 동작)

    화면에 칸이 없는 것과 **별개로** 서버가 폼을 무시해야 한다 — 열어둔 옛날 탭이나
    직접 만든 요청은 없어진 칸을 그냥 통과해서, 비싼 모델이 이미지 수십 장에 붙는다.
    """
    print("[그림 읽기 모델 — 사용자가 고르지 않는다]")
    extract_cache.clear()

    class RecordingProvider(FakeProvider):
        """describe_image가 어떤 모델로 불렸는지 받아 적는다."""

        def __init__(self, image_model=""):
            self.image_model = image_model
            self.image_models_used = []

        def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None,
                           max_tokens=None):
            self.image_models_used.append(model)
            return "그림 설명"

    topic_analysis.run_topic_analysis = lambda *a, **k: dict(FAKE_RESULT)
    client = app_module.app.test_client()

    # 메인 모델과 폼의 analysis_model을 **다른 값**으로 둔다. 같게 두면 어느 쪽이
    # 쓰였는지 구별되지 않아 검사가 통과해도 아무것도 증명하지 못한다.
    # 글자가 몇 자 없는 쪽 = 스캔 페이지로 잡혀 이미지 호출 대상이 된다.
    def post(prov, tag):
        topic_analysis.get_provider = lambda *a, **k: prov
        return client.post("/analyze-topics", data={
            "api_key": "test-key", "model": "main-model", "provider": "fake",
            "analysis_model": "user-picked",    # ← 옛날 탭이 남겨 보낸 값 (무시돼야 한다)
            "title": f"고정 테스트 {tag}",
            "lectures": (io.BytesIO(make_pdf([f"필기 {tag}"])), "강의록.pdf"),
            "exams":    (io.BytesIO(make_pdf([f"1. 뼈 {tag}"])), "기출.pdf"),
        }, content_type="multipart/form-data")

    pinned = RecordingProvider(image_model="pinned-image-model")
    r1 = post(pinned, "A")
    check("고정 제공사 200 응답", r1.status_code == 200, r1.get_json())
    check("이미지 호출이 실제로 났다", bool(pinned.image_models_used),
          "0회 — 대상 페이지가 안 잡혔다면 이 검사는 의미가 없다")
    check("선언한 제공사는 고정 모델을 쓴다",
          set(pinned.image_models_used) == {"pinned-image-model"},
          str(set(pinned.image_models_used)))

    # 선언이 없는 제공사(OpenAI·Anthropic·Gemini)는 모델 id가 제공사 안에서만 유효해
    # 고정할 값이 없다 → 모델을 나누기 전처럼 메인 모델 하나로 돈다.
    free = RecordingProvider()
    r2 = post(free, "B")
    check("미선언 제공사 200 응답", r2.status_code == 200, r2.get_json())
    check("미선언 제공사는 메인 모델을 그대로 쓴다",
          set(free.image_models_used) == {"main-model"},
          str(set(free.image_models_used)))
    check("폼의 analysis_model은 어느 쪽에서도 안 쓰인다",
          "user-picked" not in (set(pinned.image_models_used) | set(free.image_models_used)))


if __name__ == "__main__":
    try:
        test_topic_confirm()
        test_image_model_pinned()
    finally:
        try:
            pathlib.Path(_tmp.name).unlink()
        except OSError:
            pass
    print()
    if _failures:
        print(f"실패 {len(_failures)}건: " + ", ".join(_failures))
        sys.exit(1)
    print("전부 통과")
