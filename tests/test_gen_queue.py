"""
문제 생성 대기열(features/gen_queue) 검증.

왜 필요한가 — 이 줄서기가 틀리면 증상이 조용하다. 자리를 반납 안 하면 그 자리가
영영 막혀 아무도 생성을 못 하는데, 예외도 로그도 안 남고 그냥 '대기 중'만 뜬다.
반대로 상한이 안 먹으면 평소엔 멀쩡하다가 사람이 몰릴 때만 메모리가 터진다.

확인하는 축:
  ① 상한 안에서는 아무도 안 기다린다 (평소 화면이 지금과 같아야 한다)
  ② 상한을 넘으면 순서대로 기다리고, '앞에 N명'이 단조롭게 줄어든다
  ③ 반납하면 다음 사람이 바로 들어온다
  ④ 중간에 끊긴 사람(release)이 있어도 뒷사람이 안 밀린다
  ⑤ 시간을 넘기면 QueueTimeout

pytest 없이 그냥 실행:

    python tests/test_gen_queue.py
"""

import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import config
from features import gen_queue

_failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        _failures.append(name)


config.MAX_CONCURRENT_GENERATIONS = 2      # 테스트 내내 상한 2로 본다
gen_queue.clear()

# ──────────────────────────────────────────────
# ① 상한 안에서는 아무도 안 기다린다
# ──────────────────────────────────────────────
print("① 상한 안 (2명)")
t1 = gen_queue.take_ticket()
t2 = gen_queue.take_ticket()
check("1번 대기 없음", gen_queue.ahead_of(t1) == 0, gen_queue.ahead_of(t1))
check("2번 대기 없음", gen_queue.ahead_of(t2) == 0, gen_queue.ahead_of(t2))
check("stats: 2 실행 · 0 대기", gen_queue.stats() == {"running": 2, "waiting": 0},
      gen_queue.stats())

# wait_for_slot 이 즉시 끝나야 한다 (queue 이벤트가 한 번도 안 나가야 한다)
events = list(gen_queue.wait_for_slot(t2))
check("차례가 이미 왔으면 이벤트 0개", events == [], events)

# ──────────────────────────────────────────────
# ② 상한을 넘으면 순서대로 기다린다
# ──────────────────────────────────────────────
print("\n② 상한 초과 (3·4번)")
t3 = gen_queue.take_ticket()
t4 = gen_queue.take_ticket()
check("3번은 앞에 1명", gen_queue.ahead_of(t3) == 1, gen_queue.ahead_of(t3))
check("4번은 앞에 2명", gen_queue.ahead_of(t4) == 2, gen_queue.ahead_of(t4))
check("stats: 2 실행 · 2 대기", gen_queue.stats() == {"running": 2, "waiting": 2},
      gen_queue.stats())

# ──────────────────────────────────────────────
# ③ 반납하면 다음 사람이 들어온다
# ──────────────────────────────────────────────
print("\n③ 반납 → 다음 차례")
gen_queue.release(t1)
check("3번 차례가 됐다", gen_queue.ahead_of(t3) == 0, gen_queue.ahead_of(t3))
check("4번은 앞에 1명으로 줄었다", gen_queue.ahead_of(t4) == 1, gen_queue.ahead_of(t4))

# ──────────────────────────────────────────────
# ④ 기다리던 사람이 중간에 끊겨도 뒷사람이 안 밀린다
#    (브라우저가 끊기면 제너레이터의 finally 가 release 를 부른다)
# ──────────────────────────────────────────────
print("\n④ 대기 중이던 사람이 끊긴 경우")
t5 = gen_queue.take_ticket()
before = gen_queue.ahead_of(t5)
gen_queue.release(t4)                      # 앞에서 기다리던 4번이 창을 닫았다
check("5번이 한 자리 당겨졌다", gen_queue.ahead_of(t5) == before - 1,
      f"{before} → {gen_queue.ahead_of(t5)}")
check("이미 나간 번호를 또 반납해도 조용하다",
      gen_queue.release(t4) is None)

# ──────────────────────────────────────────────
# ⑤ 실제로 기다렸다가 풀리는가 (숫자가 줄어드는지까지)
# ──────────────────────────────────────────────
print("\n⑤ 대기 → 해제")
gen_queue.clear()
a = gen_queue.take_ticket()
b = gen_queue.take_ticket()
c = gen_queue.take_ticket()                # 상한 2 → c 는 앞에 1명

seen = []


def run_waiter():
    for ahead in gen_queue.wait_for_slot(c, poll=0.05):
        seen.append(ahead)


th = threading.Thread(target=run_waiter, daemon=True)
th.start()
time.sleep(0.2)
check("기다리는 동안 '앞에 1명'이 나왔다", seen and seen[0] == 1, seen)
gen_queue.release(a)
th.join(timeout=3)
check("자리가 나자 대기가 끝났다", not th.is_alive())
check("보고된 숫자가 늘어난 적 없다", seen == sorted(seen, reverse=True), seen)

# ──────────────────────────────────────────────
# ⑥ 시간을 넘기면 QueueTimeout
# ──────────────────────────────────────────────
print("\n⑥ 대기 시간 초과")
gen_queue.clear()
x = gen_queue.take_ticket()
y = gen_queue.take_ticket()
z = gen_queue.take_ticket()                # z 는 계속 기다린다
timed_out = False
try:
    for _ in gen_queue.wait_for_slot(z, timeout=0.3, poll=0.05):
        pass
except gen_queue.QueueTimeout:
    timed_out = True
check("QueueTimeout 이 났다", timed_out)

gen_queue.clear()

print("\n" + "=" * 56)
if _failures:
    print(f"실패 {len(_failures)}건:")
    for f in _failures:
        print("  -", f)
    sys.exit(1)
print("전부 통과")
