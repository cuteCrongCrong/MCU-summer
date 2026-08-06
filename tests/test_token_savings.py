"""
토큰 절약 장치들이 실제로 동작하는지 확인한다.

여기 있는 것들은 전부 "조용히 되돌아가도 아무도 모르는" 종류다 —
동작이 깨지는 게 아니라 **요금만 다시 오르기** 때문에, 화면으로는 티가 안 난다.
그래서 테스트로 고정해둔다.

  ① 이미지 처리 대상 선별 — 로고 한 점 때문에 본문 페이지를 Vision에 보내지 않는다
  ② 이미지 결과 캐시     — 같은 PDF를 다시 올리면 Vision 호출이 0이 된다
  ②-c 모드 분리         — 같은 페이지라도 '그림 설명'과 '글자 전사'는 캐시를 공유하면 안 된다
  ③ 이미지 예산          — 기출 여러 개를 올려도 '파일당'이 아니라 '전체' 상한이 걸린다
  ④ 기출 분석 1회 병합   — 기출 전문을 두 번 보내지 않는다
  ⑤ 생성 프롬프트 분리   — 배치가 달라도 캐시 접두부는 글자 하나까지 같다

LLM 호출 없이 가짜 프로바이더로 돈다. API 키도 요금도 필요 없다:

    python tests/test_token_savings.py
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 한글 윈도우 기본 코드페이지(cp949)로는 이 파일이 찍는 문자를 쓸 수 없다
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import fitz

import db

# 캐시가 실제 sessions.db 를 건드리지 않도록 임시 파일로 돌린다.
# db.get_conn() 이 호출 시점에 db.DB_PATH 를 읽으므로 이렇게 바꿔치면 된다.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
db.DB_PATH = _tmp.name
db.init_db()

import llm


_failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        _failures.append(name)


# ──────────────────────────────────────────────
# 준비물
# ──────────────────────────────────────────────

def _png(size: int, gray: int = 128) -> bytes:
    """단색 PNG 한 장 (페이지에 심을 그림용)."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, size, size))
    pix.clear_with(gray)
    return pix.tobytes("png")


def build_pdf() -> bytes:
    """
    세 가지 페이지를 가진 PDF:
      1쪽 — 본문 가득 + 아주 작은 로고   → 설명 대상이 **아니어야** 한다
      2쪽 — 본문 가득 + 큰 그림          → 설명 대상
      3쪽 — 텍스트 거의 없음(스캔본 흉내) → 설명 대상
    """
    doc = fitz.open()

    body = "\n".join(f"{i}. 대퇴골의 몸쪽 끝에 있는 구조를 고르시오." for i in range(20))

    p1 = doc.new_page()
    p1.insert_text((50, 60), body, fontsize=9)
    p1.insert_image(fitz.Rect(10, 10, 28, 28), stream=_png(8))        # 로고 크기

    p2 = doc.new_page()
    p2.insert_text((50, 60), body, fontsize=9)
    p2.insert_image(fitz.Rect(50, 300, 500, 750), stream=_png(64))    # 페이지 절반 이상

    p3 = doc.new_page()
    p3.insert_text((50, 60), "3", fontsize=9)                          # 20자 미만

    out = doc.tobytes()
    doc.close()
    return out


class CountingProvider:
    """describe_image 가 실제로 몇 번 불렸는지 센다."""
    name = "fake"
    label = "가짜"
    default_model = "fake-model"
    supports_credits = False

    def __init__(self, complete_text="{}"):
        self.image_calls = 0
        self.complete_calls = 0
        self.last_prompt = None
        self.last_cache_prefix = None
        self._complete_text = complete_text

    def complete(self, prompt, api_key, model, max_tokens=None, usage=None,
                 cache_prefix=None):
        self.complete_calls += 1
        self.last_prompt = prompt
        self.last_cache_prefix = cache_prefix
        return self._complete_text

    def complete_stream(self, prompt, api_key, model, max_tokens=None, usage=None,
                        cache_prefix=None):
        yield self.complete(prompt, api_key, model, max_tokens, usage, cache_prefix)

    def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None):
        self.image_calls += 1
        self.last_image_prompt = prompt
        return f"그림 결과 {self.image_calls}"

    def list_models(self, api_key):
        return [self.default_model]


class FakeUpload:
    """werkzeug FileStorage 흉내 — extract_labeled_docs 가 쓰는 것만."""

    def __init__(self, name, data):
        self.filename = name
        self._data = data

    def read(self):
        return self._data


def describe_all(img_jobs, provider, model="fake-model", mode=None):
    """진행률 제너레이터를 끝까지 돌린다."""
    for _ in llm.describe_images_progressively(img_jobs, "key", model, provider,
                                               None, mode or llm.IMAGE_DESCRIBE):
        pass


# ──────────────────────────────────────────────
# ① 이미지 설명 대상 선별
# ──────────────────────────────────────────────

def test_image_selection():
    print("[① 이미지 처리 대상 선별]")
    pdf = build_pdf()

    pages, img_jobs = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    picked = sorted(e["idx"] for e in img_jobs)

    check("로고만 있는 본문 페이지는 제외", 0 not in picked, f"선택={picked}")
    check("큰 그림이 있는 페이지는 포함", 1 in picked, f"선택={picked}")
    check("텍스트 없는 페이지는 포함", 2 in picked, f"선택={picked}")

    # 모드를 안 주면 하나도 안 고른다 (텍스트 레이어만)
    _, none_jobs = llm.read_pdf_pages(pdf, "key", None)
    check("image_mode=None 이면 0개", len(none_jobs) == 0, str(len(none_jobs)))

    # 상한을 1로 주면 1개만
    _, one_job = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE, max_images=1)
    check("max_images 상한이 걸린다", len(one_job) == 1, str(len(one_job)))

    # 전사 모드는 손글씨를 놓치면 안 되므로 더 헐겁게 잡는다 (기준값이 반대 방향)
    check("전사 모드 면적 기준이 설명보다 헐겁다",
          llm.TRANSCRIBE_MIN_AREA_RATIO < llm.DESCRIBE_MIN_AREA_RATIO)
    check("전사 모드 해상도가 설명보다 높다 (손글씨 판독)",
          llm.TRANSCRIBE_RENDER_DPI > llm.DESCRIBE_RENDER_DPI)


# ──────────────────────────────────────────────
# ② 이미지 설명 캐시
# ──────────────────────────────────────────────

def test_image_cache():
    print("[② 이미지 결과 캐시]")
    pdf = build_pdf()

    prov = CountingProvider()
    _, jobs1 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs1, prov)
    first_calls = prov.image_calls
    check("첫 실행은 실제로 호출한다", first_calls == len(jobs1),
          f"{first_calls} != {len(jobs1)}")
    check("결과가 채워진다", all(e.get("desc") for e in jobs1))

    # 같은 PDF를 다시 올린 상황
    prov2 = CountingProvider()
    _, jobs2 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs2, prov2)
    check("두 번째 실행은 호출 0회", prov2.image_calls == 0, str(prov2.image_calls))
    check("캐시에서 같은 결과가 나온다",
          [e["desc"] for e in jobs2] == [e["desc"] for e in jobs1])

    # 모델이 다르면 캐시를 공유하지 않는다 (싼 모델 설명을 비싼 회차에 물려주지 않게)
    prov3 = CountingProvider()
    _, jobs3 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs3, prov3, model="other-model")
    check("모델이 다르면 다시 호출", prov3.image_calls == len(jobs3),
          f"{prov3.image_calls} != {len(jobs3)}")


def test_cache_modes_do_not_collide():
    """
    같은 페이지를 기출(그림 설명)과 강의록(글자 전사)으로 각각 올리는 상황.
    키에 프롬프트가 안 들어가면 먼저 넣은 쪽 결과가 반대쪽에 그대로 돌아가고,
    강의록 원문 자리에 모델이 지어낸 설명 문장이 조용히 들어간다.
    """
    print("[②-c 설명/전사 캐시가 섞이지 않는다]")

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 60), "7", fontsize=9)     # 텍스트 거의 없음 → 무조건 대상
    pdf = doc.tobytes()
    doc.close()

    # 먼저 '그림 설명'으로 캐시를 채운다
    a = CountingProvider()
    _, jobs_a = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs_a, a, mode=llm.IMAGE_DESCRIBE)
    check("설명 모드가 호출됨", a.image_calls == len(jobs_a))
    check("설명 프롬프트가 전달됨", a.last_image_prompt == llm.IMAGE_DESC_PROMPT)

    # 같은 페이지를 '글자 전사'로 요청 — 캐시가 가로채면 안 된다
    b = CountingProvider()
    _, jobs_b = llm.read_pdf_pages(pdf, "key", llm.IMAGE_TRANSCRIBE)
    describe_all(jobs_b, b, mode=llm.IMAGE_TRANSCRIBE)
    check("전사 모드는 캐시를 쓰지 않고 다시 호출", b.image_calls == len(jobs_b),
          f"{b.image_calls} — 설명 결과가 전사 자리에 재사용됐다")
    check("전사 프롬프트가 전달됨", b.last_image_prompt == llm.IMAGE_TEXT_PROMPT)

    # 각 모드는 자기 캐시를 제대로 재사용한다
    c = CountingProvider()
    _, jobs_c = llm.read_pdf_pages(pdf, "key", llm.IMAGE_TRANSCRIBE)
    describe_all(jobs_c, c, mode=llm.IMAGE_TRANSCRIBE)
    check("전사 재실행은 호출 0회", c.image_calls == 0, str(c.image_calls))

    key_d = llm.image_cache_key(llm.IMAGE_DESC_PROMPT, b"png")
    key_t = llm.image_cache_key(llm.IMAGE_TEXT_PROMPT, b"png")
    check("같은 이미지라도 모드가 다르면 키가 다르다", key_d != key_t)


def test_failure_not_cached():
    print("[②-b 실패는 캐시하지 않는다]")

    class FailingProvider(CountingProvider):
        def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None):
            self.image_calls += 1
            raise RuntimeError("이미지 미지원")

    # 캐시가 비어 있도록 이 테스트 전용 PDF를 따로 만든다
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 60), "9", fontsize=9)
    pdf = doc.tobytes()
    doc.close()

    fail = FailingProvider()
    _, jobs = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs, fail)
    check("실패 시 폴백 문구", "실패" in (jobs[0].get("desc") or ""))

    retry = CountingProvider()
    _, jobs2 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs2, retry)
    check("다음 회차에 다시 시도한다", retry.image_calls == len(jobs2),
          f"{retry.image_calls} != {len(jobs2)} (폴백 문구가 캐시에 굳었다)")


# ──────────────────────────────────────────────
# ③ 이미지 예산은 '한쪽 전체' 기준
# ──────────────────────────────────────────────

def test_image_coverage():
    """
    상한에 걸려 못 읽은 쪽을 끝까지 세는지.
    예전에는 상한 검사가 후보 판정 안에 있어서, 상한을 채우는 순간 뒤 페이지를
    후보인지조차 안 따졌다 → 진행률이 15/15로 떠서 다 읽은 것처럼 보였다.
    """
    print("[⑥ 상한 초과분을 끝까지 센다]")

    # 큰 그림이 있는 페이지 6장 (전부 후보)
    doc = fitz.open()
    body = "\n".join(f"{i}. 대퇴골의 몸쪽 끝 구조는?" for i in range(20))
    for _ in range(6):
        p = doc.new_page()
        p.insert_text((50, 60), body, fontsize=9)
        p.insert_image(fitz.Rect(50, 300, 500, 750), stream=_png(64))
    pdf = doc.tobytes()
    doc.close()

    pages, jobs = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE, max_images=2)
    cov = llm.image_coverage(pages, jobs)

    check("실제 처리는 상한까지만", cov["processed"] == 2, str(cov["processed"]))
    check("후보는 상한 넘어서도 전부 센다", cov["candidates"] == 6, str(cov["candidates"]))
    check("건너뛴 수", cov["skipped"] == 4, str(cov["skipped"]))
    check("건너뛴 쪽 번호(1부터)", cov["skipped_pages"] == [3, 4, 5, 6],
          str(cov["skipped_pages"]))
    check("렌더는 상한까지만 (건너뛴 쪽엔 png 없음)",
          sum(1 for e in pages if e.get("png")) == 2)

    # 상한에 안 걸리면 경고가 안 뜬다
    pages2, jobs2 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE, max_images=99)
    cov2 = llm.image_coverage(pages2, jobs2)
    check("여유가 있으면 건너뛴 쪽 없음", cov2["skipped"] == 0 and cov2["processed"] == 6,
          str(cov2))

    # 이미지 처리를 안 하면 후보 자체가 0 (헛돌지 않는다)
    pages3, jobs3 = llm.read_pdf_pages(pdf, "key", None)
    cov3 = llm.image_coverage(pages3, jobs3)
    check("모드가 없으면 후보 0", cov3["candidates"] == 0, str(cov3))

    # 추출 텍스트에도 '몇 쪽 중 몇 쪽'이 남는다 (LLM이 자료가 온전치 않음을 알게)
    raw = llm.assemble_pdf_text(pages, len(jobs), llm.IMAGE_DESCRIBE, 2)
    check("본문에 누락 사실이 남는다", "6쪽 중 앞 2쪽만" in raw,
          raw[-90:].replace("\n", " "))


def test_image_budget():
    print("[③ 이미지 예산 — 파일당이 아니라 전체]")
    pdf = build_pdf()      # 파일 하나당 설명 대상 2쪽
    files = [FakeUpload(f"기출{i}.pdf", pdf) for i in range(1, 6)]

    prov = CountingProvider()
    llm.extract_labeled_docs(files, "기출", "key", "budget-model",
                             image_mode=llm.IMAGE_DESCRIBE, provider=prov,
                             img_budget=4)
    check("전체 예산을 넘지 않는다", prov.image_calls <= 4, str(prov.image_calls))

    # 예전 동작(파일당 상한)이었다면 5 × 2 = 10회가 나갔을 것
    check("파일당 상한이 아니다", prov.image_calls < 10, str(prov.image_calls))

    # 예산을 생략하면 모드의 max_pages 를 '이쪽 전체'에 쓴다 (파일당이 아니라)
    prov_d = CountingProvider()
    llm.extract_labeled_docs(files, "기출", "key", "budget-default",
                             image_mode=llm.IMAGE_DESCRIBE, provider=prov_d)
    check("생략 시 모드 상한이 전체에 걸린다",
          prov_d.image_calls <= llm.IMAGE_DESCRIBE["max_pages"],
          f"{prov_d.image_calls} > {llm.IMAGE_DESCRIBE['max_pages']}")

    prov0 = CountingProvider()
    llm.extract_labeled_docs(files, "강의록", "key", "budget-model",
                             image_mode=None, provider=prov0)
    check("모드가 없으면 이미지 처리를 하지 않는다", prov0.image_calls == 0,
          str(prov0.image_calls))


# ──────────────────────────────────────────────
# ④ 기출 분석 1회 병합
# ──────────────────────────────────────────────

EXAM_JSON = """{
  "기출출제개념": ["대퇴골", "슬관절"],
  "빈출포인트": ["몸쪽 끝 구조"],
  "유형통계": {"객관식": 3, "빈칸채우기": 1, "단답형": 0, "서술형": 0},
  "대표문제": [
    {"유형": "객관식", "원문": "1. 대퇴골의 몸쪽 끝 구조는?\\n① 대퇴골두\\n정답: ①"},
    {"유형": "빈칸 채우기", "원문": "2. 대퇴골 몸쪽 끝은 ____ 이다.\\n정답: 대퇴골두"}
  ]
}"""


def test_exam_merge():
    print("[④ 기출 분석 — 전문을 한 번만 보낸다]")
    prov = CountingProvider(complete_text=EXAM_JSON)
    exam_text = "기출 전문 " * 100

    result = llm.analyze_exam(exam_text, "key", "fake-model", prov)

    check("LLM 호출 1회", prov.complete_calls == 1, str(prov.complete_calls))
    check("기출 전문이 프롬프트에 1번만 들어간다",
          prov.last_prompt.count("기출 전문 기출 전문") >= 1
          and prov.last_prompt.count(exam_text) == 1)

    ec = result["exam_concepts"]
    check("출제 개념 파싱", ec["기출출제개념"] == ["대퇴골", "슬관절"], str(ec))
    check("유형통계 파싱", ec["유형통계"].get("객관식") == 3, str(ec["유형통계"]))

    sq = result["sample_questions"]
    check("예시가 기존 형식으로 복원된다", "[유형: 객관식]" in sq, sq[:60])
    check("표기 흔들림('빈칸 채우기')을 정규화", "[유형: 빈칸채우기]" in sq, sq)
    check("예시 사이는 빈 줄 두 개", "\n\n\n" in sq)

    # 통계가 그대로 흘러가는지 (정규식 폴백으로 안 새는지)
    stats = llm.resolve_type_stats(ec, exam_text)
    check("LLM 유형 분류가 채택된다", stats.get("판별근거") == "LLM 유형 분류",
          str(stats.get("판별근거")))

    # JSON이 깨져도 파이프라인이 죽지 않아야 한다
    broken = CountingProvider(complete_text="죄송합니다, 분석할 수 없습니다.")
    fallback = llm.analyze_exam(exam_text, "key", "fake-model", broken)
    check("JSON 실패해도 터지지 않는다", fallback["sample_questions"] == ""
          and fallback["exam_concepts"]["기출출제개념"] == [])


# ──────────────────────────────────────────────
# ⑤ 생성 프롬프트 — 접두부가 배치마다 동일
# ──────────────────────────────────────────────

def test_prompt_split():
    print("[⑤ 생성 프롬프트 — 캐시 접두부 고정]")
    concepts = {"핵심질환": ["대퇴골 골절"], "핵심개념": ["몸쪽 끝"],
                "중요수치": [], "진단포인트": [], "치료원칙": []}
    exam_concepts = {"기출출제개념": ["대퇴골"], "빈출포인트": ["몸쪽 끝"]}
    priority = ["대퇴골 골절"]

    def build(count, avoid):
        return llm.build_question_generation_prompt(
            concepts, "[유형: 객관식]\n1. 예시", "문제유형: 객관식",
            count, exam_concepts, priority, 5, {"객관식": count},
            avoid_questions=avoid,
        )

    head1, tail1 = build(4, [])
    head2, tail2 = build(4, ["대퇴골의 몸쪽 끝 구조는?"])
    head3, tail3 = build(2, ["대퇴골의 몸쪽 끝 구조는?", "슬관절의 인대는?"])

    check("반환값이 (접두부, 접미부) 두 개", isinstance(head1, str) and isinstance(tail1, str))
    check("배치가 달라도 접두부는 완전히 동일", head1 == head2 == head3)
    check("접미부는 배치마다 다르다", tail1 != tail2 != tail3)

    check("고정 자료는 접두부에", "[유형: 객관식]" in head1 and "대퇴골 골절" in head1)
    check("출력 형식도 접두부에", "---QUESTION---" in head1)
    check("문제 수는 접미부에", "문제 수: 4개" in tail1 and "문제 수" not in head1)
    check("회피 목록은 접미부에", "대퇴골의 몸쪽 끝 구조는?" in tail2
          and "대퇴골의 몸쪽 끝 구조는?" not in head2)
    check("우선 주제 목록은 접두부, 개수 지시는 접미부",
          "대퇴골 골절" in head1 and "최소" in tail1)
    check("접두부가 캐시 최소 길이를 넘길 만큼 크다", len(head1) > 1000, str(len(head1)))


if __name__ == "__main__":
    try:
        test_image_selection()
        test_image_cache()
        test_cache_modes_do_not_collide()
        test_failure_not_cached()
        test_image_coverage()
        test_image_budget()
        test_exam_merge()
        test_prompt_split()
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
