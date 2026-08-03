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

from db import get_conn, owner_clause, LEGACY_PROVIDER
# current_owner: (user_id, guest_id) 튜플. 게스트도 브라우저별로 서로 격리된다.
from features.auth import current_owner
from providers.base import (
    ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.factory import (
    DEFAULT_PROVIDER, UnknownProviderError, get_provider, list_providers,
)
from providers.usage import UsageCollector, credits_result, credits_snapshot
from llm import (
    QUESTION_TYPES, StreamingQuestionParser,
    read_pdf_pages, describe_images_progressively, assemble_pdf_text,
    truncate, build_source_info,
    analyze_concepts_progressively, analyze_exam_format_progressively,
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
                    provider: str = None, title: str = None) -> int:
    """
    한 번의 문제 생성 결과를 세션에 연결해 이력으로 저장.
    title은 사용자가 붙인 이름 — 비우면 NULL로 두고 화면에서 '제N회'로 대체한다.
    """
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO generations
               (session_id, created_at, count, weight, model, type_targets,
                questions, raw, provider, title)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
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
            ),
        )
        conn.commit()
        return cur.lastrowid
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
    lecture_bytes = exam_bytes = None
    lecture_name = ""
    if not session_id:
        lecture_file = request.files.get("lecture")
        exam_file    = request.files.get("exam")
        if not lecture_file or not exam_file:
            raise GenerateParamError("강의자료와 기출문제 파일을 모두 업로드해주세요.")
        lecture_bytes = lecture_file.read()
        exam_bytes    = exam_file.read()
        lecture_name  = lecture_file.filename or "강의자료"

    return {
        "api_key":        api_key,
        "provider":       provider,
        "model":          request.form.get("model", "").strip() or provider.default_model,
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
        "lecture_bytes":  lecture_bytes,
        "exam_bytes":     exam_bytes,
        "lecture_name":   lecture_name,
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
            # 강의자료: 텍스트만 추출 (이미지 설명 생략)
            lecture_pages, _ = read_pdf_pages(p["lecture_bytes"], api_key,
                                              describe_images=False)
            lecture_raw = assemble_pdf_text(lecture_pages)
            # 기출문제: 이미지/그림 페이지는 Vision LLM 설명으로 보존
            #  (예: 신체 부위 그림 → 부위 이름 쓰기 문제 등)
            exam_pages, img_jobs = read_pdf_pages(p["exam_bytes"], api_key,
                                                  describe_images=True)
            total_imgs = len(img_jobs)
            yield {"type": "progress", "key": "extract", "done": 0, "total": total_imgs}
            for done_imgs in describe_images_progressively(img_jobs, api_key, model,
                                                          provider, usage):
                yield {"type": "progress", "key": "extract",
                       "done": done_imgs, "total": total_imgs}
            exam_raw = assemble_pdf_text(exam_pages, total_imgs)

            lecture_text = truncate(lecture_raw)
            exam_text    = truncate(exam_raw)
            source_info = {
                "lecture": build_source_info(lecture_raw),   # 원문 반영 범위
                "exam":    build_source_info(exam_raw),
            }
            yield {"type": "stage", "key": "extract", "status": "done",
                   "source_info": source_info}

            usage.set_stage("concepts")
            yield {"type": "stage", "key": "concepts", "status": "active"}
            yield {"type": "progress", "key": "concepts", "done": 0, "total": 2}
            gen1 = analyze_concepts_progressively(lecture_text, exam_text,
                                                  api_key, model, provider, usage)
            part1 = yield from _forward_progress(gen1, "concepts", 2)
            yield {"type": "stage", "key": "concepts", "status": "done"}

            usage.set_stage("format")
            yield {"type": "stage", "key": "format", "status": "active"}
            yield {"type": "progress", "key": "format", "done": 0, "total": 2}
            gen2 = analyze_exam_format_progressively(exam_text, part1["type_stats"],
                                                     api_key, model, provider, usage)
            part2 = yield from _forward_progress(gen2, "format", 2)
            yield {"type": "stage", "key": "format", "status": "done"}

            analysis = {**part1, **part2, "source_info": source_info}

            # 세션 저장 (이름: 사용자 지정 or 파일명+시각) — 현재 소유자(로그인/게스트) 소유
            base = p["lecture_name"].rsplit(".", 1)[0]
            name = p["name"] or f"{base} · {datetime.now().strftime('%m/%d %H:%M')}"
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
            batch_prompt = build_question_generation_prompt(
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
            for piece in call_llm_stream(batch_prompt, api_key, model,
                                         provider=provider, max_tokens=GEN_MAX_TOKENS,
                                         usage=usage):
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
        generation_id = save_generation(
            int(session_id), count, weight, model, type_targets, questions,
            question_raw, provider.name, p["gen_title"],
        )

        yield {"type": "done", "payload": {
            "success":          True,
            "session_id":       session_id,          # 재사용용 세션 id
            "session_name":     analysis.get("name", ""),
            "generation_id":    generation_id,       # 방금 저장된 이력 id
            "title":            p["gen_title"],      # 사용자가 붙인 이름 (없으면 "")
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
            "usage":            usage.summary(),      # 이번 생성에 쓴 토큰
            # 크레딧 과금 제공사만 채워진다. 있으면 화면이 토큰 대신 이걸 보여준다.
            "credits":          credits_result(credits_before,
                                                credits_snapshot(provider, api_key)),
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
