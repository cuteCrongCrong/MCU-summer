"""
기출 주제 분석 기능 — "강의록 몇 페이지의 주제가 어떤 기출 몇 번 문제로 나왔는가" 대응표.

담당 테이블: topic_analyses (분석 결과 보관 — 보관함 '분석한 주제' 카드가 읽는다)
사용자별 분리: 각 조회/저장은 current_owner()로 소유자를 필터한다. (question_gen과 같은 규칙)
문제 생성기와의 차이:
  - 유형별 문제 수 · 기출 반영 강도 없음 (문제를 만들지 않으므로)
  - 강의록·기출을 **여러 개** 올릴 수 있다 ("어떤 강의록/어떤 기출"을 구분해 보여줘야 하므로)
  - 세션(분석 자산 캐시)을 쓰지 않는다. 세션에는 페이지 번호가 없어서 출처를 만들 수 없다.
    분석 결과는 재사용 자산이 아니라 완성된 결과물이라 한 행으로 그대로 보관한다.
제목: 문제 생성기(generations.title)와 같은 방식. 입력 화면의 '이 분석 세트 이름'
      (선택)을 그대로 저장하고, 비워두면 NULL로 두어 화면에서 '제N회'로 대체한다.
      LLM에게 제목을 짓게 하지 않는다 — 회차 번호가 항상 유일하고 저렴하다.
LLM 프롬프트·파싱은 llm.py의 '기출 주제 분석' 섹션에 있다.
"""

import json
from datetime import datetime

from flask import Blueprint, request, jsonify, Response

from db import get_conn, json_col, owner_clause, LEGACY_PROVIDER
from features.auth import current_owner
from providers.base import (
    ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.factory import UnknownProviderError, get_provider
from providers.usage import (
    UsageCollector, credits_for_history, credits_result, credits_snapshot,
)
from llm import (
    IMAGE_DESCRIBE, IMAGE_TRANSCRIBE, MAX_FILES_PER_SIDE,
    describe_images_progressively, finish_labeled_docs, read_labeled_pdfs,
    remaining_char_budget, run_topic_analysis,
)
from features import extract_cache

topic_bp = Blueprint("topic", __name__)


class TopicParamError(Exception):
    """요청 값이 잘못된 경우 — 라우트가 상태 코드와 함께 그대로 응답한다."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _collect_pdfs(field: str):
    """업로드 목록에서 빈 항목을 걸러내고 확장자를 확인한다. (question_gen과 같은 규칙)"""
    files = [f for f in request.files.getlist(field) if f and f.filename]
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            raise TopicParamError(f"PDF 파일만 올릴 수 있습니다: {f.filename}")
    return files


def _analysis_payload(result, lecture_docs, exam_docs, model, provider, title,
                      spend, owner):
    """
    분석 결과를 보관하고 응답 본문(dict)을 만든다.
    바로 진행한 경우와 '분량 초과 확인 후 진행'한 경우가 같은 응답을 내도록 공유한다.
    owner 를 인자로 받는 이유: 스트리밍 경로는 요청 컨텍스트가 닫힌 뒤에 실행돼서
    여기서 current_owner() 를 부르면 터진다.
    """
    # 응답과 보관에 같은 값을 쓴다 (한 번만 조회 — 잔액 조회가 왕복 요청이라서)
    spent = spend()

    # 보관함('분석한 주제')에서 다시 볼 수 있도록 저장.
    # 주제를 하나도 못 찾은 결과는 보관하지 않는다 — 목록에 빈 항목만 쌓인다.
    analysis_id = None
    if result["topics"]:
        analysis_id = save_analysis(
            result, lecture_docs, exam_docs, model, provider.name,
            owner, title,
            usage=spent["usage"],
            # 잔액은 시간이 지나면 틀린 값이 되므로 쓴 만큼만 남긴다
            credits=credits_for_history(spent["credits"]),
        )

    return {
        "success": True,
        "analysis_id": analysis_id,       # 보관된 분석 id (주제가 없으면 null)
        "title": title,                   # 사용자가 붙인 이름 (없으면 "")
        "topics": result["topics"],
        "dropped": result["dropped"],
        "total_questions": result["total_questions"],
        "lecture_docs": [_doc_meta(d) for d in lecture_docs],
        "exam_docs": [_doc_meta(d) for d in exam_docs],
        "raw": result["raw"],
        "model": model,
        "provider": provider.name,
        **spent,                          # usage(토큰) + credits(크레딧)
    }


def truncation_warnings(*doc_groups) -> list:
    """
    상한을 넘겨 일부가 버려지는 문서만 골라 화면용 경고 목록으로. 없으면 빈 리스트.
    (llm.finish_labeled_docs가 doc["cut"]·doc["img"]에 넣어둔 계산 결과를 옮겨 담는다)

    두 종류를 한 목록에 담고 kind로 구분한다 — 사용자에게는 "이대로 진행할까"라는 같은
    질문이라, 모달을 두 번 띄우면 확인을 두 번 받아야 한다.
      kind="text"  : 글자수 상한 초과 (어느 구간이 버려지는지)
      kind="image" : 그림·필기 쪽수 상한 초과 (몇 쪽 중 몇 쪽만 읽었는지)
    """
    out = []
    for side, docs in doc_groups:
        for d in docs:
            if d.get("cut"):
                out.append({"kind": "text", "side": side, "name": d["name"],
                            **d["cut"]})
            img = d.get("img") or {}
            if img.get("skipped"):
                out.append({"kind": "image", "side": side, "name": d["name"], **img})
    return out


def _doc_meta(d: dict) -> dict:
    """문서에서 원문 텍스트를 뺀 메타만 (응답·저장 공용). 텍스트는 보관하지 않는다."""
    return {
        "label": d["label"],
        "name": d["name"],
        "pages": d["pages"],
        "source": d["source"],
    }


# ──────────────────────────────────────────────
# 분석 결과 보관 (topic_analyses) — 사용자별 격리
# ──────────────────────────────────────────────

def save_analysis(result: dict, lecture_docs: list, exam_docs: list,
                  model: str, provider: str, owner, title: str = None,
                  usage: dict = None, credits: dict = None) -> int:
    """
    분석 한 건을 보관하고 id 반환.
    title은 사용자가 입력 화면에서 붙인 이름 — 비우면 NULL로 두고 화면에서 '제N회'로 대체한다.
    usage·credits는 이번 분석에 쓴 양 — 없으면 NULL로 두어 '모름'과 0을 구분한다.
    """
    user_id, guest_id = owner
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO topic_analyses
               (title, created_at, model, provider, lecture_docs, exam_docs,
                topics, dropped, total_questions, user_id, guest_id, usage, credits)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                (title or "").strip() or None,
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                model,
                provider or LEGACY_PROVIDER,
                json.dumps([_doc_meta(d) for d in lecture_docs], ensure_ascii=False),
                json.dumps([_doc_meta(d) for d in exam_docs], ensure_ascii=False),
                json.dumps(result.get("topics", []), ensure_ascii=False),
                int(result.get("dropped", 0) or 0),
                int(result.get("total_questions", 0) or 0),
                user_id,
                guest_id,
                json.dumps(usage, ensure_ascii=False) if usage else None,
                json.dumps(credits, ensure_ascii=False) if credits else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_analyses(owner) -> list:
    """보관된 분석 목록 (주제 배열은 제외한 가벼운 메타만). 소유자 것만, 최신순."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT id, title, created_at, model, provider, lecture_docs,
                       exam_docs, dropped, total_questions,
                       json_array_length(topics) AS num_topics
                FROM topic_analyses WHERE {frag} ORDER BY id DESC""",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "",
            "created_at": r["created_at"],
            "model": r["model"] or "",
            "provider": r["provider"] or LEGACY_PROVIDER,
            "lecture_names": [d.get("name", "") for d in json.loads(r["lecture_docs"] or "[]")],
            "exam_names": [d.get("name", "") for d in json.loads(r["exam_docs"] or "[]")],
            "dropped": r["dropped"] or 0,
            "total_questions": r["total_questions"] or 0,
            "num_topics": r["num_topics"] or 0,
        }
        for r in rows
    ]


def load_analysis(aid: int, owner):
    """분석 한 건 전체 복원. 소유자만. 없으면 None."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        r = conn.execute(
            f"SELECT * FROM topic_analyses WHERE id=? AND {frag}", [aid] + params
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "title": r["title"] or "",
        "created_at": r["created_at"],
        "model": r["model"] or "",
        "provider": r["provider"] or LEGACY_PROVIDER,
        "lecture_docs": json.loads(r["lecture_docs"] or "[]"),
        "exam_docs": json.loads(r["exam_docs"] or "[]"),
        "topics": json.loads(r["topics"] or "[]"),
        "dropped": r["dropped"] or 0,
        "total_questions": r["total_questions"] or 0,
        # 컬럼 추가 이전에 만든 분석은 값이 없다 → None. 화면은 상자를 숨긴다.
        "usage": json_col(r, "usage"),
        "credits": json_col(r, "credits"),
    }


def rename_analysis(aid: int, title: str, owner) -> bool:
    """소유한 분석만 이름 변경. 빈 값을 보내면 이름을 지워 '제N회' 표시로 되돌린다."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        cur = conn.execute(
            f"UPDATE topic_analyses SET title=? WHERE id=? AND {frag}",
            [(title or "").strip() or None, aid] + params,
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_analysis(aid: int, owner):
    """소유한 분석만 삭제."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        conn.execute(f"DELETE FROM topic_analyses WHERE id=? AND {frag}", [aid] + params)
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 라우트: 보관된 분석 (보관함 '분석한 주제')
# ──────────────────────────────────────────────

@topic_bp.route("/topic-analyses", methods=["GET"])
def topic_analyses_list():
    return jsonify({"analyses": list_analyses(current_owner())})


@topic_bp.route("/topic-analysis/<int:aid>", methods=["GET"])
def topic_analysis_get(aid):
    data = load_analysis(aid, current_owner())
    if not data:
        return jsonify({"error": "분석 결과를 찾을 수 없습니다."}), 404
    return jsonify(data)


@topic_bp.route("/topic-analysis/<int:aid>/rename", methods=["POST"])
def topic_analysis_rename(aid):
    """분석 이름 변경. 빈 값을 보내면 이름을 지워 '제N회' 표시로 되돌린다."""
    title = request.form.get("title", "").strip()
    if len(title) > 100:
        return jsonify({"error": "이름은 100자 이내로 입력해주세요."}), 400
    if not rename_analysis(aid, title, current_owner()):
        return jsonify({"error": "분석 결과를 찾을 수 없습니다."}), 404
    return jsonify({"success": True, "title": title})


@topic_bp.route("/topic-analysis/<int:aid>", methods=["DELETE"])
def topic_analysis_delete(aid):
    delete_analysis(aid, current_owner())
    return jsonify({"success": True})


# ──────────────────────────────────────────────
# 라우트: 기출 주제 분석 실행
# ──────────────────────────────────────────────

def read_topic_params() -> dict:
    """
    요청에서 분석에 필요한 값을 전부 꺼내 dict로 반환.

    스트리밍 경로는 응답 제너레이터가 **요청 컨텍스트가 닫힌 뒤** 실행되므로,
    PDF 내용과 소유자까지 여기서 읽어둬야 한다. (뒤늦게 request를 만지면 터짐)
    """
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        raise TopicParamError("API 키를 입력해주세요.")

    try:
        provider = get_provider(request.form.get("provider"))
    except UnknownProviderError as e:
        raise TopicParamError(str(e))

    # 이번에 분석할 내용 이름(선택). 비우면 화면에서 '제N회'로 표시된다.
    title = request.form.get("title", "").strip()
    if len(title) > 100:
        raise TopicParamError("분석 세트 이름은 100자 이내로 입력해주세요.")

    # 분량 초과 확인 후 다시 온 요청은 토큰만 들고 온다 — 파일 검사를 건너뛴다.
    token = request.form.get("extract_token", "").strip()
    lectures, exams = [], []
    if not token:
        lectures = _collect_pdfs("lectures")
        exams = _collect_pdfs("exams")
        if not lectures:
            raise TopicParamError("강의록 PDF를 1개 이상 올려주세요.")
        if not exams:
            raise TopicParamError("기출문제 PDF를 1개 이상 올려주세요.")
        if len(lectures) > MAX_FILES_PER_SIDE or len(exams) > MAX_FILES_PER_SIDE:
            raise TopicParamError(
                f"강의록·기출은 각각 최대 {MAX_FILES_PER_SIDE}개까지 올릴 수 있습니다.")

    model = request.form.get("model", "").strip() or provider.default_model
    # 이미지 호출 전용 모델 — 기출 그림 설명(IMAGE_DESCRIBE)과 강의록 글자 전사
    # (IMAGE_TRANSCRIBE) 양쪽 다. 주제 대조 자체는 위 model이 한다.
    # 사용자가 고르지 않는다: 제공사가 image_model을 선언했으면(게이트웨이) 그 모델로
    # 고정하고, 아니면 위 model을 그대로 쓴다. 하는 일이 '읽고 옮기기'라 본작업과 체급을
    # 맞출 이유가 없다 — 근거 실측은 providers/jbnu_gateway.py 참고.
    # 폼의 analysis_model은 **일부러 읽지 않는다** — 열어둔 옛날 탭이나 직접 만든 요청이
    # 비싼 모델을 이미지 수십 장에 밀어넣는 길이 되어선 안 된다.
    # (문제 생성기도 같은 방식이지만, 거기는 개념·형식 분석까지 맡아 analysis_model을 본다)
    analysis_model = getattr(provider, "image_model", "") or model

    return {
        "api_key":  api_key,
        "provider": provider,
        "model":    model,
        "analysis_model": analysis_model,
        "title":    title,
        "extract_token": token,
        "owner":    current_owner(),
        # 지금 bytes로 읽어둔다 (read_pdf_pages는 업로드 객체와 bytes를 모두 받는다)
        "lecture_files": [(f.filename, f.read()) for f in lectures],
        "exam_files":    [(f.filename, f.read()) for f in exams],
    }


def run_topic_analysis_events(p: dict):
    """
    주제 분석을 실행하면서 진행 상황을 이벤트로 흘려보내는 제너레이터.

    /analyze-topics 는 이걸 끝까지 돌려 결과만 반환하고,
    /analyze-topics/stream 은 각 이벤트를 그대로 흘려보낸다 → 로직이 한 벌만 존재한다.
    (문제 생성기의 run_generation_events 와 같은 구조·같은 이벤트 이름)

    단계 키는 화면의 li id(topic-step-*)와 1:1로 대응한다:
      extract → PDF 텍스트 추출 (그림 장수만큼 실제 진행률이 나온다)
      topics  → 주제 대응표 만들기 (LLM 호출 1회라 0 → 1로만 움직인다)
      finish  → 결과 정리·보관
    """
    api_key  = p["api_key"]
    provider = p["provider"]
    model    = p["model"]
    owner    = p["owner"]
    # 이미지 호출 전용 모델 (read_topic_params 주석 참고).
    # .get — 이 키가 없던 시절의 호출부/테스트도 그대로 돌게.
    analysis_model = p.get("analysis_model") or model

    # 이번 분석에 쓴 토큰을 단계별로 모은다. 오류로 끝나도 그 시점까지의 사용량은
    # 알려줘야 하므로 try 바깥에 둔다 (except 절에서도 참조).
    usage = UsageCollector()
    # 크레딧 과금 제공사(전북대 게이트웨이)의 분석 전 잔액. LLM 호출 이전에 찍어야
    # 이번 분석분만 차이로 잡힌다.
    credits_before = None

    def spend() -> dict:
        """응답에 실을 사용량. 성공·오류 응답이 함께 쓴다."""
        return {
            "usage":   usage.summary(),
            # 지원하지 않는 제공사·키 오류면 조회가 실패해 None이 된다
            "credits": credits_result(credits_before,
                                      credits_snapshot(provider, api_key)),
        }

    try:
        usage.set_stage("extract")
        yield {"type": "stage", "key": "extract", "status": "active"}

        # ── 2단계: 분량 초과 경고를 확인하고 다시 온 요청 ──
        # 1단계에서 이미 추출(그림 속 글자 전사 포함)을 끝냈으므로 그대로 재사용한다.
        # 다시 추출하면 Vision 호출값을 두 번 낸다.
        cached = extract_cache.take(p["extract_token"], owner) if p["extract_token"] else None
        if p["extract_token"] and not cached:
            yield {"type": "error", "status": 400,
                   "message": "확인 시간이 지났습니다. 파일을 다시 올려 분석해주세요."}
            return

        if cached:
            lecture_docs   = cached["lecture_docs"]
            exam_docs      = cached["exam_docs"]
            usage          = cached["usage"]        # 1단계에서 쓴 몫까지 이어서 센다
            credits_before = cached["credits_before"]
            model          = cached["model"]
            provider       = get_provider(cached["provider"])
            # 추출은 이미 끝났으므로 진행률을 100%로 채워 보낸다 (바가 0%로 되돌아가지 않게)
            imgs = cached["img_count"]
            yield {"type": "progress", "key": "extract", "done": imgs, "total": imgs}
        else:
            # 첫 LLM 호출 직전 — 여기서 찍어야 이번 분석분만 잔액 차이로 잡힌다
            credits_before = credits_snapshot(provider, api_key)

            # 여기까지는 LLM 호출이 없다 (페이지 선정만) — 진행률 총계를 먼저 알아야 하므로
            # 강의록·기출을 모두 읽어 그림 수를 센 뒤에 Vision 호출을 시작한다.
            lec_parts  = read_labeled_pdfs(p["lecture_files"], "강의록",
                                           api_key, IMAGE_TRANSCRIBE)
            exam_parts = read_labeled_pdfs(p["exam_files"], "기출",
                                           api_key, IMAGE_DESCRIBE)

            total_imgs = sum(len(x["img_jobs"]) for x in lec_parts + exam_parts)
            yield {"type": "progress", "key": "extract", "done": 0, "total": total_imgs}
            done_imgs = 0
            # 강의록: 그림 속 '글자만' 전사(손글씨·판서 포함), 그림 해설은 금지 —
            #   주제 이름은 '강의록에 있는 단어'만 써야 하는데, 설명을 시키면 LLM이 지어낸
            #   문장까지 '강의록에 있는 것'이 되어 용어 규칙이 무력해진다.
            # 기출: 그림 문제(부위 이름 쓰기 등)를 놓치지 않도록 그림 해설 포함.
            for parts, mode in ((lec_parts, IMAGE_TRANSCRIBE), (exam_parts, IMAGE_DESCRIBE)):
                for x in parts:
                    # 이미지 호출만 analysis_model로 간다 (주제 대조는 아래에서 model이 한다)
                    for d in describe_images_progressively(x["img_jobs"], api_key,
                                                           analysis_model,
                                                           provider, usage, mode):
                        yield {"type": "progress", "key": "extract",
                               "done": done_imgs + d, "total": total_imgs}
                    done_imgs += len(x["img_jobs"])

            lecture_docs = finish_labeled_docs(lec_parts, image_mode=IMAGE_TRANSCRIBE)
            # 기출은 강의록이 쓰고 남긴 몫까지 받는다 (근거는 llm.TOPIC_TOTAL_CHAR_BUDGET).
            # 강의록 조립이 끝난 지금은 실사용량이 확정돼 있으므로 여기서 계산할 수 있다.
            # by_question: 상한을 넘길 때 문항 번호 경계로 자른다 (반쪽짜리 문항 방지).
            exam_docs    = finish_labeled_docs(exam_parts, image_mode=IMAGE_DESCRIBE,
                                               side_budget=remaining_char_budget(lecture_docs),
                                               by_question=True)

            # 아래 두 오류는 기출 이미지 설명(LLM)이 이미 돈 뒤에 나므로 사용량을 함께 보낸다
            if not any((d["text"] or "").strip() for d in lecture_docs):
                yield {"type": "error", "status": 400,
                       "message": "강의록에서 텍스트를 추출하지 못했습니다. "
                                  "스캔 이미지로만 된 PDF는 주제를 읽을 수 없습니다.",
                       **spend()}
                return
            if not any((d["text"] or "").strip() for d in exam_docs):
                yield {"type": "error", "status": 400,
                       "message": "기출문제에서 텍스트를 추출하지 못했습니다. "
                                  "문제 번호를 읽을 수 없어 출처를 만들 수 없습니다.",
                       **spend()}
                return

            # 상한을 넘겨 일부가 버려지는 파일이 있으면 진행 전에 물어본다.
            # 추출 결과를 잠시 보관하고 토큰만 내려보낸다 — 재추출하면 Vision 재과금.
            warnings = truncation_warnings(("강의록", lecture_docs), ("기출문제", exam_docs))
            if warnings:
                token = extract_cache.put({
                    "lecture_docs": lecture_docs, "exam_docs": exam_docs,
                    "usage": usage, "credits_before": credits_before,
                    "model": model, "provider": provider.name,
                    # 확인 후 이어서 돌 때 추출 진행률을 100%로 채우는 데 쓴다
                    "img_count": total_imgs,
                }, owner)
                yield {"type": "needs_confirm", "payload": {
                    "extract_token": token,
                    "warnings": warnings,
                    **spend(),        # 여기까지(추출) 쓴 양도 알려준다
                }}
                return

        yield {"type": "stage", "key": "extract", "status": "done"}

        # 주제 대응표는 LLM 호출 한 번이라 안을 쪼갤 수 없다 — 0 → 1 로만 움직인다
        usage.set_stage("topics")
        yield {"type": "stage", "key": "topics", "status": "active"}
        yield {"type": "progress", "key": "topics", "done": 0, "total": 1}
        result = run_topic_analysis(lecture_docs, exam_docs, api_key, model, provider,
                                    usage)
        yield {"type": "stage", "key": "topics", "status": "done"}

        yield {"type": "stage", "key": "finish", "status": "active"}
        payload = _analysis_payload(result, lecture_docs, exam_docs, model, provider,
                                    p["title"], spend, owner)
        yield {"type": "stage", "key": "finish", "status": "done"}
        yield {"type": "done", "payload": payload}

    except ProviderAuthError as e:
        # 키가 틀린 경우엔 잔액 조회도 실패하므로 credits는 None이 된다
        yield {"type": "error", "status": 401, "message": str(e), **spend()}
    except ProviderRateLimitError as e:
        yield {"type": "error", "status": 429, "message": str(e), **spend()}
    except ProviderError as e:
        yield {"type": "error", "status": 400, "message": str(e), **spend()}
    except ValueError as e:
        # PDF를 열 수 없음 등 사용자가 고칠 수 있는 입력 문제 (llm.read_labeled_pdfs)
        yield {"type": "error", "status": 400, "message": str(e), **spend()}
    except Exception as e:
        yield {"type": "error", "status": 500, "message": f"서버 오류: {str(e)}",
               **spend()}


@topic_bp.route("/analyze-topics", methods=["POST"])
def analyze_topics():
    """비스트리밍 경로 — 같은 파이프라인을 끝까지 돌린 뒤 결과만 한 번에 반환."""
    try:
        params = read_topic_params()
    except TopicParamError as e:
        return jsonify({"error": str(e)}), e.status

    for ev in run_topic_analysis_events(params):
        if ev["type"] == "error":
            return jsonify({"error": ev["message"],
                            "usage": ev.get("usage"),
                            "credits": ev.get("credits")}), ev["status"]
        # 분량 초과 — 진행 여부를 물어야 하므로 결과 대신 확인 요청을 돌려준다
        if ev["type"] == "needs_confirm":
            return jsonify({"needs_confirm": True, **ev["payload"]})
        if ev["type"] == "done":
            return jsonify(ev["payload"])
    return jsonify({"error": "분석 결과를 받지 못했습니다."}), 500


@topic_bp.route("/analyze-topics/stream", methods=["POST"])
def analyze_topics_stream():
    """
    스트리밍 경로 — 진행 상황을 SSE로 흘려보낸다. (문제 생성기 /generate/stream 과 동일)

    EventSource는 GET만 지원하는데 여기는 PDF를 multipart POST로 받으므로,
    프런트는 fetch + ReadableStream으로 읽는다. (형식은 SSE 그대로)
    """
    # 파라미터 오류는 스트림을 열기 전이라 정상적인 HTTP 상태 코드로 응답할 수 있다.
    # 스트림이 시작된 뒤(200 전송 후)의 오류는 error 이벤트로만 알릴 수 있다.
    try:
        params = read_topic_params()
    except TopicParamError as e:
        return jsonify({"error": str(e)}), e.status

    def sse():
        try:
            for ev in run_topic_analysis_events(params):
                # ensure_ascii=False로 한글을 그대로, 줄바꿈은 \n으로 이스케이프되어
                # SSE 프레임 구분(빈 줄)과 충돌하지 않는다.
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:      # 제너레이터 밖에서 터진 예외도 이벤트로 전달
            fallback = {"type": "error", "status": 500,
                        "message": f"서버 오류: {str(e)}"}
            yield f"data: {json.dumps(fallback, ensure_ascii=False)}\n\n"

    return Response(sse(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",   # 프록시(nginx 등)가 버퍼링해 스트림을 막지 않도록
    })

