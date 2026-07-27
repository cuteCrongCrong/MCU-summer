"""
공용 DB 계층 (SQLite) — 연결 + 스키마 관리.

⚠️ 팀 규칙: 이 파일은 **DB 스키마의 단일 소유처**입니다.
  - 새 테이블/컬럼은 여기 init_db()에 추가하고, 기존 테이블 컬럼 추가는 _ensure_column() 사용.
  - 공용 테이블(sessions, generations) 변경은 팀 합의 후 반영.
  - 각 기능의 실제 쿼리(CRUD)는 features/<기능>.py 에 둡니다.
"""

import os
import sqlite3

_BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
DB_PATH = os.path.join(_BASE_DIR, "sessions.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, col: str, decl: str):
    """CREATE TABLE IF NOT EXISTS는 기존 테이블에 컬럼을 못 넣으므로 ALTER로 보강."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def owner_clause(user_id, col: str = "user_id"):
    """
    사용자별 데이터 분리용 WHERE 조각과 파라미터를 반환.
      - 로그인 사용자(user_id 존재) → "user_id = ?", [user_id]
      - 게스트(None)              → "user_id IS NULL", []
    사용 예:
        frag, params = owner_clause(uid)
        conn.execute(f"SELECT ... WHERE {frag}", params)
    """
    if user_id is None:
        return f"{col} IS NULL", []
    return f"{col} = ?", [user_id]


def init_db():
    """앱 로드 시 모든 테이블/인덱스/마이그레이션을 보장."""
    conn = get_conn()
    try:
        # ── 문제 생성: 세션(분석 자산) + 생성 이력 ──
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS generations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                count INTEGER,
                weight INTEGER,
                model TEXT,
                type_targets TEXT,
                questions TEXT,
                raw TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gen_session ON generations(session_id)"
        )
        # ── 오답 노트: 폴더 + 폴더에 담긴 문제 ──
        conn.execute(
            """CREATE TABLE IF NOT EXISTS wrong_folders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS wrong_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                folder_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                added_at TEXT NOT NULL
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wrong_folder ON wrong_items(folder_id)"
        )
        # ── 로그인: 구글 계정 사용자 ──
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_sub TEXT UNIQUE NOT NULL,   -- 구글 계정 고유 id
                email TEXT,
                name TEXT,
                picture TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        # ── 마이그레이션: 기존 DB에 없을 수 있는 컬럼 보강 ──
        _ensure_column(conn, "sessions", "source_info", "TEXT")
        # 사용자별 데이터 분리 (로그인=본인 소유, 비로그인=NULL 게스트 소유)
        _ensure_column(conn, "sessions", "user_id", "INTEGER")
        _ensure_column(conn, "wrong_folders", "user_id", "INTEGER")
        conn.commit()
    finally:
        conn.close()
