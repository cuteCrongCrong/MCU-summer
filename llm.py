"""
LLM 도메인 로직 — PDF 텍스트/이미지 추출, 프롬프트 설계, 응답 파싱, 분석 파이프라인.

실제 LLM 호출은 providers/ 계층에 위임한다. LLM 호출 함수는 provider 인자를
받으며, 생략하면 기본 프로바이더(전북대 게이트웨이)를 쓴다.
라우트(HTTP)와 분리되어 있어 단위 테스트·재사용이 쉽습니다.
"""

import json
import re
from concurrent.futures import ThreadPoolExecutor
import fitz  # pymupdf

from providers.factory import get_provider

# ──────────────────────────────────────────────
# 설정 상수
# ──────────────────────────────────────────────
# provider 인자가 생략됐을 때 쓰는 기본 프로바이더 (전북대 게이트웨이)
_default_provider = get_provider()

# 강의/기출 텍스트를 LLM에 넣기 전 문서당 최대 글자 수.
MAX_TEXT_CHARS = 100000

# 이미지/스캔 페이지 → Vision LLM으로 '무슨 이미지인지' 설명 생성 (토큰 비용 상한용)
IMAGE_DESC_MAX = 15          # 설명할 이미지 페이지 최대 개수
SPARSE_TEXT_THRESHOLD = 20   # 페이지 텍스트가 이보다 짧으면 이미지 페이지로 간주
IMAGE_RENDER_DPI = 150       # 페이지 렌더 해상도

# 문제 유형 4분류 (집계·few-shot·생성에서 공통 사용)
QUESTION_TYPES = ["객관식", "빈칸채우기", "단답형", "서술형"]
# 유형별 정의 (프롬프트에 주입 — 분류 기준 통일)
TYPE_DEFINITIONS = (
    "- 객관식: 선택지(①②③④⑤ 등) 중에서 답을 고르는 문제\n"
    "- 빈칸채우기: 문장 속 빈칸(____, 괄호 등)에 들어갈 말을 채우는 문제\n"
    "- 단답형: 선택지 없이 단어·구·수치 등 짧은 답을 쓰는 문제\n"
    "- 서술형: 여러 문장으로 설명·기술·논술하는 문제"
)


# ──────────────────────────────────────────────
# LLM / PDF 유틸
# ──────────────────────────────────────────────

def extract_text_from_pdf(file_storage, api_key: str = None, model: str = None,
                          describe_images: bool = True, provider=None) -> str:
    """
    PDF에서 텍스트 레이어를 추출하고, 이미지/스캔 페이지는 Vision LLM으로
    '무슨 이미지인지' 설명을 생성해 텍스트로 함께 남긴다.
    - 텍스트가 거의 없는 페이지(스캔·사진) 또는 그림이 포함된 페이지를 이미지 설명 대상으로 선정
    - 비용 상한: 최대 IMAGE_DESC_MAX개 페이지만 설명, 이미지 설명은 병렬 처리
    """
    prov = provider or _default_provider
    pdf_bytes = file_storage.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    pages = []       # {idx, text, png(optional), desc}
    img_jobs = []    # 설명 대상 페이지 (엔트리 참조)
    for i, page in enumerate(doc):
        text = page.get_text()
        entry = {"idx": i, "text": text if text.strip() else "", "desc": None}
        want_img = (
            describe_images and api_key
            and len(img_jobs) < IMAGE_DESC_MAX
            and (len(text.strip()) < SPARSE_TEXT_THRESHOLD or bool(page.get_images()))
        )
        if want_img:
            try:
                pix = page.get_pixmap(dpi=IMAGE_RENDER_DPI)
                entry["png"] = pix.tobytes("png")
                img_jobs.append(entry)
            except Exception:
                pass
        pages.append(entry)
    doc.close()

    # 이미지 설명 병렬 생성 (게이트웨이가 이미지 미지원이면 폴백 문구)
    if img_jobs:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(prov.describe_image, e["png"], api_key, model): e for e in img_jobs}
            for fut, e in futs.items():
                try:
                    e["desc"] = fut.result()
                except Exception:
                    e["desc"] = "(이미지 설명 생성 실패 — 게이트웨이 이미지 미지원일 수 있음)"

    parts = []
    for e in pages:
        block = []
        if e["text"]:
            block.append(f"[페이지 {e['idx']+1}]\n{e['text']}")
        if e.get("desc"):
            block.append(f"[페이지 {e['idx']+1} 이미지 설명]\n{e['desc']}")
        if block:
            parts.append("\n".join(block))

    if len(img_jobs) >= IMAGE_DESC_MAX:
        parts.append(f"...(이미지 설명은 최대 {IMAGE_DESC_MAX}개까지만 생성됨)")

    return "\n\n".join(parts)


def truncate(text: str, max_chars: int = MAX_TEXT_CHARS) -> str:
    """
    상한 초과 시 앞부분만 남기던 방식 → 앞(70%)+뒤(30%) 보존.
    기출 뒤쪽 문제·강의 마무리 내용이 통째로 사라지지 않도록 함.
    """
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return text[:head] + "\n\n...(중략: 분량 초과로 일부 생략)...\n\n" + text[-tail:]


def build_source_info(raw_text: str) -> dict:
    """원문 추출 글자수 대비 LLM에 실제 반영된 범위 (truncate 여부·비율)."""
    chars = len(raw_text or "")
    truncated = chars > MAX_TEXT_CHARS
    used = MAX_TEXT_CHARS if truncated else chars
    return {
        "chars": chars,
        "used": used,
        "limit": MAX_TEXT_CHARS,
        "truncated": truncated,
        "coverage": (round(used / chars * 100) if chars else 100),
    }


def call_llm(prompt: str, api_key: str, model: str, provider=None,
             max_tokens: int = None) -> str:
    # provider 계층으로 위임 — max_tokens를 주면 프로바이더 기본값 대신 그 값을 쓴다
    # (문제 생성처럼 응답이 길어 잘림을 막아야 할 때 호출부가 넉넉히 지정).
    return (provider or _default_provider).complete(
        prompt, api_key, model, max_tokens=max_tokens
    )


# ──────────────────────────────────────────────
# 기출 유형 통계 (정규식 — LLM 미사용, 0 토큰)
# ──────────────────────────────────────────────

def normalize_type_stats(raw: dict) -> dict:
    """
    LLM/정규식이 준 유형 카운트를 4분류 표준 형태로 정규화.
    키 표기 흔들림(예: '빈칸 채우기' → '빈칸채우기')을 흡수. 반환에 '총문항' 포함.
    """
    stats = {t: 0 for t in QUESTION_TYPES}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).replace(" ", "")
            if key in stats:
                try:
                    stats[key] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
    stats["총문항"] = sum(stats[t] for t in QUESTION_TYPES)
    return stats


def count_question_types(exam_text: str) -> dict:
    """
    정규식 폴백 집계 (LLM 4분류가 없을 때만 사용, 0 토큰).
    - 객관식: 줄머리 '①' 개수 (문항당 1개 → 안정적). '정답: ①' 표기는 제외.
    - 전체 문항: 줄머리 문제번호 패턴
    - 비객관식은 4분류를 정규식으로 가릴 수 없어 '단답형'으로 일괄 분류(부정확)
    """
    text = exam_text or ""
    objective = len(re.findall(r"(?m)^\s*①", text))
    markers = re.findall(r"(?m)^\s*(?:문제\s*)?\d{1,3}\s*[.)\]]", text)
    total = max(len(markers), objective)
    non_obj = max(0, total - objective)

    stats = {"객관식": objective, "빈칸채우기": 0, "단답형": non_obj, "서술형": 0}
    stats["총문항"] = total
    if total == 0:
        stats["판별근거"] = "문항 구분 실패 — 통계 신뢰 낮음"
    elif non_obj:
        stats["판별근거"] = "정규식 추정 — 비객관식은 단답형으로 일괄 분류(부정확)"
    else:
        stats["판별근거"] = "정규식 추정 (객관식만)"
    return stats


def resolve_type_stats(exam_concepts: dict, exam_text: str) -> dict:
    """LLM 4분류(exam_concepts.유형통계) 우선, 없으면 정규식 폴백."""
    stats = normalize_type_stats((exam_concepts or {}).get("유형통계"))
    if stats["총문항"] > 0:
        stats["판별근거"] = "LLM 유형 분류"
        return stats
    return count_question_types(exam_text)


def compute_type_targets(type_stats: dict, count: int) -> dict:
    """
    기출 유형 통계 비율을 생성 문제 수(count)에 그대로 투영 (4분류).
    최대잉여법(largest remainder)으로 합이 정확히 count가 되게 배분.
    통계가 없으면 빈 dict(→ 기존 예시 비율 유지).
    """
    counts = {t: max(0, int((type_stats or {}).get(t, 0) or 0)) for t in QUESTION_TYPES}
    total = sum(counts.values())
    if total == 0:
        return {}

    raw = {t: count * counts[t] / total for t in QUESTION_TYPES}
    targets = {t: int(raw[t]) for t in QUESTION_TYPES}          # 내림
    remainder = count - sum(targets.values())
    # 남은 몫은 소수부가 큰 유형부터 1개씩 (소수부 0인 유형은 사실상 제외됨)
    order = sorted(QUESTION_TYPES, key=lambda t: raw[t] - targets[t], reverse=True)
    for i in range(remainder):
        targets[order[i % len(order)]] += 1
    return targets


# ──────────────────────────────────────────────
# 기출문제 예시 추출 (Few-shot용)
# ──────────────────────────────────────────────

def extract_sample_questions(exam_text: str, api_key: str, model: str,
                             type_stats: dict = None, provider=None) -> str:
    """
    기출 PDF에서 **대표성 있는** 문제를 최대 5개까지 원문 그대로 추출 (4분류).
    선택 기준:
    - 존재하는 각 유형(객관식/빈칸채우기/단답형/서술형)마다 최소 1개씩 포함
    - 총 최대 5개
    - 앞쪽 순서가 아니라, 그 시험에서 '가장 전형적·대표적'인 문제를 판단해 선택
    """
    stats_hint = ""
    if type_stats and type_stats.get("총문항"):
        present = ", ".join(
            f"{t} 약 {type_stats.get(t, 0)}문항"
            for t in QUESTION_TYPES if type_stats.get(t, 0)
        )
        if present:
            stats_hint = f"\n참고 — 이 기출의 대략적 유형 구성: {present}.\n"

    prompt = f"""아래는 의대 기출문제 PDF에서 추출한 텍스트입니다.
이 텍스트에서 **그 시험을 가장 잘 대표하는** 완전한 문제를 골라 원문 그대로 추출해주세요.
{stats_hint}
## 문제 유형 4분류 (먼저 각 문제의 유형을 판별)
{TYPE_DEFINITIONS}

## 선택 기준 (중요)
- **총 최대 5개**까지 선택.
- 텍스트에 존재하는 **각 유형마다 최소 1개씩** 반드시 포함하세요.
  (해당 유형이 하나도 없으면 생략, 있으면 최소 1개 보장.)
- 5개 한도 안에서, 문항이 많은 유형은 1개보다 많이 넣어 실제 비중을 반영해도 좋습니다.
- **앞에서부터 순서대로 고르지 말고**, 그 시험에서 **전형적(대표적)**인 문제를 우선 선택하세요.
  특이하거나 예외적인 형식의 문제는 피하세요.

## 추출 규칙 (공통: 해설·부연설명 등 답이 아닌 내용은 절대 포함하지 말 것)
- [객관식] 문제 번호, 지문(증례), 선택지 ①②③④⑤, 정답만 원문 그대로.
- [빈칸채우기/단답형/서술형] 문제와 정답(모범답안)만. (선택지 없음)

## 출력 형식 (각 문제마다 — 유형 라벨은 4분류 중 하나 정확히)
[유형: 객관식] / [유형: 빈칸채우기] / [유형: 단답형] / [유형: 서술형]
(문제 원문 — 위 규칙대로)

기타 조건:
- 추출한 문제 사이는 빈 줄 2개로 구분
- 문제 외 다른 설명 텍스트(선택 이유 등)는 추가하지 말 것

## 기출문제 텍스트
{exam_text}"""

    return call_llm(prompt, api_key, model, provider)


def analyze_format(sample_questions: str, api_key: str, model: str, provider=None) -> str:
    """
    추출된 기출 예시를 분석해서 형식 규칙을 **키워드 위주**로 정리.
    프론트엔드에서 태그 형태로 렌더링하기 좋게 "라벨: 키워드1, 키워드2" 라인 형식.
    """
    prompt = f"""아래는 실제 기출문제 예시입니다.
이 문제들의 형식적 특징을 **키워드 위주로** 간결하게 정리하세요.
산문 설명 없이, 각 항목을 반드시 "라벨: 키워드1, 키워드2, ..." 한 줄 형태로만 작성하세요.

항목 (해당 없으면 '해당없음'):
문제유형: (객관식/빈칸채우기/단답형/서술형 비율 키워드)
증례제시: (나이/성별/주소/검사 제시 순서 키워드)
문체: (어투, 문장 길이, 전문용어 사용 키워드)
선택지구성: (객관식 선택지 길이·구조 키워드 / 비객관식이면 '해당없음')
질문형태: (진단/치료/다음단계 등 키워드)
기타: (특이사항 키워드)

## 기출문제 예시
{sample_questions}"""

    return call_llm(prompt, api_key, model, provider)


def extract_exam_concepts(exam_text: str, api_key: str, model: str, provider=None) -> str:
    """
    기출 전문에서 '무엇을 물었는가(출제 개념·경향)'를 추출.
    형식(how)이 아니라 내용(what)에 집중 → 강의개념 가중치 계산에 사용.
    + 유형통계(4분류)도 같은 호출에서 집계 (추가 토큰 최소).
    """
    prompt = f"""아래는 의대 기출문제 PDF에서 추출한 전문입니다.
이 기출에서 **실제로 출제된 핵심 개념·주제와 출제 경향**을 추출하고,
**각 문제의 유형을 아래 4분류로 세어** 함께 반환하세요.

## 문제 유형 4분류
{TYPE_DEFINITIONS}

다음 JSON 형식으로만 반환하세요 (코드블록 없이 JSON만):
{{
  "기출출제개념": ["출제된 개념/주제1", "개념/주제2"],
  "빈출포인트": ["반복 출제되거나 강조된 포인트1", "포인트2"],
  "유형통계": {{"객관식": 0, "빈칸채우기": 0, "단답형": 0, "서술형": 0}}
}}
- 유형통계는 기출 전체 문항을 4분류로 **빠짐없이** 센 개수입니다(합 = 전체 문항 수).

## 기출문제 텍스트
{exam_text}"""

    return call_llm(prompt, api_key, model, provider)


def _norm_term(s: str) -> str:
    """비교용 정규화: 괄호 이후 제거, 공백 제거, 소문자화."""
    return s.split("(")[0].replace(" ", "").lower()


def _terms_overlap(a: str, b: str) -> bool:
    """
    두 용어가 의미상 겹치는지 대략 판정.
    - 정규화 문자열 상호 부분 포함
    - 2글자+ 토큰이 상대 정규화 문자열에 부분 포함
      (한국어 조사 '심근경색' vs '심근경색의 …' 매칭을 위해 토큰-부분포함 사용)
    """
    na, nb = _norm_term(a), _norm_term(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    for t in re.findall(r"[가-힣A-Za-z]{2,}", a):
        if t.lower() in nb:
            return True
    for t in re.findall(r"[가-힣A-Za-z]{2,}", b):
        if t.lower() in na:
            return True
    return False


def compute_priority_topics(concepts: dict, exam_concepts: dict) -> list:
    """
    강의자료 개념 중 기출 출제개념·빈출포인트와 겹치는 주제를 '우선 출제 주제'로 선별.
    → 실제 시험 경향에 가까운 문제를 만들도록 가중치 부여.
    """
    exam_terms = (exam_concepts.get("기출출제개념", []) or []) + \
                 (exam_concepts.get("빈출포인트", []) or [])
    lecture_items = []
    for key in ["핵심질환", "핵심개념", "중요수치", "감별진단포인트", "치료원칙"]:
        lecture_items.extend(concepts.get(key, []) or [])

    priority, seen = [], set()
    for item in lecture_items:
        if not item or item in seen:
            continue
        if any(_terms_overlap(item, et) for et in exam_terms):
            seen.add(item)
            priority.append(item)
    return priority


# ──────────────────────────────────────────────
# 프롬프트 설계
# ──────────────────────────────────────────────

def build_concept_extraction_prompt(lecture_text: str) -> str:
    return f"""당신은 한국 의과대학 교육 전문가입니다.
아래 강의자료에서 핵심 정보를 추출하여 JSON 형식으로 반환하세요.

## 강의자료
{lecture_text}

## 추출 항목
다음 JSON 형식으로 정확하게 반환하세요 (코드블록 없이 JSON만):
{{
  "핵심질환": ["질환명1", "질환명2"],
  "핵심개념": ["개념1", "개념2"],
  "중요수치": ["수치1 (의미)", "수치2 (의미)"],
  "감별진단포인트": ["포인트1", "포인트2"],
  "치료원칙": ["원칙1", "원칙2"]
}}"""


def build_question_generation_prompt(
    concepts: dict,
    sample_questions: str,
    format_analysis: str,
    count: int,
    exam_concepts: dict = None,
    priority_topics: list = None,
    weight: int = 5,
    type_targets: dict = None,
) -> str:
    """
    핵심 변경: 기출 패턴 요약 대신
    ① 실제 기출 예시 (Few-shot)
    ② 형식 분석 결과
    ③ 기출 출제 경향 + 강의개념 가중 주제
    를 모두 프롬프트에 포함

    weight(1~10): 기출(개념·형식) 반영 강도. 사용자가 조절.
      - 높을수록 기출 출제개념·형식을 강하게 재현, 우선 주제 출제 비율↑
      - 낮을수록 강의자료 전반에서 자유롭게 출제, 형식은 느슨하게 참고
    """
    exam_concepts = exam_concepts or {}
    priority_topics = priority_topics or []
    type_targets = type_targets or {}

    # 가중치 정규화 및 파생 지표
    weight = max(1, min(10, int(weight)))
    # 우선 주제에서 출제할 최소 문제 수 (weight 비례)
    min_priority = max(0, min(count, round(count * weight / 10)))

    if weight >= 8:
        weight_guide = (
            "기출에서 다뤄진 개념과 문투·구조·선택지 형식을 **거의 그대로 재현**하세요. "
            "출제 내용도 기출 경향에 최대한 밀착시킵니다."
        )
    elif weight >= 4:
        weight_guide = (
            "기출 경향과 형식을 **균형 있게 반영**하되, 강의자료 개념도 폭넓게 활용하세요."
        )
    else:
        weight_guide = (
            "기출은 **참고만** 하고 강의자료 핵심 개념 위주로 폭넓게 출제하세요. "
            "형식은 큰 틀만 느슨하게 맞춥니다."
        )

    # 유형 구성 지시 (기출 유형 통계 → 생성 문제 수 배분, 4분류)
    if type_targets:
        parts = [f"{t} {type_targets.get(t, 0)}개"
                 for t in QUESTION_TYPES if type_targets.get(t, 0) > 0]
        type_rule = "**" + ", ".join(parts) + "**로 출제 (기출 유형 구성 비율 반영, 합계 준수)"
    else:
        type_rule = "**기출문제 예시에 나타난 유형 비율을 그대로 유지**할 것"

    weight_block = f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [0] 기출 반영 강도: {weight}/10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{weight_guide}
"""

    exam_trend_block = ""
    if exam_concepts.get("기출출제개념") or exam_concepts.get("빈출포인트"):
        exam_trend_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3-A] 기출 출제 경향 (실제 기출에서 물었던 내용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 기출 출제 개념: {', '.join(exam_concepts.get('기출출제개념', []))}
- 빈출 포인트: {', '.join(exam_concepts.get('빈출포인트', []))}
"""

    priority_block = ""
    if priority_topics:
        priority_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3-B] ⭐ 우선 출제 주제 (강의자료 ∩ 기출 경향 — 가중치 높음)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
아래 주제는 강의자료 핵심이면서 기출에서도 다뤄진 항목입니다.
**전체 {count}문제 중 최소 {min_priority}문제 이상을 이 주제에서 출제**하세요. (기출 반영 강도 {weight}/10 기준)
{chr(10).join('- ' + t for t in priority_topics)}
"""

    return f"""당신은 한국 의과대학 중간/기말고사 출제위원입니다.
아래 [기출문제 예시]의 형식과 **유형(4분류)**을 참고하여 새로운 예상 문제를 생성하세요.
단, 아래 [0] 기출 반영 강도에 따라 기출을 얼마나 강하게 반영할지 조절하세요.

## 문제 유형 4분류
{TYPE_DEFINITIONS}

{weight_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [1] 기출문제 예시 (형식·유형 참고용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{sample_questions}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [2] 형식 키워드 (반드시 준수)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{format_analysis}
{exam_trend_block}{priority_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3] 출제할 개념 (강의자료 기반)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 핵심 질환: {', '.join(concepts.get('핵심질환', []))}
- 핵심 개념: {', '.join(concepts.get('핵심개념', []))}
- 중요 수치: {', '.join(concepts.get('중요수치', []))}
- 감별 진단 포인트: {', '.join(concepts.get('감별진단포인트', []))}
- 치료 원칙: {', '.join(concepts.get('치료원칙', []))}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [4] 생성 규칙
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 문제 수: {count}개
- 유형 구성: {type_rule}
- 유형별 형식:
  · 객관식 → 선택지 ①②③④⑤ 포함
  · 빈칸채우기 → 문제 문장에 빈칸 '____'를 두고, 선택지 없이 빈칸에 들어갈 답 제시
    (빈칸이 2개 이상이면 각 정답을 문제에 등장하는 빈칸 순서대로 ' | '로 구분해
     한 줄에 나열. 예: 정답: NADH | FADH2  ※빈칸 1개면 구분자 없이 답만)
  · 단답형 → 선택지 없이 단어·구·수치로 답
  · 서술형 → 선택지 없이 여러 문장으로 서술하는 모범답안 제시
- 문체·구조·선택지 형식은 위 [0] 기출 반영 강도({weight}/10)에 맞춰 반영
- 기출문제와 내용이 동일한 문제는 출제 금지
- 각 문제에 해설과 함정포인트 포함

## 출력 형식 (마크다운 코드블록 사용 금지)

[객관식 문제일 때]
---QUESTION---
유형: 객관식
번호: 1
문제: [증례 포함 문제 전체]
선택지:
① [선택지1]
② [선택지2]
③ [선택지3]
④ [선택지4]
⑤ [선택지5]
정답: [번호 예: ③]
해설: [상세 해설]
함정포인트: [핵심 함정]
---END---

[빈칸채우기 — 빈칸이 2개 이상일 때]
---QUESTION---
유형: 빈칸채우기
번호: 2
문제: 해당당과정에서 ____(은)는 조효소로 작용하며, 최종 산물은 ____이다.
정답: NADH | 피루브산
해설: [상세 해설]
함정포인트: [핵심 함정]
---END---

[그 외 비객관식(빈칸채우기 1개/단답형/서술형)일 때 — 유형 라벨만 바꿔 사용]
---QUESTION---
유형: 단답형
번호: 3
문제: [문제 전체 — 빈칸채우기는 문장에 '____' 포함]
정답: [모범 답안]
해설: [상세 해설]
함정포인트: [핵심 함정]
---END---"""


# ──────────────────────────────────────────────
# 파싱 함수
# ──────────────────────────────────────────────

def parse_questions(raw_text: str) -> list:
    questions = []
    blocks = raw_text.split("---QUESTION---")
    for block in blocks:
        block = block.strip()
        if not block or "---END---" not in block:
            continue
        block = block.replace("---END---", "").strip()

        q = {}
        lines = block.split("\n")
        current_key = None
        choice_lines = []
        buffer = []

        def flush_buffer(key, buf):
            if key and buf:
                q[key] = "\n".join(buf).strip()

        for line in lines:
            line = line.strip()
            if line.startswith("유형:"):
                flush_buffer(current_key, buffer); buffer = []
                q["유형"] = line.replace("유형:", "").strip(); current_key = None
            elif line.startswith("번호:"):
                flush_buffer(current_key, buffer); buffer = []
                q["번호"] = line.replace("번호:", "").strip(); current_key = None
            elif line.startswith("문제:"):
                flush_buffer(current_key, buffer); buffer = []
                current_key = "문제"
                val = line.replace("문제:", "").strip()
                if val: buffer.append(val)
            elif line == "선택지:":
                flush_buffer(current_key, buffer); buffer = []
                current_key = "선택지"
            elif line.startswith(("①", "②", "③", "④", "⑤")):
                choice_lines.append(line)
            elif line.startswith("정답:"):
                flush_buffer(current_key, buffer); buffer = []
                q["정답"] = line.replace("정답:", "").strip(); current_key = None
            elif line.startswith("해설:"):
                flush_buffer(current_key, buffer); buffer = []
                current_key = "해설"
                val = line.replace("해설:", "").strip()
                if val: buffer.append(val)
            elif line.startswith("함정포인트:"):
                flush_buffer(current_key, buffer); buffer = []
                current_key = "함정포인트"
                val = line.replace("함정포인트:", "").strip()
                if val: buffer.append(val)
            else:
                if current_key: buffer.append(line)

        flush_buffer(current_key, buffer)
        if choice_lines:
            q["선택지"] = choice_lines
        q["유형"] = normalize_question_type(q.get("유형"), choice_lines, q.get("문제", ""))
        if q.get("문제"):
            questions.append(q)

    return questions


def normalize_question_type(raw_type: str, choice_lines: list, question_text: str) -> str:
    """
    생성 문제의 유형을 4분류 표준값으로 정규화.
    라벨이 있으면 표기 흔들림을 흡수, 없으면 선택지·빈칸 유무로 추정.
    """
    if raw_type:
        key = raw_type.replace(" ", "")
        for t in QUESTION_TYPES:
            if t in key:            # '객관식', '빈칸채우기', '단답형', '서술형' 부분일치
                return t
        if "객관" in key:
            return "객관식"
    if choice_lines:
        return "객관식"
    # 빈칸 기호(____, □, ( ))가 있으면 빈칸채우기로 추정, 아니면 단답형
    if re.search(r"_{2,}|□{2,}|\(\s*\)", question_text or ""):
        return "빈칸채우기"
    return "단답형"


def safe_parse_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception:
        pass
    return {}


# ──────────────────────────────────────────────
# 분석 파이프라인 (토큰 소모 집중 구간 — 세션에 저장해 재사용)
# ──────────────────────────────────────────────

def run_analysis(lecture_text: str, exam_text: str, api_key: str, model: str,
                 provider=None) -> dict:
    """
    강의·기출을 분석해 재사용 자산을 생성.
    의존성 없는 두 LLM 호출을 병렬 실행해 대기시간 단축 (결과·동작은 순차 실행과 동일):
      [동시] ① 강의 핵심개념  ┐
      [동시] ② 기출 개념+유형통계 ┘ → ③ 예시추출 → ④ 형식분석
    (④ 형식분석은 ③ 결과를 입력받으므로 ③에 의존 → 병렬 불가)
    """
    # ①·② 동시 실행 (서로 독립)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_concepts = ex.submit(
            call_llm, build_concept_extraction_prompt(lecture_text), api_key, model, provider
        )
        f_exam = ex.submit(extract_exam_concepts, exam_text, api_key, model, provider)
        concepts = safe_parse_json(f_concepts.result())
        exam_concepts = safe_parse_json(f_exam.result())

    # ③ 예시추출 (②의 유형통계를 힌트로 사용) → ④ 형식분석 (③에 의존)
    type_stats = resolve_type_stats(exam_concepts, exam_text)  # LLM 4분류 우선, 정규식 폴백
    sample_questions = extract_sample_questions(exam_text, api_key, model, type_stats, provider)
    format_analysis = analyze_format(sample_questions, api_key, model, provider)
    priority_topics = compute_priority_topics(concepts, exam_concepts)
    return {
        "concepts": concepts,
        "sample_questions": sample_questions,
        "format_analysis": format_analysis,
        "exam_concepts": exam_concepts,
        "priority_topics": priority_topics,
        "type_stats": type_stats,
    }
