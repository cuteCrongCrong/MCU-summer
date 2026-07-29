"""
로그인 기능 — 구글 OAuth2(Authlib 서버 사이드 리다이렉트) + users 테이블.

담당 테이블: users
연결 계약: current_owner() 를 다른 기능(question_gen/wrong_note)이 import해
           사용자별 데이터 분리에 사용. (CONTRIBUTING.md 4-A)
           비로그인 방문자에게도 브라우저별 익명 소유자 id(guest_id)를 발급하므로,
           게스트끼리도 서로의 세션/오답노트를 볼 수 없다.
설정: config.py 가 secret_config.py/환경변수에서 client id·secret·세션키를 읽음.
      값이 없으면 GOOGLE_LOGIN_ENABLED=False 로 로그인만 비활성(앱 나머지는 정상).
"""

import secrets
from datetime import datetime

from flask import Blueprint, session, redirect, url_for, jsonify
from authlib.integrations.flask_client import OAuth

import config
from db import get_conn

auth_bp = Blueprint("auth", __name__)
oauth = OAuth()

GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"


def init_auth(app):
    """app.py에서 호출 — Authlib를 앱에 붙이고 구글 provider 등록."""
    oauth.init_app(app)
    if config.GOOGLE_LOGIN_ENABLED:
        oauth.register(
            name="google",
            client_id=config.GOOGLE_CLIENT_ID,
            client_secret=config.GOOGLE_CLIENT_SECRET,
            server_metadata_url=GOOGLE_METADATA_URL,
            client_kwargs={"scope": "openid email profile"},
        )


GUEST_KEY = "guest_id"   # 세션 쿠키에 담기는 익명 소유자 id의 키


def current_user_id():
    """현재 로그인 사용자 id (게스트면 None)."""
    return session.get("user_id")


def current_guest_id() -> str:
    """
    비로그인 방문자의 익명 소유자 id. 없으면 새로 발급해 세션 쿠키에 심는다.

    브라우저마다 다른 값이 발급되므로 게스트 데이터도 방문자별로 격리된다.
    쿠키는 Flask 서명 쿠키이므로 FLASK_SECRET_KEY가 유출되면 위조 가능하다
    → 배포 시 반드시 고유한 키를 환경변수로 지정할 것.
    """
    gid = session.get(GUEST_KEY)
    if not gid:
        gid = secrets.token_urlsafe(16)
        session[GUEST_KEY] = gid
        session.permanent = True      # 브라우저를 닫아도 유지 (수명은 app.py에서 설정)
    return gid


def current_owner():
    """
    현재 요청의 데이터 소유자 (user_id, guest_id) 튜플. db.owner_clause()에 그대로 전달.
      - 로그인 사용자 → (id, None)
      - 게스트        → (None, 발급된 익명 id)
    """
    uid = session.get("user_id")
    if uid is not None:
        return (uid, None)
    return (None, current_guest_id())


# ── users DB ──

def upsert_user(google_sub: str, email: str, name: str, picture: str) -> int:
    """구글 sub 기준으로 사용자 생성/갱신 후 내부 user id 반환."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE google_sub=?", (google_sub,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE users SET email=?, name=?, picture=? WHERE id=?",
                (email, name, picture, row["id"]),
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            """INSERT INTO users (google_sub, email, name, picture, created_at)
               VALUES (?,?,?,?,?)""",
            (google_sub, email, name, picture,
             datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


# ── 라우트 ──

@auth_bp.route("/login/google")
def google_login():
    if not config.GOOGLE_LOGIN_ENABLED:
        return ("구글 로그인이 설정되지 않았습니다. "
                "secret_config.py에 GOOGLE_CLIENT_ID/SECRET을 채워주세요."), 503
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/login/google/callback")
def google_callback():
    if not config.GOOGLE_LOGIN_ENABLED:
        return redirect("/")
    try:
        token = oauth.google.authorize_access_token()
    except Exception as e:
        return f"로그인 처리 중 오류가 발생했습니다: {e}", 400

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = oauth.google.userinfo(token=token)
    sub = userinfo.get("sub")
    if not sub:
        return "구글 계정 정보를 가져오지 못했습니다.", 400

    uid = upsert_user(sub, userinfo.get("email"),
                      userinfo.get("name"), userinfo.get("picture"))
    session["user_id"] = uid
    session["user"] = {
        "id": uid,
        "email": userinfo.get("email"),
        "name": userinfo.get("name"),
        "picture": userinfo.get("picture"),
    }
    return redirect("/")


@auth_bp.route("/logout", methods=["POST"])
def logout():
    # 로그인 전에 게스트로 만들어 둔 데이터를 다시 볼 수 있도록 익명 id는 유지한다.
    gid = session.get(GUEST_KEY)
    session.clear()
    if gid:
        session[GUEST_KEY] = gid
        session.permanent = True
    return jsonify({"success": True})


@auth_bp.route("/me")
def me():
    """현재 로그인 사용자 + 로그인 기능 활성 여부 (프런트가 버튼 노출 판단에 사용)."""
    return jsonify({
        "user": session.get("user"),
        "login_enabled": config.GOOGLE_LOGIN_ENABLED,
    })
