"""
의대 예상문제 생성기 — Flask 백엔드 (전북대 LLM 플랫폼)
실행: python app.py  /  py app.py
접속: http://localhost:5000
필요 패키지: pip install flask pymupdf openai
"""

import json
import re
import os
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify, send_from_directory
import fitz  # pymupdf
from openai import OpenAI, AuthenticationError, RateLimitError

app = Flask(__name__, static_folder=".")

GATEWAY_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"
DEFAULT_MODEL    = "claude-sonnet-4-5"

_BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
DB_PATH = os.path.join(_BASE_DIR, "sessions.db")


# ──────────────────────────────────────────────
# 세션 저장 (SQLite) — 분석 결과 캐싱 → 재분석 없이 재생성
# ──────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                model TEXT,
                created_at TEXT NOT NULL,
                concepts TEXT,
                sample_questions TEXT,
                format_analysis TEXT,
                exam_concepts TEXT,
                priority_topics TEXT,
                type_stats TEXT
            )"""
        )
        conn.commit()
    finally:
        conn.close()


def save_session(name: str, model: str, analysis: dict) -> int:
    """분석 결과(재사용 자산)를 한 세션으로 저장하고 id 반환."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO sessions
               (name, model, created_at, concepts, sample_questions,
                format_analysis, exam_concepts, priority_topics, type_stats)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                name,
                model,
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                json.dumps(analysis.get("concepts", {}), ensure_ascii=False),
                analysis.get("sample_questions", ""),
                analysis.get("format_analysis", ""),
                json.dumps(analysis.get("exam_concepts", {}), ensure_ascii=False),
                json.dumps(analysis.get("priority_topics", []), ensure_ascii=False),
                json.dumps(analysis.get("type_stats", {}), ensure_ascii=False),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def load_session(sid: int):
    """세션 하나를 분석 자산 dict로 복원. 없으면 None."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "model": row["model"],
        "created_at": row["created_at"],
        "concepts": json.loads(row["concepts"] or "{}"),
        "sample_questions": row["sample_questions"] or "",
        "format_analysis": row["format_analysis"] or "",
        "exam_concepts": json.loads(row["exam_concepts"] or "{}"),
        "priority_topics": json.loads(row["priority_topics"] or "[]"),
        "type_stats": json.loads(row["type_stats"] or "{}"),
    }


def list_sessions() -> list:
    """세션 목록(가벼운 메타데이터만). 최신순."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, name, model, created_at, type_stats FROM sessions ORDER BY id DESC"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "model": r["model"],
            "created_at": r["created_at"],
            "type_stats": json.loads(r["type_stats"] or "{}"),
        }
        for r in rows
    ]


def delete_session(sid: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
        conn.commit()
    finally:
        conn.close()


def rename_session(sid: int, name: str):
    conn = get_conn()
    try:
        conn.execute("UPDATE sessions SET name=? WHERE id=?", (name, sid))
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 유틸 함수
# ──────────────────────────────────────────────

def extract_text_from_pdf(file_storage) -> str:
    pdf_bytes = file_storage.read()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    pages_text = []
    for i, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            pages_text.append(f"[페이지 {i+1}]\n{text}")
    doc.close()
    return "\n\n".join(pages_text)


def truncate(text: str, max_chars: int = 8000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n...(이하 생략)"


def call_llm(prompt: str, api_key: str, model: str) -> str:
    client = OpenAI(api_key=api_key, base_url=GATEWAY_BASE_URL)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=4096,
    )
    return response.choices[0].message.content


# ──────────────────────────────────────────────
# 기출 유형 통계 (정규식 — LLM 미사용, 0 토큰)
# ──────────────────────────────────────────────

def count_question_types(exam_text: str) -> dict:
    """
    기출 전문을 정규식으로 스캔해 객관식/주관식 문항 수를 집계 (LLM 호출 없음 → 0 토큰).
    - 객관식 수: 첫 선택지 기호 '①' 개수 (문항당 정확히 1개 → 가장 안정적인 신호)
    - 전체 문항 수: 줄머리 문제번호 패턴('1.', '문제 2)', '[3]' 등)
    - 주관식 수: 전체 − 객관식 (음수 방지)
    반환: {"객관식", "주관식", "총문항", "판별근거"}
    """
    text = exam_text or ""
    # 객관식: 문항마다 반드시 등장하는 첫 선택지 기호 '①' 개수.
    # 줄머리 '①'만 집계 → 정답 표기('정답: ①')의 ①은 제외.
    objective = len(re.findall(r"(?m)^\s*①", text))
    # 전체 문항: 줄머리 문제번호 (선택지 번호 '1)'와의 혼동을 줄이려 줄머리로 한정)
    markers = re.findall(r"(?m)^\s*(?:문제\s*)?\d{1,3}\s*[.)\]]", text)
    total = max(len(markers), objective)
    subjective = max(0, total - objective)

    if total == 0:
        basis = "문항 구분 실패 — 통계 신뢰 낮음"
    elif len(markers) == 0:
        basis = "선택지 기호(①) 기준 집계 — 문제번호 미검출"
    else:
        basis = "문제번호·선택지 기호 기준 집계 (근사치)"

    return {
        "객관식": objective,
        "주관식": subjective,
        "총문항": total,
        "판별근거": basis,
    }


def compute_type_targets(type_stats: dict, count: int) -> dict:
    """
    기출 유형 통계 비율을 생성 문제 수(count)에 그대로 투영해
    '객관식 몇 개 / 주관식 몇 개'를 산출. 통계가 없으면 빈 dict(→ 기존 예시 비율 유지).
    """
    obj = max(0, int((type_stats or {}).get("객관식", 0) or 0))
    subj = max(0, int((type_stats or {}).get("주관식", 0) or 0))
    total = obj + subj
    if total == 0:
        return {}
    obj_n = max(0, min(count, round(count * obj / total)))
    return {"객관식": obj_n, "주관식": count - obj_n}


# ──────────────────────────────────────────────
# 기출문제 예시 추출 (Few-shot용)
# ──────────────────────────────────────────────

def extract_sample_questions(exam_text: str, api_key: str, model: str,
                             type_stats: dict = None) -> str:
    """
    기출 PDF에서 **대표성 있는** 문제를 최대 5개까지 원문 그대로 추출.
    선택 기준:
    - 존재하는 각 유형(객관식/주관식)마다 최소 2개씩 포함 (해당 유형이 2개 이상 있을 때)
    - 총 최대 5개
    - 앞쪽 순서가 아니라, 그 시험에서 '가장 전형적·대표적'인 문제를 판단해 선택
    유형별 추출 규칙:
    - 객관식: 문제 + 선택지 + 정답
    - 주관식: 문제 + 정답
    """
    stats_hint = ""
    if type_stats and type_stats.get("총문항"):
        stats_hint = (
            f"\n참고 — 이 기출의 대략적 유형 구성: "
            f"객관식 약 {type_stats.get('객관식', 0)}문항, "
            f"주관식 약 {type_stats.get('주관식', 0)}문항.\n"
        )

    prompt = f"""아래는 의대 기출문제 PDF에서 추출한 텍스트입니다.
이 텍스트에서 **그 시험을 가장 잘 대표하는** 완전한 문제를 골라 원문 그대로 추출해주세요.
{stats_hint}
먼저 각 문제의 유형을 판별하세요:
- 객관식: 선택지(①②③④⑤ 등)가 있는 문제
- 주관식: 선택지 없이 서술·단답으로 답하는 문제

## 선택 기준 (중요)
- **총 최대 5개**까지 선택.
- 텍스트에 **두 유형이 모두 있으면, 각 유형마다 최소 2개씩** 포함하세요.
  (단, 해당 유형 문항이 2개 미만이면 있는 만큼만.)
- 한 유형만 있으면 그 유형에서 대표성 있는 문제를 최대 5개까지.
- **앞에서부터 순서대로 고르지 말고**, 형식·출제 방식이 그 시험에서 **전형적(대표적)**인
  문제를 우선 선택하세요. 특이하거나 예외적인 형식의 문제는 피하세요.

## 유형별 추출 규칙 (공통: 해설·부연설명 등 답이 아닌 내용은 절대 포함하지 말 것)
- [객관식] 문제 번호, 지문(증례), 선택지 ①②③④⑤, 정답만 원문 그대로 추출.
- [주관식] 문제와 정답(모범답안)만 추출.

## 출력 형식 (각 문제마다)
[유형: 객관식] 또는 [유형: 주관식]
(문제 원문 — 위 규칙대로)

기타 조건:
- 추출한 문제 사이는 빈 줄 2개로 구분
- 문제 외 다른 설명 텍스트(선택 이유 등)는 추가하지 말 것

## 기출문제 텍스트
{exam_text}"""

    return call_llm(prompt, api_key, model)


def analyze_format(sample_questions: str, api_key: str, model: str) -> str:
    """
    추출된 기출 예시를 분석해서 형식 규칙을 **키워드 위주**로 정리.
    프론트엔드에서 태그 형태로 렌더링하기 좋게 "라벨: 키워드1, 키워드2" 라인 형식.
    """
    prompt = f"""아래는 실제 기출문제 예시입니다.
이 문제들의 형식적 특징을 **키워드 위주로** 간결하게 정리하세요.
산문 설명 없이, 각 항목을 반드시 "라벨: 키워드1, 키워드2, ..." 한 줄 형태로만 작성하세요.

항목 (해당 없으면 '해당없음'):
문제유형: (객관식/주관식 비율 키워드)
증례제시: (나이/성별/주소/검사 제시 순서 키워드)
문체: (어투, 문장 길이, 전문용어 사용 키워드)
선택지구성: (객관식 선택지 길이·구조 키워드 / 주관식이면 '해당없음')
질문형태: (진단/치료/다음단계 등 키워드)
기타: (특이사항 키워드)

## 기출문제 예시
{sample_questions}"""

    return call_llm(prompt, api_key, model)


def extract_exam_concepts(exam_text: str, api_key: str, model: str) -> str:
    """
    기출 전문에서 '무엇을 물었는가(출제 개념·경향)'를 추출.
    형식(how)이 아니라 내용(what)에 집중 → 강의개념 가중치 계산에 사용.
    """
    prompt = f"""아래는 의대 기출문제 PDF에서 추출한 전문입니다.
이 기출에서 **실제로 출제된 핵심 개념·주제와 출제 경향**을 추출하세요.
문제 형식이 아니라 '무엇을 물었는가(내용)'에 집중하세요.

다음 JSON 형식으로만 반환하세요 (코드블록 없이 JSON만):
{{
  "기출출제개념": ["출제된 개념/주제1", "개념/주제2"],
  "빈출포인트": ["반복 출제되거나 강조된 포인트1", "포인트2"]
}}

## 기출문제 텍스트
{exam_text}"""

    return call_llm(prompt, api_key, model)


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
    min_priority = max(0, min(count, round(count * weight / 10)))  ##### 지나친 방어코드일 가능성. weight에 1~10의 정수 이외의 값이 들어올 가능성이 있는지 확인 필요

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

    # 유형 구성 지시 (기출 유형 통계 → 생성 문제 수 배분)
    if type_targets:
        type_rule = (
            f"**객관식 {type_targets.get('객관식', 0)}개, "
            f"주관식 {type_targets.get('주관식', 0)}개**로 출제 "
            f"(기출 유형 구성 비율 반영)"
        )
    else:
        type_rule = "**기출문제 예시에 나타난 유형(객관식/주관식) 비율을 그대로 유지**할 것"

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
아래 [기출문제 예시]의 형식과 **유형(객관식/주관식)**을 참고하여 새로운 예상 문제를 생성하세요.
단, 아래 [0] 기출 반영 강도에 따라 기출을 얼마나 강하게 반영할지 조절하세요.

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
- 객관식은 선택지 ①②③④⑤ 포함, 주관식은 선택지 없이 서술·단답형으로 출제
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

[주관식 문제일 때]
---QUESTION---
유형: 주관식
번호: 2
문제: [문제 전체]
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
        # 유형이 없으면 선택지 유무로 자동 판별
        if not q.get("유형"):
            q["유형"] = "객관식" if choice_lines else "주관식"
        if q.get("문제"):
            questions.append(q)

    return questions


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

def run_analysis(lecture_text: str, exam_text: str, api_key: str, model: str) -> dict:
    """강의·기출을 분석해 재사용 자산을 생성 (LLM 4~5회 호출)."""
    concepts = safe_parse_json(
        call_llm(build_concept_extraction_prompt(lecture_text), api_key, model)
    )
    type_stats = count_question_types(exam_text)  # 정규식, 0 토큰
    sample_questions = extract_sample_questions(exam_text, api_key, model, type_stats)
    format_analysis = analyze_format(sample_questions, api_key, model)
    exam_concepts = safe_parse_json(extract_exam_concepts(exam_text, api_key, model))
    priority_topics = compute_priority_topics(concepts, exam_concepts)
    return {
        "concepts": concepts,
        "sample_questions": sample_questions,
        "format_analysis": format_analysis,
        "exam_concepts": exam_concepts,
        "priority_topics": priority_topics,
        "type_stats": type_stats,
    }


# ──────────────────────────────────────────────
# API 라우트
# ──────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(".", "index.html")


# ── 세션 CRUD ──

@app.route("/sessions", methods=["GET"])
def sessions_list():
    return jsonify({"sessions": list_sessions()})


@app.route("/session/<int:sid>", methods=["GET"])
def session_get(sid):
    sess = load_session(sid)
    if not sess:
        return jsonify({"error": "세션을 찾을 수 없습니다."}), 404
    return jsonify(sess)


@app.route("/session/<int:sid>", methods=["DELETE"])
def session_delete(sid):
    delete_session(sid)
    return jsonify({"success": True})


@app.route("/session/<int:sid>/rename", methods=["POST"])
def session_rename(sid):
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "세션 이름을 입력하세요."}), 400
    rename_session(sid, name)
    return jsonify({"success": True})


@app.route("/models", methods=["GET"])
def get_models():
    api_key = request.headers.get("X-Api-Key", "")
    if not api_key:
        return jsonify({"error": "API 키가 필요합니다."}), 400
    try:
        client = OpenAI(api_key=api_key, base_url=GATEWAY_BASE_URL)
        models = client.models.list()
        model_ids = sorted([m.id for m in models.data])
        return jsonify({"models": model_ids})
    except Exception as e:
        return jsonify({"models": [DEFAULT_MODEL], "error": str(e)})


@app.route("/generate", methods=["POST"])
def generate():
    try:
        api_key    = request.form.get("api_key", "").strip()
        count      = int(request.form.get("count", 5))
        weight     = int(request.form.get("weight", 5))
        model      = request.form.get("model", DEFAULT_MODEL).strip()
        session_id = request.form.get("session_id", "").strip()

        if not api_key:
            return jsonify({"error": "API 키를 입력해주세요."}), 400
        if not (1 <= count <= 30):
            return jsonify({"error": "문제 수는 1~30개 사이로 설정해주세요."}), 400
        if not (1 <= weight <= 10):
            return jsonify({"error": "기출 반영 강도는 1~10 사이로 설정해주세요."}), 400

        # ── 경로 A: 저장된 세션 재사용 (분석 LLM 호출 0회 → 토큰 절약) ──
        if session_id:
            analysis = load_session(int(session_id))
            if not analysis:
                return jsonify({"error": "세션을 찾을 수 없습니다. 새로 분석해주세요."}), 404
            reused = True
        # ── 경로 B: 새 파일 업로드 → 분석 후 세션 저장 ──
        else:
            lecture_file = request.files.get("lecture")
            exam_file    = request.files.get("exam")
            if not lecture_file or not exam_file:
                return jsonify({"error": "강의자료와 기출문제 파일을 모두 업로드해주세요."}), 400

            lecture_text = truncate(extract_text_from_pdf(lecture_file))
            exam_text    = truncate(extract_text_from_pdf(exam_file))
            analysis = run_analysis(lecture_text, exam_text, api_key, model)

            # 세션 저장 (이름: 사용자 지정 or 파일명+시각)
            base = (lecture_file.filename or "강의자료").rsplit(".", 1)[0]
            name = request.form.get("name", "").strip() or \
                   f"{base} · {datetime.now().strftime('%m/%d %H:%M')}"
            session_id = save_session(name, model, analysis)
            analysis["name"] = name
            reused = False

        # ── 공통: 예상문제 생성 (분석 자산 재사용, LLM 1회) ──
        type_stats   = analysis.get("type_stats", {})
        type_targets = compute_type_targets(type_stats, count)
        question_prompt = build_question_generation_prompt(
            analysis.get("concepts", {}),
            analysis.get("sample_questions", ""),
            analysis.get("format_analysis", ""),
            count,
            analysis.get("exam_concepts", {}),
            analysis.get("priority_topics", []),
            weight, type_targets,
        )
        question_raw = call_llm(question_prompt, api_key, model)
        questions    = parse_questions(question_raw)

        return jsonify({
            "success":          True,
            "session_id":       session_id,          # 재사용용 세션 id
            "session_name":     analysis.get("name", ""),
            "reused":           reused,              # 저장된 세션 재사용 여부
            "concepts":         analysis.get("concepts", {}),
            "exam_concepts":    analysis.get("exam_concepts", {}),
            "priority_topics":  analysis.get("priority_topics", []),
            "type_stats":       type_stats,
            "type_targets":     type_targets,
            "sample_questions": analysis.get("sample_questions", ""),
            "format_analysis":  analysis.get("format_analysis", ""),
            "questions":        questions,
            "raw":              question_raw,
            "model":            model,
            "weight":           weight,
        })

    except AuthenticationError:
        return jsonify({"error": "API 키가 올바르지 않습니다. 플랫폼에서 키를 확인해주세요."}), 401
    except RateLimitError:
        return jsonify({"error": "요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."}), 429
    except Exception as e:
        return jsonify({"error": f"서버 오류: {str(e)}"}), 500


init_db()  # 앱 로드 시 세션 테이블 보장 (flask run / gunicorn 포함)

if __name__ == "__main__":
    print("=" * 55)
    print("  의대 예상문제 생성기 서버 시작")
    print("  접속 주소: http://localhost:5000")
    print(f"  LLM 게이트웨이: {GATEWAY_BASE_URL}")
    print(f"  기본 모델: {DEFAULT_MODEL}")
    print("=" * 55)
    app.run(debug=True, port=5000)
