"""
로그인 기능 — 구글 OAuth2(Authlib 서버 사이드 리다이렉트) + users 테이블.

담당 테이블: users
연결 계약: current_user_id() 를 다른 기능(question_gen/wrong_note)이 import해
           사용자별 데이터 분리에 사용. (CONTRIBUTING.md 4-A)
설정: config.py 가 secret_config.py/환경변수에서 client id·secret·세션키를 읽음.
      값이 없으면 GOOGLE_LOGIN_ENABLED=False 로 로그인만 비활성(앱 나머지는 정상).
"""

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


def current_user_id():
    """현재 로그인 사용자 id (게스트면 None). 다른 기능이 데이터 분리에 사용."""
    return session.get("user_id")


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
    session.clear()
    return jsonify({"success": True})


@auth_bp.route("/me")
def me():
    """현재 로그인 사용자 + 로그인 기능 활성 여부 (프런트가 버튼 노출 판단에 사용)."""
    return jsonify({
        "user": session.get("user"),
        "login_enabled": config.GOOGLE_LOGIN_ENABLED,
    })
