"""
추출 결과 임시 보관 — "상한 초과 경고 → 사용자 확인 → 진행" 2단계 흐름용.

왜 필요한가:
  어느 줄이 잘려나가는지는 PDF 추출(그림 속 글자 전사 포함)을 **끝내야** 알 수 있다.
  그런데 확인을 받고 나서 처음부터 다시 추출하면 Vision 호출값을 두 번 낸다.
  그래서 1단계 추출 결과를 잠깐 들고 있다가, 사용자가 '진행'을 누르면 그대로 재사용한다.

사용량(UsageCollector)과 크레딧 스냅샷도 함께 보관한다. 1단계에서 이미 쓴 Vision 토큰이
있으므로, 2단계에서 새 수집기를 만들면 화면에 나오는 '이번에 쓴 양'이 그만큼 적게 나온다.

보관 위치:
  waitress는 한 프로세스에서 스레드로 돌기 때문에 프로세스 메모리로 충분하다
  (serve.py — SERVER_THREADS). 멀티 프로세스로 바꾸면 이 모듈을 DB나 외부 저장소로
  옮겨야 한다. 그때 고칠 곳이 여기 하나가 되도록 저장 방식을 이 파일에 가둬 둔다.
"""

import secrets
import threading
import time

# 확인을 기다리는 시간. 사용자가 경고를 읽고 누를 만큼만 — 길게 잡으면 메모리에 쌓인다.
TTL_SECONDS = 20 * 60
# 동시에 들고 있을 최대 건수 (한 건이 수 MB일 수 있다). 넘으면 가장 오래된 것부터 버린다.
MAX_ENTRIES = 12

_lock = threading.Lock()
_store = {}      # token -> {"at": float, "owner": tuple, "payload": dict}


def _purge_locked(now: float):
    """만료분 정리 + 개수 상한 유지. 호출부가 _lock을 잡고 있어야 한다."""
    for tok in [t for t, e in _store.items() if now - e["at"] > TTL_SECONDS]:
        _store.pop(tok, None)
    while len(_store) > MAX_ENTRIES:
        oldest = min(_store, key=lambda t: _store[t]["at"])
        _store.pop(oldest, None)


def put(payload: dict, owner) -> str:
    """추출 결과를 보관하고 토큰을 돌려준다."""
    token = secrets.token_urlsafe(24)
    now = time.time()
    with _lock:
        _store[token] = {"at": now, "owner": owner, "payload": payload}
        _purge_locked(now)
    return token


def take(token: str, owner):
    """
    토큰에 해당하는 결과를 꺼내며 **삭제**한다. 없거나 만료됐거나 소유자가 다르면 None.

    한 번 쓰고 버리는 이유: 확인 후 진행은 한 번뿐이고, 남겨두면 같은 추출로 여러 번
    분석이 돌 수 있다. 소유자 확인은 남의 토큰을 주워 쓰는 것을 막는다.
    """
    if not token:
        return None
    now = time.time()
    with _lock:
        _purge_locked(now)
        entry = _store.get(token)
        if not entry or entry["owner"] != owner:
            return None
        del _store[token]
    return entry["payload"]


def clear():
    """테스트용 — 보관분을 전부 비운다."""
    with _lock:
        _store.clear()
