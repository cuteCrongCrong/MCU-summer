"""
문제 생성 동시 실행 제한 — 메모리를 먹는 구간에만 줄을 세운다.

왜 필요한가:
  생성 하나가 요청 스레드를 1~3분 붙잡고, 그동안 Vision 호출을 IMAGE_WORKERS 만큼
  병렬로 돌린다. 이 '동시 몇 건'을 세는 장치가 없어서 지금까지는 SERVER_THREADS
  (=요청을 받는 능력)가 그 역할을 대신 떠맡고 있었다. 그러면 생성이 스레드를 다
  차지한 순간 **생성과 무관한 사람들까지** 사이트를 못 쓴다 — 정적 파일(JS·CSS·뼈
  그림)도 앱을 거치므로 페이지가 아예 안 열린다.

  둘을 분리한다. 스레드는 넉넉히 두고(요청은 일단 받는다), 메모리를 먹는 생성만
  여기서 센다. 덕분에 기다리는 사람에게 '앞에 N명'을 **말해줄 수 있다** — 스레드가
  없으면 요청이 앱에 닿지도 못해 아무 말도 못 하고 화면이 멈춘 것처럼 보인다.

줄 세우는 방식:
  발급 순서(FIFO)를 그대로 지킨다. threading.Semaphore 는 깨우는 순서를 보장하지
  않아 '앞에 N명'이 늘었다 줄었다 하는데, 대기 화면에 숫자를 띄우려면 그 숫자가
  단조롭게 줄어야 믿을 만하다.

보관 위치:
  waitress 는 한 프로세스에서 스레드로 돈다(serve.py). 그래서 프로세스 메모리로
  충분하다. 멀티 프로세스로 바꾸면 이 모듈을 외부 저장소로 옮겨야 한다 —
  features/extract_cache.py 와 같은 제약이고, 그때 고칠 곳이 두 파일이다.
"""

import itertools
import threading
import time

import config

# 대기 중에도 이만큼마다 한 번은 이벤트를 내보낸다.
#   이 yield 가 브라우저로 나가는 순간이 곧 **연결이 살아 있는지 확인하는 순간**이다.
#   끊긴 줄 모르고 조용히 자고 있으면 그 자리만큼 뒷사람이 헛되이 밀린다.
HEARTBEAT_SECONDS = 5


class QueueTimeout(Exception):
    """정해진 시간 안에 차례가 오지 않았다."""


_lock = threading.Lock()
_tickets = []                 # 발급 순서대로. 앞에서부터 MAX_CONCURRENT_GENERATIONS 개가 '실행 중'
_seq = itertools.count(1)


def take_ticket() -> int:
    """줄 맨 뒤에 선다. **반드시 release() 로 반납할 것** (finally 절에서)."""
    with _lock:
        ticket = next(_seq)
        _tickets.append(ticket)
        return ticket


def release(ticket: int):
    """자리를 비운다. 이미 빠진 번호를 또 반납해도 조용히 넘어간다."""
    with _lock:
        try:
            _tickets.remove(ticket)
        except ValueError:
            pass


def ahead_of(ticket: int) -> int:
    """내 앞에 몇 명이 남았는지. 0이면 지금 시작해도 된다."""
    with _lock:
        try:
            idx = _tickets.index(ticket)
        except ValueError:
            return 0          # 이미 반납된 번호 — 막을 이유가 없다
        return max(0, idx - config.MAX_CONCURRENT_GENERATIONS + 1)


def wait_for_slot(ticket: int, timeout: int = None, poll: float = 0.5):
    """
    차례가 올 때까지 기다리며 '앞에 몇 명'을 yield 하는 제너레이터.
    차례가 오면 아무것도 yield 하지 않고 끝난다. 시간을 넘기면 QueueTimeout.

    숫자가 바뀔 때 + HEARTBEAT_SECONDS 마다 내보낸다 (위 상수 설명 참고).
    """
    deadline = time.time() + (timeout or config.GEN_QUEUE_TIMEOUT)
    last_ahead, last_beat = None, 0.0
    while True:
        ahead = ahead_of(ticket)
        if ahead == 0:
            return
        now = time.time()
        if ahead != last_ahead or now - last_beat >= HEARTBEAT_SECONDS:
            last_ahead, last_beat = ahead, now
            yield ahead
        if time.time() >= deadline:
            raise QueueTimeout()
        time.sleep(poll)


def stats() -> dict:
    """지금 몇 건이 돌고 몇 건이 기다리나 (관측·테스트용)."""
    with _lock:
        total = len(_tickets)
        running = min(total, config.MAX_CONCURRENT_GENERATIONS)
        return {"running": running, "waiting": total - running}


def clear():
    """테스트용 — 줄을 비운다."""
    with _lock:
        _tickets.clear()
