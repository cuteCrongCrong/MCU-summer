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

from flask import Blueprint, request, jsonify

from db import get_conn, owner_clause, LEGACY_PROVIDER
# current_owner: (user_id, guest_id) 튜플. 게스트도 브라우저별로 서로 격리된다.
from features.auth import current_owner
from providers.base import (
    ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.factory import (
    DEFAULT_PROVIDER, UnknownProviderError, get_provider, list_providers,
)
from llm import (
    QUESTION_TYPES,
    extract_text_from_pdf, truncate, build_source_info,
    run_analysis, compute_type_targets,
    build_question_generation_prompt, call_llm, parse_questions,
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
                    provider: str = None) -> int:
    """한 번의 문제 생성 결과를 세션에 연결해 이력으로 저장."""
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO generations
               (session_id, created_at, count, weight, model, type_targets,
                questions, raw, provider)
               VALUES (?,?,?,?,?,?,?,?,?)""",
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
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_generations(session_id: int, owner) -> list:
    """세션의 생성 이력 목록. 세션을 소유한 경우에만 반환(아니면 빈 목록)."""
    frag, params = owner_clause(owner)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"""SELECT id, created_at, count, weight, model, provider,
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


@gen_bp.route("/generation/<int:gid>", methods=["DELETE"])
def generation_delete(gid):
    delete_generation(gid, current_owner())
    return jsonify({"success": True})


# ── 라우트: 프로바이더 · 모델 목록 ──

@gen_bp.route("/providers", methods=["GET"])
def get_providers():
    """선택 가능한 LLM 프로바이더 목록 (표시명·기본 모델·키 안내 문구)."""
    return jsonify({"providers": list_providers(), "default": DEFAULT_PROVIDER})


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

@gen_bp.route("/generate", methods=["POST"])
def generate():
    try:
        api_key    = request.form.get("api_key", "").strip()
        weight     = int(request.form.get("weight", 5))
        session_id = request.form.get("session_id", "").strip()
        owner      = current_owner()

        try:
            provider = get_provider(request.form.get("provider"))
        except UnknownProviderError as e:
            return jsonify({"error": str(e)}), 400
        model = request.form.get("model", "").strip() or provider.default_model

        if not api_key:
            return jsonify({"error": "API 키를 입력해주세요."}), 400

        # 유형별 개수 직접 지정(수동 모드) — 있으면 count/type_targets를 여기서 확정
        manual_targets = None
        type_targets_raw = request.form.get("type_targets", "").strip()
        if type_targets_raw:
            try:
                parsed = json.loads(type_targets_raw)
            except (TypeError, ValueError):
                return jsonify({"error": "유형별 개수 형식이 올바르지 않습니다."}), 400
            if not isinstance(parsed, dict):
                return jsonify({"error": "유형별 개수 형식이 올바르지 않습니다."}), 400
            manual_targets = {}
            for t in QUESTION_TYPES:
                try:
                    v = int(parsed.get(t, 0) or 0)
                except (TypeError, ValueError):
                    return jsonify({"error": "유형별 개수는 정수여야 합니다."}), 400
                if v < 0:
                    return jsonify({"error": "유형별 개수는 0 이상이어야 합니다."}), 400
                manual_targets[t] = v
            count = sum(manual_targets.values())
        else:
            count = int(request.form.get("count", 5))

        if not (1 <= count <= 30):
            return jsonify({"error": "문제 수는 1~30개 사이로 설정해주세요."}), 400
        if not (1 <= weight <= 10):
            return jsonify({"error": "기출 반영 강도는 1~10 사이로 설정해주세요."}), 400

        # ── 경로 A: 저장된 세션 재사용 (분석 LLM 호출 0회 → 토큰 절약) ──
        if session_id:
            analysis = load_session(int(session_id), owner)   # 본인 세션만 재사용 가능
            if not analysis:
                return jsonify({"error": "세션을 찾을 수 없습니다. 새로 분석해주세요."}), 404
            reused = True
        # ── 경로 B: 새 파일 업로드 → 분석 후 세션 저장 ──
        else:
            lecture_file = request.files.get("lecture")
            exam_file    = request.files.get("exam")
            if not lecture_file or not exam_file:
                return jsonify({"error": "강의자료와 기출문제 파일을 모두 업로드해주세요."}), 400

            # 강의자료: 텍스트만 추출 (이미지 설명 생략)
            lecture_raw = extract_text_from_pdf(lecture_file, api_key, model,
                                                describe_images=False, provider=provider)
            # 기출문제: 이미지/그림 페이지는 Vision LLM 설명으로 보존
            #  (예: 신체 부위 그림 → 부위 이름 쓰기 문제 등)
            exam_raw    = extract_text_from_pdf(exam_file, api_key, model,
                                                describe_images=True, provider=provider)
            lecture_text = truncate(lecture_raw)
            exam_text    = truncate(exam_raw)
            analysis = run_analysis(lecture_text, exam_text, api_key, model, provider)
            # 원문 반영 범위(전체 읽었는지/일부만인지) 기록
            analysis["source_info"] = {
                "lecture": build_source_info(lecture_raw),
                "exam":    build_source_info(exam_raw),
            }

            # 세션 저장 (이름: 사용자 지정 or 파일명+시각) — 현재 사용자 소유
            base = (lecture_file.filename or "강의자료").rsplit(".", 1)[0]
            name = request.form.get("name", "").strip() or \
                   f"{base} · {datetime.now().strftime('%m/%d %H:%M')}"
            session_id = save_session(name, model, analysis, owner, provider.name)
            analysis["name"] = name
            reused = False

        # ── 공통: 예상문제 생성 (분석 자산 재사용) ──
        # 문제마다 증례 지문+선택지+해설+함정포인트를 요구하는 출력 포맷이라
        # count가 커지면 한 번의 LLM 호출로는 max_tokens을 넘겨 응답이 잘리고
        # (---END--- 누락) parse_questions()가 전부 버려버리는 문제가 있었다.
        # → GEN_BATCH_SIZE 문제씩 여러 번 호출해 잘림을 방지하고 결과를 합친다.
        type_stats   = analysis.get("type_stats", {})
        type_targets = manual_targets if manual_targets is not None else compute_type_targets(type_stats, count)

        # 유형별 목표를 슬롯 리스트로 펼쳐서 배치 단위로 잘라내면(비율 재계산 없이)
        # 각 유형의 합계가 배치를 나눠도 원래 type_targets와 정확히 일치한다.
        type_slots = None
        if type_targets:
            type_slots = []
            for t in QUESTION_TYPES:
                type_slots.extend([t] * type_targets.get(t, 0))

        questions = []
        raw_parts = []
        remaining = count
        offset = 0
        while remaining > 0:
            batch_count  = min(GEN_BATCH_SIZE, remaining)
            if type_slots is not None:
                batch_slice = type_slots[offset: offset + batch_count]
                batch_targets = {t: batch_slice.count(t) for t in QUESTION_TYPES if batch_slice.count(t) > 0}
            else:
                batch_targets = {}
            batch_prompt = build_question_generation_prompt(
                analysis.get("concepts", {}),
                analysis.get("sample_questions", ""),
                analysis.get("format_analysis", ""),
                batch_count,
                analysis.get("exam_concepts", {}),
                analysis.get("priority_topics", []),
                weight, batch_targets,
            )
            batch_raw = call_llm(batch_prompt, api_key, model,
                                 provider=provider, max_tokens=GEN_MAX_TOKENS)
            raw_parts.append(batch_raw)
            questions.extend(parse_questions(batch_raw))
            offset += batch_count
            remaining -= batch_count

        for i, q in enumerate(questions, start=1):
            q["번호"] = str(i)
        question_raw = "\n\n".join(raw_parts)

        # 생성 이력 저장 (세션에 연결)
        generation_id = save_generation(
            int(session_id), count, weight, model, type_targets, questions,
            question_raw, provider.name,
        )

        return jsonify({
            "success":          True,
            "session_id":       session_id,          # 재사용용 세션 id
            "session_name":     analysis.get("name", ""),
            "generation_id":    generation_id,       # 방금 저장된 이력 id
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
        })

    except ProviderAuthError as e:
        return jsonify({"error": str(e)}), 401
    except ProviderRateLimitError as e:
        return jsonify({"error": str(e)}), 429
    except ProviderError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"서버 오류: {str(e)}"}), 500
