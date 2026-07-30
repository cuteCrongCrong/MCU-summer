"""
기출 주제 분석 기능 — "강의록 몇 페이지의 주제가 어떤 기출 몇 번 문제로 나왔는가" 대응표.

담당 테이블: topic_analyses (분석 결과 보관 — 보관함 '분석한 주제' 카드가 읽는다)
사용자별 분리: 각 조회/저장은 current_owner()로 소유자를 필터한다. (question_gen과 같은 규칙)
문제 생성기와의 차이:
  - 유형별 문제 수 · 기출 반영 강도 없음 (문제를 만들지 않으므로)
  - 강의록·기출을 **여러 개** 올릴 수 있다 ("어떤 강의록/어떤 기출"을 구분해 보여줘야 하므로)
  - 세션(분석 자산 캐시)을 쓰지 않는다. 세션에는 페이지 번호가 없어서 출처를 만들 수 없다.
    분석 결과는 재사용 자산이 아니라 완성된 결과물이라 한 행으로 그대로 보관한다.
제목: LLM이 주제들을 대표하는 키워드 구(句)로 지어준다 (llm.clean_topic_title / build_topic_title).
      사용자가 보관함에서 바꿀 수 있고, 비우면 자동 제목으로 되돌아간다.
LLM 프롬프트·파싱은 llm.py의 '기출 주제 분석' 섹션에 있다.
"""

import json
from datetime import datetime

from flask import Blueprint, request, jsonify

from db import get_conn, owner_clause, LEGACY_PROVIDER
from features.auth import current_owner
from providers.base import (
    ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.factory import UnknownProviderError, get_provider
from llm import (
    extract_labeled_docs, run_topic_analysis, build_topic_title,
    clean_topic_title,
)

topic_bp = Blueprint("topic", __name__)

# 한쪽(강의록/기출)당 업로드 파일 개수 상한.
# 파일 수가 늘수록 문서당 반영 글자수가 줄고(예산 분할) 프롬프트도 커지므로 상한을 둔다.
MAX_FILES_PER_SIDE = 5


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
                  model: str, provider: str, owner) -> int:
    """분석 한 건을 보관하고 id 반환."""
    user_id, guest_id = owner
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO topic_analyses
               (title, created_at, model, provider, lecture_docs, exam_docs,
                topics, dropped, total_questions, user_id, guest_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                result.get("title", ""),
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
    }


def rename_analysis(aid: int, title: str, owner):
    """
    제목 변경. 빈 문자열을 주면 보관된 주제들로 자동 제목을 다시 만든다
    (이름을 지웠을 때 목록에 빈 칸이 남지 않게).
    실제로 저장된 제목을 반환하고, 그 분석이 없으면 None을 반환한다.
    """
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT topics FROM topic_analyses WHERE id=? AND {frag}", [aid] + params
        ).fetchone()
        if not row:
            return None
        if not title:
            title = build_topic_title(json.loads(row["topics"] or "[]"))
        conn.execute(
            f"UPDATE topic_analyses SET title=? WHERE id=? AND {frag}",
            [title, aid] + params,
        )
        conn.commit()
        return title
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
    # 빈 값으로 보내면 자동 제목이 다시 만들어지므로, 저장된 제목을 그대로 돌려준다
    # (프런트가 목록·헤더를 재요청 없이 갱신할 수 있게)
    saved = rename_analysis(aid, clean_topic_title(request.form.get("title", "")),
                            current_owner())
    if saved is None:
        return jsonify({"error": "분석 결과를 찾을 수 없습니다."}), 404
    return jsonify({"success": True, "title": saved})


@topic_bp.route("/topic-analysis/<int:aid>", methods=["DELETE"])
def topic_analysis_delete(aid):
    delete_analysis(aid, current_owner())
    return jsonify({"success": True})


@topic_bp.route("/analyze-topics", methods=["POST"])
def analyze_topics():
    try:
        api_key = request.form.get("api_key", "").strip()
        if not api_key:
            return jsonify({"error": "API 키를 입력해주세요."}), 400

        try:
            provider = get_provider(request.form.get("provider"))
        except UnknownProviderError as e:
            return jsonify({"error": str(e)}), 400
        model = request.form.get("model", "").strip() or provider.default_model

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

        # 강의록: 이미지 설명 생략.
        #   주제 이름은 '강의록에 있는 단어'만 써야 하는데, 이미지 설명은 LLM이 새로 쓴
        #   문장이라 강의록에 없는 용어를 끌어들인다. (토큰도 아낀다)
        lecture_docs = extract_labeled_docs(lectures, "강의록", api_key, model,
                                            describe_images=False, provider=provider)
        # 기출: 그림 문제(부위 이름 쓰기 등)를 놓치지 않도록 이미지 설명 포함 (문제 생성기와 동일)
        exam_docs = extract_labeled_docs(exams, "기출", api_key, model,
                                         describe_images=True, provider=provider)

        if not any((d["text"] or "").strip() for d in lecture_docs):
            return jsonify({
                "error": "강의록에서 텍스트를 추출하지 못했습니다. "
                         "스캔 이미지로만 된 PDF는 주제를 읽을 수 없습니다."
            }), 400
        if not any((d["text"] or "").strip() for d in exam_docs):
            return jsonify({
                "error": "기출문제에서 텍스트를 추출하지 못했습니다. "
                         "문제 번호를 읽을 수 없어 출처를 만들 수 없습니다."
            }), 400

        result = run_topic_analysis(lecture_docs, exam_docs, api_key, model, provider)

        # 보관함('분석한 주제')에서 다시 볼 수 있도록 저장.
        # 주제를 하나도 못 찾은 결과는 보관하지 않는다 — 목록에 빈 항목만 쌓인다.
        analysis_id = None
        if result["topics"]:
            analysis_id = save_analysis(result, lecture_docs, exam_docs,
                                        model, provider.name, current_owner())

        return jsonify({
            "success": True,
            "analysis_id": analysis_id,       # 보관된 분석 id (주제가 없으면 null)
            "title": result.get("title", ""),
            "topics": result["topics"],
            "dropped": result["dropped"],
            "total_questions": result["total_questions"],
            "lecture_docs": [_doc_meta(d) for d in lecture_docs],
            "exam_docs": [_doc_meta(d) for d in exam_docs],
            "raw": result["raw"],
            "model": model,
            "provider": provider.name,
        })

    except ProviderAuthError as e:
        return jsonify({"error": str(e)}), 401
    except ProviderRateLimitError as e:
        return jsonify({"error": str(e)}), 429
    except ProviderError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        # PDF를 열 수 없음 등 사용자가 고칠 수 있는 입력 문제 (llm.extract_labeled_docs)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"서버 오류: {str(e)}"}), 500
