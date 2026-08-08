"""
토큰 절약 장치들이 실제로 동작하는지 확인한다.

여기 있는 것들은 전부 "조용히 되돌아가도 아무도 모르는" 종류다 —
동작이 깨지는 게 아니라 **요금만 다시 오르기** 때문에, 화면으로는 티가 안 난다.
그래서 테스트로 고정해둔다.

  ① 이미지 처리 대상 선별 — 로고 한 점 때문에 본문 페이지를 Vision에 보내지 않는다
  ② 이미지 결과 캐시     — 같은 PDF를 다시 올리면 Vision 호출이 0이 된다
  ②-c 모드 분리         — 같은 페이지라도 '그림 설명'과 '글자 전사'는 캐시를 공유하면 안 된다
  ②-d 이미지 호출 폴백   — 한 모델이 죽어도 그림이 사라지지 않는다 (단 키 오류는 재시도 금지)
  ③ 이미지 예산          — 기출 여러 개를 올려도 '파일당'이 아니라 '전체' 상한이 걸린다
  ④ 기출 분석 1회 병합   — 기출 전문을 두 번 보내지 않는다
  ⑤ 생성 프롬프트 분리   — 배치가 달라도 캐시 접두부는 글자 하나까지 같다
  ⑥ 상한 초과분 집계     — 못 읽은 쪽까지 끝까지 세어 '몇 쪽 중 몇 쪽'을 만든다
  ⑦ 이미지 출력 한도     — 모드별로 다르고, 바뀌면 캐시가 무효화된다

아래 둘은 '요금'이 아니라 **결과가 조용히 나빠지는** 쪽이다. 되돌아가도 화면에는
주제 표가 멀쩡하게 나오고 기출만 소리 없이 잘린다:

  ⑧ 글자 예산 넘겨주기   — 강의록이 안 쓴 몫이 기출로 넘어간다
  ⑨ 기출 자르기 경계     — 상한을 넘겨도 반쪽짜리 문항은 남기지 않는다
  ⑰ 중지가 실제로 멈춘다 — 취소하면 아직 시작 안 한 그림 호출을 버린다

LLM 호출 없이 가짜 프로바이더로 돈다. API 키도 요금도 필요 없다:

    python tests/test_token_savings.py
"""

import io
import os
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 한글 윈도우 기본 코드페이지(cp949)로는 이 파일이 찍는 문자를 쓸 수 없다
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import fitz

import db
from providers.base import ProviderAuthError

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

    def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None,
                       max_tokens=None):
        self.image_calls += 1
        self.last_image_prompt = prompt
        self.last_image_max_tokens = max_tokens
        return f"그림 결과 {self.image_calls}"

    def list_models(self, api_key):
        return [self.default_model]


class SlowProvider(CountingProvider):
    """그림 한 장에 시간이 걸리는 프로바이더 — 취소했을 때 큐에 남는 장이 생기도록."""
    name = "slow-fake"
    CALL_SECONDS = 0.25

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()   # 여러 워커가 동시에 센다

    def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None,
                       max_tokens=None):
        # 자기 **전에** 센다 — 돈은 호출이 시작되는 순간 나가기 때문이다
        with self._lock:
            self.image_calls += 1
            n = self.image_calls
        time.sleep(self.CALL_SECONDS)
        return f"그림 결과 {n}"


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
    # 폴백 문구도 truthy 라서 '채워졌다'만 보면 안 된다 — describe_image 시그니처가
    # 어긋나면 TypeError 가 폴백으로 삼켜져 테스트가 통과해버린 적이 있다.
    check("결과가 채워진다", all(e.get("desc") for e in jobs1))
    check("폴백(실패) 문구가 아니다",
          not any("실패" in (e.get("desc") or "") for e in jobs1),
          str([e.get("desc") for e in jobs1][:1]))

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


def test_output_cap():
    """
    이미지 호출 출력 한도가 모드별로 전달되는지, 그리고 한도를 바꾸면 캐시가
    자동으로 무효화되는지.

    왜 중요한가 — 사고(thinking)를 하는 모델은 사고 토큰도 이 한도에 포함된다.
    한도가 빠듯하면 사고가 예산을 먹고 전사가 단어 중간에서 잘리는데, 잘린 결과가
    캐시에 남으면 한도만 올려도 옛 결과가 그대로 재사용된다.
    """
    print("[⑦ 출력 한도 — 모드별 전달 + 한도 변경 시 캐시 무효화]")

    # 예전에는 '전사 한도가 설명보다 커야 한다'로 못박았다. 설명은 한 문단이면 된다는
    # 전제였는데 (필기: …) 규칙이 들어오면서 깨졌다 — 스캔 기출은 인쇄 문항과 손글씨를
    # 갈라 적어야 해서 출력이 전사만큼 길어진다. 실제로 설명이 2048에 눌려 본문 0자로
    # 나온 회차가 있었다(generation 209 — llm.py의 DESCRIBE_MAX_OUTPUT 주석 참고).
    # 지금 지킬 것은 둘의 대소가 아니라 '사고 토큰에 눌리지 않을 만큼 넉넉한가'다.
    check("두 모드 다 사고에 눌리지 않을 한도",
          min(llm.DESCRIBE_MAX_OUTPUT, llm.TRANSCRIBE_MAX_OUTPUT) >= 4096,
          f"설명 {llm.DESCRIBE_MAX_OUTPUT} / 전사 {llm.TRANSCRIBE_MAX_OUTPUT}")
    check("두 모드 모두 한도를 들고 있다",
          llm.IMAGE_DESCRIBE.get("max_output") and llm.IMAGE_TRANSCRIBE.get("max_output"))

    doc = fitz.open()
    doc.new_page().insert_text((50, 60), "8", fontsize=9)
    pdf = doc.tobytes()
    doc.close()

    # 모드마다 제 한도가 프로바이더까지 내려가는지
    for mode, want in ((llm.IMAGE_DESCRIBE, llm.DESCRIBE_MAX_OUTPUT),
                       (llm.IMAGE_TRANSCRIBE, llm.TRANSCRIBE_MAX_OUTPUT)):
        prov = CountingProvider()
        _, jobs = llm.read_pdf_pages(pdf, "key", mode)
        describe_all(jobs, prov, model=f"cap-{want}", mode=mode)
        check(f"{mode['label']} 모드는 한도 {want} 전달",
              prov.last_image_max_tokens == want, str(prov.last_image_max_tokens))

    # 한도가 키에 들어가야 값을 올렸을 때 다시 받는다
    k_low = llm.image_cache_key(llm.IMAGE_TEXT_PROMPT, b"png", 1024)
    k_high = llm.image_cache_key(llm.IMAGE_TEXT_PROMPT, b"png", 4096)
    check("한도가 다르면 캐시 키가 다르다", k_low != k_high)
    check("한도가 같으면 캐시 키도 같다",
          k_high == llm.image_cache_key(llm.IMAGE_TEXT_PROMPT, b"png", 4096))


def _sparse_pdf(tag: str) -> bytes:
    """
    이미지 처리 대상 페이지 1장짜리 PDF (텍스트가 거의 없어 '스캔 페이지'로 잡힌다).
    tag가 다르면 렌더된 PNG가 달라져 캐시 키도 갈린다 — 테스트끼리 캐시를 안 나눠 쓰게.
    """
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 60), tag, fontsize=9)
    pdf = doc.tobytes()
    doc.close()
    return pdf


def test_failure_not_cached():
    print("[②-b 실패는 캐시하지 않는다]")

    class FailingProvider(CountingProvider):
        def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None,
                           max_tokens=None):
            self.image_calls += 1
            raise RuntimeError("이미지 미지원")

    # 캐시가 비어 있도록 이 테스트 전용 PDF를 따로 만든다
    pdf = _sparse_pdf("9")

    fail = FailingProvider()
    _, jobs = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs, fail)
    check("실패 시 폴백 문구", "실패" in (jobs[0].get("desc") or ""))

    retry = CountingProvider()
    _, jobs2 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs2, retry)
    check("다음 회차에 다시 시도한다", retry.image_calls == len(jobs2),
          f"{retry.image_calls} != {len(jobs2)} (폴백 문구가 캐시에 굳었다)")


def test_image_fallback():
    """
    본 모델이 실패하면 폴백 모델로 한 번 더 물어본다.

    폴백이 없으면 그 페이지는 설명 없이 문구만 남는데, 기출의 그림 문제는
    설명이 곧 문항이라 한 문항이 통째로 날아간다. 화면에는 아무 오류도 안 뜬다.
    """
    print("[②-d 이미지 호출 폴백]")

    class FallbackProvider(CountingProvider):
        """본 모델에는 실패하고 폴백 모델에만 답한다."""
        image_fallback_model = "backup"

        def __init__(self, error=RuntimeError):
            super().__init__()
            self._error = error
            self.models_called = []

        def describe_image(self, png_bytes, api_key, model, usage=None, prompt=None,
                           max_tokens=None):
            self.image_calls += 1
            self.models_called.append(model)
            if model != self.image_fallback_model:
                raise self._error("본 모델 실패")
            return "폴백이 만든 설명"

    pdf = _sparse_pdf("10")

    prov = FallbackProvider()
    _, jobs = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs, prov, model="main")
    check("본 모델이 죽으면 폴백이 결과를 만든다",
          jobs[0].get("desc") == "폴백이 만든 설명", str(jobs[0].get("desc")))
    check("폴백은 한 번만 — 장당 2회", prov.image_calls == 2 * len(jobs),
          f"{prov.image_calls} != {2 * len(jobs)} ({prov.models_called})")

    # 폴백이 만든 결과도 캐시에서 나와야 한다. 안 그러면 본 모델이 죽어 있는 동안
    # 회차마다 실패 호출이 한 번씩 더 난다.
    again = FallbackProvider()
    _, jobs2 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs2, again, model="main")
    check("폴백이 만든 결과도 캐시에서 재사용", again.image_calls == 0,
          f"{again.image_calls}회 재호출 ({again.models_called})")

    # 키 오류는 어느 모델로 보내도 똑같이 401이다. 워커 4개 × 상한(15·40장)만큼의
    # 헛호출이 두 배로 나지 않게 여기서만 폴백을 건너뛴다.
    auth = FallbackProvider(error=ProviderAuthError)
    _, jobs3 = llm.read_pdf_pages(_sparse_pdf("11"), "key", llm.IMAGE_DESCRIBE)
    describe_all(jobs3, auth, model="main")
    check("키 오류는 폴백하지 않는다", auth.image_calls == len(jobs3),
          f"{auth.image_calls} != {len(jobs3)} ({auth.models_called})")
    check("키 오류면 실패 문구가 남는다", "실패" in (jobs3[0].get("desc") or ""))


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
    # 렌더 결과는 메모리가 아니라 디스크에 있다 (llm.py의 spill 절) — 경로만 들고 있다.
    check("렌더는 상한까지만 (건너뛴 쪽엔 png 없음)",
          sum(1 for e in pages if e.get("png_path")) == 2)
    check("PNG를 메모리에 들고 있지 않는다",
          all("png" not in e for e in pages))
    check("내려둔 PNG 파일이 실제로 있다",
          all(os.path.exists(e["png_path"]) for e in pages if e.get("png_path")))
    llm.discard_spills(pages)
    check("discard_spills가 파일과 경로를 함께 지운다",
          all("png_path" not in e for e in pages))

    # 상한에 안 걸리면 경고가 안 뜬다
    pages2, jobs2 = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE, max_images=99)
    cov2 = llm.image_coverage(pages2, jobs2)
    check("여유가 있으면 건너뛴 쪽 없음", cov2["skipped"] == 0 and cov2["processed"] == 6,
          str(cov2))
    llm.discard_spills(pages2)

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

    # 예산이 앞 파일에서 동나도, 뒤 파일의 못 읽은 쪽은 경고에 남아야 한다.
    # (예전에는 몫이 0이면 image_mode를 None으로 바꿔 후보 판정 자체를 건너뛰었다 →
    #  화면에서 '그림이 없는 파일'과 구분이 안 되고 경고가 조용히 사라졌다)
    docs = llm.extract_labeled_docs(files, "기출", "key", "budget-model",
                                    image_mode=llm.IMAGE_DESCRIBE,
                                    provider=CountingProvider(), img_budget=4)
    last = docs[-1]["img"]
    check("예산이 동난 뒤 파일도 후보를 센다", last["candidates"] == 2, str(last))
    check("그 쪽들이 '못 읽음'으로 잡힌다", last["skipped"] == 2, str(last))
    check("실제 처리 합계는 예산과 같다",
          sum(d["img"]["processed"] for d in docs) == 4,
          str([d["img"]["processed"] for d in docs]))


def test_gen_image_budget():
    """
    문제 생성도 이미지 상한이 '한쪽 전체'에 걸리는지. (주제 분석과 같은 방식)

    예전에는 파일당이라 파일을 7개 올리면 상한이 그대로 7배가 됐다. 비용도 비용이지만
    더 급한 건 메모리다 — 렌더한 PNG를 설명이 끝날 때까지 전부 들고 있어서, RAM 1GB인
    배포(deploy/gcp)에서는 요금보다 OOM이 먼저 온다.

    read_labeled_pdfs는 두 탭이 함께 쓴다(주제 분석·문제 생성). ③이 조립까지 끝난
    결과로 확인한다면 여기는 읽기 단계 자체를 본다 — 예산 배분·이월·못 읽은 쪽 집계가
    여기서 정해지고, 뒤 단계는 그 결과를 옮겨 담기만 한다.
    """
    print("[③-2 이미지 예산(읽기 단계) — 파일당이 아니라 전체]")

    pdf = build_pdf()                                      # 파일당 설명 대상 2쪽
    files = [(f"기출{i}.pdf", pdf) for i in range(1, 6)]    # 후보 총 10쪽
    tight = dict(llm.IMAGE_DESCRIBE, max_pages=2)          # 후보보다 작게 걸어 갈라본다

    parts = llm.read_labeled_pdfs(files, "기출", "key", tight)
    picked = sum(len(x["img_jobs"]) for x in parts)
    check("파일당이 아니라 전체 상한", picked == 2, str(picked))
    check("예전 동작(파일당)이었다면 10쪽", picked < 10, str(picked))

    check("기본값도 전체 상한 안",
          sum(len(x["img_jobs"])
              for x in llm.read_labeled_pdfs(files, "기출", "key", llm.IMAGE_DESCRIBE))
          <= llm.IMAGE_DESCRIBE["max_pages"])

    # 파일별 몫을 남겨야 assemble_pdf_text가 '몇 개까지만 처리됨'을 제대로 적는다
    # (모드의 max_pages를 쓰면 예산을 나눠 쓴 파일에서 숫자가 거짓이 된다)
    check("파일별 몫이 parts에 남는다", all("quota" in x for x in parts),
          str([sorted(x) for x in parts[:1]]))

    # 예산이 동나도 못 읽은 쪽은 계속 센다 (확인 모달의 경고가 조용히 사라지지 않게)
    cov = llm.image_coverage(parts[-1]["pages"], parts[-1]["img_jobs"])
    check("예산이 동난 뒤 파일도 후보를 센다", cov["candidates"] == 2, str(cov))
    check("그 쪽들이 '못 읽음'으로 잡힌다", cov["skipped"] == 2, str(cov))

    # 앞 파일이 안 쓴 몫은 뒤로 넘어간다 — 그림이 한 파일에 몰려 있어도 낭비가 없다.
    # 파일당 고정 상한이었다면 마지막 파일은 제 몫(2÷5→1)만 받고 1쪽을 버렸다.
    mixed = [("텍스트.pdf", _text_pdf(200))] * 4 + [("스캔.pdf", pdf)]
    parts_m = llm.read_labeled_pdfs(mixed, "기출", "key", tight)
    check("앞 파일이 안 쓴 몫이 뒤로 넘어간다", len(parts_m[-1]["img_jobs"]) == 2,
          str([len(x["img_jobs"]) for x in parts_m]))

    check("모드가 없으면 후보도 0",
          sum(len(x["img_jobs"])
              for x in llm.read_labeled_pdfs(files, "기출", "key", None)) == 0)


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


# ──────────────────────────────────────────────
# ⑧ 글자 예산 넘겨주기 (주제 분석)
# ──────────────────────────────────────────────

def _text_pdf(chars: int) -> bytes:
    """대략 chars 글자쯤 되는 텍스트 PDF. 쪽을 넘겨 가며 그린다 (쪽 밖은 추출되지 않는다)."""
    doc = fitz.open()
    line = "femur proximal end structure item choose one "      # 45자
    page, y = doc.new_page(), 60
    for _ in range(chars // len(line) + 1):
        if y > 740:
            page, y = doc.new_page(), 60
        page.insert_text((40, y), line, fontsize=8)
        y += 12
    out = doc.tobytes()
    doc.close()
    return out


def test_char_budget_handoff():
    """
    이건 '요금'이 아니라 '결과'가 조용히 나빠지는 쪽이다 — 넘겨주기가 사라지면 기출
    가운데가 통째로 잘리는데, 화면에는 주제 표가 멀쩡하게 나온다. 그래서 고정해둔다.
    """
    print("[⑧ 글자 예산 — 강의록이 남긴 몫이 기출로 넘어간다]")

    check("총예산은 한쪽 몫의 2배 (양쪽이 다 채우면 예전과 같다)",
          llm.TOPIC_TOTAL_CHAR_BUDGET == llm.TOPIC_SIDE_CHAR_BUDGET * 2,
          f"{llm.TOPIC_TOTAL_CHAR_BUDGET} vs {llm.TOPIC_SIDE_CHAR_BUDGET}")

    # 실측(topic_analyses id=9·10·13·14·18·19)에서 강의록은 상한의 21~46%만 썼다.
    # 예산 상수를 바꿔도 그 상황이 유지되도록 비율로 잡는다 — 고정값으로 두면 상한을
    # 올린 순간 '강의록이 예산을 남긴다'는 전제 자체가 깨져서 아래 갈래가 안 갈린다.
    lecture_used = int(llm.TOPIC_SIDE_CHAR_BUDGET * 0.42)
    lecture_docs = [{"text": "가" * lecture_used}]
    check("남은 몫 = 총예산 − 강의록 실사용",
          llm.remaining_char_budget(lecture_docs)
          == llm.TOPIC_TOTAL_CHAR_BUDGET - lecture_used,
          str(llm.remaining_char_budget(lecture_docs)))
    check("강의록이 상한을 다 써도 한쪽 몫은 보장",
          llm.remaining_char_budget([{"text": "가" * llm.TOPIC_SIDE_CHAR_BUDGET}])
          >= llm.TOPIC_SIDE_CHAR_BUDGET)
    check("강의록이 없으면 총예산 그대로",
          llm.remaining_char_budget([]) == llm.TOPIC_TOTAL_CHAR_BUDGET)

    # 실제 추출에 걸어본다. 한쪽 몫으로는 넘치고 남은 몫으로는 들어가는 크기여야
    # 두 갈래가 갈린다 — 크기가 어긋나면 조용히 통과하지 않도록 여기서 먼저 확인한다.
    # 이 크기도 예산에 비례해 잡는다 (한쪽 몫의 1.25배 → 남은 몫 1.58배 안에 들어온다).
    exam = [FakeUpload("기출.pdf", _text_pdf(int(llm.TOPIC_SIDE_CHAR_BUDGET * 1.25)))]
    prov = CountingProvider()

    old = llm.extract_labeled_docs(exam, "기출", "key", "m", provider=prov)
    chars = old[0]["source"]["chars"]
    check("시험용 기출이 한쪽 몫과 남은 몫 사이 크기",
          llm.TOPIC_SIDE_CHAR_BUDGET < chars
          <= llm.remaining_char_budget(lecture_docs), str(chars))
    check("예전 칸막이(한쪽 몫)로는 잘린다", old[0]["source"]["truncated"] is True,
          str(old[0]["source"]))

    new = llm.extract_labeled_docs(exam, "기출", "key", "m", provider=prov,
                                   side_budget=llm.remaining_char_budget(lecture_docs))
    check("남은 몫을 받으면 안 잘린다", new[0]["source"]["truncated"] is False,
          str(new[0]["source"]))
    check("확인 모달을 띄울 경고도 사라진다", new[0]["cut"] is None, str(new[0]["cut"]))
    check("본문이 통째로 들어간다", len(new[0]["text"]) == chars,
          f'{len(new[0]["text"])} vs {chars}')


def test_gen_char_budget_handoff():
    """
    문제 생성 쪽 넘겨주기. 주제 분석(⑧)과 같은 고장을 같은 방식으로 막는다 —
    예산을 한쪽씩 고정하면 강의자료에 수만 자가 놀고 있는데 기출만 잘린다.
    여기가 더 조용한 이유: 기출이 잘려도 화면에는 문제가 멀쩡히 생성돼 나오는데,
    그 문제의 유형별 개수를 정하는 type_stats가 잘린 기출에서 세어진 값이다.
    """
    print("[⑧-2 글자 예산(문제 생성) — 강의자료가 남긴 몫이 기출로 넘어간다]")

    check("총예산은 한쪽 몫의 2배",
          llm.GEN_TOTAL_CHAR_BUDGET == llm.GEN_SIDE_CHAR_BUDGET * 2,
          f"{llm.GEN_TOTAL_CHAR_BUDGET} vs {llm.GEN_SIDE_CHAR_BUDGET}")

    lecture = "가" * 54632          # 실측(session 63)의 강의자료 사용량
    check("남은 몫 = 총예산 − 강의자료 실사용",
          llm.remaining_gen_budget(lecture)
          == llm.GEN_TOTAL_CHAR_BUDGET - 54632,
          str(llm.remaining_gen_budget(lecture)))
    check("강의자료가 상한을 다 써도 한쪽 몫은 보장",
          llm.remaining_gen_budget("가" * llm.GEN_SIDE_CHAR_BUDGET)
          >= llm.GEN_SIDE_CHAR_BUDGET)
    check("강의자료가 없으면 총예산 그대로",
          llm.remaining_gen_budget("") == llm.GEN_TOTAL_CHAR_BUDGET)

    # 한 번의 호출로 나가는 쪽이라 컨텍스트 천장을 넘기면 안 된다.
    # 천장이 예산보다 낮게 잘못 잡히면 넘겨주기가 조용히 무력해지므로 함께 고정한다.
    check("어떤 경우에도 호출당 천장을 넘지 않는다",
          llm.remaining_gen_budget("") <= llm.GEN_CALL_CHAR_CEILING,
          f"{llm.remaining_gen_budget('')} vs {llm.GEN_CALL_CHAR_CEILING}")
    check("천장이 한쪽 몫보다는 커서 넘겨주기가 살아 있다",
          llm.GEN_CALL_CHAR_CEILING > llm.GEN_SIDE_CHAR_BUDGET,
          f"{llm.GEN_CALL_CHAR_CEILING} vs {llm.GEN_SIDE_CHAR_BUDGET}")

    # 실측 자료 크기(session 63: 강의 54,632 / 기출 151,407)를 그대로 걸어본다.
    # 예전 예산(10만)에서는 63%만 반영됐다 — 그 회귀를 여기서 잡는다.
    exam = "나" * 151407
    old = llm.build_source_info(exam, 100000, by_question=True)
    check("예전 예산(10만)으로는 잘렸다", old["truncated"] is True, str(old))

    new = llm.build_source_info(exam, llm.remaining_gen_budget(lecture),
                                by_question=True)
    check("지금 예산에서는 기출이 통째로 들어간다", new["truncated"] is False, str(new))
    check("반영률 100%", new["coverage"] == 100, str(new))
    check("확인 모달을 띄울 경고도 없다",
          llm.truncation_report(exam, llm.remaining_gen_budget(lecture),
                                by_question=True) is None)


# ──────────────────────────────────────────────
# ⑨ 기출 자르기 경계 — 반쪽짜리 문항 금지
# ──────────────────────────────────────────────

CUT_NOTE = "...(중략: 분량 초과로 일부 생략)..."


def _exam_text(questions: int = 80, body_words: int = 40) -> str:
    """[페이지 N] 마커가 붙은 기출 흉내. 문항 5개마다 쪽이 넘어가고, 문항은 '?'로 끝난다."""
    out = []
    for i in range(1, questions + 1):
        if (i - 1) % 5 == 0:
            out.append(f"[페이지 {(i - 1) // 5 + 1}]")
        out.append(f"{i}. " + "다음설명중 " * body_words + "옳은 것은?")
    return "\n".join(out)


def _halves(text):
    head, _, tail = text.partition(CUT_NOTE)
    return head.strip(), tail.strip()


def test_question_boundary_cut():
    print("[⑨ 기출 자르기 — 반쪽짜리 문항을 남기지 않는다]")
    raw = _exam_text()
    limit = len(raw) // 2

    old = llm.truncate(raw, limit)                      # 글자수 방식 (강의록)
    new = llm.truncate(raw, limit, by_question=True)    # 문항 경계 (기출)
    check("둘 다 잘리기는 한다", CUT_NOTE in old and CUT_NOTE in new)

    # 예전 방식이 실제로 무엇을 남기는지 — 이게 이 변경의 이유다.
    #   앞: "…옳은 것은?\n29."   ← 번호만 있고 지문은 통째로 없는 문항
    #   뒤: "?\n69. 다음설명중…"  ← 번호 없이 앞 문항의 꼬리부터 시작
    o_head, o_tail = _halves(old)
    check("예전 방식은 번호만 남은 문항이 앞에 붙는다",
          o_head.rsplit("\n", 1)[-1].strip().rstrip(".)]").isdigit(),
          repr(o_head[-30:]))
    check("예전 방식은 뒤가 앞 문항의 꼬리로 시작한다",
          not llm._QUESTION_START.match(o_tail.split("\n", 1)[1]),
          repr(o_tail[:40]))

    # 남아 있는 문항 번호 수 = 완결된 문항 수('?'로 끝나는 것)
    check("문항 경계로 자르면 남은 문항이 모두 온전하다",
          len(llm._QUESTION_START.findall(new)) == new.count("?"),
          f'번호 {len(llm._QUESTION_START.findall(new))} vs 완결 {new.count("?")}')

    n_head, n_tail = _halves(new)
    check("앞부분은 문항 끝에서 끊긴다", n_head.endswith("?"), n_head[-30:])
    check("뒷부분은 쪽 표시로 시작한다", n_tail.startswith("[페이지 "), n_tail[:30])
    check("쪽 표시 다음은 문항 번호", bool(llm._QUESTION_START.match(
        n_tail.split("\n", 1)[1])), n_tail.split("\n", 1)[1][:30])

    # 복원한 쪽 번호가 그 문항의 실제 쪽과 같아야 한다 (틀린 출처는 없는 출처보다 나쁘다)
    first_no = int(llm._QUESTION_START.findall(n_tail.split("\n", 1)[1])[0]
                   .strip().rstrip(".)]").strip())
    check("복원한 쪽 번호가 실제 쪽과 같다",
          n_tail.startswith(f"[페이지 {(first_no - 1) // 5 + 1}]"),
          f"{n_tail[:20]} / 문항 {first_no}")

    # 번호를 못 찾는 기출(스캔본 등)은 예전 방식으로 되돌아간다
    plain = "가나다라마바사아자차" * 5000
    check("번호가 없으면 글자수 방식으로 되돌아간다",
          llm.truncate(plain, 1000, by_question=True)
          == llm.truncate(plain, 1000), "폴백 실패")

    # 보고 값이 실제로 남긴 양과 맞는지 — 상한을 그대로 쓰면 반영률이 부풀려진다
    h, t = llm._cut_points(raw, limit, True)
    info = llm.build_source_info(raw, limit, by_question=True)
    check("반영량은 실제로 남긴 양", info["used"] == h + len(raw) - t,
          f'{info["used"]} vs {h + len(raw) - t}')
    check("문항 경계로 당긴 만큼 상한보다 적다", info["used"] < limit,
          f'{info["used"]} vs {limit}')
    check("경고의 반영률도 같은 값",
          llm.truncation_report(raw, limit, True)["coverage"] == info["coverage"],
          str(llm.truncation_report(raw, limit, True)["coverage"]))

    # 글자수 방식은 예전 그대로 (상한을 꽉 채운다)
    check("글자수 방식의 반영량은 그대로 상한",
          llm.build_source_info(raw, limit)["used"] == limit,
          str(llm.build_source_info(raw, limit)["used"]))

    # 유형 집계가 자르기와 같은 '문항 시작' 정의를 쓴다
    check("문항 집계가 같은 정의를 쓴다",
          llm.count_question_types(raw)["총문항"] == 80,
          str(llm.count_question_types(raw)["총문항"]))


def test_upload_spill():
    """
    업로드를 디스크로 넘겨도 읽는 결과가 같아야 한다.

    라우트는 업로드를 경로로 바꿔 넘긴다(llm.spill_upload) — bytes로 들고 있으면
    생성이 끝날 때까지 파일 전체가 힙에 남기 때문이다. 결과가 조금이라도 달라지면
    메모리를 아끼려다 추출 품질을 바꾸는 셈이 되므로 여기서 못을 박는다.
    """
    print("\n[⑮ 업로드를 디스크로 넘겨도 결과가 같다]")

    doc = fitz.open()
    for i in range(3):
        p = doc.new_page()
        p.insert_text((50, 60), f"{i + 1}. 대퇴골의 몸쪽 끝 구조는?", fontsize=10)
        p.insert_image(fitz.Rect(50, 300, 500, 750), stream=_png(64))
    pdf = doc.tobytes()
    doc.close()

    by_bytes, jobs_b = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE, max_images=99)
    text_b = "".join(e["text"] for e in by_bytes)
    llm.discard_spills(by_bytes)

    path = llm.spill_upload(io.BytesIO(pdf))
    check("업로드 사본이 만들어진다", os.path.exists(path))
    check("사본 크기가 원본과 같다", os.path.getsize(path) == len(pdf))

    by_path, jobs_p = llm.read_pdf_pages(path, "key", llm.IMAGE_DESCRIBE, max_images=99)
    text_p = "".join(e["text"] for e in by_path)

    check("텍스트가 같다", text_b == text_p, f"{len(text_b)} vs {len(text_p)}")
    check("이미지 후보 수가 같다", len(jobs_b) == len(jobs_p),
          f"{len(jobs_b)} vs {len(jobs_p)}")
    llm.discard_spills(by_path)

    # 라우트의 finally가 부르는 정리 — (이름, 경로) 목록을 그대로 받는다
    files = [("기출.pdf", path)]
    llm.discard_upload_spills(files)
    check("discard_upload_spills가 사본을 지운다", not os.path.exists(path))

    # bytes가 섞여 들어와도(세션 재사용 경로 등) 터지지 않아야 한다
    llm.discard_upload_spills([("x.pdf", b"not a path")])


def test_cancel_stops_image_calls():
    """
    중지하면 아직 시작도 안 한 그림 호출은 버려야 한다.

    describe_images_progressively 는 제너레이터라, 사용자가 '중지'를 누르거나 브라우저가
    끊기면 닫히면서(GeneratorExit) 스레드 풀을 정리한다. 이때 with 문의 기본 동작
    (shutdown(wait=True))에 맡기면 큐에 남은 장을 **끝까지 다 돌린다** — 사용자는 멈췄다고
    보는데 남은 그림값이 그대로 나가고, 중지도 그게 끝날 때까지 붙잡혀 있다.
    상한이 강의록 100 + 기출 60이라 한 번에 날아갈 수 있는 양이 작지 않다.

    화면에는 아무 흔적이 없고 요금서에만 보이는 종류라 여기서 못을 박는다.
    """
    print("\n[⑰ 중지하면 남은 그림 호출을 버린다]")

    total = 12
    doc = fitz.open()
    for i in range(total):
        p = doc.new_page()
        p.insert_text((50, 60), f"{i + 1}", fontsize=9)
        # 장마다 회색을 달리한다 — 같은 PNG면 캐시 키가 겹쳐 호출 없이 지나간다
        p.insert_image(fitz.Rect(50, 300, 500, 750), stream=_png(64, gray=10 + i * 7))
    pdf = doc.tobytes()
    doc.close()

    pages, jobs = llm.read_pdf_pages(pdf, "key", llm.IMAGE_DESCRIBE, max_images=total)
    check("그림 페이지가 다 잡혔다", len(jobs) == total, len(jobs))

    prov = SlowProvider()
    saved_workers = llm.config.IMAGE_WORKERS
    llm.config.IMAGE_WORKERS = 2        # 큐에 남는 장이 생기도록 좁힌다
    try:
        gen = llm.describe_images_progressively(jobs, "key", "fake-model", provider=prov)
        next(gen)                       # 한 장 받아보고
        t0 = time.perf_counter()
        gen.close()                     # 사용자가 '중지' → GeneratorExit
        blocked = time.perf_counter() - t0
    finally:
        llm.config.IMAGE_WORKERS = saved_workers

    # 이미 워커에 올라간 장은 뒤늦게 끝난다 — 그것까지 세고 나서 판정한다
    time.sleep(SlowProvider.CALL_SECONDS * 3)
    called = prov.image_calls

    # 고치기 전에는 12/12 가 나가고 중지가 1초 넘게 붙잡혀 있었다
    check("남은 장을 버린다", called < total, f"{called}/{total}장이 호출됐다")
    check("중지가 큐를 기다리지 않는다", blocked < 1.0, f"{blocked:.2f}초 붙잡혔다")

    llm.discard_spills(pages)
    check("bytes가 섞여도 조용히 넘어간다", True)


if __name__ == "__main__":
    try:
        test_image_selection()
        test_image_cache()
        test_cache_modes_do_not_collide()
        test_output_cap()
        test_failure_not_cached()
        test_image_fallback()
        test_image_coverage()
        test_image_budget()
        test_gen_image_budget()
        test_exam_merge()
        test_prompt_split()
        test_char_budget_handoff()
        test_gen_char_budget_handoff()
        test_question_boundary_cut()
        test_upload_spill()
        test_cancel_stops_image_calls()
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
