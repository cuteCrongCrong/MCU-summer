"""
LLM 응답을 문제 dict 로 바꾸는 파싱(parse_question_block) 검증.

왜 필요한가 — 이 파싱은 모델이 실제로 무엇을 뱉느냐에 달려 있어서, 형식이
조금만 어긋나도 조용히 잘못된 결과를 만든다. 예외가 나지 않고 '그럴듯한'
문항이 나오기 때문에 화면을 눈으로 보기 전까지 아무도 모른다.

실제로 이런 일이 있었다. 모델이 해설을 선지별로 나눠 쓰면

    해설: ④ 세포는 두 전자전달체를 분리해 운용합니다.
    ① NADPH는 리보오스 2' 에 인산기가 더 있습니다.
    ② NADH는 이화작용에 쓰입니다.

선택지 판정에 조건이 없어서 이 줄들이 전부 선택지로 들어갔다. 화면에는
선지가 10개로 보이고, 동시에 해설은 그만큼 비었다 — 같은 원인의 두 증상이고
해설 손실이 더 아프다.

아래 네 가지는 모델 출력이 흔들리는 축이라 특히 조용히 깨지기 쉽다.
  ① 해설을 선지별로 ①②③④⑤ 로 나열
  ② '선택지:' 머리말 생략
  ③ 객관식이 아닌데 해설이 번호로 시작
  ④ 스트리밍/일괄 파서가 같은 결과를 내는가

외부 의존성 없이 문자열만 넣어 확인한다. pytest 없이 그냥 실행:

    python tests/test_question_parse.py
"""

import pathlib
import sys

# 저장소 루트를 import 경로에 넣는다 (python tests/test_question_parse.py 로 바로 실행되게)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 한글 윈도우의 기본 콘솔 코드페이지(cp949)로는 이 파일이 찍는 '→' 등을 쓸 수 없어
# UnicodeEncodeError 로 죽는다. pytest 처럼 stdout 을 바꿔치기하는 환경도 있어 확인 후 부른다.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from llm import parse_question_block, StreamingQuestionParser

_failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        _failures.append(name)


# ──────────────────────────────────────────────
# ① 해설을 선지별로 나열한 경우 (실제로 사용자 화면에 나온 형태)
# ──────────────────────────────────────────────

ITEMIZED = """유형: 객관식
번호: 2
문제: 세포 내 활성운반체인 NADH와 NADPH의 비교로 옳은 것은?
선택지:
① NADH와 NADPH는 구조가 완전히 동일하다.
② NADH는 주로 동화작용에서 환원제로 작용한다.
③ NADPH는 이화작용 경로에서 주로 생성된다.
④ 세포는 [NAD+]/[NADH] 와 [NADPH]/[NADP+] 를 모두 높게 유지한다.
⑤ NADPH 1분자가 산화될 때 ATP 양은 NADH의 정확히 2배이다.
정답: ④
해설: ④ 세포는 대사 조절을 위해 두 전자전달체를 분리하여 운용합니다.
① NADPH는 NADH의 리보오스 2' 위치에 인산기가 하나 더 결합되어 있습니다.
② NADH는 주로 이화작용에 사용되어 ATP 생성에 기여합니다.
③ NADPH는 주로 동화작용 및 항산화 작용에 환원력을 제공합니다.
⑤ NADPH는 미토콘드리아 전자전달계로 직접 들어가지 않습니다.
함정포인트: ①과 ②를 바꿔 외우기 쉽습니다."""

print("① 해설을 선지별로 나열한 경우")
q = parse_question_block(ITEMIZED)
choices = q.get("선택지") or []
explain = q.get("해설") or ""

check("선택지가 정확히 5개", len(choices) == 5, f"{len(choices)}개")
check("선택지에 해설 문장이 섞이지 않음",
      all("리보오스" not in c and "대사 조절" not in c for c in choices))
check("해설에 선지별 설명이 모두 남음",
      all(k in explain for k in ("대사 조절", "리보오스", "이화작용", "항산화", "미토콘드리아")),
      repr(explain)[:70])
check("정답 유지", q.get("정답") == "④", q.get("정답"))
check("함정포인트 유지", "바꿔 외우기" in (q.get("함정포인트") or ""))
check("유형 객관식", q.get("유형") == "객관식", q.get("유형"))

# ──────────────────────────────────────────────
# ② 형식이 온전한 경우 — 위 수정이 정상 동작을 깨지 않았는가
# ──────────────────────────────────────────────

PLAIN = """유형: 객관식
번호: 1
문제: 다음 중 옳은 것은?
선택지:
① 가
② 나
③ 다
④ 라
⑤ 마
정답: ③
해설: 다가 옳다.
함정포인트: 나와 헷갈린다."""

print("\n② 형식이 온전한 경우 (회귀 확인)")
q2 = parse_question_block(PLAIN)
check("선택지 5개", len(q2.get("선택지") or []) == 5)
check("해설 그대로", q2.get("해설") == "다가 옳다.", repr(q2.get("해설")))
check("함정포인트 그대로", q2.get("함정포인트") == "나와 헷갈린다.")

# ──────────────────────────────────────────────
# ③ '선택지:' 머리말을 생략한 경우 — 모델이 종종 빠뜨린다.
#    이때도 선택지로 잡아야 한다(관대함을 유지해야 하는 쪽).
# ──────────────────────────────────────────────

NO_HEADER = """유형: 객관식
문제: 다음 중 옳은 것은?
① 가
② 나
③ 다
④ 라
⑤ 마
정답: ①
해설: 가가 옳다."""

print("\n③ '선택지:' 머리말 생략")
q3 = parse_question_block(NO_HEADER)
check("선택지 5개로 인식", len(q3.get("선택지") or []) == 5,
      f"{len(q3.get('선택지') or [])}개")
check("문제 본문에 선택지가 섞이지 않음", "①" not in (q3.get("문제") or ""),
      repr(q3.get("문제"))[:60])

# ──────────────────────────────────────────────
# ④ 객관식이 아닌데 해설이 번호로 시작하는 줄을 갖는 경우.
#    번호를 '해설:' 과 같은 줄에 두면 이 경로를 안 타므로 반드시 줄을 나눈다.
# ──────────────────────────────────────────────

SHORT_ANSWER = """유형: 단답형
문제: 해당과정의 최종 산물은?
정답: 피루브산
해설:
① 젖산은 무산소 조건의 산물이다.
② 아세틸-CoA 는 이후 단계에서 만들어진다."""

print("\n④ 단답형인데 해설이 번호로 시작")
q4 = parse_question_block(SHORT_ANSWER)
check("선택지 없음", not q4.get("선택지"), q4.get("선택지"))
check("유형 단답형 유지", q4.get("유형") == "단답형", q4.get("유형"))
check("해설에 번호 설명이 남음", "젖산" in (q4.get("해설") or ""),
      repr(q4.get("해설"))[:60])

# ──────────────────────────────────────────────
# ⑤ 스트리밍 파서도 같은 결과를 내는가.
#    파싱 로직은 parse_question_block 한 곳뿐이어야 한다 — 두 벌이 되면
#    스트리밍 여부에 따라 결과가 달라진다.
# ──────────────────────────────────────────────

print("\n⑤ 스트리밍 파서 일치")
parser = StreamingQuestionParser()
streamed = parser.feed("---QUESTION---\n" + ITEMIZED + "\n---END---\n")
check("문제 1개 완성", len(streamed) == 1, f"{len(streamed)}개")
if streamed:
    check("일괄 파싱 결과와 동일", streamed[0] == q,
          "선택지 " + str(len(streamed[0].get("선택지") or [])) + "개")

# 청크가 문제 중간을 잘라도 결과가 같아야 한다
print("\n⑥ 청크 경계가 문제를 잘라도 동일")
whole = "---QUESTION---\n" + ITEMIZED + "\n---END---\n"
parser2 = StreamingQuestionParser()
collected = []
for i in range(0, len(whole), 17):          # 17자씩 흘려보낸다
    collected.extend(parser2.feed(whole[i:i + 17]))
check("문제 1개 완성", len(collected) == 1, f"{len(collected)}개")
if collected:
    check("한 번에 넣은 것과 동일", collected[0] == q)

print("\n" + "=" * 56)
if _failures:
    print(f"실패 {len(_failures)}건:")
    for f in _failures:
        print("  -", f)
    sys.exit(1)
print("전부 통과")
