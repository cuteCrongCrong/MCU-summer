"""
기출 주제 분석의 '분량 초과 → 확인 → 진행' 2단계 경로를 라우트째로 흘려본다.

왜 필요한가 — 주제 분석은 라우트 하나가 추출·경고·LLM·보관을 다 하는데, 확인 경로는
파일 대신 토큰으로 들어와 앞부분을 통째로 건너뛴다. 두 갈래가 같은 응답을 내는지는
함수를 따로 부르면 확인되지 않는다 (test_generation_flow.py 와 같은 이유).

API 키 없이 돈다 — 프로바이더와 주제 분석 LLM 호출을 가짜로 바꾼다.
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import fitz

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

    def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None):
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
    check("버릴 줄 범위를 알려준다",
          w.get("drop_to", 0) > w.get("drop_from", 0) >= 1, w)
    check("전체 줄 수도 알려준다", w.get("total_lines", 0) > w.get("drop_to", 0), w)
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


if __name__ == "__main__":
    test_topic_confirm()
    print()
    if _failures:
        print(f"실패 {len(_failures)}건: " + ", ".join(_failures))
        sys.exit(1)
    print("전부 통과")
