"""
문제 생성 기능 — 세션(분석 자산)·생성 이력의 DB 접근 + HTTP 라우트.

담당 테이블: sessions, generations
사용자별 분리: 각 조회/저장은 current_owner()로 소유자를 필터한다.
  - 로그인 사용자: 본인 소유(user_id=id)만
  - 게스트(비로그인): 브라우저별로 발급된 익명 id 소유(guest_id=...)만
                     → 게스트끼리도 서로의 세션을 볼 수 없다.
연결 계약: 생성 문제 dict의 키(문제/선택지/정답/해설/함정포인트/유형)는
           오답노트가 그대로 저장·렌더링하므로 이름을 바꾸지 말 것. (CONTRIBUTING.md 4-B)
"""

import json
from datetime import datetime

from flask import Blueprint, request, jsonify, Response

from db import get_conn, json_col, owner_clause, LEGACY_PROVIDER
# current_owner: (user_id, guest_id) 튜플. 게스트도 브라우저별로 서로 격리된다.
from features.auth import current_owner
from features import extract_cache
from providers.base import (
    ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.factory import (
    DEFAULT_PROVIDER, UnknownProviderError, get_provider, list_providers,
)
from providers.usage import (
    UsageCollector, credits_for_history, credits_result, credits_snapshot,
)
from llm import (
    QUESTION_TYPES, StreamingQuestionParser, IMAGE_DESCRIBE, IMAGE_TRANSCRIBE,
    MAX_FILES_PER_SIDE, GEN_SIDE_CHAR_BUDGET, GEN_DOC_MIN_CHARS,
    remaining_gen_budget,
    read_labeled_pdfs, describe_images_progressively, assemble_pdf_text,
    image_coverage, shrink_pdf_bytes,
    truncate, truncation_report, build_source_info,
    analyze_concepts_progressively, analyze_format_progressively,
    compute_type_targets, build_question_generation_prompt, call_llm_stream,
)

gen_bp = Blueprint("gen", __name__)

# 문제 생성 LLM 호출을 나누는 단위(문제 수) — 한 번에 너무 많이 요청하면
# 응답이 max_tokens에서 잘려 파싱이 실패하므로 배치로 나눠 호출한다.
GEN_BATCH_SIZE = 4
GEN_MAX_TOKENS = 8000


# ──────────────────────────────────────────────
# 세션 저장 (분석 결과 캐싱 → 재분석 없이 재생성) — 사용자별 격리
# ──────────────────────────────────────────────

def _row_provider(row) -> str:
    """
    행의 provider 값을 읽되, 다중 프로바이더 지원 이전 데이터(컬럼 없음/NULL)는
    전북대 게이트웨이로 간주한다. 오래된 세션도 그대로 재사용할 수 있게 하는 하위 호환.
    """
    if "provider" not in row.keys():
        return LEGACY_PROVIDER
    return row["provider"] or LEGACY_PROVIDER


def _row_title(row) -> str:
    """
    회차에 붙은 이름. 이름을 안 붙였거나 컬럼이 없던 구버전 행은 빈 문자열.
    화면은 빈 값을 '제N회'로 대체한다.
    """
    if "title" not in row.keys():
        return ""
    return row["title"] or ""


def save_session(name: str, model: str, analysis: dict, owner,
                 provider: str = None) -> int:
    """분석 결과(재사용 자산)를 한 세션으로 저장하고 id 반환. 소유자=owner 튜플."""
    user_id, guest_id = owner
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO sessions
               (name, model, created_at, concepts, sample_questions,
                format_analysis, exam_concepts, priority_topics, type_stats,
                source_info, user_id, guest_id, provider)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
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
                json.dumps(analysis.get("source_info", {}), ensure_ascii=False),
                user_id,
                guest_id,
                provider or LEGACY_PROVIDER,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def load_session(sid: int, owner):
    """세션 하나를 분석 자산 dict로 복원. 소유자만 접근. 없으면 None."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        row = conn.execute(
            f"SELECT * FROM sessions WHERE id=? AND {frag}", [sid] + params
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {
        "id": row["id"],
        "name": row["name"],
        "model": row["model"],
        # 컬럼 추가 이전에 만들어진 세션은 NULL → 게이트웨이로 간주
        "provider": _row_provider(row),
        "created_at": row["created_at"],
        "concepts": json.loads(row["concepts"] or "{}"),
        "sample_questions": row["sample_questions"] or "",
        "format_analysis": row["format_analysis"] or "",
        "exam_concepts": json.loads(row["exam_concepts"] or "{}"),
        "priority_topics": json.loads(row["priority_topics"] or "[]"),
        "type_stats": json.loads(row["type_stats"] or "{}"),
        "source_info": json.loads((row["source_info"] if "source_info" in row.keys() else None) or "{}"),
    }


def list_sessions(owner) -> list:
    """세션 목록(가벼운 메타데이터만). 소유자 것만, 최신순."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT id, name, model, provider, created_at, type_stats
                FROM sessions WHERE {frag} ORDER BY id DESC""",
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "model": r["model"],
            "provider": _row_provider(r),
            "created_at": r["created_at"],
            "type_stats": json.loads(r["type_stats"] or "{}"),
        }
        for r in rows
    ]


def delete_session(sid: int, owner):
    """소유한 세션만 삭제(연결된 이력도 함께)."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        conn.execute(
            f"""DELETE FROM generations WHERE session_id IN
                (SELECT id FROM sessions WHERE id=? AND {frag})""",
            [sid] + params,
        )
        conn.execute(f"DELETE FROM sessions WHERE id=? AND {frag}", [sid] + params)
        conn.commit()
    finally:
        conn.close()


def rename_session(sid: int, name: str, owner):
    """소유한 세션만 이름 변경."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE sessions SET name=? WHERE id=? AND {frag}", [name, sid] + params
        )
        conn.commit()
    finally:
        conn.close()


# ── 생성 문제 이력 (소유한 세션을 통해서만 접근) ──

def save_generation(session_id: int, count: int, weight: int, model: str,
                    type_targets: dict, questions: list, raw: str,
                    provider: str = None, title: str = None,
                    usage: dict = None, credits: dict = None) -> int:
    """
    한 번의 문제 생성 결과를 세션에 연결해 이력으로 저장.
    title은 사용자가 붙인 이름 — 비우면 NULL로 두고 화면에서 '제N회'로 대체한다.
    usage·credits는 이 회차에 쓴 양 — 없으면 NULL로 두어 '모름'과 0을 구분한다.
    저장된 세션을 재사용한 회차는 분석을 건너뛰므로 사용량이 훨씬 작게 잡히는데,
    그게 사실이라 그대로 둔다 (재사용이 얼마나 아꼈는지가 회차마다 드러난다).
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO generations
               (session_id, created_at, count, weight, model, type_targets,
                questions, raw, provider, title, usage, credits)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                session_id,
                datetime.now().strftime("%Y-%m-%d %H:%M"),
                count,
                weight,
                model,
                json.dumps(type_targets or {}, ensure_ascii=False),
                json.dumps(questions or [], ensure_ascii=False),
                raw or "",
                provider or LEGACY_PROVIDER,
                (title or "").strip() or None,
                json.dumps(usage, ensure_ascii=False) if usage else None,
                json.dumps(credits, ensure_ascii=False) if credits else None,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def generation_ordinal(session_id: int, gid: int) -> int:
    """
    같은 세션 안에서 이 회차가 몇 번째인지 (오래된 것이 제1회).

    보관함 목록이 화면에서 세는 규칙과 같다 — 세션별로 묶어 id 오름차순.
    앞 회차를 지우면 번호가 밀리므로 DB에 저장하지 않고 그때그때 센다.
    (저장해 두면 삭제 후 목록과 결과 화면의 번호가 어긋난다)
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM generations WHERE session_id=? AND id<=?",
            (session_id, gid),
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def rename_generation(gid: int, title: str, owner) -> bool:
    """소유한 세션의 이력만 이름 변경. 실제로 바뀌었으면 True."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        cur = conn.execute(
            f"""UPDATE generations SET title=?
                WHERE id=? AND session_id IN (SELECT id FROM sessions WHERE {frag})""",
            [(title or "").strip() or None, gid] + params,
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_generations(session_id: int, owner) -> list:
    """세션의 생성 이력 목록. 세션을 소유한 경우에만 반환(아니면 빈 목록)."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT id, created_at, count, weight, model, provider, title,
                       type_targets, questions
                FROM generations
                WHERE session_id=? AND session_id IN (SELECT id FROM sessions WHERE {frag})
                ORDER BY id DESC""",
            [session_id] + params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        qs = json.loads(r["questions"] or "[]")
        out.append({
            "id": r["id"],
            "created_at": r["created_at"],
            "count": r["count"],
            "weight": r["weight"],
            "model": r["model"],
            "provider": _row_provider(r),
            "title": _row_title(r),
            "type_targets": json.loads(r["type_targets"] or "{}"),
            "num_questions": len(qs),
        })
    return out


def load_generation(gid: int, owner):
    """이력 하나를 전체 복원. 소유한 세션의 이력만. 없으면 None."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        r = conn.execute(
            f"""SELECT * FROM generations
                WHERE id=? AND session_id IN (SELECT id FROM sessions WHERE {frag})""",
            [gid] + params,
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    return {
        "id": r["id"],
        "session_id": r["session_id"],
        "created_at": r["created_at"],
        "count": r["count"],
        "weight": r["weight"],
        "model": r["model"],
        "provider": _row_provider(r),
        "title": _row_title(r),
        "type_targets": json.loads(r["type_targets"] or "{}"),
        "questions": json.loads(r["questions"] or "[]"),
        "raw": r["raw"] or "",
        # 컬럼 추가 이전 회차는 값이 없다 → None. 화면은 상자를 숨긴다.
        "usage": json_col(r, "usage"),
        "credits": json_col(r, "credits"),
    }


def delete_generation(gid: int, owner):
    """소유한 세션의 이력만 삭제."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        conn.execute(
            f"""DELETE FROM generations
                WHERE id=? AND session_id IN (SELECT id FROM sessions WHERE {frag})""",
            [gid] + params,
        )
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 라우트: 세션 CRUD (current_owner()로 소유자 분리)
# ──────────────────────────────────────────────

@gen_bp.route("/sessions", methods=["GET"])
def sessions_list():
    return jsonify({"sessions": list_sessions(current_owner())})


@gen_bp.route("/session/<int:sid>", methods=["GET"])
def session_get(sid):
    sess = load_session(sid, current_owner())
    if not sess:
        return jsonify({"error": "세션을 찾을 수 없습니다."}), 404
    return jsonify(sess)


@gen_bp.route("/session/<int:sid>", methods=["DELETE"])
def session_delete(sid):
    delete_session(sid, current_owner())
    return jsonify({"success": True})


@gen_bp.route("/session/<int:sid>/rename", methods=["POST"])
def session_rename(sid):
    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "세션 이름을 입력하세요."}), 400
    rename_session(sid, name, current_owner())
    return jsonify({"success": True})


# ── 라우트: 생성 이력 ──

@gen_bp.route("/session/<int:sid>/generations", methods=["GET"])
def generations_list(sid):
    return jsonify({"generations": list_generations(sid, current_owner())})


@gen_bp.route("/generation/<int:gid>", methods=["GET"])
def generation_get(gid):
    gen = load_generation(gid, current_owner())
    if not gen:
        return jsonify({"error": "생성 이력을 찾을 수 없습니다."}), 404
    return jsonify(gen)


@gen_bp.route("/generation/<int:gid>/rename", methods=["POST"])
def generation_rename(gid):
    """회차 이름 변경. 빈 값을 보내면 이름을 지워 '제N회' 표시로 되돌린다."""
    title = request.form.get("title", "").strip()
    if len(title) > 100:
        return jsonify({"error": "이름은 100자 이내로 입력해주세요."}), 400
    if not rename_generation(gid, title, current_owner()):
        return jsonify({"error": "생성 이력을 찾을 수 없습니다."}), 404
    return jsonify({"success": True, "title": title})


@gen_bp.route("/generation/<int:gid>", methods=["DELETE"])
def generation_delete(gid):
    delete_generation(gid, current_owner())
    return jsonify({"success": True})


# ── 라우트: 프로바이더 · 모델 목록 ──

@gen_bp.route("/providers", methods=["GET"])
def get_providers():
    """선택 가능한 LLM 프로바이더 목록 (표시명·기본 모델·키 안내 문구)."""
    return jsonify({"providers": list_providers(), "default": DEFAULT_PROVIDER})


@gen_bp.route("/credits", methods=["GET"])
def get_credits():
    """크레딧 잔액 조회 — 지원하는 제공사(전북대 게이트웨이)에서만 동작."""
    api_key = request.headers.get("X-Api-Key", "")
    if not api_key:
        return jsonify({"error": "API 키가 필요합니다."}), 400
    try:
        provider = get_provider(request.args.get("provider"))
    except UnknownProviderError as e:
        return jsonify({"error": str(e)}), 400
    if not provider.supports_credits:
        return jsonify({"error": f"{provider.label}는 크레딧 조회를 지원하지 않습니다."}), 400
    try:
        return jsonify(provider.get_credits(api_key))
    except ProviderAuthError as e:
        return jsonify({"error": str(e)}), 401
    except ProviderRateLimitError as e:
        return jsonify({"error": str(e)}), 429
    except ProviderError as e:
        return jsonify({"error": str(e)}), 400


@gen_bp.route("/models", methods=["GET"])
def get_models():
    api_key = request.headers.get("X-Api-Key", "")
    if not api_key:
        return jsonify({"error": "API 키가 필요합니다."}), 400
    try:
        provider = get_provider(request.args.get("provider"))
    except UnknownProviderError as e:
        return jsonify({"error": str(e)}), 400
    try:
        return jsonify({"models": provider.list_models(api_key)})
    except Exception as e:
        # 목록 조회 실패해도 기본 모델 하나는 고를 수 있게 폴백
        return jsonify({"models": [provider.default_model], "error": str(e)})


# ──────────────────────────────────────────────
# 라우트: 예상문제 생성
# ──────────────────────────────────────────────

class GenerateParamError(Exception):
    """요청 값이 잘못된 경우 — 라우트가 상태 코드와 함께 그대로 응답한다."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _collect_pdfs(field: str):
    """업로드 목록에서 빈 항목을 걸러내고 확장자를 확인한다. (topic_analysis와 같은 규칙)"""
    files = [f for f in request.files.getlist(field) if f and f.filename]
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            raise GenerateParamError(f"PDF 파일만 올릴 수 있습니다: {f.filename}")
    return files


def read_generate_params() -> dict:
    """
    요청에서 생성에 필요한 값을 전부 꺼내 dict로 반환.

    스트리밍 경로는 응답 제너레이터가 **요청 컨텍스트가 닫힌 뒤** 실행되므로,
    PDF 내용까지 여기서 bytes로 읽어둬야 한다. (뒤늦게 request를 만지면 터짐)
    """
    api_key = request.form.get("api_key", "").strip()
    if not api_key:
        raise GenerateParamError("API 키를 입력해주세요.")

    try:
        provider = get_provider(request.form.get("provider"))
    except UnknownProviderError as e:
        raise GenerateParamError(str(e))

    try:
        weight = int(request.form.get("weight", 5))
    except (TypeError, ValueError):
        raise GenerateParamError("기출 반영 강도는 숫자여야 합니다.")

    # 유형별 개수 직접 지정(수동 모드) — 있으면 count/type_targets를 여기서 확정
    manual_targets = None
    type_targets_raw = request.form.get("type_targets", "").strip()
    if type_targets_raw:
        try:
            parsed = json.loads(type_targets_raw)
        except (TypeError, ValueError):
            raise GenerateParamError("유형별 개수 형식이 올바르지 않습니다.")
        if not isinstance(parsed, dict):
            raise GenerateParamError("유형별 개수 형식이 올바르지 않습니다.")
        manual_targets = {}
        for t in QUESTION_TYPES:
            try:
                v = int(parsed.get(t, 0) or 0)
            except (TypeError, ValueError):
                raise GenerateParamError("유형별 개수는 정수여야 합니다.")
            if v < 0:
                raise GenerateParamError("유형별 개수는 0 이상이어야 합니다.")
            manual_targets[t] = v
        count = sum(manual_targets.values())
    else:
        try:
            count = int(request.form.get("count", 5))
        except (TypeError, ValueError):
            raise GenerateParamError("문제 수는 숫자여야 합니다.")

    if not (1 <= count <= 30):
        raise GenerateParamError("문제 수는 1~30개 사이로 설정해주세요.")
    if not (1 <= weight <= 10):
        raise GenerateParamError("기출 반영 강도는 1~10 사이로 설정해주세요.")

    # 이번에 만들 문제 세트 이름 (선택). 비우면 화면에서 '제N회'로 표시된다.
    gen_title = request.form.get("title", "").strip()
    if len(gen_title) > 100:
        raise GenerateParamError("문제 세트 이름은 100자 이내로 입력해주세요.")

    session_id = request.form.get("session_id", "").strip()
    # 분량 초과 경고를 확인하고 다시 온 요청이면 파일 대신 토큰이 온다 (재추출 방지)
    extract_token = request.form.get("extract_token", "").strip()
    lecture_files = []
    exam_files = []
    lecture_name = ""
    if not session_id and not extract_token:
        lectures = _collect_pdfs("lecture")
        exams    = _collect_pdfs("exam")
        if not lectures or not exams:
            raise GenerateParamError("강의자료와 기출문제 파일을 모두 업로드해주세요.")
        if len(lectures) > MAX_FILES_PER_SIDE or len(exams) > MAX_FILES_PER_SIDE:
            raise GenerateParamError(
                f"강의자료·기출은 각각 최대 {MAX_FILES_PER_SIDE}개까지 올릴 수 있습니다.")
        # 요청 컨텍스트가 살아 있을 때 바이트로 읽어둔다
        # (스트리밍 제너레이터는 컨텍스트가 닫힌 뒤에 돈다 — owner를 미리 잡는 것과 같은 이유)
        # 파일을 하나씩 읽고 곧바로 축소한다 — 한꺼번에 다 읽어서 쌓아두면(리스트 컴프리헨션)
        # 제일 큰 원본들이 전부 한때 메모리에 같이 떠 있는다. RAM이 1GB인 배포 환경에서는
        # 이 차이가 크다 (config.MAX_UPLOAD_MB·배포-GCP.md 참고).
        lecture_files = [(f.filename or "강의자료", shrink_pdf_bytes(f.read())) for f in lectures]
        exam_files    = [(f.filename or "기출문제", shrink_pdf_bytes(f.read())) for f in exams]
        lecture_name  = lecture_files[0][0]

    model = request.form.get("model", "").strip() or provider.default_model
    # 분석 단계 전용 모델 — 그림 읽기(extract)와 개념·형식 분석(concepts·format)을 맡는다.
    # 문제를 실제로 만드는 것은 위 model이다.
    #
    # 이 화면에서도 사용자가 고르지 않는다(주제 분석 탭과 같은 방식). 제공사가
    # analysis_model을 선언했으면 그 모델로 고정하고, 아니면 위 model을 그대로 쓴다.
    #   - 고정: 입력 토큰의 90%가 이 단계에서 나가는데(실측 generation 92 — 전체 입력
    #           119,521토큰 중 103,392), 같은 자료로 두 모델을 재보니 싼 쪽이 오히려
    #           나았다. 근거 실측은 providers/jbnu_gateway.py의 ANALYSIS_MODEL 주석.
    #   - 나머지 제공사: 모델 id가 제공사 안에서만 유효해 고정할 값이 없다.
    #           고를 칸을 남기느니 나누기 전처럼 한 모델로 도는 편이 화면이 단순하다.
    # 폼의 analysis_model은 **일부러 읽지 않는다** — 열어둔 옛날 탭이나 직접 만든 요청이
    # 비싼 모델을 그 90%에 밀어넣는 길이 되어선 안 된다.
    analysis_model = getattr(provider, "analysis_model", "") or model

    return {
        "api_key":        api_key,
        "provider":       provider,
        "model":          model,
        "analysis_model": analysis_model,
        "count":          count,
        "weight":         weight,
        "manual_targets": manual_targets,
        # 기출에 있는 유형이 비율 반올림으로 0개가 되는 것을 막는다(자동 배분에서만 의미).
        # 비율을 일부러 왜곡하므로 기본은 꺼짐이고 사용자가 켤 때만 적용한다.
        "preserve_types": request.form.get("preserve_types") == "1",
        "gen_title":      gen_title,
        "session_id":     session_id,
        # 소유자는 요청 컨텍스트가 살아 있을 때 확정해둔다 (스트리밍 제너레이터에서는 못 읽음)
        "owner":          current_owner(),
        "lecture_files":  lecture_files,   # [(파일명, bytes)]
        "exam_files":     exam_files,
        "lecture_name":   lecture_name,    # 세션 이름에 쓸 대표 파일명 (첫 파일)
        "extract_token":  extract_token,   # 있으면 보관해둔 추출 결과를 재사용
        "name":           request.form.get("name", "").strip(),
    }


def _forward_progress(gen, key: str, total: int):
    """
    llm.py의 진행률 제너레이터(완료 개수를 yield하고 결과를 return)를
    화면용 progress 이벤트로 바꿔 흘려보내고, 최종 결과를 돌려준다.
    """
    while True:
        try:
            done = next(gen)
        except StopIteration as stop:
            return stop.value
        yield {"type": "progress", "key": key, "done": done, "total": total}


def _session_base(p: dict) -> str:
    """세션 이름의 앞부분 — 첫 강의자료 파일명(확장자 제거) + 나머지 개수."""
    base = p["lecture_name"].rsplit(".", 1)[0]
    extra = len(p["lecture_files"]) - 1
    return f"{base} 외 {extra}개" if extra > 0 else base


def _join_side(parts, image_mode, by_question=False,
               side_budget=GEN_SIDE_CHAR_BUDGET):
    """
    한쪽(강의자료 전체 / 기출 전체)의 파일별 텍스트를 예산 안에서 잘라 하나로 합친다.

    parts: llm.read_labeled_pdfs가 돌려준 목록. 파일명·페이지·이미지 몫이 다 들어 있어
           파일 목록을 따로 받아 zip하지 않는다 (예전에는 이름만 얻으려고 그랬다).
    예산을 파일 수로 나눠 배정하므로, 파일이 1개면 그 쪽 예산 전부를 쓴다.
    파일이 여럿이면 어디부터 다른 자료인지 LLM이 알 수 있게 파일명 구분선을 넣는다.
    by_question: 기출 쪽이면 True — 상한을 넘길 때 문항 경계로 자른다. 반쪽짜리 문항이
                 남으면 few-shot 예시로 그대로 실려 생성 문제의 본이 된다.
    side_budget: 이 쪽에 배정된 글자 예산. 생략하면 한쪽 몫(GEN_SIDE_CHAR_BUDGET).
                 기출은 강의자료가 남긴 몫을 받으므로 호출부가 remaining_gen_budget()으로
                 계산해 넘긴다 — 칸막이를 고정하면 한쪽에 예산이 남는데 반대쪽만 잘린다.
    반환: (합친 텍스트, 반영 범위 dict, 잘린 파일 목록)
          범위는 파일들을 합산한 값이고, 잘린 목록은 진행 전 경고에 쓴다.
    """
    per_doc = max(GEN_DOC_MIN_CHARS, side_budget // len(parts))
    chunks, chars, used, cuts = [], 0, 0, []
    for p in parts:
        raw = assemble_pdf_text(p["pages"], len(p["img_jobs"]), image_mode,
                                p.get("quota"))
        info = build_source_info(raw, per_doc, by_question)
        chars += info["chars"]
        used  += info["used"]
        # 상한을 넘겼으면 어느 줄이 버려지는지
        cut = truncation_report(raw, per_doc, by_question)
        if cut:
            cuts.append({"name": p["name"], **cut})
        text = truncate(raw, per_doc, by_question)
        chunks.append(f"===== {p['name']} =====\n{text}" if len(parts) > 1 else text)
    return "\n\n".join(chunks), {
        "chars": chars,
        "used": used,
        "limit": per_doc * len(parts),
        "truncated": used < chars,
        "coverage": round(used / chars * 100) if chars else 100,
    }, cuts


def _image_warnings(parts, side: str) -> list:
    """
    상한에 걸려 못 읽은 그림·필기 쪽이 있는 파일을 화면용 경고로.

    상한 자체는 '한쪽 전체'에 걸리지만(llm.read_labeled_pdfs), 경고는 파일마다 따로
    낸다 — 사용자가 확인할 것은 "어느 파일의 몇 쪽이 안 읽혔나"이지 합계가 아니다.
    없으면 빈 리스트 — 그때는 확인 모달이 안 뜬다.
    """
    out = []
    for p in parts:
        cov = image_coverage(p["pages"], p["img_jobs"])
        if cov["skipped"]:
            out.append({"kind": "image", "side": side, "name": p["name"], **cov})
    return out


def _batch_targets(type_slots, offset, batch_count) -> dict:
    """이번 배치가 가져갈 유형별 개수. 슬롯을 잘라 세므로 전체 합계가 보존된다."""
    if type_slots is None:
        return {}
    batch_slice = type_slots[offset: offset + batch_count]
    return {t: batch_slice.count(t) for t in QUESTION_TYPES if batch_slice.count(t)}


def run_generation_events(p: dict):
    """
    문제 생성 파이프라인을 진행 이벤트를 내보내는 제너레이터로 실행.

    이벤트 종류:
      {"type": "stage",    "key": ..., "status": "active"|"done", ...}
      {"type": "analysis", "payload": {...}}      분석 결과 (경로 B에서만)
      {"type": "question", "index": n, "total": N, "question": {...}}
      {"type": "done",     "payload": {...}}      기존 /generate 응답과 동일한 형태
      {"type": "error",    "message": ..., "status": 4xx}

    /generate 는 이 제너레이터를 끝까지 소진해 done 페이로드만 반환하고,
    /generate/stream 은 각 이벤트를 그대로 흘려보낸다 → 로직이 한 벌만 존재한다.
    """
    api_key, provider = p["api_key"], p["provider"]
    model, count, weight = p["model"], p["count"], p["weight"]
    # 분석·이미지 설명용 모델. 지정하지 않았으면 생성 모델과 같다.
    # (.get 으로 읽는 이유 — 이 키가 없던 시절의 호출부/테스트도 그대로 돌게)
    analysis_model = p.get("analysis_model") or model
    session_id, owner = p["session_id"], p["owner"]

    # 이번 생성에서 쓴 토큰을 단계별로 모은다. 오류·취소로 끝나도 그 시점까지의
    # 사용량은 알려줘야 하므로 try 바깥에 둔다 (except 절에서도 참조).
    usage = UsageCollector()

    # 크레딧으로 과금하는 제공사는 생성 전 잔액을 먼저 찍어둔다 (LLM 호출 이전이어야
    # 이번 생성분만 차이로 잡힌다). 지원하지 않는 제공사는 None.
    credits_before = credits_snapshot(provider, api_key)

    try:
        # ── 경로 A: 저장된 세션 재사용 (분석 LLM 호출 0회 → 토큰 절약) ──
        if session_id:
            analysis = load_session(int(session_id), owner)   # 본인 세션만 재사용 가능
            if not analysis:
                yield {"type": "error", "status": 404,
                       "message": "세션을 찾을 수 없습니다. 새로 분석해주세요."}
                return
            reused = True
        # ── 경로 B: 새 파일 업로드 → 분석 후 세션 저장 ──
        else:
            usage.set_stage("extract")
            yield {"type": "stage", "key": "extract", "status": "active"}

            # 분량 초과 경고를 확인하고 돌아온 요청 — 보관해둔 추출 결과를 그대로 쓴다.
            # 다시 추출하면 손글씨 전사(Vision) 값을 두 번 낸다.
            cached = extract_cache.take(p["extract_token"], owner) if p["extract_token"] else None
            if p["extract_token"] and not cached:
                yield {"type": "error", "status": 400,
                       "message": "확인 시간이 지났습니다. 파일을 다시 올려 생성해주세요."}
                return
            if cached:
                lecture_text   = cached["lecture_text"]
                exam_text      = cached["exam_text"]
                source_info    = cached["source_info"]
                session_base   = cached["session_base"]
                usage          = cached["usage"]        # 1단계에서 쓴 몫까지 이어서 센다
                credits_before = cached["credits_before"]
                # 추출은 이미 끝났으므로 진행률은 100%로 채워 보낸다
                imgs = cached["img_count"]
                yield {"type": "progress", "key": "extract", "done": imgs, "total": imgs}
            else:
                # 강의자료: 그림 속 글자만 그대로 전사 (손글씨·판서 보존). 그림 해설은 안 한다 —
                #  해설은 LLM이 지어낸 문장이라 강의자료에 없는 용어를 끌어들인다.
                # 기출문제: 이미지/그림 페이지는 Vision LLM 설명으로 보존
                #  (예: 신체 부위 그림 → 부위 이름 쓰기 문제 등)
                # 여기까지는 LLM 호출이 없다 (페이지 선정만) — 진행률 총계를 먼저 알아야 하므로
                # 양쪽 파일을 모두 읽어 대상 페이지를 센 뒤에 Vision 호출을 시작한다.
                #
                # 이미지 상한은 **한쪽 전체**에 걸린다 (파일당이 아니다).
                # 읽기·예산 배분은 주제 분석과 같은 함수를 쓴다 — llm.read_labeled_pdfs.
                lec_parts  = read_labeled_pdfs(p["lecture_files"], "강의자료",
                                               api_key, IMAGE_TRANSCRIBE)
                exam_parts = read_labeled_pdfs(p["exam_files"], "기출",
                                               api_key, IMAGE_DESCRIBE)

                total_imgs = sum(len(x["img_jobs"]) for x in lec_parts + exam_parts)
                yield {"type": "progress", "key": "extract", "done": 0, "total": total_imgs}
                done_imgs = 0
                for parts, mode in ((lec_parts, IMAGE_TRANSCRIBE),
                                    (exam_parts, IMAGE_DESCRIBE)):
                    for x in parts:
                        for d in describe_images_progressively(x["img_jobs"], api_key,
                                                               analysis_model,
                                                               provider, usage, mode):
                            yield {"type": "progress", "key": "extract",
                                   "done": done_imgs + d, "total": total_imgs}
                        done_imgs += len(x["img_jobs"])

                lecture_text, lecture_src, lecture_cuts = _join_side(
                    lec_parts, IMAGE_TRANSCRIBE)
                # 기출은 강의자료가 쓰고 남긴 몫까지 받는다 — 예산을 한쪽씩 고정하면
                # 강의자료에 수만 자가 놀고 있는데 기출만 잘린다(실측: 기출 반영률 63%,
                # 같은 시점 강의자료는 제 예산의 55%만 사용). 기출이 잘리면 유형통계·
                # 대표문제·빈출포인트가 전부 잘린 텍스트 위에서 계산되므로 손해가 크다.
                # 강의자료를 먼저 합친 지금은 실사용량이 확정돼 있어 여기서 계산할 수 있다.
                exam_text, exam_src, exam_cuts = _join_side(
                    exam_parts, IMAGE_DESCRIBE, by_question=True,
                    side_budget=remaining_gen_budget(lecture_text))
                source_info = {
                    "lecture": lecture_src,   # 원문 반영 범위 (파일 여러 개면 합산)
                    "exam":    exam_src,
                }
                session_base = _session_base(p)

                # 상한을 넘겨 일부가 버려지는 파일이 있으면 진행 전에 물어본다.
                # 추출 결과를 잠시 보관하고 토큰만 내려보낸다 — 확인 후 재추출하면 Vision 재과금.
                # 글자수 초과와 그림 쪽수 초과를 한 목록에 담는다 (kind로 구분) —
                # 사용자에게는 "이대로 진행할까"라는 같은 질문이라 모달을 두 번 띄우면 안 된다.
                warnings = ([{"kind": "text", "side": "강의자료", **c} for c in lecture_cuts]
                            + [{"kind": "text", "side": "기출문제", **c} for c in exam_cuts]
                            + _image_warnings(lec_parts, "강의자료")
                            + _image_warnings(exam_parts, "기출문제"))
                if warnings:
                    token = extract_cache.put({
                        "lecture_text": lecture_text, "exam_text": exam_text,
                        "source_info": source_info, "session_base": session_base,
                        "usage": usage, "credits_before": credits_before,
                        "img_count": total_imgs,
                    }, owner)
                    yield {"type": "needs_confirm", "payload": {
                        "extract_token": token,
                        "warnings": warnings,
                        "usage": usage.summary(),
                        "credits": credits_result(
                            credits_before, credits_snapshot(provider, api_key)),
                    }}
                    return

            yield {"type": "stage", "key": "extract", "status": "done",
                   "source_info": source_info}

            usage.set_stage("concepts")
            yield {"type": "stage", "key": "concepts", "status": "active"}
            yield {"type": "progress", "key": "concepts", "done": 0, "total": 2}
            gen1 = analyze_concepts_progressively(lecture_text, exam_text,
                                                  api_key, analysis_model, provider,
                                                  usage)
            part1 = yield from _forward_progress(gen1, "concepts", 2)
            yield {"type": "stage", "key": "concepts", "status": "done"}

            usage.set_stage("format")
            yield {"type": "stage", "key": "format", "status": "active"}
            # 예전에는 2회(예시 추출 → 형식 분석)였다. 예시 추출이 1단계로 합쳐지면서
            # 여기는 형식 분석 1회만 남았다 — 기출 전문을 다시 보내지 않는다.
            yield {"type": "progress", "key": "format", "done": 0, "total": 1}
            gen2 = analyze_format_progressively(part1["sample_questions"],
                                                api_key, analysis_model, provider,
                                                usage)
            part2 = yield from _forward_progress(gen2, "format", 1)
            yield {"type": "stage", "key": "format", "status": "done"}

            analysis = {**part1, **part2, "source_info": source_info}

            # 세션 저장 (이름: 사용자 지정 or 파일명+시각) — 현재 소유자(로그인/게스트) 소유
            name = p["name"] or f"{session_base} · {datetime.now().strftime('%m/%d %H:%M')}"
            session_id = save_session(name, model, analysis, owner, provider.name)
            analysis["name"] = name
            reused = False

        type_stats = analysis.get("type_stats", {})
        type_targets = (p["manual_targets"] if p["manual_targets"] is not None
                        else compute_type_targets(type_stats, count,
                                                  p["preserve_types"]))

        yield {"type": "analysis", "payload": {
            "session_id":       session_id,
            "session_name":     analysis.get("name", ""),
            "reused":           reused,
            "concepts":         analysis.get("concepts", {}),
            "exam_concepts":    analysis.get("exam_concepts", {}),
            "priority_topics":  analysis.get("priority_topics", []),
            "type_stats":       type_stats,
            "type_targets":     type_targets,
            "source_info":      analysis.get("source_info", {}),
            "sample_questions": analysis.get("sample_questions", ""),
            "format_analysis":  analysis.get("format_analysis", ""),
        }}

        # ── 공통: 예상문제 생성 (분석 자산 재사용) ──
        # 문제마다 증례 지문+선택지+해설+함정포인트를 요구하는 출력 포맷이라
        # count가 커지면 한 번의 LLM 호출로는 max_tokens을 넘겨 응답이 잘리고
        # (---END--- 누락) 파싱이 전부 버려버리는 문제가 있었다.
        # → GEN_BATCH_SIZE 문제씩 여러 번 호출해 잘림을 방지하고 결과를 합친다.
        #
        # 유형별 목표를 슬롯 리스트로 펼쳐서 배치 단위로 잘라내면(비율 재계산 없이)
        # 각 유형의 합계가 배치를 나눠도 원래 type_targets와 정확히 일치한다.
        type_slots = None
        if type_targets:
            type_slots = []
            for t in QUESTION_TYPES:
                type_slots.extend([t] * type_targets.get(t, 0))

        usage.set_stage("generate")
        yield {"type": "stage", "key": "generate", "status": "active", "total": count}

        questions, raw_parts = [], []
        remaining, offset = count, 0
        while remaining > 0:
            batch_count = min(GEN_BATCH_SIZE, remaining)
            # (고정 접두부, 이번 배치 지시) — 접두부는 배치마다 동일해서 캐시가 걸린다
            batch_head, batch_tail = build_question_generation_prompt(
                analysis.get("concepts", {}),
                analysis.get("sample_questions", ""),
                analysis.get("format_analysis", ""),
                batch_count,
                analysis.get("exam_concepts", {}),
                analysis.get("priority_topics", []),
                weight, _batch_targets(type_slots, offset, batch_count),
                # 앞 배치들이 만든 문제를 넘겨 같은 문제가 다시 나오지 않게 한다.
                # questions 는 배치를 가로질러 누적되므로 여기서 그대로 쓸 수 있다.
                avoid_questions=[q.get("문제", "") for q in questions],
            )
            # 조각을 받는 즉시 파싱해, 완성된 문제부터 화면에 내보낸다
            parser = StreamingQuestionParser()
            batch_raw = []
            for piece in call_llm_stream(batch_tail, api_key, model,
                                         provider=provider, max_tokens=GEN_MAX_TOKENS,
                                         usage=usage, cache_prefix=batch_head):
                batch_raw.append(piece)
                for q in parser.feed(piece):
                    questions.append(q)
                    q["번호"] = str(len(questions))   # 배치를 가로질러 이어지는 번호
                    yield {"type": "question", "index": len(questions),
                           "total": count, "question": q}
            raw_parts.append("".join(batch_raw))
            offset += batch_count
            remaining -= batch_count

        yield {"type": "stage", "key": "generate", "status": "done",
               "generated": len(questions)}

        question_raw = "\n\n".join(raw_parts)

        # 응답과 보관에 같은 값을 쓴다 (한 번만 조회 — 잔액 조회가 왕복 요청이라서)
        gen_usage = usage.summary()
        gen_credits = credits_result(credits_before,
                                     credits_snapshot(provider, api_key))

        generation_id = save_generation(
            int(session_id), count, weight, model, type_targets, questions,
            question_raw, provider.name, p["gen_title"],
            usage=gen_usage,
            # 잔액은 시간이 지나면 틀린 값이 되므로 쓴 만큼만 남긴다
            credits=credits_for_history(gen_credits),
        )

        yield {"type": "done", "payload": {
            "success":          True,
            "session_id":       session_id,          # 재사용용 세션 id
            "session_name":     analysis.get("name", ""),
            "generation_id":    generation_id,       # 방금 저장된 이력 id
            "title":            p["gen_title"],      # 사용자가 붙인 이름 (없으면 "")
            # 이름을 안 붙였을 때 결과 화면이 '제N회'로 대신 부르는 데 쓴다
            "ordinal":          generation_ordinal(session_id, generation_id),
            "reused":           reused,              # 저장된 세션 재사용 여부
            "concepts":         analysis.get("concepts", {}),
            "exam_concepts":    analysis.get("exam_concepts", {}),
            "priority_topics":  analysis.get("priority_topics", []),
            "type_stats":       type_stats,
            "type_targets":     type_targets,
            "source_info":      analysis.get("source_info", {}),  # 원문 반영 범위
            "sample_questions": analysis.get("sample_questions", ""),
            "format_analysis":  analysis.get("format_analysis", ""),
            "questions":        questions,
            "raw":              question_raw,
            "model":            model,
            "provider":         provider.name,
            "weight":           weight,
            "usage":            gen_usage,           # 이번 생성에 쓴 토큰
            # 크레딧 과금 제공사만 채워진다. 있으면 화면이 토큰 대신 이걸 보여준다.
            "credits":          gen_credits,
        }}

    # 스트리밍은 HTTP 상태를 이미 보낸 뒤라 오류도 이벤트로 흘려보내야 한다
    except ProviderAuthError as e:
        # 키가 틀린 경우엔 잔액 조회도 실패하므로 credits는 None이 된다
        yield {"type": "error", "status": 401, "message": str(e),
               "usage": usage.summary(), "credits": None}
    except ProviderRateLimitError as e:
        yield {"type": "error", "status": 429, "message": str(e),
               "usage": usage.summary(),
               "credits": credits_result(credits_before,
                                          credits_snapshot(provider, api_key))}
    except ProviderError as e:
        yield {"type": "error", "status": 400, "message": str(e),
               "usage": usage.summary(),
               "credits": credits_result(credits_before,
                                          credits_snapshot(provider, api_key))}
    except ValueError as e:
        # 손상·암호 걸린 PDF 등 사용자가 고칠 수 있는 입력 문제 (llm.read_labeled_pdfs가
        # 어느 파일인지까지 넣어 던진다). 500 '서버 오류'로 뭉개면 고칠 방법이 안 보인다.
        yield {"type": "error", "status": 400, "message": str(e),
               "usage": usage.summary(),
               "credits": credits_result(credits_before,
                                          credits_snapshot(provider, api_key))}
    except Exception as e:
        yield {"type": "error", "status": 500, "message": f"서버 오류: {str(e)}",
               "usage": usage.summary(),
               "credits": credits_result(credits_before,
                                          credits_snapshot(provider, api_key))}


@gen_bp.route("/generate", methods=["POST"])
def generate():
    """비스트리밍 경로 — 같은 파이프라인을 끝까지 돌린 뒤 결과만 한 번에 반환."""
    try:
        params = read_generate_params()
    except GenerateParamError as e:
        return jsonify({"error": str(e)}), e.status

    for ev in run_generation_events(params):
        if ev["type"] == "error":
            return jsonify({"error": ev["message"],
                            "usage": ev.get("usage"),
                            "credits": ev.get("credits")}), ev["status"]
        # 분량 초과 — 진행 여부를 물어야 하므로 결과 대신 확인 요청을 돌려준다
        if ev["type"] == "needs_confirm":
            return jsonify({"needs_confirm": True, **ev["payload"]})
        if ev["type"] == "done":
            return jsonify(ev["payload"])
    return jsonify({"error": "생성 결과를 받지 못했습니다."}), 500


@gen_bp.route("/generate/stream", methods=["POST"])
def generate_stream():
    """
    스트리밍 경로 — 진행 상황과 완성된 문제를 SSE로 흘려보낸다.

    EventSource는 GET만 지원하는데 여기는 PDF를 multipart POST로 받으므로,
    프런트는 fetch + ReadableStream으로 읽는다. (형식은 SSE 그대로)
    """
    # 파라미터 오류는 스트림을 열기 전이라 정상적인 HTTP 상태 코드로 응답할 수 있다.
    # 스트림이 시작된 뒤(200 전송 후)의 오류는 error 이벤트로만 알릴 수 있다.
    try:
        params = read_generate_params()
    except GenerateParamError as e:
        return jsonify({"error": str(e)}), e.status

    def sse():
        try:
            for ev in run_generation_events(params):
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
