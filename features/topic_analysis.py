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

from flask import Blueprint, request, jsonify

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
    extract_labeled_docs, run_topic_analysis,
)

topic_bp = Blueprint("topic", __name__)

# 업로드 파일 개수 상한은 글자 예산과 맞물려 있어 llm.py에서 한 번만 정한다
# (MAX_FILES_PER_SIDE — 문제 생성기와 공용).


def _collect_pdfs(field: str):
    """업로드된 파일 목록에서 빈 항목을 걸러낸다. (name, files) 또는 오류 문구 반환."""
    files = [f for f in request.files.getlist(field) if f and f.filename]
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            return None, f"PDF 파일만 올릴 수 있습니다: {f.filename}"
    return files, None


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


@topic_bp.route("/analyze-topics", methods=["POST"])
def analyze_topics():
    # 이번 분석에 쓴 토큰을 단계별로 모은다. 오류로 끝나도 그 시점까지의 사용량은
    # 알려줘야 하므로 try 바깥에 둔다 (except 절에서도 참조).
    usage = UsageCollector()
    # 크레딧 과금 제공사(전북대 게이트웨이)의 분석 전 잔액. LLM 호출 이전에 찍어야
    # 이번 분석분만 차이로 잡힌다. 아래에서 제공사가 정해진 뒤 채운다.
    provider = None
    api_key = ""
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
        api_key = request.form.get("api_key", "").strip()
        if not api_key:
            return jsonify({"error": "API 키를 입력해주세요."}), 400

        try:
            provider = get_provider(request.form.get("provider"))
        except UnknownProviderError as e:
            return jsonify({"error": str(e)}), 400
        model = request.form.get("model", "").strip() or provider.default_model

        # 이번에 분석할 내용 이름(선택). 비우면 화면에서 '제N회'로 표시된다.
        title = request.form.get("title", "").strip()
        if len(title) > 100:
            return jsonify({"error": "분석 세트 이름은 100자 이내로 입력해주세요."}), 400

        lectures, err = _collect_pdfs("lectures")
        if err:
            return jsonify({"error": err}), 400
        exams, err = _collect_pdfs("exams")
        if err:
            return jsonify({"error": err}), 400

        if not lectures:
            return jsonify({"error": "강의록 PDF를 1개 이상 올려주세요."}), 400
        if not exams:
            return jsonify({"error": "기출문제 PDF를 1개 이상 올려주세요."}), 400
        if len(lectures) > MAX_FILES_PER_SIDE or len(exams) > MAX_FILES_PER_SIDE:
            return jsonify({
                "error": f"강의록·기출은 각각 최대 {MAX_FILES_PER_SIDE}개까지 올릴 수 있습니다."
            }), 400

        # 첫 LLM 호출 직전 — 여기서 찍어야 이번 분석분만 잔액 차이로 잡힌다
        credits_before = credits_snapshot(provider, api_key)

        usage.set_stage("extract")
        # 강의록: 그림 속 '글자만' 전사 (손글씨·판서 포함), 그림 해설은 금지.
        #   주제 이름은 '강의록에 있는 단어'만 써야 한다. 그림이 무엇인지 설명하게 하면
        #   LLM이 지어낸 문장이 강의록 텍스트에 섞이고, 그 표현까지 '강의록에 있는 것'이
        #   되어 용어 규칙이 무력해진다. 전사만 시키면 손글씨는 살리면서 그 위험은 없다.
        lecture_docs = extract_labeled_docs(lectures, "강의록", api_key, model,
                                            image_mode=IMAGE_TRANSCRIBE, provider=provider,
                                            usage=usage)
        # 기출: 그림 문제(부위 이름 쓰기 등)를 놓치지 않도록 그림 해설 포함 (문제 생성기와 동일)
        exam_docs = extract_labeled_docs(exams, "기출", api_key, model,
                                         image_mode=IMAGE_DESCRIBE, provider=provider,
                                         usage=usage)

        # 아래 두 오류는 기출 이미지 설명(LLM)이 이미 돈 뒤에 나므로 사용량을 함께 보낸다
        if not any((d["text"] or "").strip() for d in lecture_docs):
            return jsonify({
                "error": "강의록에서 텍스트를 추출하지 못했습니다. "
                         "스캔 이미지로만 된 PDF는 주제를 읽을 수 없습니다.",
                **spend(),
            }), 400
        if not any((d["text"] or "").strip() for d in exam_docs):
            return jsonify({
                "error": "기출문제에서 텍스트를 추출하지 못했습니다. "
                         "문제 번호를 읽을 수 없어 출처를 만들 수 없습니다.",
                **spend(),
            }), 400

        usage.set_stage("topics")
        result = run_topic_analysis(lecture_docs, exam_docs, api_key, model, provider,
                                    usage)

        # 응답과 보관에 같은 값을 쓴다 (한 번만 조회 — 잔액 조회가 왕복 요청이라서)
        spent = spend()

        # 보관함('분석한 주제')에서 다시 볼 수 있도록 저장.
        # 주제를 하나도 못 찾은 결과는 보관하지 않는다 — 목록에 빈 항목만 쌓인다.
        analysis_id = None
        if result["topics"]:
            analysis_id = save_analysis(
                result, lecture_docs, exam_docs, model, provider.name,
                current_owner(), title,
                usage=spent["usage"],
                # 잔액은 시간이 지나면 틀린 값이 되므로 쓴 만큼만 남긴다
                credits=credits_for_history(spent["credits"]),
            )

        return jsonify({
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
        })

    except ProviderAuthError as e:
        # 키가 틀린 경우엔 잔액 조회도 실패하므로 credits는 None이 된다
        return jsonify({"error": str(e), **spend()}), 401
    except ProviderRateLimitError as e:
        return jsonify({"error": str(e), **spend()}), 429
    except ProviderError as e:
        return jsonify({"error": str(e), **spend()}), 400
    except ValueError as e:
        # PDF를 열 수 없음 등 사용자가 고칠 수 있는 입력 문제 (llm.extract_labeled_docs)
        return jsonify({"error": str(e), **spend()}), 400
    except Exception as e:
        return jsonify({"error": f"서버 오류: {str(e)}", **spend()}), 500
