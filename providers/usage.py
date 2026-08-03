"""
LLM 토큰 사용량 수집기.

한 번의 "생성하기"에 LLM 호출이 10~20번 일어난다(이미지 설명 N회 + 개념 분석 2회
+ 형식 분석 2회 + 문제 생성 배치 N회). 그중 이미지 설명과 개념 분석은
ThreadPoolExecutor에서 병렬로 돌기 때문에, 누적은 Lock으로 보호한다.

프로바이더마다 usage 필드 이름이 달라서 여기서 input/output으로 정규화한다.
  - OpenAI 호환: prompt_tokens / completion_tokens
  - Anthropic  : input_tokens  / output_tokens

중요 — "0 토큰"과 "제공사가 안 알려줬음"은 다르다. 중계 서버가 usage를 떼고
보내는 경우가 있어서, 못 읽은 호출은 따로 세어 화면에서 구분해 보여준다.
(0으로 합쳐버리면 실제로 쓴 토큰을 안 쓴 것처럼 보여주게 된다)
"""

import threading

# 화면에 보여줄 단계 이름 — 키는 SSE stage 키와 같은 값을 쓴다.
# 표시 순서도 이 순서를 따른다.
STAGE_LABELS = {
    "extract":  "이미지 설명",
    "concepts": "개념 분석",
    "format":   "형식 분석",
    "generate": "문제 생성",
}


def _read_int(obj, *names):
    """여러 표기 중 먼저 발견되는 정수 필드를 읽는다."""
    for n in names:
        v = getattr(obj, n, None)
        if isinstance(v, int):
            return v
    return None


def _normalize(raw):
    """SDK usage 객체에서 (input, output)을 꺼낸다. 못 꺼내면 None."""
    if raw is None:
        return None
    inp = _read_int(raw, "prompt_tokens", "input_tokens")
    out = _read_int(raw, "completion_tokens", "output_tokens")
    if inp is None and out is None:
        return None
    return (inp or 0, out or 0)


class UsageCollector:
    """생성 한 번 동안의 토큰 사용량을 모은다. 프로바이더가 add()로 기록한다."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stage = ""
        self._rows = []          # 제공사가 usage를 알려준 호출
        self._unreported = []    # 알려주지 않은 호출 (호출 수만 셈)

    def set_stage(self, stage: str):
        """
        이후 기록될 호출이 어느 단계인지 지정한다.

        run_generation_events()의 단계는 순차적으로만 진행되므로(한 단계가 끝난 뒤
        다음 단계 시작) 값 하나를 공유해도 단계가 섞이지 않는다. 한 단계 안에서
        병렬로 나가는 호출들은 모두 같은 단계라 문제되지 않는다.
        """
        with self._lock:
            self._stage = stage

    def add(self, model: str, raw):
        """프로바이더가 호출 직후 부른다. raw는 SDK usage 객체(없으면 None)."""
        pair = _normalize(raw)
        with self._lock:
            stage = self._stage
            if pair is None:
                self._unreported.append({"stage": stage, "model": model or ""})
            else:
                self._rows.append({"stage": stage, "model": model or "",
                                   "input": pair[0], "output": pair[1]})

    def _group(self, rows, unreported, key):
        """key(stage 또는 model)별로 합친다. 미보고 호출은 호출 수에만 더한다."""
        out = {}
        for r in rows:
            d = out.setdefault(r[key], {"calls": 0, "input": 0, "output": 0,
                                        "unreported_calls": 0})
            d["calls"] += 1
            d["input"] += r["input"]
            d["output"] += r["output"]
        for u in unreported:
            d = out.setdefault(u[key], {"calls": 0, "input": 0, "output": 0,
                                        "unreported_calls": 0})
            d["calls"] += 1
            d["unreported_calls"] += 1
        for d in out.values():
            d["total"] = d["input"] + d["output"]
        return out

    def summary(self) -> dict:
        """화면에 뿌릴 형태로 집계. 호출이 없으면 calls=0."""
        with self._lock:
            rows = list(self._rows)
            unreported = list(self._unreported)

        by_stage_map = self._group(rows, unreported, "stage")
        by_model_map = self._group(rows, unreported, "model")

        # 알려진 단계를 정의 순서대로 먼저, 모르는 키는 뒤에 붙인다
        ordered = [k for k in STAGE_LABELS if k in by_stage_map]
        ordered += [k for k in by_stage_map if k not in STAGE_LABELS]
        by_stage = [
            {"key": k, "label": STAGE_LABELS.get(k, k or "기타"), **by_stage_map[k]}
            for k in ordered
        ]
        by_model = [
            {"model": m, **by_model_map[m]}
            for m in sorted(by_model_map, key=lambda x: -by_model_map[x]["total"])
        ]

        return {
            "calls":            len(rows) + len(unreported),
            "unreported_calls": len(unreported),
            "input":            sum(r["input"] for r in rows),
            "output":           sum(r["output"] for r in rows),
            "total":            sum(r["input"] + r["output"] for r in rows),
            "by_stage":         by_stage,
            "by_model":         by_model,
        }
