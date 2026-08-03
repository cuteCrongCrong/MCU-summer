"""
문제 생성 파이프라인(run_generation_events)을 끝까지 한 번 돌려보는 연기 테스트.

왜 필요한가 — 이 파이프라인은 API 키가 있어야 지나가는 경로라 평소 개발 중에
실행되지 않는다. 그래서 "이름 하나 안 가져왔다"(NameError) 같은 오류가 사용자가
실제로 생성을 돌릴 때까지 발견되지 않는다. 실제로 credits_for_history 를 import
하지 않은 채 호출하는 버그가 이 방식으로 사용자에게 먼저 발견됐다.
단위 테스트로는 못 잡는다 — 함수를 하나씩 직접 부르면 그 사이를 잇는 코드가
실행되지 않기 때문. 그래서 파이프라인 전체를 흘려본다.

프로바이더를 가짜로 바꿔 LLM 호출 없이 돈다. API 키도 요금도 필요 없다:

    python tests/test_generation_flow.py
"""

import pathlib
import sys

# 저장소 루트를 import 경로에 넣는다 (python tests/test_generation_flow.py 로 바로 실행되게)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import db
from features.question_gen import (
    run_generation_events, save_session, delete_session, load_generation,
)

_failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        _failures.append(name)


# ──────────────────────────────────────────────
# 가짜 프로바이더 — 실제 LLM 대신 정해진 답을 돌려준다
# ──────────────────────────────────────────────

QUESTION_BLOCK = (
    "---QUESTION---\n"
    "유형: 객관식\n"
    "문제: 대퇴골의 몸쪽 끝에 있는 구조는?\n"
    "선택지:\n"
    "1) 대퇴골두\n2) 내측과\n3) 외측과\n4) 슬개면\n"
    "정답: 1\n"
    "해설: 대퇴골두는 몸쪽 끝에서 관골구와 관절한다.\n"
    "---END---\n"
)


class FakeUsage:
    """SDK usage 객체 흉내 (OpenAI 표기)."""
    prompt_tokens = 100
    completion_tokens = 50


class FakeProvider:
    name = "fake"
    label = "가짜 제공사"
    default_model = "fake-model"
    supports_credits = False        # 크레딧 미지원 경로

    def complete(self, prompt, api_key, model, max_tokens=None, usage=None):
        if usage is not None:
            usage.add(model, FakeUsage())
        return "{}"

    def complete_stream(self, prompt, api_key, model, max_tokens=None, usage=None):
        # 조각 경계가 문제 중간을 자르는 실제 상황을 흉내낸다
        mid = len(QUESTION_BLOCK) // 2
        yield QUESTION_BLOCK[:mid]
        yield QUESTION_BLOCK[mid:]
        if usage is not None:
            usage.add(model, FakeUsage())

    def describe_image(self, png_bytes, api_key, model, usage=None):
        if usage is not None:
            usage.add(model, FakeUsage())
        return "그림 설명"

    def list_models(self, api_key):
        return [self.default_model]


class FakeCreditProvider(FakeProvider):
    """크레딧 과금 제공사 경로 — 저장 직전 credits_for_history 를 지나간다."""
    name = "fake_credit"
    supports_credits = True

    def __init__(self):
        self._used = 100.0

    def get_credits(self, api_key):
        # 호출할 때마다 조금씩 늘어난다 → 전후 차이 = 이번에 쓴 크레딧
        self._used += 2.5
        return {
            "monthly_allocated": {"quota": 1000.0, "used": self._used,
                                  "remaining": 1000.0 - self._used,
                                  "renewal_date": "2026-09-01"},
            "purchased": {"quota": 0.0, "used": 0.0, "remaining": 0.0},
            "total": {"quota": 1000.0, "used": self._used,
                      "remaining": 1000.0 - self._used},
            "sections": [],
        }


# ──────────────────────────────────────────────
# 세션 재사용 경로(A)를 끝까지 흘려본다
# ──────────────────────────────────────────────

def run_pipeline(provider, owner, sid):
    """이벤트를 전부 소진하고 (done 페이로드, error 이벤트)를 돌려준다."""
    # 라우트의 _parse_generate_params() 가 만드는 것과 같은 형태.
    # 키를 빠뜨리면 KeyError가 나므로, 라우트가 넣는 키를 여기서도 모두 채운다.
    params = {
        "api_key": "test-key", "provider": provider, "model": "fake-model",
        "count": 2, "weight": 5, "session_id": sid, "owner": owner,
        "gen_title": "연기 테스트",
        "manual_targets": None,      # 자동 배분
        "preserve_types": False,
        "lecture_bytes": None, "exam_bytes": None,   # 경로 A라 안 쓰인다
    }
    done = error = None
    for ev in run_generation_events(params):
        if ev["type"] == "done":
            done = ev["payload"]
        elif ev["type"] == "error":
            error = ev
    return done, error


def test_flow():
    print("[생성 파이프라인]")
    db.init_db()
    owner = (None, "__flow_test__")
    sid = save_session("연기 테스트 세션", "fake-model", {
        "concepts": {}, "sample_questions": "", "format_analysis": "",
        "exam_concepts": {}, "priority_topics": [], "type_stats": {}, "source_info": {},
    }, owner, "fake")

    try:
        # ① 토큰 과금 제공사
        done, error = run_pipeline(FakeProvider(), owner, sid)
        check("오류 없이 끝난다", error is None, error)
        check("done 페이로드가 온다", done is not None)
        if not done:
            return

        check("문제가 파싱된다", len(done["questions"]) > 0, done["questions"])
        check("이력이 저장된다", done["generation_id"] is not None)
        check("응답에 usage 포함", (done.get("usage") or {}).get("calls", 0) > 0,
              done.get("usage"))

        # 보관함에서 다시 꺼냈을 때도 남아 있어야 한다
        saved = load_generation(done["generation_id"], owner)
        check("보관된 회차에 usage 저장됨",
              (saved.get("usage") or {}).get("total", 0) > 0, saved.get("usage"))
        check("토큰 제공사는 credits 없음", saved.get("credits") is None, saved.get("credits"))

        # ② 크레딧 과금 제공사 — 저장 직전 credits_for_history 를 지나간다
        done2, error2 = run_pipeline(FakeCreditProvider(), owner, sid)
        check("크레딧 경로도 오류 없음", error2 is None, error2)
        if not done2:
            return
        check("응답에 쓴 크레딧", (done2.get("credits") or {}).get("spent_known") is True,
              done2.get("credits"))

        saved2 = load_generation(done2["generation_id"], owner)
        kept = saved2.get("credits") or {}
        check("보관된 회차에 쓴 크레딧만 저장됨",
              kept.get("spent_known") is True and "remaining" not in kept, kept)
    finally:
        delete_session(sid, owner)      # 연결된 회차도 함께 지워진다


if __name__ == "__main__":
    test_flow()
    print()
    if _failures:
        print(f"실패 {len(_failures)}건: " + ", ".join(_failures))
        sys.exit(1)
    print("전부 통과")
