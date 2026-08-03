"""
토큰 사용량 수집(providers/usage.py) 검증.

이 코드는 평소 개발 중에 저절로 검증되지 않는다 — 실제 API 키로 생성을 돌려야
지나가는 경로이고, 그마저도 프로바이더를 바꿔가며 다 해봐야 한다. 특히 이 세 가지는
조용히 깨지기 쉬워서 테스트로 고정해둔다.

  ① 프로바이더마다 usage 필드 이름이 다르다
     (OpenAI: prompt/completion_tokens, Anthropic: input/output_tokens)
  ② 스트리밍은 usage를 기본으로 안 준다 → stream_options를 요청하고,
     서버가 400으로 거부하면 옵션 없이 재시도해야 한다
  ③ "0 토큰"과 "제공사가 안 알려줌"은 다르다 (0으로 합치면 안 쓴 것처럼 보인다)

외부 의존성 없이 SDK 클라이언트를 가짜로 바꿔 확인한다. pytest 없이 그냥 실행:

    python tests/test_usage.py
"""

import pathlib
import sys
import threading

# 저장소 루트를 import 경로에 넣는다 (python tests/test_usage.py 로 바로 실행되게)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import httpx
from openai import APIStatusError

from providers.usage import UsageCollector
from providers.openai_provider import OpenAIProvider
from providers.anthropic_provider import AnthropicProvider


# ──────────────────────────────────────────────
# 아주 작은 테스트 러너 (프레임워크 없이)
# ──────────────────────────────────────────────
_failures = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else f"  → {extra}"))
    if not cond:
        _failures.append(name)


# ──────────────────────────────────────────────
# 가짜 SDK 응답
# ──────────────────────────────────────────────
class OAUsage:            # OpenAI 호환 표기
    prompt_tokens, completion_tokens = 120, 34


class AntUsage:           # Anthropic 표기
    input_tokens, output_tokens = 11, 22


class FakeResp:
    def __init__(self, content="hi", usage=None):
        msg = type("X", (), {"content": content})()
        self.choices = [type("M", (), {"message": msg})()]
        self.usage = usage or OAUsage()


class FakeChunk:
    """delta=None 이면 usage 전용 청크 (choices가 빈 청크)."""
    def __init__(self, delta=None, usage=None):
        self.usage = usage
        if delta is None:
            self.choices = []
        else:
            d = type("D", (), {"content": delta})()
            self.choices = [type("C", (), {"delta": d})()]


class FakeStream:
    def __init__(self, chunks): self._chunks = chunks
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __iter__(self): return iter(self._chunks)


def make_oa_client(*, stream_chunks=None, reject_stream_options=False):
    """OpenAI SDK 자리에 끼울 가짜 클라이언트. reject_stream_options=True 면 400."""
    seen = {"stream_options_used": None}

    def create(**kw):
        if kw.get("stream"):
            seen["stream_options_used"] = "stream_options" in kw
            if reject_stream_options and "stream_options" in kw:
                raise APIStatusError(
                    "unknown parameter: stream_options", body=None,
                    response=httpx.Response(
                        400, request=httpx.Request("POST", "http://fake")),
                )
            return FakeStream(stream_chunks or [])
        return FakeResp()

    cli = type("Cli", (), {})()
    cli.chat = type("Chat", (), {})()
    cli.chat.completions = type("Comp", (), {"create": staticmethod(create)})()
    return cli, seen


def with_client(provider, client):
    """_client()만 가짜로 바꿔서, 나머지 프로바이더 코드는 실제 경로를 타게 한다."""
    provider._client = lambda api_key: client
    return provider


# ──────────────────────────────────────────────
# 1. 수집기 자체
# ──────────────────────────────────────────────
def test_collector():
    print("[수집기]")
    u = UsageCollector()
    check("호출 0회면 calls=0", u.summary()["calls"] == 0)

    u.set_stage("extract")
    for _ in range(3):
        u.add("gpt-5.6", OAUsage())
    u.set_stage("concepts")
    u.add("gpt-5.6", AntUsage())
    u.add("gpt-5.6", None)                      # 제공사가 안 알려준 호출
    u.set_stage("generate")
    # 병렬 기록이 유실되지 않는지 (Lock 검증) — 실제로 이미지 설명·개념 분석이
    # ThreadPoolExecutor에서 동시에 기록한다
    ts = [threading.Thread(target=u.add, args=("mini", OAUsage())) for _ in range(50)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    s = u.summary()
    # 기대값은 가짜 상수에서 유도한다 (상수를 바꿔도 테스트가 따라오게)
    want_in  = 3 * OAUsage.prompt_tokens + AntUsage.input_tokens + 50 * OAUsage.prompt_tokens
    want_out = 3 * OAUsage.completion_tokens + AntUsage.output_tokens \
               + 50 * OAUsage.completion_tokens

    check("병렬 기록 유실 없음", s["calls"] == 55, s["calls"])
    check("미보고 호출 분리", s["unreported_calls"] == 1, s)
    check("입력 합계", s["input"] == want_in, f'{s["input"]} != {want_in}')
    check("출력 합계", s["output"] == want_out, f'{s["output"]} != {want_out}')
    check("합계 = 입력+출력", s["total"] == s["input"] + s["output"])
    check("단계 표시 순서 유지",
          [r["key"] for r in s["by_stage"]] == ["extract", "concepts", "generate"],
          [r["key"] for r in s["by_stage"]])

    concepts = next(r for r in s["by_stage"] if r["key"] == "concepts")
    check("미보고도 호출 수에는 포함", concepts["calls"] == 2, concepts)
    check("단계별 미보고 수", concepts["unreported_calls"] == 1, concepts)


# ──────────────────────────────────────────────
# 2. OpenAI 호환 (OpenAI·Gemini·전북대 게이트웨이가 공유하는 경로)
# ──────────────────────────────────────────────
def test_openai_compatible():
    print("[OpenAI 호환]")
    u = UsageCollector()
    u.set_stage("concepts")
    cli, _ = make_oa_client()
    with_client(OpenAIProvider(), cli).complete("p", "k", "gpt-5.6", usage=u)
    check("complete 기록", u.summary()["total"] == 154, u.summary())

    u = UsageCollector()
    u.set_stage("extract")
    cli, _ = make_oa_client()
    with_client(OpenAIProvider(), cli).describe_image(b"png", "k", "gpt-5.6", usage=u)
    check("describe_image 기록", u.summary()["total"] == 154, u.summary())

    # 스트리밍 정상 — 마지막 usage 청크를 읽는다
    u = UsageCollector()
    u.set_stage("generate")
    cli, seen = make_oa_client(
        stream_chunks=[FakeChunk("가"), FakeChunk("나"), FakeChunk(None, OAUsage())])
    out = "".join(with_client(OpenAIProvider(), cli)
                  .complete_stream("p", "k", "m", usage=u))
    check("스트리밍 텍스트 보존", out == "가나", out)
    check("stream_options 요청함", seen["stream_options_used"] is True)
    check("스트리밍 usage 기록", u.summary()["total"] == 154, u.summary())

    # 스트리밍인데 서버가 stream_options를 모른다 → 400 후 폴백
    u = UsageCollector()
    u.set_stage("generate")
    cli, _ = make_oa_client(stream_chunks=[FakeChunk("가"), FakeChunk("나")],
                            reject_stream_options=True)
    out = "".join(with_client(OpenAIProvider(), cli)
                  .complete_stream("p", "k", "m", usage=u))
    s = u.summary()
    check("400 폴백에도 생성은 정상", out == "가나", out)
    check("400 폴백은 미보고로 기록",
          s["calls"] == 1 and s["unreported_calls"] == 1 and s["total"] == 0, s)

    # usage 인자를 생략해도 기존처럼 동작해야 한다 (하위 호환)
    cli, _ = make_oa_client(stream_chunks=[FakeChunk("가"), FakeChunk(None, OAUsage())])
    p = with_client(OpenAIProvider(), cli)
    check("usage 생략 — complete", p.complete("p", "k", "m") == "hi")
    check("usage 생략 — stream", "".join(p.complete_stream("p", "k", "m")) == "가")


# ──────────────────────────────────────────────
# 3. Anthropic (필드 이름이 다르고, 거절 처리 순서가 중요)
# ──────────────────────────────────────────────
def _ant_client(resp):
    cli = type("A", (), {})()
    cli.messages = type("M", (), {"create": staticmethod(lambda **kw: resp)})()
    return cli


def test_anthropic():
    print("[Anthropic]")

    class Ok:
        stop_reason = "end_turn"
        usage = AntUsage()
        content = [type("B", (), {"type": "text", "text": "안녕"})()]

    u = UsageCollector()
    u.set_stage("format")
    with_client(AnthropicProvider(), _ant_client(Ok())).complete(
        "p", "k", "claude-opus-5", usage=u)
    check("input/output 표기 정규화", u.summary()["total"] == 33, u.summary())

    # 거절(refusal)은 예외로 터지지만, 토큰은 이미 소모됐으므로 기록돼야 한다
    class Refused:
        stop_reason = "refusal"
        usage = AntUsage()
        content = []

    u = UsageCollector()
    u.set_stage("format")
    raised = False
    try:
        with_client(AnthropicProvider(), _ant_client(Refused())).complete(
            "p", "k", "m", usage=u)
    except Exception:
        raised = True
    check("거절은 예외로 전달", raised)
    check("거절이어도 토큰 기록", u.summary()["total"] == 33, u.summary())


# ──────────────────────────────────────────────
# 4. 전북대 게이트웨이 크레딧 (토큰 대신 크레딧으로 과금)
# ──────────────────────────────────────────────
def test_credits():
    print("[크레딧]")
    from providers.usage import credits_for_history, credits_result
    from providers.jbnu_gateway import JbnuGatewayProvider

    # 문서상 응답 형태 (quota/used/remaining, 월별에는 renewal_date)
    api_resp = {
        "object": "credit_balance",
        "monthly_allocated": {"quota": 10000.0, "used": 1234.5,
                              "remaining": 8765.5, "renewal_date": "2026-08-01"},
        "purchased": {"quota": 5000.0, "used": 500.0, "remaining": 4500.0},
        "total":     {"quota": 15000.0, "used": 1734.5, "remaining": 13265.5},
    }
    shaped = JbnuGatewayProvider._shape_credits(api_resp)
    check("세 섹션 순서 유지",
          [s["key"] for s in shaped["sections"]]
          == ["monthly_allocated", "purchased", "total"],
          [s["key"] for s in shaped["sections"]])
    check("total 파싱", shaped["total"]["remaining"] == 13265.5, shaped["total"])
    check("renewal_date 보존",
          shaped["monthly_allocated"]["renewal_date"] == "2026-08-01")
    check("supports_credits 선언", JbnuGatewayProvider.supports_credits is True)

    # 필드가 빠지거나 형식이 달라도 죽지 않고 None으로 떨어져야 한다
    broken = JbnuGatewayProvider._shape_credits({"total": {"quota": "많음"}})
    check("이상한 값은 None으로", broken["total"]["quota"] is None, broken["total"])
    check("섹션 누락도 견딤", broken["purchased"]["used"] is None)

    # 이번 생성에 쓴 크레딧 = 후 used - 전 used
    before = JbnuGatewayProvider._shape_credits(api_resp)
    after_resp = dict(api_resp, total={"quota": 15000.0, "used": 1750.0,
                                       "remaining": 13250.0})
    after = JbnuGatewayProvider._shape_credits(after_resp)

    r = credits_result(before, after)
    check("사용분 = 전후 차이", r["spent"] == 15.5, r["spent"])
    check("남은 크레딧은 조회 후 값", r["remaining"] == 13250.0, r["remaining"])
    check("사용분 계산됨 표시", r["spent_known"] is True)

    # 생성 전 잔액을 못 읽은 경우 → 사용분은 모르지만 잔액은 보여준다
    r = credits_result(None, after)
    check("before 없으면 spent 미상", r["spent"] is None and r["spent_known"] is False, r)
    check("before 없어도 remaining 제공", r["remaining"] == 13250.0)

    # 월 갱신으로 카운터가 초기화되면 음수가 나온다 → 음수를 보여주지 않는다
    reset = JbnuGatewayProvider._shape_credits(
        dict(api_resp, total={"quota": 15000.0, "used": 0.0, "remaining": 15000.0}))
    r = credits_result(before, reset)
    check("카운터 초기화 시 음수 안 보여줌",
          r["spent"] is None and r["spent_known"] is False, r)

    # 조회 후 값이 아예 없으면 표시할 게 없다
    check("after 없으면 None", credits_result(before, None) is None)

    # 보관용 — 잔액은 시간이 지나면 틀린 값이라 '쓴 만큼'만 남긴다
    keep = credits_for_history(credits_result(before, after))
    check("보관용은 쓴 크레딧만", keep == {"spent": 15.5, "spent_known": True}, keep)
    check("보관용에 잔액 없음", "remaining" not in keep and "sections" not in keep, keep)
    check("사용분을 모르면 보관 안 함",
          credits_for_history(credits_result(None, after)) is None)
    check("크레딧 자체가 없으면 보관 안 함", credits_for_history(None) is None)

    # 크레딧을 지원하지 않는 제공사는 기본 구현이 막아준다
    check("미지원 제공사는 supports_credits=False",
          OpenAIProvider.supports_credits is False)
    raised = False
    try:
        OpenAIProvider().get_credits("k")
    except NotImplementedError:
        raised = True
    check("미지원 제공사 get_credits는 NotImplementedError", raised)


if __name__ == "__main__":
    test_collector()
    test_openai_compatible()
    test_anthropic()
    test_credits()
    print()
    if _failures:
        print(f"실패 {len(_failures)}건: " + ", ".join(_failures))
        sys.exit(1)
    print("전부 통과")
