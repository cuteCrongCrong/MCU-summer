"""
오답 노트 기능 — 폴더 + 폴더에 담긴 문제의 DB 접근 + HTTP 라우트.

담당 테이블: wrong_folders, wrong_items
연결 계약: 저장하는 question dict은 문제 생성 결과 형식을 그대로 따름
           (문제/선택지/정답/해설/함정포인트/유형). CONTRIBUTING.md 4-B 참고.
"""

import json
from datetime import datetime

from flask import Blueprint, request, jsonify

from db import get_conn

wrong_bp = Blueprint("wrong", __name__)


# ──────────────────────────────────────────────
# 오답 노트 CRUD (폴더 + 담긴 문제)
# ──────────────────────────────────────────────

def create_wrong_folder(name: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO wrong_folders (name, created_at) VALUES (?,?)",
            (name, datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_wrong_folders() -> list:
    """오답 폴더 목록 + 각 폴더의 문제 개수. 최신순."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT f.id, f.name, f.created_at, COUNT(i.id) AS item_count
               FROM wrong_folders f
               LEFT JOIN wrong_items i ON i.folder_id = f.id
               GROUP BY f.id
               ORDER BY f.id DESC"""
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "item_count": r["item_count"],
        }
        for r in rows
    ]


def load_wrong_folder(fid: int):
    """폴더 하나 + 담긴 문제 전체. 없으면 None."""
    conn = get_conn()
    try:
        folder = conn.execute(
            "SELECT * FROM wrong_folders WHERE id=?", (fid,)
        ).fetchone()
        if not folder:
            return None
        rows = conn.execute(
            "SELECT * FROM wrong_items WHERE folder_id=? ORDER BY id ASC", (fid,)
        ).fetchall()
    finally:
        conn.close()
    items = [
        {
            "id": r["id"],
            "added_at": r["added_at"],
            "question": json.loads(r["question"] or "{}"),
        }
        for r in rows
    ]
    return {
        "id": folder["id"],
        "name": folder["name"],
        "created_at": folder["created_at"],
        "items": items,
        "questions": [it["question"] for it in items],
    }


def rename_wrong_folder(fid: int, name: str):
    conn = get_conn()
    try:
        conn.execute("UPDATE wrong_folders SET name=? WHERE id=?", (name, fid))
        conn.commit()
    finally:
        conn.close()


def delete_wrong_folder(fid: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wrong_items WHERE folder_id=?", (fid,))
        conn.execute("DELETE FROM wrong_folders WHERE id=?", (fid,))
        conn.commit()
    finally:
        conn.close()


def add_wrong_item(fid: int, question: dict) -> dict:
    """폴더에 문제 추가. 같은 문제(문제 본문 기준)가 이미 있으면 중복 저장하지 않음."""
    conn = get_conn()
    try:
        folder = conn.execute(
            "SELECT id FROM wrong_folders WHERE id=?", (fid,)
        ).fetchone()
        if not folder:
            return {"error": "폴더를 찾을 수 없습니다.", "status": 404}

        q_text = (question.get("문제") or "").strip()
        if q_text:
            for r in conn.execute(
                "SELECT question FROM wrong_items WHERE folder_id=?", (fid,)
            ).fetchall():
                existing = json.loads(r["question"] or "{}")
                if (existing.get("문제") or "").strip() == q_text:
                    return {"duplicate": True}

        cur = conn.execute(
            "INSERT INTO wrong_items (folder_id, question, added_at) VALUES (?,?,?)",
            (
                fid,
                json.dumps(question or {}, ensure_ascii=False),
                datetime.now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        conn.commit()
        return {"item_id": cur.lastrowid}
    finally:
        conn.close()


def delete_wrong_item(item_id: int):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM wrong_items WHERE id=?", (item_id,))
        conn.commit()
    finally:
        conn.close()


# ──────────────────────────────────────────────
# 라우트
# ──────────────────────────────────────────────

@wrong_bp.route("/wrong-folders", methods=["GET"])
def wrong_folders_list():
    return jsonify({"folders": list_wrong_folders()})


@wrong_bp.route("/wrong-folders", methods=["POST"])
def wrong_folder_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "폴더 이름을 입력하세요."}), 400
    fid = create_wrong_folder(name)
    return jsonify({"success": True, "id": fid, "name": name})


@wrong_bp.route("/wrong-folders/<int:fid>", methods=["GET"])
def wrong_folder_get(fid):
    folder = load_wrong_folder(fid)
    if not folder:
        return jsonify({"error": "폴더를 찾을 수 없습니다."}), 404
    return jsonify(folder)


@wrong_bp.route("/wrong-folders/<int:fid>/rename", methods=["POST"])
def wrong_folder_rename(fid):
    name = (request.form.get("name") or "").strip()
    if not name:
        return jsonify({"error": "폴더 이름을 입력하세요."}), 400
    rename_wrong_folder(fid, name)
    return jsonify({"success": True})


@wrong_bp.route("/wrong-folders/<int:fid>", methods=["DELETE"])
def wrong_folder_delete(fid):
    delete_wrong_folder(fid)
    return jsonify({"success": True})


@wrong_bp.route("/wrong-folders/<int:fid>/items", methods=["POST"])
def wrong_item_add(fid):
    raw = request.form.get("question", "")
    try:
        question = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        return jsonify({"error": "문제 데이터가 올바르지 않습니다."}), 400
    if not question or not question.get("문제"):
        return jsonify({"error": "저장할 문제 내용이 없습니다."}), 400
    result = add_wrong_item(fid, question)
    if result.get("error"):
        return jsonify({"error": result["error"]}), result.get("status", 400)
    if result.get("duplicate"):
        return jsonify({"success": True, "duplicate": True})
    return jsonify({"success": True, "item_id": result["item_id"]})


@wrong_bp.route("/wrong-items/<int:item_id>", methods=["DELETE"])
def wrong_item_delete(item_id):
    delete_wrong_item(item_id)
    return jsonify({"success": True})
