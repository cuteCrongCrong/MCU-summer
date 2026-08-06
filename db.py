"""
공용 DB 계층 (SQLite) — 연결 + 스키마 관리.

⚠️ 팀 규칙: 이 파일은 **DB 스키마의 단일 소유처**입니다.
  - 새 테이블/컬럼은 여기 init_db()에 추가하고, 기존 테이블 컬럼 추가는 _ensure_column() 사용.
  - 공용 테이블(sessions, generations) 변경은 팀 합의 후 반영.
  - 각 기능의 실제 쿼리(CRUD)는 features/<기능>.py 에 둡니다.
"""

import json
import os
import sqlite3
from datetime import datetime

import config

_BASE_DIR = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
# 배포 시에는 DB_PATH 환경변수로 영구 디스크 경로를 지정한다(예: /var/data/sessions.db).
# 지정하지 않으면 기존과 동일하게 프로젝트 폴더의 sessions.db 를 쓴다.
DB_PATH = config.DB_PATH or os.path.join(_BASE_DIR, "sessions.db")

# 다중 프로바이더 지원 이전에 만들어진 행은 전부 전북대 게이트웨이로 생성된 것.
# (그때는 선택지가 없었음) — 현재 기본 프로바이더와는 별개의 '과거 사실'이라 여기 고정.
LEGACY_PROVIDER = "jbnu_gateway"


def get_conn():
    # timeout: 다른 요청이 쓰기 중이면 즉시 실패하지 않고 기다린다("database is locked" 완화).
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_column(conn, table: str, col: str, decl: str) -> bool:
    """
    CREATE TABLE IF NOT EXISTS는 기존 테이블에 컬럼을 못 넣으므로 ALTER로 보강.
    이번 실행에서 실제로 컬럼을 추가했으면 True (기존 행 백필이 필요한지 판단용).
    """
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if col in cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return True


def json_col(row, name):
    """
    나중에 붙인 JSON 컬럼을 읽는다. 컬럼이 없는 구버전 행이거나 값이 NULL이면 None.

    None은 '모름'이라는 뜻이고, 화면은 이걸 0(안 씀)과 다르게 다뤄야 한다.
    (사용량 컬럼은 백필하지 않으므로 추가 이전 행은 전부 여기로 떨어진다)
    """
    if name not in row.keys() or not row[name]:
        return None
    try:
        return json.loads(row[name])
    except ValueError:
        return None


def owner_clause(owner, prefix: str = ""):
    """
    사용자별 데이터 분리용 WHERE 조각과 파라미터를 반환.

    owner 는 features.auth.current_owner() 가 준 (user_id, guest_id) 튜플:
      - 로그인 사용자 → (id, None)   → "user_id = ?",  [id]
      - 게스트        → (None, gid)  → "guest_id = ?", [gid]
        (gid = 브라우저별로 발급된 익명 소유자 id. 게스트끼리도 서로 격리된다.)

    prefix 는 JOIN 시 테이블 별칭 (예: "f." → "f.guest_id").
    사용 예:
        frag, params = owner_clause(owner)
        conn.execute(f"SELECT ... WHERE {frag}", params)
    """
    user_id, guest_id = owner
    if user_id is not None:
        return f"{prefix}user_id = ?", [user_id]
    if guest_id:
        return f"{prefix}guest_id = ?", [guest_id]
    # 소유자를 특정할 수 없으면 아무것도 매칭하지 않는다(데이터 노출 방지).
    return "0 = 1", []


# ──────────────────────────────────────────────
# 이미지 설명 캐시 (llm.py가 쓴다 — 특정 기능 전용이 아니라 공용 인프라)
#
# 같은 PDF를 다시 올리면 예전에는 Vision 호출을 처음부터 다시 냈다. 학생들이
# 같은 기출을 여러 번 돌리는 게 흔해서, 재실행 비용의 대부분이 여기서 나왔다.
#
# 키(cache_key)는 llm.image_cache_key() 가 만든다 — '프롬프트 + 렌더된 PNG'의 해시다.
# 파일명·페이지 번호로 잡으면 렌더 해상도를 바꿨을 때 옛 설명이 재사용되고,
# PNG만 해싱하면 같은 페이지를 기출(그림 설명)과 강의록(글자 전사)으로 각각 올렸을 때
# 먼저 넣은 쪽이 반대쪽 요청에 그대로 돌아온다. 둘 다 키에 넣어야 섞이지 않는다.
#
# 소유자(user_id/guest_id) 컬럼이 없는 이유 — 내용 주소 방식이라 같은 이미지를
# 이미 갖고 있어야만 꺼낼 수 있다. 남의 자료가 새어나가는 경로가 아니다.
# ──────────────────────────────────────────────

def get_cached_image_desc(cache_key: str, provider: str, model: str):
    """캐시된 이미지 처리 결과. 없으면 None. 조회 실패는 '없음'과 같게 다룬다."""
    try:
        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT description FROM image_desc_cache
                   WHERE cache_key=? AND provider=? AND model=?""",
                (cache_key, provider or "", model or ""),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None                     # 캐시 문제로 생성이 멈추면 안 된다
    return row["description"] if row else None


def put_cached_image_desc(cache_key: str, provider: str, model: str,
                          description: str):
    """이미지 처리 결과를 캐시에 남긴다. 실패해도 조용히 넘어간다(캐시는 부가 기능)."""
    if not description:
        return
    try:
        conn = get_conn()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO image_desc_cache
                   (cache_key, provider, model, description, created_at)
                   VALUES (?,?,?,?,?)""",
                (cache_key, provider or "", model or "", description,
                 datetime.now().strftime("%Y-%m-%d %H:%M")),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def init_db():
    """앱 로드 시 모든 테이블/인덱스/마이그레이션을 보장."""
    # 배포 시 DB_PATH가 마운트된 디스크의 하위 경로일 수 있으므로 폴더를 먼저 만든다.
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    conn = get_conn()
    try:
        # WAL: 읽기와 쓰기가 서로를 막지 않아 동시 접속에 훨씬 안정적이다.
        # (DB 파일에 한 번 기록되면 유지되는 설정)
        conn.execute("PRAGMA journal_mode = WAL")
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
                type_stats TEXT,
                provider TEXT
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
                raw TEXT,
                provider TEXT
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
        # ── 로그인: Google 계정 사용자 ──
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                google_sub TEXT UNIQUE NOT NULL,   -- Google 계정 고유 id
                email TEXT,
                name TEXT,
                picture TEXT,
                created_at TEXT NOT NULL
            )"""
        )
        # ── 마이그레이션: 기존 DB에 없을 수 있는 컬럼 보강 ──
        _ensure_column(conn, "sessions", "source_info", "TEXT")
        # 사용자별 데이터 분리
        #   user_id  : 로그인 사용자 소유 (users.id)
        #   guest_id : 비로그인 방문자 소유 (브라우저 세션 쿠키의 익명 id)
        #   두 컬럼 중 정확히 하나만 채워진다. (owner_clause 참고)
        _ensure_column(conn, "sessions", "user_id", "INTEGER")
        _ensure_column(conn, "sessions", "guest_id", "TEXT")
        _ensure_column(conn, "wrong_folders", "user_id", "INTEGER")
        _ensure_column(conn, "wrong_folders", "guest_id", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_guest ON sessions(guest_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wrong_folders_guest ON wrong_folders(guest_id)"
        )
        # 어떤 LLM 제공사로 만든 세션/이력인지 기록 (다중 프로바이더 지원)
        # 컬럼을 새로 붙인 경우에만 기존 행을 게이트웨이로 백필한다.
        for table in ("sessions", "generations"):
            if _ensure_column(conn, table, "provider", "TEXT"):
                conn.execute(
                    f"UPDATE {table} SET provider=? WHERE provider IS NULL",
                    (LEGACY_PROVIDER,),
                )
        # 생성 회차에 사용자가 붙인 이름. 백필하지 않는다 —
        # NULL(이름 없음)과 사용자가 지은 이름을 구분해야 화면에서 '제N회'로 대체할 수 있다.
        _ensure_column(conn, "generations", "title", "TEXT")
        # 회차 한 번에 쓴 LLM 사용량. 백필하지 않는 이유는 topic_analyses 쪽과 같다
        # (아래 주석 참고) — 컬럼 추가 이전 회차는 사용량을 알 길이 없다.
        _ensure_column(conn, "generations", "usage", "TEXT")     # JSON: usage.summary()
        _ensure_column(conn, "generations", "credits", "TEXT")   # JSON: 쓴 크레딧만

        # ── 기출 주제 분석: 분석 결과 보관 (features/topic_analysis.py 담당) ──
        # 문제 생성의 sessions/generations와 무관한 독립 테이블이다. 분석 결과는
        # 재사용 자산이 아니라 완성된 결과물이라 회차/세션 구분이 필요 없다.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS topic_analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,                        -- 사용자가 붙인 이름. 비우면 NULL(화면은 '제N회')
                created_at TEXT NOT NULL,
                model TEXT,
                provider TEXT,
                lecture_docs TEXT,                 -- JSON: [{label,name,pages,source}]
                exam_docs TEXT,                    -- JSON: 같은 형태
                topics TEXT,                       -- JSON: [{주제,강의록,기출,출제형태,문항수}]
                dropped INTEGER,                   -- 출처 미확인으로 제외한 항목 수
                total_questions INTEGER,
                user_id INTEGER,                   -- 소유자 (owner_clause 참고)
                guest_id TEXT
            )"""
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_topic_analyses_guest ON topic_analyses(guest_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_topic_analyses_user ON topic_analyses(user_id)"
        )
        # 분석 한 번에 쓴 LLM 사용량. 위 CREATE TABLE이 아니라 여기서 붙이는 이유는
        # 이미 만들어진 DB에는 CREATE TABLE이 다시 실행되지 않기 때문. (CONTRIBUTING 3번)
        # 백필하지 않는다 — 추가 이전에 만든 분석은 사용량을 알 길이 없고,
        # NULL(모름)과 0(안 씀)은 화면에서 다르게 보여줘야 한다.
        _ensure_column(conn, "topic_analyses", "usage", "TEXT")     # JSON: usage.summary()
        _ensure_column(conn, "topic_analyses", "credits", "TEXT")   # JSON: 쓴 크레딧만

        # ── 이미지 처리 결과 캐시 (llm.py 담당 — 위 헬퍼 함수 주석 참고) ──
        # 모델까지 키에 넣는 이유: 모델이 다르면 결과 품질도 다르다. 싼 모델로 만든
        # 설명을 비싼 모델을 고른 회차에 그대로 물려주면 안 된다.
        #
        # 키 컬럼이 image_sha256이던 초기 버전은 프롬프트를 키에 넣지 않아, 같은 페이지를
        # 기출과 강의록으로 각각 올리면 서로의 결과를 돌려줬다. 이건 순수 캐시라 버려도
        # 다시 만들어지므로, 옛 스키마가 보이면 그냥 지우고 새로 만든다.
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(image_desc_cache)").fetchall()]
        if cols and "cache_key" not in cols:
            conn.execute("DROP TABLE image_desc_cache")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS image_desc_cache (
                cache_key TEXT NOT NULL,      -- llm.image_cache_key(프롬프트, PNG)
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                description TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (cache_key, provider, model)
            )"""
        )
        conn.commit()
    finally:
        conn.close()
