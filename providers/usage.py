"""
한 번의 작업(문제 생성 / 주제 분석)에 무엇을 얼마나 썼는지 모으는 곳.

과금 방식이 두 갈래라 수집 방법도 둘이다.
  - 토큰 과금(대부분) → UsageCollector 가 호출마다 토큰을 누적한다
  - 크레딧 과금(전북대 게이트웨이) → 토큰 수가 소모와 연결되지 않으므로
    작업 전후 잔액을 찍어 그 차이를 쓴다 (credits_snapshot / credits_result)

토큰 수집 쪽 배경:
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

import statistics
import threading
import time
from contextlib import contextmanager

# 화면에 보여줄 단계 이름 — 키는 SSE stage 키와 같은 값을 쓴다.
# 표시 순서도 이 순서를 따른다. 문제 생성과 주제 분석이 한 표를 공유하는데,
# 두 기능이 겹쳐 도는 일이 없어서(각자 자기 요청 안에서만 산다) 섞이지 않는다.
STAGE_LABELS = {
    "extract":  "이미지 설명",
    "concepts": "개념 분석",
    "format":   "형식 분석",
    "generate": "문제 생성",
    "topics":   "주제 분석",      # 기출 주제 분석 (features/topic_analysis.py)
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


# ──────────────────────────────────────────────
# 구간 시간 계측 — "추출 단계 15분 중 렌더가 몇 초인가"
#
#   추출 단계는 성격이 다른 두 구간이 붙어 있는데(llm.read_pdf_pages의 렌더 →
#   llm.describe_images_progressively의 LLM 호출) SSE stage 키가 'extract' 하나라
#   로그로도 안 갈린다. 어느 쪽을 고쳐야 빨라지는지 알려면 따로 재야 한다.
# ──────────────────────────────────────────────

# 계측 구간 이름 → 로그·응답에 쓸 라벨. 표시 순서도 이 순서를 따른다.
TIMING_LABELS = {
    "text_layer":  "텍스트 레이어 추출",   # fitz get_text (LLM 없음, CPU)
    "render":      "PDF 렌더",             # get_pixmap + PNG 인코딩 (CPU, 직렬)
    "cache_probe": "캐시 키 계산",         # PNG 재읽기 + 해싱 (CPU·디스크, 직렬)
    "image_call":  "이미지 LLM 호출",      # Vision 호출 (네트워크, 병렬)
}

# 개별 작업 시간을 몇 개까지 들고 중앙값을 낼지. 한쪽 상한이 300쪽이라
# (config.IMAGE_CAP_LECTURE) 실사용에서는 안 닿지만, 상한을 크게 올린 배포에서
# 표본이 무한정 쌓이지 않게 막아둔다. 넘긴 만큼은 개수만 세고 버린다.
MAX_TIMING_SAMPLES = 4000


class PhaseTimer:
    """
    한 번의 작업 안에서 어느 구간에 시간이 갔는지 모은다.

    두 가지를 **따로** 재는 것이 요점이다:
      wall — 구간 전체의 벽시계 시간          (span)
      busy — 그 안 개별 작업 시간의 합        (measure / mark)

    렌더는 한 장씩 직렬이라 wall ≈ busy 다. 이미지 LLM 호출은 IMAGE_WORKERS개가
    병렬로 도니 busy > wall 이고, 그 비(busy/wall)가 곧 **실효 동시성**이다.
    워커를 12로 잡았는데 이 값이 3이면 워커가 아니라 다른 데서 막히고 있다는 뜻이라,
    워커를 더 올려도 소용없다는 것을 숫자 하나로 알 수 있다.

    wall과 busy의 차이도 정보다 — 렌더 구간에서 그 차이는 렌더가 아닌 일
    (텍스트 레이어 추출·후보 판정·파일 입출력)에 간 시간이다.

    스레드 안전 — mark()는 이미지 워커들이 동시에 부른다.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._wall = {}       # 구간 → 누적 벽시계 (span이 여러 번 불릴 수 있다)
        self._ops = {}        # 구간 → [개별 소요시간]
        self._dropped = {}    # 구간 → 표본 상한을 넘겨 버린 개수
        self._counts = {}     # 이름 → 순수 카운터 (시간 없는 값: 캐시 적중 등)

    @contextmanager
    def span(self, name: str):
        """구간 전체를 감싼다. 같은 이름으로 여러 번 감싸면 더해진다.

        (강의자료·기출을 따로 읽으므로 렌더 구간은 실제로 두 번 열린다)
        제너레이터를 감싼 채로 중지되면 GeneratorExit이 지나가는데, finally라
        그때까지의 시간은 남는다.
        """
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            with self._lock:
                self._wall[name] = self._wall.get(name, 0.0) + dt

    @contextmanager
    def measure(self, name: str):
        """개별 작업 하나를 감싼다. 예외가 나도 그때까지 쓴 시간은 기록한다
        (실패한 LLM 호출도 시간은 실제로 썼다)."""
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.mark(name, time.perf_counter() - t0)

    def mark(self, name: str, seconds: float):
        with self._lock:
            samples = self._ops.setdefault(name, [])
            if len(samples) < MAX_TIMING_SAMPLES:
                samples.append(seconds)
            else:
                self._dropped[name] = self._dropped.get(name, 0) + 1

    def count(self, name: str, n: int = 1):
        """시간이 없는 값 — 캐시 적중처럼 '일어난 횟수'만 의미 있는 것."""
        with self._lock:
            self._counts[name] = self._counts.get(name, 0) + n

    def summary(self) -> dict:
        """로그·응답에 실을 형태로 집계. 잰 게 없으면 phases가 빈 목록."""
        with self._lock:
            wall = dict(self._wall)
            ops = {k: list(v) for k, v in self._ops.items()}
            dropped = dict(self._dropped)
            counts = dict(self._counts)

        keys = set(wall) | set(ops)
        ordered = [k for k in TIMING_LABELS if k in keys]
        ordered += sorted(k for k in keys if k not in TIMING_LABELS)

        phases = []
        for k in ordered:
            s = ops.get(k, [])
            w = wall.get(k)
            busy = sum(s)
            row = {
                "key":     k,
                "label":   TIMING_LABELS.get(k, k),
                "wall":    round(w, 3) if w is not None else None,
                "busy":    round(busy, 3),
                # count는 실제 작업 수, sampled는 그중 시간을 들고 있는 수
                "count":   len(s) + dropped.get(k, 0),
                "sampled": len(s),
            }
            if s:
                row["median_ms"] = round(statistics.median(s) * 1000, 1)
                row["min_ms"]    = round(min(s) * 1000, 1)
                row["max_ms"]    = round(max(s) * 1000, 1)
            if w and w > 0:
                row["concurrency"] = round(busy / w, 2)
            phases.append(row)

        return {
            "phases":   phases,
            "counts":   counts,
            # 렌더와 LLM 호출을 겹쳤을 때 줄어드는 시간 = 둘 중 짧은 쪽.
            # 지금은 렌더가 **전부** 끝난 뒤에 호출이 시작되므로 두 벽시계가 그대로
            # 더해진다(features/question_gen.py의 추출 단계). 겹치면 max(둘)만 남는다.
            "overlap_saving": _overlap_saving(wall),
        }

    def report_lines(self, prefix: str = "") -> list:
        """서버 로그에 찍을 줄. systemd 배포에서는 journalctl로 보인다."""
        s = self.summary()
        head = f"[timing]{(' ' + prefix) if prefix else ''}"
        lines = []
        for p in s["phases"]:
            bits = [f"wall={_fmt_s(p['wall'])}", f"busy={_fmt_s(p['busy'])}",
                    f"n={p['count']}"]
            if "median_ms" in p:
                bits.append(f"median={p['median_ms']:.0f}ms")
            if "concurrency" in p:
                bits.append(f"conc={p['concurrency']:.2f}")
            lines.append(f"{head} {p['label']:<16s} " + " ".join(bits))
        for name, n in sorted(s["counts"].items()):
            lines.append(f"{head} {name}={n}")
        save = s["overlap_saving"]
        if save:
            lines.append(
                f"{head} 렌더·호출을 겹치면 최대 {_fmt_s(save['seconds'])} 절감 "
                f"({_fmt_s(save['now'])} → {_fmt_s(save['overlapped'])})"
            )
        return lines


def _fmt_s(v) -> str:
    if v is None:
        return "-"
    return f"{v:.1f}s" if v < 60 else f"{v/60:.1f}m"


def _overlap_saving(wall: dict):
    """렌더 구간과 이미지 호출 구간을 파이프라인으로 겹쳤을 때의 절감 추정.

    두 구간이 지금은 순차라 `렌더 + 호출`이지만, 한 장 렌더할 때마다 바로 호출 큐에
    넣으면 `max(렌더, 호출)`에 수렴한다. 그래서 절감 상한이 곧 **짧은 쪽 전체**다.
    상한인 이유 — 첫 장 렌더는 어차피 앞에 와야 하고, 렌더가 CPU를 잡는 동안
    호출 쪽 처리도 조금 느려지므로 실제 절감은 이보다 작다.
    """
    r = wall.get("render")
    c = wall.get("image_call")
    if not r or not c:
        return None
    return {
        "seconds":    round(min(r, c), 3),
        "now":        round(r + c, 3),
        "overlapped": round(max(r, c), 3),
    }


class _NullTimer:
    """계측기를 안 넘긴 호출부(테스트·주제 분석)에서 쓰는 무동작 타이머.

    호출부마다 `if timing:` 을 두지 않으려고 둔다 — 계측 코드가 본 로직 사이에
    끼면 읽기 나빠지는데, 이 계측은 어디까지나 곁다리다.
    """

    @contextmanager
    def span(self, name: str):
        yield

    @contextmanager
    def measure(self, name: str):
        yield

    def mark(self, name: str, seconds: float):
        pass

    def count(self, name: str, n: int = 1):
        pass


NO_TIMING = _NullTimer()


# ──────────────────────────────────────────────
# 크레딧 과금 제공사 (전북대 게이트웨이)
#   토큰이 아니라 크레딧으로 과금하므로 토큰 수를 보여줘도 실제 소모와 연결되지
#   않는다. 대신 작업 전후 잔액을 찍어 그 차이를 이번 사용분으로 쓴다.
#   문제 생성·주제 분석이 같은 방식으로 쓰므로 여기(공용)에 둔다.
# ──────────────────────────────────────────────

def credits_snapshot(provider, api_key):
    """잔액 조회. 실패해도 작업을 막아선 안 되므로 예외를 삼키고 None을 준다."""
    if not getattr(provider, "supports_credits", False):
        return None
    try:
        return provider.get_credits(api_key)
    except Exception:
        return None


def credits_result(before, after):
    """이번 작업에 쓴 크레딧 = (작업 후 누적 사용) - (작업 전 누적 사용)."""
    if not after:
        return None
    a_total = after.get("total") or {}
    spent = None
    if before:
        b_used = (before.get("total") or {}).get("used")
        a_used = a_total.get("used")
        if b_used is not None and a_used is not None:
            spent = round(a_used - b_used, 6)      # float 오차 정리
            # 월 갱신으로 카운터가 초기화되면 음수가 나온다 → 모르는 것으로 처리
            if spent < 0:
                spent = None
    return {
        "spent":        spent,
        "remaining":    a_total.get("remaining"),
        "quota":        a_total.get("quota"),
        "used":         a_total.get("used"),
        "sections":     after.get("sections", []),
        "renewal_date": (after.get("monthly_allocated") or {}).get("renewal_date"),
        # 작업 전 잔액을 못 읽었으면 이번 사용분을 계산할 수 없다 (화면에서 구분)
        "spent_known":  spent is not None,
    }


def credits_for_history(credits):
    """
    보관용으로 남길 크레딧 정보만 추린다.

    잔액·할당·누적 사용은 '조회한 그 순간'의 값이라, 나중에 꺼내 보면 이미 틀린
    숫자다. 그대로 저장하면 지난 기록을 열 때마다 옛날 잔액을 지금 잔액처럼
    보여주게 된다. 반면 '이번에 쓴 크레딧'은 시간이 지나도 사실이라 남길 수 있다.
    남길 게 없으면(사용분을 계산 못 했으면) None — 저장하지 않는다.
    """
    if not credits or not credits.get("spent_known"):
        return None
    return {"spent": credits.get("spent"), "spent_known": True}
