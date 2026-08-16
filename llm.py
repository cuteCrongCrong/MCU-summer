"""
LLM 도메인 로직 — PDF 텍스트/이미지 추출, 프롬프트 설계, 응답 파싱, 분석 파이프라인.

실제 LLM 호출은 providers/ 계층에 위임한다. LLM 호출 함수는 provider 인자를
받으며, 생략하면 기본 프로바이더(전북대 게이트웨이)를 쓴다.
라우트(HTTP)와 분리되어 있어 단위 테스트·재사용이 쉽습니다.
"""

import collections
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
import fitz  # pymupdf

import config
from db import get_cached_image_desc, put_cached_image_desc
from providers.base import (ProviderAuthError, ProviderError,
                            IMAGE_DESC_PROMPT, IMAGE_TEXT_PROMPT)
from providers.factory import get_provider
from providers.usage import NO_TIMING

# ──────────────────────────────────────────────
# 설정 상수
# ──────────────────────────────────────────────
# provider 인자가 생략됐을 때 쓰는 기본 프로바이더 (전북대 게이트웨이)
_default_provider = get_provider()

# truncate()·build_source_info()의 기본 상한. 인자를 생략한 호출에만 쓰인다.
# ⚠️ 아래 예산 상수와 엮지 말 것 — 엮으면 여길 고칠 때 한쪽 예산만 따라 움직인다.
MAX_TEXT_CHARS = 100000

# 한쪽(강의록/기출)당 업로드 파일 개수 상한 — 주제 분석·문제 생성 공통.
# 프런트(static/js/topic_analysis.js · question_gen.js)도 같은 값을 들고 있으므로 함께 고친다.
MAX_FILES_PER_SIDE = 7

# 문제 생성: 한쪽(강의자료 전체 / 기출 전체)에 배정하는 글자 예산.
# 50만 → 70만 (2026-08). 한쪽에 다 몰아줘도 총 140만 자 ≈ 73만 토큰이라 아래
# GEN_CALL_CHAR_CEILING(150만) 바로 아래에서 멈춘다 — 천장은 그대로 두고 예산만 올린다.
GEN_SIDE_CHAR_BUDGET = 700000
# 양쪽 합계. 칸막이를 고정하지 않는 이유는 실측(session 63·64) — 기출은 63%만 반영되는데
# 같은 시점 강의자료는 제 예산의 55%만 썼다. 강의자료를 먼저 합쳐 실사용량이 확정되면
# 남은 몫을 기출에 넘긴다 (remaining_gen_budget). 기출이 잘리면 유형통계·대표문제·
# 빈출포인트가 전부 잘린 텍스트 위에서 계산되므로 손해가 크다.
GEN_TOTAL_CHAR_BUDGET = GEN_SIDE_CHAR_BUDGET * 2
# 한 번의 LLM 호출에 실을 수 있는 최대 글자수 — 컨텍스트 보호용 천장.
#   강의자료와 기출은 **서로 다른 호출**로 나가므로(analyze_concepts_progressively)
#   프롬프트 크기를 정하는 건 총예산이 아니라 이 값이다. 예산을 넘겨받은 기출 한쪽이
#   컨텍스트를 넘기면 프로바이더가 거절해 분석이 통째로 실패한다.
#   근거: 컨텍스트 1M 토큰, 실측 환산 1자 ≈ 0.52토큰(generation 92·93) → 150만 자 ≈ 78만 토큰.
#   ※ 모델을 바꿔 컨텍스트가 줄면 예산과 무관하게 이 값만 내리면 된다.
GEN_CALL_CHAR_CEILING = 1500000
# 파일을 여러 개 올려도 문서당 이만큼은 보장. 상한까지 채웠을 때의 몫으로 잡아
# '문서당 몫 × 파일수 ≤ 예산'이 허용 개수 전 구간에서 성립하게 한다. (주제 분석과 같은 방식)
GEN_DOC_MIN_CHARS = GEN_SIDE_CHAR_BUDGET // MAX_FILES_PER_SIDE

# 이미지/스캔 페이지 → Vision LLM으로 텍스트를 남긴다.
# 상한은 **한쪽(강의록 전체 / 기출 전체) 기준**이다 — 파일당이 아니다. 양쪽 탭 공통.
# 값을 정하는 건 컨텍스트가 아니라 세 가지다:
#   ① 글자 예산 — 전사는 쪽당 ~800자라 100쪽이면 8만 자다. 한쪽 몫이 이걸 받아줘야
#      한다. 아니면 돈 주고 뽑은 전사를 truncate가 가운데부터 도로 버린다.
#   ② 시간 — 호출 1건이 수 초인데, 그 앞의 렌더는 read_pdf_pages 루프에서 **한 장씩
#      직렬로** 돈다. 워커(IMAGE_WORKERS)가 줄여주는 것은 뒷부분뿐이다.
#   ③ 전송량 — PNG를 base64로 실어 보낸다. 서버가 무료 티어면 여기서 egress가 닳는다.
#      (config.IMAGE_CAP_* 주석 참고)
# 메모리는 이제 여기 안 낀다 — PNG는 디스크에 있고 상주량은 IMAGE_WORKERS × 한 장이다.
IMAGE_DESC_MAX = config.IMAGE_CAP_EXAM        # 기출: 설명할 이미지 페이지 최대 개수
LECTURE_IMAGE_MAX = config.IMAGE_CAP_LECTURE  # 강의록: 손글씨·판서가 많아 기출보다 넉넉히
SPARSE_TEXT_THRESHOLD = 20   # 페이지 텍스트가 이보다 짧으면 이미지 페이지로 간주

# 깨진 텍스트 레이어 판정 — ToUnicode CMap이 없는 한글 서브셋 폰트를 만나면 MuPDF가
# 글리프 번호를 유니코드 값으로 그대로 뱉는다. 그러면 한글이 **일정한 오프셋만큼 밀린
# 다른 문자**로 나온다 (실제 사고: "C6의 carotid…" → "C6넍carotid…", 전 글자 -5707).
# 글자 '수'는 멀쩡해서 SPARSE_TEXT_THRESHOLD에 안 걸리고, 그대로 프롬프트에 실려 LLM이
# 베껴 온다. 그래서 길이가 아니라 '한국어처럼 생겼는가'를 따로 본다.
#
# 임계값 근거 — 이 저장소의 한글 문서 34k자를 정상 표본으로, 같은 글을 여러 오프셋으로
# 밀어 깨진 표본으로 삼아 쟀다 (KS적중 = 한글 음절 중 KS X 1001 2,350자에 드는 비율):
#                        비한글 글자    KS적중
#   정상(산문·의학지문·용어나열·한자병기)   0~30%    95~100%
#   -28 / -56 / -84 / -112              0~4%     74~86%
#   -0x0100 / -0x0500                   4~6%     31~55%
#   -0x164B (실제 사고)                   55%       2%
#   -0x2000                              86%      34%
#   -0x3000 이상                         100%    (음절 0개)
# 두 신호가 서로의 사각을 메운다. 오프셋이 작으면 글자가 한글 구간에 그대로 떨어져
# 비한글 비율이 0%지만(신호 A 통과) 안 쓰는 음절로 흩어져 KS적중이 무너진다. 오프셋이
# 크면 아예 한자 구간으로 넘어가 셀 음절이 없어지므로 그때는 신호 A만 남는다.
#
# 받침 분포(정상 54% 무받침)도 재봤지만 신호로는 쓰지 않는다 — 뼈 이름 나열처럼 받침이
# 몰린 멀쩡한 쪽(15~25%)을 깨진 것으로 잡고, 28의 배수 오프셋은 받침이 그대로 보존돼
# 못 잡는다. 양쪽으로 다 틀리는 신호다.
BROKEN_NON_HANGUL_MAX = 0.40   # 신호 A: 글자 중 한글 아닌 것이 이 비율을 넘으면 깨진 것
BROKEN_KS_MIN         = 0.92   # 신호 C: KS적중이 이보다 낮으면 한국어가 아니다
BROKEN_MIN_SYLLABLES  = 20     # 신호 C는 표본이 이만큼은 돼야 믿는다 (짧은 쪽은 요동친다)

# 되살리기 채택 기준 — 밀린 글자는 오프셋 하나만 알아내면 LLM 호출 없이 원문이 그대로
# 돌아온다. 다만 **틀린 오프셋으로 되살리면 그럴듯한 딴 글이 된다.** 쓰레기 글자는 눈에
# 띄기라도 하지만 잘못 되살린 글은 아무도 못 알아채므로, 애매하면 포기하고 이미지로 넘긴다.
#
# 근거 — 무작위 본문·무작위 오프셋 351회 시험에서 '2등과의 여유'가 결정적이었다:
#   여유 0     → 38건이 엉뚱한 오프셋을 채택 (28의 배수만큼 떨어진 형제 오프셋이 같은 점수)
#   여유 0.03↑ → 오답 채택 0건, 구조율 86.6%
#   여유 0.05  → 오답 채택 0건, 구조율 83.5%   ← 구조율 3%p를 안전에 쓴다
REPAIR_MIN_LETTERS = 40     # 글자가 이보다 적으면 통계를 안 믿는다
REPAIR_MIN_KS      = 0.95   # 되살린 결과의 KS적중이 이만큼은 나와야 한다
REPAIR_MIN_MARGIN  = 0.05   # 2등 오프셋과 이만큼 벌어져야 한다 ← 핵심 안전장치

# 페이지 렌더 해상도 — 모드마다 다르다.
#   설명(기출): 무엇인지만 알면 되므로 낮춘다. 구세대 모델은 긴 변 1568px에서 서버가
#     자동 축소하는데, A4를 150DPI로 구우면 1754px이라 초과분이 버려졌다. 110DPI ≈ 1286px.
#   전사(강의록): 손글씨를 판독해야 해서 해상도가 정확도에 직결된다. 낮추지 않는다.
DESCRIBE_RENDER_DPI   = 110
TRANSCRIBE_RENDER_DPI = 150

# '처리할 가치가 있는 그림'의 최소 면적 비율 (페이지 대비).
#   page.get_images()는 머리글 로고 한 점에도 참이라 그대로 쓰면 본문이 멀쩡히 추출된
#   페이지까지 Vision에 보내 이중으로 결제한다.
#   전사(강의록)는 여백의 짧은 메모도 놓치면 기능이 무의미해지므로 훨씬 헐겁게 잡는다.
DESCRIBE_MIN_AREA_RATIO   = 0.12
TRANSCRIBE_MIN_AREA_RATIO = 0.03

# 이미지 호출 한 번의 출력 한도.
#   ⚠️ 사고(thinking)를 하는 모델은 **사고 토큰도 이 한도에 포함**된다. 빠듯하게 잡으면
#      사고가 예산을 다 먹고 본문이 단어 중간에서 잘린다. 게이트웨이가 보고하는 출력
#      토큰에는 사고분이 안 잡혀서, 사용량 표만 보면 한도에 안 걸린 것처럼 보인다.
#   실측(같은 PNG·프롬프트끼리 짝지은 강의록 전사, 쪽당 글자수): 같은 gemini-3.6-flash가
#   한도 1024에서 175자 → 한도 4096에서 783자. 갈린 것은 모델 체급이 아니라 한도였다.
#   한도는 천장일 뿐이라 올려도 실제 생성한 만큼만 과금된다.
#
#   설명(기출)도 2048에서 4096으로 올렸다. 2048은 '무엇인지 한 문단'이면 된다는 전제였는데,
#   그 전제가 (필기: …) 규칙과 함께 깨졌다 — 스캔 기출은 인쇄 문항과 손글씨를 갈라 적어야
#   해서 출력이 전사에 가까워진다. 실측(generation 209, gpt-5.6-luna): 출력이 정확히
#   2048로 끝났고 image_desc_cache에 아무것도 안 남았다. 캐시는 설명이 비었을 때만
#   건너뛰므로, **사고가 한도를 다 먹고 본문이 0자로 나온 것**이다. 그 쪽은 값만 치르고
#   프롬프트에 한 글자도 안 들어갔다. 바로 다음 회차(topic_analyses 26)는 1,909토큰으로
#   끝나 3,400자를 남겼다 — 같은 모델이 한도에 안 눌리면 이만큼 쓴다.
#   ※ 이 값은 image_cache_key에 들어간다. 바꾸면 설명 캐시가 전부 무효가 된다.
DESCRIBE_MAX_OUTPUT   = 4096
TRANSCRIBE_MAX_OUTPUT = 4096

# 태블릿 벡터 필기(굿노트 등) 감지 임계값 — 페이지의 '곡선' 세그먼트 개수.
# 인쇄 슬라이드 위에 펜으로 쓴 페이지는 텍스트 레이어가 멀쩡하고 래스터 이미지도 없어서
# 위 두 조건에 안 걸린다. 곡선만 세면 표 테두리·밑줄(직선이라 곡선 0개)과 깔끔하게 갈린다.
# 실측(21쪽 아이패드 필기 강의록): 표·인쇄만 있는 쪽 0 · 짧은 메모 757 · 필기로 채운 쪽 16,703
INK_CURVE_MIN = 150

# 이미지 페이지를 텍스트로 남기는 두 가지 방식. 아래 PDF 함수들에 그대로 넘긴다.
#   IMAGE_DESCRIBE   — 그림이 무엇인지 설명. 기출용 (그림 문제를 살리려면 해석이 필요)
#   IMAGE_TRANSCRIBE — 그림 속 글자만 그대로. 강의록용 (손글씨는 살리되 지어낸 문장은 금지)
# 라벨이 다른 이유: [페이지 N 이미지 설명]으로 들어가면 LLM이 '모델이 쓴 문장'으로 읽는다.
#   전사한 글자는 강의록 원문이므로 그렇게 보이면 안 된다.
# detect_ink: 벡터 필기가 있는 페이지도 대상에 넣을지. 강의록만 켠다 — 교수 판서는 살릴
#   값어치가 있지만 기출의 필기는 학생이 덧쓴 정답 표시라 일부러 찾을 이유가 없다.
#   ⚠️ 꺼도 기출에서 손글씨를 못 보는 것은 아니다. 스캔 기출은 페이지 전체가 래스터라 늘
#      후보이고, get_pixmap은 주석·벡터 획까지 함께 렌더한다. 인쇄 문제와 섞이는 것은
#      IMAGE_DESC_PROMPT가 (필기: …)로 갈라내서 막는다.
IMAGE_DESCRIBE = {
    "prompt": IMAGE_DESC_PROMPT, "label": "이미지 설명", "max_pages": IMAGE_DESC_MAX,
    "detect_ink": False,
    "dpi": DESCRIBE_RENDER_DPI, "min_area_ratio": DESCRIBE_MIN_AREA_RATIO,
    "max_output": DESCRIBE_MAX_OUTPUT,
}
IMAGE_TRANSCRIBE = {
    "prompt": IMAGE_TEXT_PROMPT, "label": "그림 속 글자", "max_pages": LECTURE_IMAGE_MAX,
    "detect_ink": True,
    "dpi": TRANSCRIBE_RENDER_DPI, "min_area_ratio": TRANSCRIBE_MIN_AREA_RATIO,
    "max_output": TRANSCRIBE_MAX_OUTPUT,
}

# 문제 유형 4분류 (집계·few-shot·생성에서 공통 사용)
QUESTION_TYPES = ["객관식", "빈칸채우기", "단답형", "서술형"]
# 유형별 정의 (프롬프트에 주입 — 분류 기준 통일)
TYPE_DEFINITIONS = (
    "- 객관식: 선택지(①②③④⑤ 등) 중에서 답을 고르는 문제\n"
    "- 빈칸채우기: 문장 속 빈칸(____, 괄호 등)에 들어갈 말을 채우는 문제\n"
    "- 단답형: 선택지 없이 단어·구·수치 등 짧은 답을 쓰는 문제\n"
    "- 서술형: 여러 문장으로 설명·기술·논술하는 문제"
)


# ──────────────────────────────────────────────
# LLM / PDF 유틸
# ──────────────────────────────────────────────

HANGUL_FIRST, HANGUL_LAST = 0xAC00, 0xD7A3   # 한글 음절 구간 (가 … 힣)

# KS X 1001 완성형 한글 2,350자 — '한국어에서 실제로 쓰는 음절'의 표준 목록이다.
# 유니코드 한글 음절은 11,172자지만 그중 실제 글에 나오는 것은 이 2,350자가 거의 전부라,
# 멀쩡한 한국어 쪽은 여기 적중률이 95~100%로 나오고 글리프 번호가 새어 나온 쪽은
# 나머지 8,822자로 흩어져 뚝 떨어진다. 빈도표를 들고 다닐 필요 없이 표준 하나로 끝난다.
# 만드는 법: EUC-KR 한글 영역은 16~40행(0xB0~0xC8) × 94칸 = 정확히 2,350자다.
# 코드포인트로 담는다 — 오프셋 탐색이 `코드포인트 + 오프셋 in ...`을 수십만 번 돈다.
KS_CODEPOINTS = frozenset(ord(bytes((lead, tail)).decode("euc-kr"))
                          for lead in range(0xB0, 0xC9)
                          for tail in range(0xA1, 0xFF))


def _hangul_letters(text: str):
    """
    판정에 쓸 '글자'만 골라낸다. 반환: (글자 목록, 그중 한글 음절 목록)

    글자(유니코드 카테고리 L*)만 세고 기호·숫자·문장부호는 뺀다. 안 빼면 객관식
    선택지 ①②③④⑤(카테고리 No)와 “ ” · ± ℃ 같은 것들이 전부 '한글이 아닌 문자'로
    잡혀서, 5지선다가 빽빽한 멀쩡한 기출 쪽이 신호 A에 걸린다.
    ASCII는 애초에 제외한다 — 영문 병기는 이 판정과 무관하다.
    """
    letters = [c for c in text
               if ord(c) > 0x7F and unicodedata.category(c).startswith("L")]
    syllables = [c for c in letters if HANGUL_FIRST <= ord(c) <= HANGUL_LAST]
    return letters, syllables


def korean_text_looks_broken(text: str) -> bool:
    """
    이 페이지의 텍스트 레이어가 폰트 매핑 고장으로 깨졌는가. (판정 근거는 위 상수 주석)

    깨졌다고 보면 호출부는 그 텍스트를 **버리고** 페이지를 이미지로 다시 읽는다.
    쓰레기 글자는 없느니만 못하다 — LLM이 그걸 강의록에 있는 표현으로 믿고 베낀다.
    """
    letters, syllables = _hangul_letters(text)
    if not letters:
        return False        # 영문 전용·표지·빈 쪽 — 판정할 한국어가 없다

    # 신호 A: 한글이어야 할 자리에 다른 문자가 앉았다. 오프셋이 크면 한자 구간으로
    # 넘어가 아예 한글이 남지 않는다 — 그때는 이 신호만 남는다.
    if (len(letters) - len(syllables)) / len(letters) > BROKEN_NON_HANGUL_MAX:
        return True

    # 신호 C: 한글 구간 안에 떨어졌더라도 한국어가 안 쓰는 음절들이다.
    if len(syllables) >= BROKEN_MIN_SYLLABLES:
        known = sum(1 for c in syllables if ord(c) in KS_CODEPOINTS)
        if known / len(syllables) < BROKEN_KS_MIN:
            return True

    return False


def find_hangul_offset(text: str):
    """
    깨진 텍스트를 되살릴 오프셋을 찾는다. 문서 전체(깨진 쪽을 이어 붙인 것)를 넘긴다 —
    오프셋은 폰트 속성이라 문서에 하나뿐이고, 글자가 많을수록 판정이 또렷해진다.

    반환:
      정수 N (0이 아님) — 이만큼 더하면 원문. repair_hangul에 그대로 넘긴다.
      0                — 지금 글자가 이미 최선이다. 깨진 게 아니었으니 손대지 않는다
                         (감지가 헛짚었을 때 여기서 조용히 되돌아온다).
      None             — 못 찾았다. 호출부는 그 쪽을 이미지로 다시 읽어야 한다.

    후보를 전수로 뒤지지 않는다. 밀린 글자를 **전부** 한글 구간에 넣으려면 오프셋이
    [가 - 관측최소, 힣 - 관측최대] 안에 있어야 하므로, 보통 수십~수백 개로 좁혀진다.
    이 창이 비면(관측 폭이 한글 구간보다 넓으면) 한 오프셋으로 설명되지 않는 고장이다 —
    일부 쪽만 깨졌거나 CMap이 통째로 뒤섞인 경우라 되살리기를 포기한다.
    """
    letters, _ = _hangul_letters(text)
    if len(letters) < REPAIR_MIN_LETTERS:
        return None

    freq = collections.Counter(ord(c) for c in letters)
    low, high = min(freq), max(freq)
    if high - low > HANGUL_LAST - HANGUL_FIRST:
        return None

    total = len(letters)
    scored = sorted(
        ((sum(n for cp, n in freq.items() if cp + off in KS_CODEPOINTS) / total, off)
         for off in range(HANGUL_FIRST - low, HANGUL_LAST - high + 1)),
        reverse=True)

    hit, offset = scored[0]
    if offset == 0:
        return 0            # 지금 글자가 이미 1등 — 여유를 따질 것 없이 그대로 둔다
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if hit < REPAIR_MIN_KS or hit - runner_up < REPAIR_MIN_MARGIN:
        return None
    return offset


def repair_hangul(text: str, offset: int) -> str:
    """
    find_hangul_offset이 찾은 오프셋으로 되돌린다.

    되돌렸을 때 **한글 구간에 떨어지는 비ASCII 문자만** 민다. 밀린 한글은 정의상 전부
    거기로 돌아오고, 안 깨진 것들(ASCII·괄호·①②③·“ ”)은 엉뚱한 데로 가므로 저절로
    걸러진다 — 실제 사고에서도 "carotid tubercle"과 괄호는 멀쩡했다.

    글자 카테고리(L*)로 거르지 않는 이유: 밀린 한글이 결합 문자 구간에 떨어지는 오프셋이
    있다 (예: 가 - 28 = U+ABE4, Meetei Mayek 모음 기호). 카테고리로 거르면 그 글자만
    안 돌아와 되살린 문장에 쓰레기가 한 글자씩 박힌다.
    """
    if not offset:
        return text
    return "".join(
        chr(ord(c) + offset)
        if ord(c) > 0x7F and HANGUL_FIRST <= ord(c) + offset <= HANGUL_LAST else c
        for c in text)


def has_meaningful_image(page, min_area_ratio: float) -> bool:
    """
    이 페이지에 '처리할 가치가 있는' 그림이 있는가.
    페이지 면적 대비 min_area_ratio 이상인 그림이 하나라도 있어야 참으로 본다.
    (기준값은 모드마다 다르다 — DESCRIBE/TRANSCRIBE_MIN_AREA_RATIO 참고)
    """
    try:
        infos = page.get_image_info()
    except Exception:
        # 판별할 수 없는 PyMuPDF 버전 → 예전처럼 '있으면 대상'으로 두어 놓치지 않게 한다
        return bool(page.get_images())
    if not infos:
        return False
    rect = page.rect
    page_area = float(rect.width) * float(rect.height)
    if page_area <= 0:
        return True                     # 면적을 못 재면 보수적으로 대상에 넣는다
    for info in infos:
        bbox = info.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        area = abs(bbox[2] - bbox[0]) * abs(bbox[3] - bbox[1])
        if area / page_area >= min_area_ratio:
            return True
    return False


def image_cache_key(prompt: str, png_bytes: bytes, max_output: int = 0) -> str:
    """
    이미지 결과 캐시의 키 — **결과를 바꾸는 입력을 전부** 해싱한다.

    ① 프롬프트: 같은 페이지라도 기출은 '그림 설명', 강의록은 '글자 전사'를 받아야 한다.
       PNG만 해싱하면 먼저 넣은 쪽이 반대쪽 요청에 그대로 돌아온다.
    ② PNG 바이트: 렌더 해상도가 바뀌면 키도 바뀐다.
    ③ 출력 한도: 한도를 키에 넣어야, 한도를 올렸을 때 잘린 옛 결과가 재사용되지 않는다.
    """
    seed = f"{prompt}\x00{max_output}".encode("utf-8")
    return hashlib.sha256(seed + b"\x00" + png_bytes).hexdigest()


def has_vector_ink(page) -> bool:
    """페이지에 태블릿 펜 필기로 보이는 벡터 획이 있는지. (곡선 세그먼트 개수로 판단)"""
    curves = 0
    for path in page.get_drawings():
        for item in path["items"]:
            if item[0] == "c":
                curves += 1
                if curves >= INK_CURVE_MIN:   # 넘으면 더 셀 필요 없다
                    return True
    return False


# ──────────────────────────────────────────────
# 큰 것들을 디스크로 내려두기 (spill)
# ──────────────────────────────────────────────
# 여기로 내려가는 것은 둘이다 — 렌더한 PNG(_spill_png)와 올라온 PDF 원본(spill_upload).
# 둘 다 같은 디렉터리(_spill_dir)를 쓰고 같은 청소를 받는다.
#
# 왜 디스크로 내리는가:
#   read_pdf_pages는 LLM을 부르기 **전에** 이 회차의 그림을 전부 렌더한다 —
#   진행률을 내려면 총 장수를 먼저 알아야 하기 때문이다(read_labeled_pdfs 주석).
#   그래서 메모리 피크는 '동시에 처리 중인 장수'가 아니라 '이 회차의 전체 장수'였다.
#   상한이 강의록 100 + 기출 60이고 스캔 PNG가 장당 2~3MB이므로 요청 하나가
#   수백 MB를 쥐고 있었다. RAM 1GB짜리 배포(e2-micro)에서 업로드 상한을 올리지
#   못한 진짜 이유가 이것이다.
#   경로만 들고 있다가 프로바이더에 보내기 직전에 읽으면, 동시에 메모리에 뜨는
#   장수가 IMAGE_WORKERS개로 묶인다.
#
#   ⚠️ pix.save()가 내는 바이트는 pix.tobytes("png")와 완전히 같다. 캐시 키가 PNG
#      바이트를 그대로 해싱하므로(image_cache_key) 이게 어긋나면 지난 회차에 돈을
#      내고 받아둔 설명이 전부 무효가 된다. 저장 방식을 바꿀 일이 있으면 반드시 확인할 것.
# 폴더 이름이 png인 것은 PNG만 내려두던 시절의 흔적이다(지금은 업로드 PDF도 여기 있다).
# 이름을 바꾸면 이미 돌고 있는 서버의 옛 폴더에 남은 찌꺼기를 아무도 치우지 않게 된다 —
# 청소는 지금 쓰는 폴더만 훑기 때문이다. 그 대가를 치를 만한 이유가 아니라 그대로 둔다.
_SPILL_SUBDIR = "png-spill"
# 정상 경로에서는 다 쓴 즉시 지운다. 이건 오류·취소로 그 삭제를 못 밟았을 때를 위한
# 청소 기준 — 확인 모달 대기(extract_cache.TTL_SECONDS = 20분)보다 넉넉해야 한다.
_SPILL_MAX_AGE = 3600
# 청소를 얼마나 자주 돌지. _spill_dir()은 페이지마다 불리므로(상한이면 160번) 매번
# 훑으면 디렉터리 스캔이 장수의 제곱으로 늘어난다. 찌꺼기 청소는 급할 게 없다.
_SPILL_SWEEP_INTERVAL = 600
_spill_swept_at = 0.0


def _sweep_spills(path: str, cutoff: float):
    """cutoff보다 오래된 찌꺼기를 지운다. 실패는 삼킨다 — 다음 차례에 다시 본다."""
    try:
        names = os.listdir(path)
    except OSError:
        return
    for name in names:
        stale = os.path.join(path, name)
        try:
            if os.path.getmtime(stale) < cutoff:
                os.remove(stale)
        except OSError:
            pass              # 다른 스레드가 방금 지웠거나 아직 쓰는 중


def _spill_dir() -> str:
    """내려둔 것들(PNG·업로드 PDF)이 사는 디렉터리. 없으면 만들고, 가끔 찌꺼기를 치운다.

    DB 옆에 두는 이유: 배포 스크립트가 DB_PATH를 반드시 실제 디스크의 영구 경로로
    잡는다(deploy/*/setup.sh). 시스템 임시 폴더는 배포판 설정에 따라 tmpfs(=RAM)일
    수 있어서, 메모리를 아끼려고 내린 것이 그대로 메모리에 남는다.
    DB_PATH가 없는 로컬 개발에서만 시스템 임시 폴더로 떨어진다.
    """
    global _spill_swept_at
    base = (os.path.dirname(os.path.abspath(config.DB_PATH)) if config.DB_PATH
            else tempfile.gettempdir())
    path = os.path.join(base, _SPILL_SUBDIR)
    os.makedirs(path, exist_ok=True)

    # 여러 요청 스레드가 동시에 지나갈 수 있지만 경합해도 손해가 없다 —
    # 최악이라도 청소가 한 번 더 도는 것뿐이라 잠금을 걸지 않는다.
    now = time.time()
    if now - _spill_swept_at > _SPILL_SWEEP_INTERVAL:
        _spill_swept_at = now
        _sweep_spills(path, now - _SPILL_MAX_AGE)
    return path


def _spill_png(pix) -> str:
    """pixmap을 PNG 파일로 내리고 경로를 돌려준다. (bytes를 거치지 않는다)"""
    fd, path = tempfile.mkstemp(suffix=".png", dir=_spill_dir())
    os.close(fd)
    pix.save(path, output="png")
    return path


def _read_spill(entry) -> bytes:
    """내려둔 PNG를 읽어온다. 부르는 쪽이 다 쓰면 참조를 놓아야 한다."""
    with open(entry["png_path"], "rb") as f:
        return f.read()


def _drop_spill(entry):
    """다 쓴 PNG 파일을 지운다. 경로도 지워 두 번 읽지 않게 한다."""
    path = entry.pop("png_path", None)
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def discard_spills(pages):
    """남은 PNG 파일을 정리한다 — 오류·취소로 중간에 끝났을 때 호출부가 부른다."""
    for entry in pages or []:
        _drop_spill(entry)


def spill_upload(fileobj, suffix: str = ".pdf") -> str:
    """업로드 파일을 디스크로 옮기고 경로를 돌려준다.

    라우트는 업로드를 미리 읽어둬야 한다 — 스트리밍 제너레이터가 요청 컨텍스트가
    닫힌 뒤에 돌기 때문이다. 그런데 bytes로 읽으면 **생성이 끝날 때까지** 파일 전체가
    힙에 남는다. 텍스트를 다 뽑은 뒤에도, 문제를 만드는 몇 분 내내 그대로다.
    경로로 넘기면 그 상주분이 사라지고 MuPDF가 디스크에서 필요한 만큼만 읽어간다.

    (fitz.open(stream=…)이 사본을 더 뜨지는 않는다 — 원본 bytes 객체의 참조를
     붙들어 공유한다. 대신 그래서 문서가 살아 있는 동안 그 bytes를 놓을 수도 없다.)

    1MB씩 옮기므로 이 함수 자체는 파일 크기와 무관하게 일정한 메모리만 쓴다.
    """
    fd, path = tempfile.mkstemp(suffix=suffix, dir=_spill_dir())
    with os.fdopen(fd, "wb") as out:
        shutil.copyfileobj(fileobj, out, 1024 * 1024)
    return path


def discard_upload_spills(files):
    """spill_upload로 만든 (이름, 경로) 목록을 정리한다. bytes가 섞여 있으면 건너뛴다."""
    for item in files or []:
        path = item[1] if isinstance(item, tuple) else item
        if isinstance(path, (str, os.PathLike)):
            try:
                os.remove(path)
            except OSError:
                pass


def read_pdf_pages(pdf, api_key: str = None, image_mode: dict = None,
                   max_images: int = None, timing=None):
    """
    PDF에서 텍스트 레이어를 뽑고, 이미지 처리가 필요한 페이지를 고른다. (LLM 호출 없음)

    image_mode: None이면 이미지 페이지를 건너뛴다 (텍스트 레이어만).
                IMAGE_DESCRIBE(기출) / IMAGE_TRANSCRIBE(강의록) 중 하나를 넘긴다.
    max_images: 이 PDF에서 처리할 페이지 수 상한. 생략하면 모드의 max_pages.
                호출부가 여러 PDF에 예산을 나눠 쓸 때(주제 분석) 그 몫을 넘긴다.
    timing:     providers.usage.PhaseTimer. 넘기면 렌더 한 장씩의 시간을 남긴다.
                생략하면 무동작(NO_TIMING).
    반환: (pages, img_jobs) — img_jobs는 pages 안 엔트리를 그대로 참조한다.
    """
    timing = timing or NO_TIMING
    # 경로 / 업로드 객체(FileStorage) / bytes 를 모두 받는다.
    #   경로: 스트리밍 경로가 쓰는 방식이다(spill_upload). MuPDF가 디스크에서 필요한
    #         만큼만 읽어가므로 파일 전체가 메모리에 올라오지 않는다.
    #   나머지: 테스트와 옛 호출부 호환. bytes는 그대로 메모리에 상주하므로,
    #         큰 파일을 다루는 경로에서는 쓰지 말 것.
    if isinstance(pdf, (str, os.PathLike)):
        doc = fitz.open(pdf)
    else:
        pdf_bytes = pdf.read() if hasattr(pdf, "read") else pdf
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    mode = image_mode or {}
    max_pages = mode.get("max_pages", 0) if max_images is None else max_images
    detect_ink = mode.get("detect_ink", False)
    min_area = mode.get("min_area_ratio", DESCRIBE_MIN_AREA_RATIO)
    dpi = mode.get("dpi", DESCRIBE_RENDER_DPI)

    # ── 선행 패스: 텍스트 레이어가 깨졌는지 먼저 가린다 ──
    # ToUnicode CMap이 없는 한글 폰트를 만나면 글리프 번호가 글자인 양 새어 나온다
    # (korean_text_looks_broken 위 주석). 글자 '수'는 멀쩡해서 아래 후보 판정을 그냥
    # 통과하므로, 여기서 미리 걸러 두지 않으면 쓰레기가 그대로 프롬프트로 간다.
    #
    # 오프셋은 폰트 속성이라 문서에 하나뿐이다. 쪽마다 따로 찾지 않고 깨진 쪽을 전부
    # 이어 붙여 한 번만 찾는다 — 글자가 적은 쪽도 문서 전체 표본에 얹혀 같이 살아난다.
    with timing.measure("text_layer"):
        texts = [doc[i].get_text() for i in range(doc.page_count)]
    broken = [korean_text_looks_broken(t) for t in texts]
    if any(broken):
        offset = find_hangul_offset("\n".join(t for t, b in zip(texts, broken) if b))
        if offset:
            # 되살렸다 — LLM 호출 없이 원문이 돌아왔으니 이미지로 다시 읽을 이유가 없다
            texts = [repair_hangul(t, offset) if b else t
                     for t, b in zip(texts, broken)]
            broken = [False] * len(texts)
        elif offset == 0:
            # 지금 글자가 이미 최선 = 감지가 헛짚었다. 멀쩡한 쪽을 이미지로 보내지 않는다.
            broken = [False] * len(texts)

    pages = []       # {idx, text, png(optional), desc}
    img_jobs = []    # 이미지 처리 대상 페이지 (엔트리 참조)
    for i, page in enumerate(doc):
        text = texts[i]
        # 끝내 못 되살린 쪽의 글자는 **버린다.** 남겨 두면 Vision이 옮겨 적은 글과
        # 나란히 프롬프트에 실려, LLM이 쓰레기 쪽도 원문으로 믿고 주제·문항을 뽑는다.
        entry = {"idx": i, "text": "" if broken[i] or not text.strip() else text,
                 "desc": None}
        # 상한 검사를 후보 판정 '밖'에 둔다 — 안에 두면 상한을 채우는 순간 뒤 페이지는
        # 후보인지조차 안 따져서 전체가 몇 쪽이었는지가 안 남고, 진행률이 15/15로 떠
        # 다 읽은 것처럼 보인다. 세는 것은 로컬 계산이라 공짜이므로 끝까지 세고
        # 렌더·호출만 상한에 건다. (or는 왼쪽부터 끊기므로 비싼 has_vector_ink는 마지막)
        is_candidate = (
            image_mode and api_key
            and (broken[i]
                 or len(text.strip()) < SPARSE_TEXT_THRESHOLD
                 or has_meaningful_image(page, min_area)
                 or (detect_ink and has_vector_ink(page)))
        )
        if is_candidate:
            if len(img_jobs) < max_pages:
                try:
                    # 깨진 쪽은 '글자를 읽어야' 하므로 전사용 해상도로 굽는다. 기출 기본값
                    # (DESCRIBE_RENDER_DPI=110)은 무엇을 그린 이미지인지만 알면 된다는
                    # 전제로 잡은 값이라, 문항 번호와 본문을 옮겨 적기에는 낮다.
                    page_dpi = TRANSCRIBE_RENDER_DPI if broken[i] else dpi
                    # 렌더한 즉시 디스크로 내린다 (위 spill 절 참고). 메모리에 남는 것은
                    # 경로 문자열뿐이고, pixmap은 이 줄을 벗어나면 회수된다.
                    # 계측은 PNG 인코딩·디스크 기록까지 포함한다 — 병렬화를 검토할 때
                    # 옮겨야 하는 일 전체가 이 한 덩이라서 쪼개 재면 오히려 오해를 부른다.
                    with timing.measure("render"):
                        entry["png_path"] = _spill_png(page.get_pixmap(dpi=page_dpi))
                    img_jobs.append(entry)
                except Exception:
                    pass
            else:
                # 상한을 넘겨 못 읽는 쪽. image_coverage()가 이걸 세어 경고로 만든다.
                entry["img_skipped"] = True
        pages.append(entry)
    doc.close()
    return pages, img_jobs


def image_coverage(pages, img_jobs) -> dict:
    """
    이 PDF에서 이미지 대상이 몇 쪽이었고 그중 몇 쪽을 실제로 처리했는지.

    상한에 걸려 못 읽은 쪽이 있으면 skipped_pages에 쪽 번호(1부터)가 담긴다.
    화면은 이걸로 "그림 47쪽 중 15쪽만 반영"을 보여주고, 진행 전에 확인을 받는다.
    """
    skipped = [e["idx"] + 1 for e in pages if e.get("img_skipped")]
    processed = len(img_jobs)
    return {
        "candidates":    processed + len(skipped),
        "processed":     processed,
        "skipped":       len(skipped),
        "skipped_pages": skipped,
    }


def side_image_budget(image_mode: dict, img_budget: int = None) -> int:
    """
    한쪽(강의록 전체 / 기출 전체)에 배정할 이미지 처리 예산.
    생략하면 모드의 max_pages를 **이쪽 전체**에 쓴다 — 파일당이 아니다.
    """
    if not image_mode:
        return 0
    return image_mode.get("max_pages", 0) if img_budget is None else img_budget


def next_image_quota(remaining: int, files_left: int) -> int:
    """
    남은 이미지 예산에서 이번 파일에 줄 몫. '남은 예산 ÷ 남은 파일 수'.
    앞 파일이 제 몫을 다 안 쓰면 남은 만큼이 뒤 파일로 넘어간다 (기출 6개가 텍스트 PDF,
    1개가 스캔 200쪽인 경우가 흔하다). 예산이 남아 있는 한 최소 1은 준다.

    0은 예산을 다 쓴 뒤에만 나온다. **그때도 read_pdf_pages에는 image_mode를 그대로
    넘겨야 한다** — None으로 바꾸면 후보 판정을 건너뛰어 못 읽은 쪽이 img_skipped로
    안 남고, 화면에서 '그림이 없는 파일'과 구분이 안 돼 경고가 조용히 사라진다.
    """
    return max(1, remaining // files_left) if remaining > 0 else 0


def _describe_one(prov, png_bytes: bytes, api_key: str, model: str, fallback: str,
                  usage, prompt: str, max_output: int):
    """
    이미지 한 장을 텍스트화한다. 반환: (텍스트, 실제로 쓴 모델).

    본 모델이 실패하면 fallback 모델로 **한 번만** 더 시도한다.
    단 **키 오류는 폴백하지 않는다** — 어느 모델로 보내도 똑같이 401인데, 워커가 병렬로
    도는 탓에 상한(IMAGE_CAP_*)만큼의 헛호출이 그대로 두 배가 된다. 나머지(모델 없음·
    한도 초과·5xx·연결 실패)는 다른 모델이면 살 수 있다.
    """
    try:
        return prov.describe_image(png_bytes, api_key, model, usage=usage,
                                   prompt=prompt, max_tokens=max_output), model
    except ProviderAuthError:
        raise
    except Exception:
        if not fallback:
            raise
    return prov.describe_image(png_bytes, api_key, fallback, usage=usage,
                               prompt=prompt, max_tokens=max_output), fallback


def _describe_spilled(prov, entry, api_key: str, model: str, fallback: str,
                      usage, prompt: str, max_output: int, timing=NO_TIMING):
    """내려둔 PNG를 **워커 안에서** 읽어 _describe_one에 넘긴다.

    읽기를 제출 시점이 아니라 워커 안에서 하는 것이 요점이다 — 제출할 때 읽으면
    pending 전체가 한꺼번에 메모리에 떠서 디스크로 내린 의미가 사라진다.
    워커 수(IMAGE_WORKERS)가 곧 동시에 메모리에 뜨는 장수가 된다.

    계측이 워커 **안**에 있어야 호출 하나하나의 시간이 남는다. 바깥(as_completed)에서
    재면 앞 호출을 기다린 시간까지 얹혀서 뒤로 갈수록 부풀어 오른다.
    """
    with timing.measure("image_call"):
        return _describe_one(prov, _read_spill(entry), api_key, model, fallback,
                             usage, prompt, max_output)


def describe_images_progressively(img_jobs, api_key: str, model: str, provider=None,
                                  usage=None, image_mode: dict = None, timing=None):
    """
    이미지 페이지를 병렬로 텍스트화하되, 하나 끝날 때마다 완료 개수를 yield한다.
    (진행률 표시용 — 호출부가 제너레이터를 돌리면서 이벤트를 낼 수 있게)

    image_mode를 생략하면 IMAGE_DESCRIBE(그림 설명). 한 장이 실패하면 프로바이더가
    선언한 폴백 모델로 한 번 더 시도하고, 그것마저 실패하면 폴백 문구를 채운다.
    캐시(image_desc_cache)를 먼저 뒤지며, 캐시에서 나온 항목도 완료 개수에 포함한다.

    timing: providers.usage.PhaseTimer (선택). 캐시 적중은 LLM을 안 부르므로 시간이
            아니라 **개수**로 따로 센다 — 적중이 많은 회차를 '호출이 빨랐다'로
            읽으면 계측 전체가 뒤집힌다.
    """
    if not img_jobs:
        return
    timing = timing or NO_TIMING
    prov = provider or _default_provider
    prov_name = getattr(prov, "name", "") or ""
    # 폴백 모델은 프로바이더가 선언한다 (providers/base.py 참고). 선언이 없으면 폴백 없음.
    fallback = getattr(prov, "image_fallback_model", "") or ""
    if fallback == model:
        fallback = ""                   # 방금 죽은 모델을 그대로 한 번 더 때리지 않는다
    mode = image_mode or IMAGE_DESCRIBE
    prompt = mode["prompt"]
    max_output = mode.get("max_output") or DESCRIBE_MAX_OUTPUT

    done = 0
    pending = []
    for e in img_jobs:
        # 키는 PNG 바이트 전체를 해싱하므로 한 장씩 읽어 계산하고 곧바로 놓는다.
        # 여기는 순차라 동시에 뜨는 것은 한 장이다.
        #
        # 계측을 따로 두는 이유 — 이 패스는 LLM을 안 부르는데도 호출 구간 **안**에서
        # 돈다. 한 장마다 PNG 전체를 디스크에서 읽어 해싱하므로 장수와 파일 크기에
        # 비례해 커진다(상한을 다 채우면 수백 MB를 읽는다). 묶어서 재면 이 시간이
        # 네트워크 지연으로 둔갑해 '모델이 느리다'는 엉뚱한 결론이 나온다.
        with timing.measure("cache_probe"):
            e["cache_key"] = image_cache_key(prompt, _read_spill(e), max_output)
            cached = get_cached_image_desc(e["cache_key"], prov_name, model)
            if not cached and fallback:
                # 지난 회차에 폴백이 만들어둔 결과도 받아준다. 이 조회가 없으면 본 모델이
                # 죽어 있는 동안 매 회차 실패 호출이 한 번씩 더 난다.
                cached = get_cached_image_desc(e["cache_key"], prov_name, fallback)
        if cached:
            e["desc"] = cached
            _drop_spill(e)          # 캐시에서 나왔으니 이 그림은 더 볼 일이 없다
            timing.count("image_cache_hit")
            done += 1
            yield done
        else:
            pending.append(e)

    if not pending:
        return

    # with 문 대신 직접 여닫는 이유는 아래 finally 에 있다.
    ex = ThreadPoolExecutor(max_workers=config.IMAGE_WORKERS)
    try:
        futs = {ex.submit(_describe_spilled, prov, e, api_key, model, fallback,
                          usage, prompt, max_output, timing): e
                for e in pending}
        for fut in as_completed(futs):
            entry = futs[fut]
            try:
                entry["desc"], used_model = fut.result()
            except Exception:
                entry["desc"] = "(이미지 처리 실패 — 게이트웨이 이미지 미지원일 수 있음)"
            else:
                # 성공한 것만 남긴다 — 실패 문구를 캐시하면 일시적 오류가 영구히 굳어
                # 그 페이지는 다음 회차부터 영영 설명 없이 돈다. (쓰기는 메인 스레드에서만)
                # 모델 이름은 **실제로 만든 쪽**으로 남긴다. 폴백 결과를 본 모델 이름으로
                # 저장하면 본 모델이 복구된 뒤에도 그 페이지를 다시 물어보지 않는다.
                put_cached_image_desc(entry["cache_key"], prov_name, used_model,
                                      entry["desc"])
            _drop_spill(entry)      # 성공이든 실패든 이 그림은 끝났다
            done += 1
            yield done
    finally:
        # 중지를 누르거나 브라우저가 끊기면 이 제너레이터가 닫히면서(GeneratorExit)
        # 여기를 지난다. 그때 **아직 시작도 안 한 장은 버려야 한다.**
        #   with 문의 기본 동작은 shutdown(wait=True) 라 큐에 남은 것을 끝까지 다 돌린다.
        #   상한이 160장이면 사용자는 멈췄다고 보는데 남은 그림값이 그대로 나가고,
        #   중지 자체도 그게 다 끝날 때까지 붙잡혀 있다.
        # 이미 워커에 올라간 장(최대 IMAGE_WORKERS개)은 못 되돌린다 — 그 결과는
        # 아무도 안 읽고 버려진다. wait=False 라 중지가 그것마저 기다리지도 않는다.
        ex.shutdown(wait=False, cancel_futures=True)


def assemble_pdf_text(pages, img_job_count: int = 0, image_mode: dict = None,
                      max_images: int = None) -> str:
    """
    페이지 텍스트 + 이미지에서 뽑은 텍스트를 하나로 합친다.
    image_mode의 label이 마커에 들어간다 ([페이지 3 그림 속 글자] 등) — 생략하면 그림 설명.

    max_images: 이 PDF에 실제로 적용한 상한. 생략하면 모드의 max_pages.
                '몇 쪽까지만 처리됨'을 본문에 남겨야 하므로 예산을 나눠 쓴 호출부는
                그 몫을 넘긴다.
    """
    mode = image_mode or IMAGE_DESCRIBE
    cap = mode["max_pages"] if max_images is None else max_images
    parts = []
    for e in pages:
        block = []
        if e["text"]:
            block.append(f"[페이지 {e['idx']+1}]\n{e['text']}")
        if e.get("desc"):
            block.append(f"[페이지 {e['idx']+1} {mode['label']}]\n{e['desc']}")
        if block:
            parts.append("\n".join(block))

    # 상한에 걸려 못 읽은 쪽이 있으면 LLM에게도 알린다 — 자료가 온전하지 않다는 사실을
    # 모르면 "여기 없는 내용"을 근거 삼아 단정하는 답이 나온다.
    skipped = sum(1 for e in pages if e.get("img_skipped"))
    if skipped:
        parts.append(f"...(그림·필기 {img_job_count + skipped}쪽 중 앞 {img_job_count}쪽만 "
                     f"처리됨 — 나머지 {skipped}쪽은 반영되지 않음)")
    elif cap > 0 and img_job_count >= cap:
        parts.append(f"...(이미지 페이지는 최대 {cap}개까지만 처리됨)")

    return "\n\n".join(parts)


def _truncate_split(max_chars: int):
    """truncate()가 남기는 (앞, 뒤) 길이. 실제 자르기와 경고용 계산이 어긋나지 않게 공유한다."""
    head = int(max_chars * 0.7)
    return head, max_chars - head


# 줄머리 문제 번호 — "1.", "12)", "문제 3.", "38]".
# 유형 집계(count_question_types)와 기출 자르기(_cut_points)가 같은 정의를 공유한다.
# 갈라 두면 "집계에는 문항으로 잡히는데 자를 때는 경계가 아닌" 자리가 생긴다.
_QUESTION_START = re.compile(r"(?m)^[ \t]*(?:문제\s*)?\d{1,3}\s*[.)\]]")


def _cut_points(raw: str, limit: int, by_question: bool = False):
    """
    상한을 넘길 때 **실제로 버려지는 구간** (head_end, tail_start). 안 넘으면 None.
    자르기(truncate)·경고(truncation_report)·반영 범위(build_source_info)가 이 하나를
    공유한다. 갈라 두면 화면이 알려준 구간과 실제로 빠진 구간이 어긋난다.

    by_question(기출): 경계를 문항 번호 자리로 당긴다. 글자수로만 자르면 앞쪽 끝에
      반쪽 지문이, 뒤쪽 시작에 번호 없는 꼬리가 남아 LLM이 엉뚱한 번호에 갖다 붙인다.
      틀린 출처는 없는 출처보다 나쁘다(build_topic_analysis_prompt 규칙 ②).
      번호를 못 찾는 기출(스캔본 등)은 글자수 방식으로 되돌아간다.
    """
    if len(raw) <= limit:
        return None
    head, tail = _truncate_split(limit)
    head_end, tail_start = head, len(raw) - tail
    if by_question:
        starts = [m.start() for m in _QUESTION_START.finditer(raw)]
        # 앞: 상한 안에 온전히 들어가는 마지막 문항까지 (걸친 문항은 통째로 버린다)
        h = max((s for s in starts if s <= head_end), default=None)
        # 뒤: 다시 살리는 첫 문항의 시작으로 민다 (앞 문항의 꼬리를 남기지 않는다)
        t = min((s for s in starts if s >= tail_start), default=None)
        if h is not None and t is not None and h < t:
            head_end, tail_start = h, t
    return head_end, tail_start


def truncate(text: str, max_chars: int = MAX_TEXT_CHARS,
             by_question: bool = False) -> str:
    """
    상한 초과 시 앞부분만 남기던 방식 → 앞(70%)+뒤(30%) 보존.
    기출 뒤쪽 문제·강의 마무리 내용이 통째로 사라지지 않도록 함.
    by_question이면 문항 경계로 잘라 반쪽짜리 문항을 남기지 않는다 (_cut_points 참고).
    """
    cut = _cut_points(text, max_chars, by_question)
    if not cut:
        return text
    head_end, tail_start = cut
    # 이어붙인 자리에 쪽 표시를 복원한다 — 뒤쪽 첫 문항·첫 문단이 몇 쪽인지 모르면
    # 출처를 만들 수 없어 그 뒤가 통째로 버려진다(주제 분석 프롬프트 규칙 ②).
    page = _page_at(text, tail_start)
    marker = f"[페이지 {page}]\n" if page else ""
    return (text[:head_end] + "\n\n...(중략: 분량 초과로 일부 생략)...\n\n"
            + marker + text[tail_start:])


# 추출 텍스트에 박아둔 쪽 표시 — assemble_pdf_text가 넣는다 ([페이지 3], [페이지 3 그림 속 글자])
_PAGE_MARKER = re.compile(r"\[페이지 (\d+)[^\]]*\]")
_WORD = re.compile(r"\S+")


def _page_at(raw: str, pos: int):
    """pos 앞쪽에서 가장 가까운 쪽 번호. 마커가 없으면 None."""
    page = None
    for m in _PAGE_MARKER.finditer(raw, 0, pos + 1):
        page = int(m.group(1))
    return page


def _word_start_after(raw: str, pos: int) -> int:
    """
    pos가 어절 한가운데면 그 어절 끝으로 민다.
    경계에 걸린 어절은 앞부분이 남으므로 '온전히 버려지는' 첫 어절이 아니다.
    """
    n = len(raw)
    if 0 < pos < n and not raw[pos - 1].isspace():
        while pos < n and not raw[pos].isspace():
            pos += 1
    return pos


def _word_end_before(raw: str, pos: int) -> int:
    """pos가 어절 한가운데면 그 어절 앞으로 당긴다. (뒷부분이 남는 어절을 제외)"""
    if pos < len(raw) and not raw[pos].isspace():
        while pos > 0 and not raw[pos - 1].isspace():
            pos -= 1
    return pos


def _words_near(raw: str, start: int, end: int, count: int, take_last: bool) -> str:
    """
    [start, end) 구간에서 어절 count개를 뽑는다. take_last면 뒤에서, 아니면 앞에서.
    쪽 표시([페이지 N])는 원문이 아니라 우리가 넣은 것이므로 건너뛴다.
    """
    spans = [(m.start(), m.end()) for m in _PAGE_MARKER.finditer(raw, start, end)]
    words = []
    for m in _WORD.finditer(raw, start, end):
        if any(s < m.end() and m.start() < e for s, e in spans):
            continue                       # 쪽 표시의 조각
        words.append(m.group())
        if not take_last and len(words) >= count:
            break
    picked = words[-count:] if take_last else words[:count]
    return " ".join(picked)


def truncation_report(raw: str, limit: int, by_question: bool = False) -> dict:
    """
    limit을 넘는 문서에서 **어디부터 어디까지 버려지는지**를 쪽·어절로. 안 넘으면 None.
    줄 번호 대신 쪽·어절을 쓰는 이유: 추출 텍스트의 줄 번호는 원본 PDF에서 눈으로 찾을
    수 없다. "12쪽 'superior orbital'부터"라야 사용자가 확인할 수 있다.
    """
    cut = _cut_points(raw, limit, by_question)
    if not cut:
        return None
    head_end, tail_start = cut
    # 경계에 걸친 어절은 절반이 남으므로 온전히 버려지는 구간으로 좁힌다
    start = _word_start_after(raw, head_end)        # 여기서부터 버려진다
    end = _word_end_before(raw, tail_start)         # 여기부터 다시 남는다

    return {
        "chars": len(raw),
        "limit": limit,
        # 문항 경계로 당기면 상한보다 조금 덜 들어가므로 실제 반영량으로 센다
        "coverage": round((head_end + len(raw) - tail_start) / len(raw) * 100),
        # 버려지는 첫 어절 두 개 / 마지막 어절 두 개와 그 쪽 번호
        "from_page": _page_at(raw, start),
        "from_words": _words_near(raw, start, min(start + 400, end), 2, False),
        "to_page": _page_at(raw, end),
        "to_words": _words_near(raw, max(start, end - 400), end, 2, True),
    }


def build_source_info(raw_text: str, limit: int = MAX_TEXT_CHARS,
                      by_question: bool = False) -> dict:
    """
    원문 추출 글자수 대비 LLM에 실제 반영된 범위 (truncate 여부·비율).
    limit: 이 문서에 적용한 글자수 상한. 생략하면 기본 상한(MAX_TEXT_CHARS).
           (기출 주제 분석처럼 여러 파일에 예산을 나눠 쓰는 경우 그 값을 넘긴다)
    used는 상한이 아니라 **실제로 들어간 양**이다 — 문항 경계로 자르면(by_question)
    상한보다 조금 덜 들어가는데, 상한을 그대로 쓰면 화면이 반영률을 부풀린다.
    """
    chars = len(raw_text or "")
    cut = _cut_points(raw_text or "", limit, by_question)
    used = chars if not cut else cut[0] + chars - cut[1]
    return {
        "chars": chars,
        "used": used,
        "limit": limit,
        "truncated": cut is not None,
        "coverage": (round(used / chars * 100) if chars else 100),
    }


def remaining_gen_budget(lecture_text: str) -> int:
    """
    문제 생성 — 강의자료가 쓰고 남긴 몫. 기출에 넘길 글자 예산이다.

    lecture_text는 이미 상한이 적용된 결과라 여기서 세는 값이 실제로 프롬프트에 들어가는
    양과 같다. 자르기 전 원문 길이를 세면 안 된다 — 안 들어간 몫까지 쓴 것으로 쳐서
    기출 예산을 도로 깎는다.
    하한은 한쪽 몫(GEN_SIDE_CHAR_BUDGET)이라 기출이 더 적게 받는 일은 없고,
    천장은 GEN_CALL_CHAR_CEILING이라 몰아줘도 컨텍스트를 넘기지 않는다.
    (주제 분석의 remaining_char_budget()과 같은 방식 — 그쪽은 문서 리스트를 받는다)
    """
    used = len(lecture_text or "")
    return min(GEN_CALL_CHAR_CEILING,
               max(GEN_SIDE_CHAR_BUDGET, GEN_TOTAL_CHAR_BUDGET - used))


def call_llm(prompt: str, api_key: str, model: str, provider=None,
             max_tokens: int = None, usage=None, cache_prefix: str = None) -> str:
    # provider 계층으로 위임.
    #   max_tokens   — 주면 프로바이더 기본값 대신 그 값 (응답 잘림을 막아야 할 때)
    #   usage        — 주면 프로바이더가 토큰 사용량을 거기에 기록
    #   cache_prefix — prompt 앞에 붙는 '반복되는 고정 부분'. 프롬프트 캐싱을 지원하는
    #                  제공사는 캐시 경계를 잡고, 아닌 곳은 이어붙인다(동작 동일).
    return (provider or _default_provider).complete(
        prompt, api_key, model, max_tokens=max_tokens, usage=usage,
        cache_prefix=cache_prefix,
    )


def call_llm_stream(prompt: str, api_key: str, model: str, provider=None,
                    max_tokens: int = None, usage=None, cache_prefix: str = None):
    """응답을 조각으로 흘려받는다 (호출부에서 StreamingQuestionParser와 함께 사용)."""
    return (provider or _default_provider).complete_stream(
        prompt, api_key, model, max_tokens=max_tokens, usage=usage,
        cache_prefix=cache_prefix,
    )


# ──────────────────────────────────────────────
# 기출 유형 통계 (정규식 — LLM 미사용, 0 토큰)
# ──────────────────────────────────────────────

def normalize_type_stats(raw: dict) -> dict:
    """
    LLM/정규식이 준 유형 카운트를 4분류 표준 형태로 정규화.
    키 표기 흔들림(예: '빈칸 채우기' → '빈칸채우기')을 흡수. 반환에 '총문항' 포함.
    """
    stats = {t: 0 for t in QUESTION_TYPES}
    if isinstance(raw, dict):
        for k, v in raw.items():
            key = str(k).replace(" ", "")
            if key in stats:
                try:
                    stats[key] = max(0, int(v))
                except (TypeError, ValueError):
                    pass
    stats["총문항"] = sum(stats[t] for t in QUESTION_TYPES)
    return stats


def count_question_types(exam_text: str) -> dict:
    """
    정규식 폴백 집계 (LLM 4분류가 없을 때만 사용, 0 토큰).
    - 객관식: 줄머리 '①' 개수 (문항당 1개 → 안정적). '정답: ①' 표기는 제외.
    - 전체 문항: 줄머리 문제번호 패턴
    - 비객관식은 4분류를 정규식으로 가릴 수 없어 '단답형'으로 일괄 분류(부정확)
    """
    text = exam_text or ""
    objective = len(re.findall(r"(?m)^\s*①", text))
    markers = _QUESTION_START.findall(text)      # 기출 자르기와 같은 '문항 시작' 정의
    total = max(len(markers), objective)
    non_obj = max(0, total - objective)

    stats = {"객관식": objective, "빈칸채우기": 0, "단답형": non_obj, "서술형": 0}
    stats["총문항"] = total
    if total == 0:
        stats["판별근거"] = "문항 구분 실패 — 통계 신뢰 낮음"
    elif non_obj:
        stats["판별근거"] = "정규식 추정 — 비객관식은 단답형으로 일괄 분류(부정확)"
    else:
        stats["판별근거"] = "정규식 추정 (객관식만)"
    return stats


def resolve_type_stats(exam_concepts: dict, exam_text: str) -> dict:
    """LLM 4분류(exam_concepts.유형통계) 우선, 없으면 정규식 폴백."""
    stats = normalize_type_stats((exam_concepts or {}).get("유형통계"))
    if stats["총문항"] > 0:
        stats["판별근거"] = "LLM 유형 분류"
        return stats
    return count_question_types(exam_text)


def compute_type_targets(type_stats: dict, count: int,
                         preserve_present: bool = False) -> dict:
    """
    기출 유형 통계 비율을 생성 문제 수(count)에 그대로 투영 (4분류).
    최대잉여법(largest remainder)으로 합이 정확히 count가 되게 배분.
    통계가 없으면 빈 dict(→ 기존 예시 비율 유지).

    preserve_present=True 면 기출에 1문항 이상 존재하는 유형이 비율 반올림 때문에
    0개로 사라지지 않게 최소 1개를 보장한다(모자란 몫은 가장 많이 배분된 유형에서 뺀다).
    비율을 일부러 왜곡하는 동작이라 기본값은 꺼짐이고, 화면에서 켤 때만 쓴다.
    """
    counts = {t: max(0, int((type_stats or {}).get(t, 0) or 0)) for t in QUESTION_TYPES}
    total = sum(counts.values())
    if total == 0:
        return {}

    raw = {t: count * counts[t] / total for t in QUESTION_TYPES}
    targets = {t: int(raw[t]) for t in QUESTION_TYPES}          # 내림
    remainder = count - sum(targets.values())
    # 남은 몫은 소수부가 큰 유형부터 1개씩 (소수부 0인 유형은 사실상 제외됨)
    order = sorted(QUESTION_TYPES, key=lambda t: raw[t] - targets[t], reverse=True)
    for i in range(remainder):
        targets[order[i % len(order)]] += 1

    if preserve_present:
        present = [t for t in QUESTION_TYPES if counts[t]]
        # count가 존재 유형 수보다 적으면 애초에 전부 담을 수 없다 → 비율 배분을 그대로 둔다
        if len(present) <= count:
            for t in (x for x in present if targets[x] == 0):
                donor = max(QUESTION_TYPES, key=lambda x: targets[x])
                if targets[donor] <= 1:
                    break          # 더 뺄 여유가 없으면 중단 (합계는 항상 count 유지)
                targets[donor] -= 1
                targets[t] = 1
    return targets


# ──────────────────────────────────────────────
# 기출 분석 (출제 개념 + 유형 통계 + Few-shot 예시를 한 호출로)
# ──────────────────────────────────────────────

# 대표 문제(Few-shot 예시)를 몇 개까지 뽑을지. 5 → 12 (2026-08).
# 5개일 땐 4유형 보장에 4칸이 묶여 '실제 비중 반영'에 쓸 자리가 1칸뿐이었다. 그래서
# 대표문제가 빈도가 아니라 유형 카탈로그가 됐고, 증례처럼 소수인 특징이 표본에서
# 과대표집됐다. 자유 자리를 8칸으로 늘려 비중이 실리게 한다.
#   ⚠️ 비용은 생성 1회가 아니라 **배치마다** 든다 — 이 예시는 생성 프롬프트 접두부에
#      실린다(build_question_generation_prompt). 접두부는 캐시 대상이라 증분은 완만하지만
#      0은 아니고, 객관식 원문 하나가 지문+선택지 5줄이라 개당 부피가 크다.
#   ⚠️ 기출 원문이 그대로 12개 실리므로 재출제 압력도 같이 오른다. 막는 것은
#      '동일 문제 출제 금지'(공통 규칙)와 [6] 회피 목록 두 가지다.
SAMPLE_QUESTION_MAX = 12


def build_exam_analysis_prompt(exam_text: str) -> str:
    """
    기출 전문 한 번으로 ①출제 개념 ②유형 통계 ③대표 문제(Few-shot)를 모두 뽑는다.
    셋을 한 호출로 합친 이유: 따로 부르면 기출 전문을 여러 번 결제하게 된다.
    같은 호출 안에서 세고 고르므로 통계와 예시의 근거도 일치한다.
    """
    return f"""아래는 의대 기출문제 PDF에서 추출한 전문입니다.
이 기출을 분석해 ①출제 개념 ②유형 통계 ③대표 문제 세 가지를 한 번에 반환하세요.

## 표시의 뜻 (기출 텍스트를 읽는 법)
- `[페이지 N]` 아래는 PDF에서 그대로 뽑은 **시험지 원문**입니다.
- `[페이지 N 이미지 설명]` 아래는 그 쪽의 그림을 보고 **다른 모델이 쓴 설명**이며,
  시험지에 인쇄된 문장이 아닙니다. 같은 문항이 원문과 설명 양쪽에 나오면 **원문 쪽**을
  쓰세요. 설명 안의 번호 매긴 목록은 요약의 항목이지 시험 문항이 아닙니다.
- `(필기: …)`는 학생이 손으로 덧쓴 것(정답 표시·메모)이지 시험지에 인쇄된 내용이
  아닙니다. **정답의 근거로 쓰지 말고, 대표문제 원문에도 옮기지 마세요.**
- `(판독불가)`는 읽을 수 없던 자리입니다. 내용을 지어내 채우지 마세요.

## 문제 유형 4분류
{TYPE_DEFINITIONS}

## [1] 출제 개념
이 기출에서 **실제로 출제된 핵심 개념·주제와 출제 경향**을 뽑으세요.
형식(어떻게 물었나)이 아니라 내용(무엇을 물었나)에 집중하세요.

## [2] 유형 통계
기출 전체 문항을 위 4분류로 **빠짐없이** 세세요. (합 = 전체 문항 수)

## [3] 대표 문제 — 선택 기준 (중요)
- **총 최대 {SAMPLE_QUESTION_MAX}개**까지 선택.
- 텍스트에 존재하는 **각 유형마다 최소 1개씩** 반드시 포함하세요.
  (해당 유형이 하나도 없으면 생략, 있으면 최소 1개 보장.)
- 유형별로 1개씩 채운 뒤, **남은 자리는 위 [2] 유형 통계의 문항 수 비중에 맞춰**
  채우세요. (예: 객관식이 전체의 70%면 남은 자리도 70%쯤을 객관식으로)
- **증례(환자 사례) 지문이 있는 문항과 없는 문항의 비율도 실제와 비슷하게** 고르세요.
  뒤 단계는 기출 전문을 다시 보지 않고 여기 고른 문제만 보고 증례를 얼마나 낼지
  정합니다. 증례 문항만 몰아 고르면 실제로는 소수인 증례가 생성 문제 전체로 퍼집니다.
- **앞에서부터 순서대로 고르지 말고**, 그 시험에서 **전형적(대표적)**인 문제를 우선 선택하세요.
  특이하거나 예외적인 형식의 문제는 피하세요.
- 원문 그대로 옮기되, 해설·부연설명 등 답이 아닌 내용은 절대 포함하지 마세요.
  · 객관식 → 문제 번호, 지문(증례), 선택지 ①②③④⑤, 정답까지
  · 빈칸채우기/단답형/서술형 → 문제와 정답(모범답안)만 (선택지 없음)
- **정답은 시험지에 인쇄된 정답 표기에서만** 가져오세요. 손으로 친 동그라미·체크나
  `(필기: …)`로 표시된 내용으로 정답을 정하지 마세요 — 학생이 틀리게 적었을 수 있습니다.
  인쇄된 정답이 없으면 그 문제는 **정답 줄을 아예 빼고** 문제까지만 옮기세요.
- `[페이지 N 이미지 설명]`에만 있고 원문에 없는 문항은 시험지 문장을 그대로 옮길 수
  없으므로 대표 문제로 고르지 마세요. (고를 문항이 없으면 그 유형은 생략)

## 출력 형식 — 아래 JSON만 (코드블록·설명 문장 금지)
{{
  "기출출제개념": ["출제된 개념/주제1", "개념/주제2"],
  "빈출포인트": ["반복 출제되거나 강조된 포인트1", "포인트2"],
  "유형통계": {{"객관식": 0, "빈칸채우기": 0, "단답형": 0, "서술형": 0}},
  "대표문제": [
    {{"유형": "객관식", "원문": "문제 번호부터 정답까지 원문 그대로 (줄바꿈 포함)"}}
  ]
}}
- "대표문제.유형"은 4분류 중 하나를 정확히 쓰세요.

## 기출문제 텍스트
{exam_text}"""


def _rebuild_sample_questions(items) -> str:
    """
    대표문제 JSON 배열 → Few-shot 텍스트 형식으로 복원.

    생성 프롬프트의 "[1] 기출문제 예시"와 sessions.sample_questions 가
    "[유형: X]\\n원문"을 빈 줄 두 개로 구분한 형식을 쓰므로 그 형식을 유지한다.
    (연결 계약 — 아래 단계는 이 문자열을 그대로 받는다)
    """
    blocks = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        raw = str(it.get("원문") or "").strip()
        if not raw:
            continue
        # 라벨 표기가 흔들려도(예: '빈칸 채우기') 4분류로 정규화한다
        qtype = normalize_question_type(str(it.get("유형") or ""), [], raw)
        blocks.append(f"[유형: {qtype}]\n{raw}")
    return "\n\n\n".join(blocks)


def analyze_exam(exam_text: str, api_key: str, model: str, provider=None,
                 usage=None) -> dict:
    """기출 전문 → {exam_concepts, sample_questions}. LLM 1회."""
    raw = call_llm(build_exam_analysis_prompt(exam_text), api_key, model, provider,
                   usage=usage)
    data = safe_parse_json(raw)
    return {
        "exam_concepts": {
            "기출출제개념": data.get("기출출제개념") or [],
            "빈출포인트":   data.get("빈출포인트") or [],
            # 비어 있으면 resolve_type_stats()가 정규식 폴백으로 넘어간다
            "유형통계":     data.get("유형통계") or {},
        },
        "sample_questions": _rebuild_sample_questions(data.get("대표문제")),
    }


def analyze_format(sample_questions: str, api_key: str, model: str, provider=None,
                   usage=None) -> str:
    """
    추출된 기출 예시를 분석해서 형식 규칙을 **키워드 위주**로 정리.
    프론트엔드에서 태그 형태로 렌더링하기 좋게 "라벨: 키워드1, 키워드2" 라인 형식.
    """
    prompt = f"""아래는 실제 기출문제 예시입니다.
이 문제들의 형식적 특징을 **키워드 위주로** 간결하게 정리하세요.
산문 설명 없이, 각 항목을 반드시 "라벨: 키워드1, 키워드2, ..." 한 줄 형태로만 작성하세요.

항목 (해당 없으면 '해당없음'):
문제유형: (객관식/빈칸채우기/단답형/서술형 키워드)
증례제시: (나이/성별/상황/검사 등 키워드)
문체: (어투, 문장 길이, 전문용어 사용 여부 등 키워드)
선택지구성: (객관식 선택지 길이·구조 키워드 / 비객관식이면 '해당없음')
질문형태: (진단/치료/다음단계 등 키워드)
기타: (특이사항 키워드)

## 기출문제 예시
{sample_questions}"""

    return call_llm(prompt, api_key, model, provider, usage=usage)


def _norm_term(s: str) -> str:
    """비교용 정규화: 괄호 이후 제거, 공백 제거, 소문자화."""
    return s.split("(")[0].replace(" ", "").lower()


def _terms_overlap(a: str, b: str) -> bool:
    """
    두 용어가 의미상 겹치는지 대략 판정.
    - 정규화 문자열 상호 부분 포함
    - 2글자+ 토큰이 상대 정규화 문자열에 부분 포함
      (한국어 조사 '심근경색' vs '심근경색의 …' 매칭을 위해 토큰-부분포함 사용)
    """
    na, nb = _norm_term(a), _norm_term(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    for t in re.findall(r"[가-힣A-Za-z]{2,}", a):
        if t.lower() in nb:
            return True
    for t in re.findall(r"[가-힣A-Za-z]{2,}", b):
        if t.lower() in na:
            return True
    return False


def compute_priority_topics(concepts: dict, exam_concepts: dict) -> list:
    """
    강의자료 개념 중 기출 출제개념·빈출포인트와 겹치는 주제를 '우선 출제 주제'로 선별.
    → 실제 시험 경향에 가까운 문제를 만들도록 가중치 부여.
    """
    exam_terms = (exam_concepts.get("기출출제개념", []) or []) + \
                 (exam_concepts.get("빈출포인트", []) or [])
    lecture_items = []
    for key in ["핵심질환", "핵심개념", "중요수치", "진단포인트", "치료원칙"]:
        lecture_items.extend(concepts.get(key, []) or [])

    priority, seen = [], set()
    for item in lecture_items:
        if not item or item in seen:
            continue
        if any(_terms_overlap(item, et) for et in exam_terms):
            seen.add(item)
            priority.append(item)
    return priority


# ──────────────────────────────────────────────
# 프롬프트 설계
# ──────────────────────────────────────────────

def build_concept_extraction_prompt(lecture_text: str) -> str:
    """
    강의자료 전문 → 출제할 개념 목록(JSON).

    '표시의 뜻'을 함께 싣는 이유 — 아래 텍스트에는 원문에 없던 마커가 섞여 있다
    (assemble_pdf_text가 넣는 [페이지 N]·[페이지 N 그림 속 글자], 전사가 남기는
    (판독불가)). 안 알려주면 모델이 강의자료에 인쇄된 내용과 구분할 방법이 없어,
    '(판독불가)' 같은 자리표시가 중요수치·핵심개념 목록에 그대로 올라온다.
    기출 분석(build_exam_analysis_prompt)과 주제 분석(build_topic_analysis_prompt)은
    같은 이유로 이미 이 구역을 갖고 있다 — 세 프롬프트가 같은 텍스트 형식을 받는데
    여기만 빠져 있었다.
      ※ `(필기: …)`는 일부러 넣지 않았다. 그 표시는 기출 쪽 전사(IMAGE_DESC_PROMPT)만
         만든다. 강의록 전사(IMAGE_TEXT_PROMPT)는 손글씨를 갈라내지 않고 그대로
         옮기는데, 교수 판서를 살리려는 의도된 설계다.
    """
    return f"""당신은 한국 의과대학 교육 전문가입니다.
아래 강의자료에서 핵심 정보를 추출하여 JSON 형식으로 반환하세요.

## 표시의 뜻 (강의자료를 읽는 법)
- `[페이지 N]` 아래는 PDF에서 그대로 뽑은 **강의자료 원문**입니다.
- `[페이지 N 그림 속 글자]` 아래는 그 쪽 그림 안의 글자를 **그대로 옮긴 것**이므로
  강의자료 원문으로 봐도 됩니다.
- 위 두 마커는 우리가 끼워 넣은 표시입니다. 강의자료에 인쇄된 내용이 아니므로
  추출 결과에 옮기지 마세요.
- `(판독불가)`는 읽을 수 없던 자리입니다. 내용을 지어내 채우지 말고, 이 자리표시를
  추출 결과에 그대로 옮기지도 마세요.

## 강의자료
{lecture_text}

## 추출 항목
다음 JSON 형식으로 정확하게 반환하세요 (코드블록 없이 JSON만):
{{
  "핵심질환": ["질환명1", "질환명2"],
  "관련부위": ["부위1", "부위2"],
  "핵심개념": ["개념1", "개념2"],
  "중요수치": ["수치1 (의미)", "수치2 (의미)"],
  "진단포인트": ["포인트1", "포인트2"],
  "치료원칙": ["원칙1", "원칙2"]
}}"""


# 회피 목록 상한. 프로바이더 기본 max_tokens(4096)를 회피 목록이 갉아먹으면
# 정작 문제 생성 여유가 줄어 응답이 잘린다 → 최근 것만 짧게 싣는다.
AVOID_LIST_MAX = 24
AVOID_SNIPPET_LEN = 80


def build_question_generation_prompt(
    concepts: dict,
    sample_questions: str,
    format_analysis: str,
    count: int,
    exam_concepts: dict = None,
    priority_topics: list = None,
    weight: int = 5,
    type_targets: dict = None,
    avoid_questions: list = None,
) -> tuple:
    """
    생성 프롬프트를 만든다 — 기출 예시(Few-shot)·형식 분석·기출 경향·가중 주제를 모두 싣는다.

    weight(1~10): 기출(개념·형식) 반영 강도. 사용자가 조절.
      - 높을수록 기출 출제개념·형식을 강하게 재현, 우선 주제 출제 비율↑
      - 낮을수록 강의자료 전반에서 자유롭게 출제, 형식은 느슨하게 참고

    반환: (prefix, suffix) 두 조각
      prefix — 한 회차 안에서 배치마다 **글자 하나까지 똑같은** 부분
               (기출 예시·형식 키워드·개념 목록·공통 규칙·출력 형식)
      suffix — 배치마다 달라지는 부분 (이번 배치 문제 수·유형 배분·회피 목록)

    나누는 이유 — 프롬프트 캐싱은 **앞에서부터 정확히 일치하는 구간**에만 걸린다.
    count가 크면 이 함수가 GEN_BATCH_SIZE 문제씩 여러 번 불리므로, 고정 부분을 앞에
    몰아 두면 2번째 배치부터 접두부를 캐시에서 읽는다.
    (캐시 지시 필드가 없는 제공사에서는 그냥 이어붙이므로 동작은 같다)
    """
    exam_concepts = exam_concepts or {}
    priority_topics = priority_topics or []
    type_targets = type_targets or {}

    # 가중치 정규화 및 파생 지표
    weight = max(1, min(10, int(weight)))
    # 우선 주제에서 출제할 최소 문제 수 (weight 비례)
    min_priority = max(0, min(count, round(count * weight / 10)))

    if weight >= 8:
        weight_guide = (
            "기출에서 다뤄진 개념과 문투·구조·선택지 형식을 **거의 그대로 재현**하세요. "
            "출제 내용도 기출 경향에 최대한 밀착시킵니다."
        )
    elif weight >= 4:
        weight_guide = (
            "기출 경향과 형식을 **균형 있게 반영**하되, 강의자료 개념도 폭넓게 활용하세요."
        )
    else:
        weight_guide = (
            "기출은 **참고만** 하고 강의자료 핵심 개념 위주로 폭넓게 출제하세요. "
            "형식은 큰 틀만 느슨하게 맞춥니다."
        )

    # 유형 구성 지시 (기출 유형 통계 → 생성 문제 수 배분, 4분류)
    if type_targets:
        parts = [f"{t} {type_targets.get(t, 0)}개"
                 for t in QUESTION_TYPES if type_targets.get(t, 0) > 0]
        type_rule = "**" + ", ".join(parts) + "**로 출제 (기출 유형 구성 비율 반영, 합계 준수)"
    else:
        type_rule = "**기출문제 예시에 나타난 유형 비율을 그대로 유지**할 것"

    weight_block = f"""━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [0] 기출 반영 강도: {weight}/10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{weight_guide}
"""

    exam_trend_block = ""
    if exam_concepts.get("기출출제개념") or exam_concepts.get("빈출포인트"):
        exam_trend_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3-A] 기출 출제 경향 (실제 기출에서 물었던 내용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 기출 출제 개념: {', '.join(exam_concepts.get('기출출제개념', []))}
- 빈출 포인트: {', '.join(exam_concepts.get('빈출포인트', []))}
"""

    # ── 이미 출제된 문제 회피 (배치 간 중복 방지) ──
    # 배치마다 이 함수가 같은 인자로 불려 프롬프트가 사실상 같으므로, 각 배치가 개념
    # 목록에서 '가장 눈에 띄는 것'을 독립적으로 다시 골라 표현만 다른 같은 문제가 나온다.
    # → 앞 배치들이 만든 문제문을 넘겨받아 명시적으로 배제한다.
    avoid_block = avoid_rule = ""
    recent = [" ".join((q or "").split()) for q in (avoid_questions or [])]
    recent = [q for q in recent if q][-AVOID_LIST_MAX:]
    if recent:
        avoid_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [6] 이미 출제된 문제 (이번 회차에서 앞서 만든 것 — 재출제 금지)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{chr(10).join('- ' + q[:AVOID_SNIPPET_LEN] for q in recent)}
"""
        # '개념 금지'로 읽히면 모델이 [3] 개념 목록 밖으로 나가 억지 문제를 만든다.
        # 금지 범위를 '같은 문제'로 좁히고, 같은 개념의 다른 축은 허용임을 명시한다.
        avoid_rule = (
            "\n- **아래 [6]에 있는 문제와 같은 것을 묻는 문제는 만들지 마세요.**"
            " 표현만 바꾼 재출제도 금지입니다."
            "\n  단 [3]의 개념 자체를 피하라는 뜻은 **아닙니다** — 같은 개념이라도"
            " 다른 축(정의 / 기능 / 신경지배 / 경계·내용물 / 수치·레벨)을 물으면 됩니다."
        )

    # ── 증례(환자 사례) 지시 ──
    # 출력 형식의 '문제:' 칸에는 증례를 **넣지 않는다**. 예전에는 [2] 형식 키워드가
    # '증례제시: 해당없음'이 아니면 "[증례 포함 문제 전체]"를 못박았는데, 그 판정에는
    # 비율이 없다 — analyze_format은 기출 전문이 아니라 대표문제 몇 개만 보고 저 줄을
    # 쓰므로, 표본 5개 중 1개만 증례여도 '증례 있음'이 되고 생성 문항 **전부**가
    # 증례로 시작했다. 출력 형식은 프롬프트에서 가장 강한 지시라 다른 규칙이 못 이긴다.
    # 그래서 템플릿은 중립으로 두고, 증례를 얼마나 섞을지는 [1] 기출문제 예시가
    # 스스로 보여주게 맡긴다 (few-shot이 원래 잘하는 일이다).
    #
    # 증례가 **아예 없는** 기출에서만 따로 못박는다. 이쪽은 예시에 증례가 하나도 없어
    # "만들지 마라"까지는 few-shot이 말해주지 못하는데, 모델은 의대 문제라는 이유만으로
    # 증례 지문을 지어낸다.
    no_vignette = any(
        line.strip().startswith("증례제시") and "해당없음" in line
        for line in (format_analysis or "").splitlines()
    )
    vignette_rule = (
        "\n- 이 기출에는 증례(환자 사례) 제시가 없습니다."
        " 증례 지문을 새로 만들지 말고 개념을 직접 묻는 형태로 출제하세요."
        if no_vignette else ""
    )

    # 주제 목록 자체는 회차 내내 그대로라 접두부(캐시 대상)에 두고,
    # 배치마다 달라지는 "최소 N문제" 지시만 접미부로 뺀다.
    priority_block = priority_rule = ""
    if priority_topics:
        priority_block = f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3-B] ⭐ 우선 출제 주제 (강의자료 ∩ 기출 경향 — 가중치 높음)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
아래 주제는 강의자료 핵심이면서 기출에서도 다뤄진 항목입니다.
{chr(10).join('- ' + t for t in priority_topics)}
"""
        priority_rule = (
            f"\n- **이번 {count}문제 중 최소 {min_priority}문제 이상을 위 [3-B]"
            f" 우선 출제 주제에서** 출제 (기출 반영 강도 {weight}/10 기준)"
        )

    prefix = f"""당신은 한국 의과대학 중간/기말고사 출제위원입니다.
아래 [기출문제 예시]의 형식과 **유형(4분류)**을 참고하여 새로운 예상 문제를 생성하세요.
단, 아래 [0] 기출 반영 강도에 따라 기출을 얼마나 강하게 반영할지 조절하세요.

## 문제 유형 4분류
{TYPE_DEFINITIONS}

{weight_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [1] 기출문제 예시 (형식·유형 참고용)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{sample_questions}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [2] 형식 키워드 (반드시 준수)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{format_analysis}
{exam_trend_block}{priority_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3] 출제할 개념 (강의자료 기반)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 핵심 질환: {', '.join(concepts.get('핵심질환', []))}
- 관련 부위: {', '.join(concepts.get('관련부위', []))}
- 핵심 개념: {', '.join(concepts.get('핵심개념', []))}
- 중요 수치: {', '.join(concepts.get('중요수치', []))}
- 진단 포인트: {', '.join(concepts.get('진단포인트', []))}
- 치료 원칙: {', '.join(concepts.get('치료원칙', []))}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [4] 공통 생성 규칙
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 유형별 형식:
  · 객관식 → 선택지 ①②③④⑤ 포함. **번호는 반드시 ①②③④⑤ 기호로 쓸 것**
    (a) b) c) / 1. 2. 3. / 가) 나) 다) 금지), 정답도 그 기호로 답할 것
  · 빈칸채우기 → 문제 문장에 빈칸 '____'를 두고, 선택지 없이 빈칸에 들어갈 답 제시
    (빈칸이 2개 이상이면 각 정답을 문제에 등장하는 빈칸 순서대로 ' | '로 구분해
     한 줄에 나열. 예: 정답: NADH | FADH2  ※빈칸 1개면 구분자 없이 답만)
  · 단답형 → 선택지 없이 단어·구·수치로 답
  · 서술형 → 선택지 없이 여러 문장으로 서술하는 모범답안 제시
- 문체·구조·선택지 형식은 위 [0] 기출 반영 강도({weight}/10)에 맞춰 반영
- 기출문제와 내용이 완전히 동일한 문제는 출제 금지
- **수식·기호는 LaTeX 문법을 쓰지 말고 그대로 읽히는 문자로 쓸 것.**
  화면에 수식 렌더러가 없어 LaTeX를 쓰면 기호가 그대로 글자로 보인다.
  $ ... $ / \\text{{}} / \\Delta / \\approx / ^{{}} / _{{}} 모두 금지.
  이렇게 쓸 것 → ΔG°' < 0,  K'eq > 1,  1 cal ≈ 4.184 J,  ΔG₁ + ΔG₂,  25°C,  H₂O
- 문항·선택지·해설은 모두 한국어로 쓸 것 (의학 용어의 영문 원어 병기는 허용)
- 각 문제에 해설과 함정포인트 포함{vignette_rule}

## 출력 형식 (마크다운 코드블록 사용 금지)

[객관식 문제일 때]
---QUESTION---
유형: 객관식
번호: 1
문제: [문제 전체]
선택지:
① [선택지1]
② [선택지2]
③ [선택지3]
④ [선택지4]
⑤ [선택지5]
정답: [번호 예: ③]
해설: [상세 해설]
함정포인트: [핵심 함정]
---END---

[빈칸채우기 — 빈칸이 2개 이상일 때]
---QUESTION---
유형: 빈칸채우기
번호: 2
문제: 해당과정에서 ____(은)는 조효소로 작용하며, 최종 산물은 ____이다.
정답: NADH | 피루브산
해설: [상세 해설]
함정포인트: [핵심 함정]
---END---

[그 외 비객관식(빈칸채우기 1개/단답형/서술형)일 때 — 유형 라벨만 바꿔 사용]
---QUESTION---
유형: 단답형
번호: 3
문제: [문제 전체 — 빈칸채우기는 문장에 '____' 포함]
정답: [모범 답안]
해설: [상세 해설]
함정포인트: [핵심 함정]
---END---"""

    # ── 여기부터가 배치마다 달라지는 부분 (캐시 경계 뒤) ──
    suffix = f"""

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [5] 이번에 만들 문제 (지금 지시)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- 문제 수: {count}개
- 유형 구성: {type_rule}{priority_rule}{avoid_rule}
{avoid_block}"""

    return prefix, suffix


# ──────────────────────────────────────────────
# 파싱 함수
# ──────────────────────────────────────────────

# LLM 출력에서 문제 하나를 감싸는 구분자 (프롬프트의 출력 형식과 짝을 이룸)
QUESTION_START = "---QUESTION---"
QUESTION_END   = "---END---"

# 화면·인쇄·오답노트가 모두 전제하는 선택지 기호. 여기 순서가 곧 선택지 순서다.
CIRCLED_MARKS = "①②③④⑤⑥⑦⑧⑨⑩"

# 선택지 앞머리 기호. 프롬프트는 ①②③④⑤ 를 요구하지만 모델이 'a)' · '1.' · '(가)'
# 로 쓰는 일이 잦다. 이 기호들을 못 알아보면 그 줄들이 선택지 리스트가 아니라
# 문자열 한 덩어리로 들어가고, 화면은 Array.isArray 로 걸러내 **객관식인데 보기가
# 한 줄도 안 나오는** 카드가 된다. (실제로 그렇게 생성된 세트가 있었다)
# '1.5배 …' 를 기호로 오인하지 않도록 마침표 형식은 뒤에 공백을 요구한다.
CHOICE_MARK_RE = re.compile(
    rf"^(?:[{CIRCLED_MARKS}]"                     # ① ②      — 프롬프트가 요구하는 형태
    r"|\(?[a-jA-J1-9가나다라마바사아자차]\)"       # a) (a) 1) 가)
    r"|[a-jA-J1-9]\.(?=\s))"                      # a. 1.
    r"\s*"
)

# 기호를 순번으로 바꿀 때 쓰는 표기 계열 (CHOICE_MARK_RE 가 잡아낸 글자 하나를 찾는다)
_MARK_ORDERS = ("abcdefghij", "ABCDEFGHIJ", "123456789", "가나다라마바사아자차")


def choice_mark_index(text: str) -> int:
    """'c)' · '3.' · '(나)' 같은 앞머리 기호가 몇 번째를 가리키는지 (0-based). 없으면 -1."""
    m = CHOICE_MARK_RE.match((text or "").strip())
    if not m:
        return -1
    key = m.group(0).strip("(). \t")
    if key in CIRCLED_MARKS:
        return CIRCLED_MARKS.index(key)
    for order in _MARK_ORDERS:
        if key in order:
            return order.index(key)
    return -1


def normalize_choice_marks(choice_lines: list) -> list:
    """선택지 앞머리를 ①②③④⑤ 로 통일. 이미 그 기호면 손대지 않는다."""
    if not choice_lines or all(c[:1] in CIRCLED_MARKS for c in choice_lines):
        return choice_lines
    out = []
    for i, line in enumerate(choice_lines):
        if i >= len(CIRCLED_MARKS):      # 10개를 넘으면 붙일 기호가 없다 (사실상 없는 경우)
            out.append(line)
            continue
        body = CHOICE_MARK_RE.sub("", line, count=1).strip()
        out.append(f"{CIRCLED_MARKS[i]} {body}")
    return out


def parse_question_block(block: str) -> dict:
    """구분자를 걷어낸 블록 하나를 문제 dict로 변환. '문제'가 없으면 None."""
    q = {}
    current_key = None
    choice_lines = []
    buffer = []
    # 정답:/해설:/함정포인트: 가 시작된 뒤인가.
    # 모델이 해설을 '① …는 이래서 틀렸다' 식으로 선지별로 쓰는 일이 흔한데, 그 줄들까지
    # 선택지로 집으면 선택지가 10개가 되고 해설은 그만큼 빈다. 이 구간에 들어서면
    # ①②③④⑤ 로 시작해도 선택지가 아니라 본문으로 본다.
    in_answer = False
    # 선택지 수집 상태. 모델이 답을 고쳐 쓰겠다며 같은 목록을 한 번 더 뱉는 일이 있다
    # (실제: '정답: ①, ③ (복수정답)' 을 쓴 뒤 선택지 블록을 통째로 재출력하고 '정답: ①').
    # 번호가 안 늘어나면 목록이 끝난 것으로 보고 그 뒤 줄은 버린다 — 안 그러면 선지가 10개가 된다.
    last_mark = -1
    choices_closed = False

    def flush_buffer(key, buf):
        if key and buf:
            q[key] = "\n".join(buf).strip()

    def take_choice(line):
        """선택지 줄 하나를 받는다. 기호가 없으면 앞 선택지의 줄바꿈으로 본다."""
        nonlocal last_mark, choices_closed
        idx = choice_mark_index(line)
        if idx < 0:
            if choice_lines and not choices_closed:
                choice_lines[-1] += " " + line
            return
        if idx <= last_mark:            # ①로 되돌아감 = 같은 목록을 또 쓴 것
            choices_closed = True
        if not choices_closed:
            last_mark = idx
            choice_lines.append(line)

    for line in block.split("\n"):
        line = line.strip()
        if line.startswith("유형:"):
            flush_buffer(current_key, buffer); buffer = []
            q["유형"] = line.replace("유형:", "").strip(); current_key = None
        elif line.startswith("번호:"):
            flush_buffer(current_key, buffer); buffer = []
            q["번호"] = line.replace("번호:", "").strip(); current_key = None
        elif line.startswith("문제:"):
            flush_buffer(current_key, buffer); buffer = []
            current_key = "문제"
            val = line.replace("문제:", "").strip()
            if val: buffer.append(val)
        elif line == "선택지:":
            flush_buffer(current_key, buffer); buffer = []
            current_key = "선택지"
        elif not in_answer and line.startswith(tuple(CIRCLED_MARKS)):
            take_choice(line)
        elif line.startswith("정답:"):
            flush_buffer(current_key, buffer); buffer = []
            in_answer = True
            q["정답"] = line.replace("정답:", "").strip(); current_key = None
        elif line.startswith("해설:"):
            flush_buffer(current_key, buffer); buffer = []
            in_answer = True
            current_key = "해설"
            val = line.replace("해설:", "").strip()
            if val: buffer.append(val)
        elif line.startswith("함정포인트:"):
            flush_buffer(current_key, buffer); buffer = []
            in_answer = True
            current_key = "함정포인트"
            val = line.replace("함정포인트:", "").strip()
            if val: buffer.append(val)
        elif not in_answer and current_key == "선택지" and line:
            # '선택지:' 아래인데 ①②③④⑤ 로 시작하지 않는 줄 ('a)' · '1.' 등).
            # 위 조건들이 먼저 걸리므로 정답:/해설: 라벨이 여기 들어올 일은 없다.
            # ⚠️ not in_answer 를 빼면 안 된다 — 정답 뒤에 선택지를 다시 뱉는 모델이
            #    있어서, 빼는 순간 그 두 번째 목록까지 주워 담아 선지가 10개가 된다.
            take_choice(line)
        else:
            if current_key: buffer.append(line)

    flush_buffer(current_key, buffer)
    if choice_lines:
        choice_lines = normalize_choice_marks(choice_lines)
        q["선택지"] = choice_lines
        # 선택지 기호를 바꿨으면 정답의 기호도 같이 옮긴다 — 안 그러면
        # 'c) …' 가 ③ 을 가리키는지 화면이 알 수 없어 정답 표시가 빠진다.
        k = choice_mark_index(q.get("정답", ""))
        if 0 <= k < len(choice_lines) and q.get("정답", "")[:1] not in CIRCLED_MARKS:
            body = CHOICE_MARK_RE.sub("", q["정답"], count=1).strip()
            q["정답"] = f"{CIRCLED_MARKS[k]} {body}".strip()
    q["유형"] = normalize_question_type(q.get("유형"), choice_lines, q.get("문제", ""))
    return q if q.get("문제") else None


class StreamingQuestionParser:
    """
    LLM 응답 조각(chunk)을 받아, 문제가 하나씩 '완성될 때마다' 돌려주는 파서.

    청크 경계는 문제 중간을 마음대로 자르기 때문에(예: '---EN' + 'D---'),
    끝 구분자가 확인된 블록만 잘라내고 나머지는 버퍼에 남긴다.
    끝 구분자 없이 끝난 마지막 블록은 버린다.
    """

    def __init__(self):
        self.buffer = ""

    def feed(self, chunk: str) -> list:
        """청크를 넣고, 이번 호출에서 새로 완성된 문제들을 반환."""
        self.buffer += chunk or ""
        done = []
        while True:
            end = self.buffer.find(QUESTION_END)
            if end == -1:
                break
            head, self.buffer = self.buffer[:end], self.buffer[end + len(QUESTION_END):]
            # 끝 구분자 바로 앞의 시작 구분자부터가 이번 문제
            start = head.rfind(QUESTION_START)
            if start == -1:
                continue            # 시작 없이 끝만 온 경우 → 버리고 계속
            q = parse_question_block(head[start + len(QUESTION_START):].strip())
            if q:
                done.append(q)
        return done


def normalize_question_type(raw_type: str, choice_lines: list, question_text: str) -> str:
    """
    생성 문제의 유형을 4분류 표준값으로 정규화.
    라벨이 있으면 표기 흔들림을 흡수, 없으면 선택지·빈칸 유무로 추정.
    """
    if raw_type:
        key = raw_type.replace(" ", "")
        for t in QUESTION_TYPES:
            if t in key:            # '객관식', '빈칸채우기', '단답형', '서술형' 부분일치
                return t
        if "객관" in key:
            return "객관식"
    if choice_lines:
        return "객관식"
    # 빈칸 기호(____, □, ( ))가 있으면 빈칸채우기로 추정, 아니면 단답형
    if re.search(r"_{2,}|□{2,}|\(\s*\)", question_text or ""):
        return "빈칸채우기"
    return "단답형"


def safe_parse_json(text: str) -> dict:
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
    except Exception:
        pass
    return {}


# ──────────────────────────────────────────────
# 분석 파이프라인 (토큰 소모 집중 구간 — 세션에 저장해 재사용)
# ──────────────────────────────────────────────

def analyze_concepts_progressively(lecture_text: str, exam_text: str, api_key: str,
                                   model: str, provider=None, usage=None):
    """
    분석 1단계 — 의존성 없는 두 LLM 호출을 병렬 실행:
      [동시] ① 강의 핵심개념                     ┐
      [동시] ② 기출 개념 + 유형통계 + 대표문제   ┘ (analyze_exam — 한 호출로 묶여 있다)

    하나가 끝날 때마다 완료 개수를 yield하고, 마지막에 결과 dict를 return한다.
    (호출부: `result = yield from analyze_concepts_progressively(...)`)
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_concepts = ex.submit(
            call_llm, build_concept_extraction_prompt(lecture_text), api_key, model,
            provider, None, usage
        )
        f_exam = ex.submit(analyze_exam, exam_text, api_key, model, provider, usage)
        done = 0
        for fut in as_completed([f_concepts, f_exam]):
            fut.result()          # 예외가 있으면 여기서 터뜨려 호출부가 잡게 한다
            done += 1
            yield done
        concepts = safe_parse_json(f_concepts.result())
        exam = f_exam.result()

    exam_concepts = exam["exam_concepts"]
    return {
        "concepts": concepts,
        "exam_concepts": exam_concepts,
        "sample_questions": exam["sample_questions"],
        # LLM 4분류 우선, 실패 시 정규식 폴백
        "type_stats": resolve_type_stats(exam_concepts, exam_text),
        "priority_topics": compute_priority_topics(concepts, exam_concepts),
    }


def analyze_format_progressively(sample_questions: str, api_key: str,
                                 model: str, provider=None, usage=None):
    """분석 2단계 — 대표 문제로 형식 규칙 정리 (LLM 1회)."""
    format_analysis = analyze_format(sample_questions, api_key, model, provider, usage)
    yield 1
    return {"format_analysis": format_analysis}


# ══════════════════════════════════════════════
# 기출 주제 분석 (features/topic_analysis.py 전용)
#   "강의록 몇 페이지의 주제가 어떤 기출 몇 번 문제로 나왔는가" 대응표를 만든다.
#   문제 생성과 달리 강의·기출을 여러 개 올릴 수 있어(어떤 강의록/어떤 기출을
#   구분해 보여줘야 하므로) 파일마다 라벨을 붙여 프롬프트에 넣는다.
# ══════════════════════════════════════════════

# 한쪽(강의록 전체 / 기출 전체)에 배정하는 글자 예산.
# 문제 생성(GEN_SIDE_CHAR_BUDGET)보다 작게 잡는 이유는 프롬프트 구조가 달라서다 —
# 거기는 강의자료와 기출이 서로 다른 호출로 나가지만, 주제 분석은 대조표를 만들어야 해서
# **둘을 한 프롬프트에 함께** 넣는다. 여기서는 총예산이 곧 프롬프트 하나의 크기다.
#   총 90만 자 × 0.52토큰/자 ≈ 47만 토큰. 출력 몫(TOPIC_MAX_TOKENS)과 지시문을 더해도
#   1M 컨텍스트의 절반 아래다. 이미지 상한(강의록 전사 100쪽 ≈ 8만 자)을 받아줄 수 있어야
#   돈 주고 뽑은 전사를 truncate가 도로 버리지 않는다.
#
# 30만 → 45만 (2026-08). 실측(topic_analyses id=25·26)에서 강의록은 상한의 21~46%만
# 쓰는데 **기출은 152쪽이 80%만 반영**됐다. 남는 몫을 넘겨줘도(remaining_char_budget)
# 모자랐다는 뜻이라, 칸막이가 아니라 총량을 올려야 하는 자리였다.
#   ⚠️ 서버 사양과는 무관하다 — 프롬프트는 문자열이라 90만 자여도 몇 MB다. 늘어나는 것은
#      입력 토큰, 즉 크레딧이다. 이 탭은 사용자가 고른 모델이 프롬프트 전체를 읽으므로
#      회차당 요금이 대체로 이 배수만큼 따라 오른다.
TOPIC_SIDE_CHAR_BUDGET = 450000
# 양쪽 합계. 칸막이를 고정하지 않는 이유는 실측(topic_analyses id=9·10·13·14·18·19) —
# 강의록은 상한의 21~46%만 쓰는데 기출은 매번 넘겼다(152쪽 기출이 80% 반영). 강의록을
# 먼저 추출해 실사용량이 확정되면 남은 몫을 기출에 넘긴다. (remaining_char_budget)
TOPIC_TOTAL_CHAR_BUDGET = TOPIC_SIDE_CHAR_BUDGET * 2
# 파일을 여러 개 올려도 문서당 이만큼은 보장 (예산 ÷ 파일수가 너무 작아지는 것 방지).
# 상한(MAX_FILES_PER_SIDE)까지 채웠을 때의 몫으로 잡아야 '문서당 몫 × 파일수 ≤ 예산'이
# 허용 개수 전 구간에서 성립한다. 손으로 잡으면 파일 상한을 올렸을 때 이 최소치가
# 예산을 덮어써서(max가 최소치를 고름) 프롬프트가 조용히 예산을 넘긴다.
TOPIC_DOC_MIN_CHARS = TOPIC_SIDE_CHAR_BUDGET // MAX_FILES_PER_SIDE
# 주제 목록이 길어져도 JSON이 잘리지 않도록 (항목마다 강의록발췌·기출 원문이 붙는다).
TOPIC_MAX_TOKENS = 16000


def remaining_char_budget(docs: list) -> int:
    """
    먼저 추출한 쪽(강의록)이 쓰고 남긴 몫 — 반대쪽(기출)에 넘길 글자 예산.

    docs의 text는 이미 상한이 적용된 결과라 여기서 세는 값이 실제로 프롬프트에 들어가는
    양과 같다. (원문 길이인 source["chars"]를 세면 안 된다 — 안 들어간 몫까지 쓴 것으로
    쳐서 반대쪽 예산을 도로 깎는다)
    하한은 한쪽 몫(TOPIC_SIDE_CHAR_BUDGET) — 상한을 손댔을 때 기출이 더 적게 받지 않도록.
    """
    used = sum(len(d.get("text") or "") for d in docs)
    return max(TOPIC_SIDE_CHAR_BUDGET, TOPIC_TOTAL_CHAR_BUDGET - used)


def read_labeled_pdfs(files, label_prefix: str, api_key: str = None,
                      image_mode: dict = None, img_budget: int = None,
                      timing=None) -> list:
    """
    PDF 를 페이지 단위로 읽어만 둔다 — 여기까지는 LLM 호출이 없다.

    읽기와 그림 텍스트화를 나눠 둔 이유: 진행률을 내려면 그림이 모두 몇 장인지 '먼저'
    알아야 한다. 호출부가 양쪽(강의록·기출)을 다 읽어 총계를 센 뒤에
    describe_images_progressively 를 돌리게 하려고 여기서 끊는다.

    files: (파일명, PDF) 쌍의 목록. PDF 는 디스크 경로·FileStorage·bytes 중 아무거나.
           스트리밍 경로는 **경로**를 넘긴다 — 요청 컨텍스트가 닫힌 뒤에 도는데,
           bytes 로 읽어두면 생성이 끝날 때까지 파일 전체가 힙에 남기 때문이다
           (spill_upload 주석).
    img_budget: 이미지 처리 상한 — **파일당이 아니라 이쪽 전체 기준**. 생략하면 모드의
           max_pages. '남은 예산 ÷ 남은 파일 수'로 배정한다(next_image_quota).
    timing: providers.usage.PhaseTimer (선택). 파일 여러 개를 읽어도 같은 계측기에
           누적되므로 한쪽(강의자료/기출) 전체의 렌더 시간이 한 값으로 모인다.
    반환: [{label, name, pages, img_jobs, quota}] (img_jobs 는 pages 안 엔트리를 참조한다)
          quota는 assemble_pdf_text가 '몇 개까지만 처리됨'을 적을 때 쓴다 —
          모드의 max_pages가 아니라 이 파일에 실제로 적용된 상한이어야 한다.
    """
    files = list(files or [])
    remaining_imgs = side_image_budget(image_mode, img_budget)
    parts = []
    for i, (name, pdf) in enumerate(files, start=1):
        name = name or f"{label_prefix}{i}"
        quota = next_image_quota(remaining_imgs, len(files) - i + 1)
        try:
            # quota가 0이어도 image_mode는 그대로 넘긴다 (next_image_quota 주석 참고)
            pages, img_jobs = read_pdf_pages(pdf, api_key, image_mode,
                                             max_images=quota, timing=timing)
        except Exception as e:
            # 앞 파일들이 이미 내려둔 PNG를 두고 나가지 않는다 (여러 개 중 하나만
            # 손상된 경우 — 청소 주기를 기다릴 것 없이 여기서 바로 치운다).
            for done_part in parts:
                discard_spills(done_part["pages"])
            # 손상·암호 걸린 PDF (fitz의 영문 예외를 어떤 파일이 문제인지 알려주는 문구로)
            raise ValueError(
                f"'{name}' 파일을 읽을 수 없습니다. "
                f"PDF가 손상되었거나 암호가 걸려 있는지 확인해주세요. (상세: {e})"
            ) from e
        remaining_imgs -= len(img_jobs)
        parts.append({"label": f"{label_prefix}{i}", "name": name,
                      "pages": pages, "img_jobs": img_jobs, "quota": quota})
    return parts


def finish_labeled_docs(parts: list, side_budget: int = TOPIC_SIDE_CHAR_BUDGET,
                        image_mode: dict = None, by_question: bool = False) -> list:
    """
    그림 텍스트화가 끝난 parts 를 '라벨 + 페이지 마커가 붙은 텍스트' 문서 목록으로 합친다.
    반환 형태는 아래 extract_labeled_docs 와 완전히 같다.

    by_question: 기출 쪽이면 True — 상한을 넘길 때 문항 번호 경계로 자른다
                 (반쪽짜리 문항을 남기지 않는다. _cut_points 참고)
    """
    if not parts:
        return []

    # 예산을 파일 수로 나눠 배정 (파일이 많으면 문서당 최소치는 보장)
    per_doc = max(TOPIC_DOC_MIN_CHARS, side_budget // len(parts))

    docs = []
    for p in parts:
        raw = assemble_pdf_text(p["pages"], len(p["img_jobs"]), image_mode,
                                p.get("quota"))
        docs.append({
            # cut·img 는 진행 전 확인 모달에만 쓴다 — DB에는 저장하지 않는다.
            "cut": truncation_report(raw, per_doc, by_question),   # 안 넘으면 None
            "img": image_coverage(p["pages"], p["img_jobs"]),      # 그림 몇 쪽 중 몇 쪽
            "label": p["label"],
            "name": p["name"],
            "text": truncate(raw, per_doc, by_question),
            # 내용이 잡힌 페이지 수 (스캔 PDF 판별·안내용). '[페이지 N]'과
            # '[페이지 N 이미지 설명]' 두 형태를 모두 센다 (set으로 중복 제거).
            "pages": len(set(re.findall(r"\[페이지 (\d+)[\] ]", raw))),
            "source": build_source_info(raw, per_doc, by_question),
        })
    return docs


def extract_labeled_docs(files, label_prefix: str, api_key: str, model: str,
                         image_mode: dict = None, provider=None,
                         side_budget: int = TOPIC_SIDE_CHAR_BUDGET,
                         img_budget: int = None,
                         by_question: bool = False,
                         usage=None) -> list:
    """
    업로드된 PDF 여러 개를 '라벨 + 페이지 마커가 붙은 텍스트'로 추출한다.
    (read_labeled_pdfs → describe_images_progressively → finish_labeled_docs 를 한 번에.
     진행률이 필요한 스트리밍 경로는 세 함수를 직접 조합한다)

    label_prefix: '강의록' / '기출' → 라벨은 강의록1, 강의록2 … LLM이 출처를 가리킬 때
                  쓴다. 긴 한글 파일명을 되풀이하게 하면 오타가 나므로 짧은 라벨을 쓰고,
                  파일명 복원은 파싱 단계에서 한다(_resolve_doc_name).
    side_budget:  이쪽 전체에 배정하는 글자 예산 (파일 수로 나눠 쓴다).
                  기출은 강의록이 남긴 몫을 받으므로 호출부가 remaining_char_budget()으로
                  계산해 넘긴다. 생략하면 한쪽 몫(TOPIC_SIDE_CHAR_BUDGET)이다.
    img_budget:   이미지 처리 호출 수 상한 — **파일당이 아니라 이쪽 전체 기준**.
                  생략하면 모드의 max_pages(IMAGE_CAP_EXAM / IMAGE_CAP_LECTURE).
    by_question:  기출 쪽이면 True — 상한을 넘길 때 문항 번호 경계로 자른다
                  (반쪽짜리 문항을 남기지 않는다. _cut_points 참고)
    반환: [{label, name, text, pages, source}]  (text에는 [페이지 N] 마커가 들어 있다)
    """
    files = list(files or [])
    if not files:
        return []

    parts = read_labeled_pdfs([(f.filename, f) for f in files],
                              label_prefix, api_key, image_mode, img_budget)
    for p in parts:
        try:
            for _ in describe_images_progressively(p["img_jobs"], api_key, model,
                                                   provider, usage, image_mode):
                pass
        except ProviderError:
            raise                  # 키·한도 오류는 라우트가 상태코드로 구분하므로 그대로
    return finish_labeled_docs(parts, side_budget, image_mode, by_question)


def build_topic_doc_block(docs: list) -> str:
    """추출된 문서들을 라벨·파일명 구분선과 함께 하나의 프롬프트 블록으로 합친다."""
    parts = []
    for d in docs:
        parts.append(
            f"───────── [{d['label']}] 파일명: {d['name']} ─────────\n"
            f"{d['text'] or '(텍스트를 추출하지 못했습니다)'}"
        )
    return "\n\n".join(parts)


def build_topic_analysis_prompt(lecture_block: str, exam_block: str) -> str:
    """
    강의록 주제 ↔ 기출 문항 대응표 프롬프트.

    설계 의도 네 가지:
      ① 주제명은 **강의록에 실제로 있는 표현**만 쓰게 강제한다.
         (교과서 용어로 바꿔 쓰면 학생이 강의록에서 그 주제를 못 찾는다)
      ② 페이지·문항 번호를 확인할 수 없는 항목은 아예 버리게 한다.
         (틀린 출처는 없는 출처보다 나쁘다 — 학생이 그 페이지를 찾아보게 되므로)
      ③ 목차 페이지는 근거로 인정하지 않는다. 목차 줄에도 주제 이름은 있어서 ②를
         글자 그대로는 만족하는 탓에, 회차마다 목차 쪽과 본문 쪽이 번갈아 뽑혔다
         (실측: 같은 주제가 2쪽 / 128쪽). '설명이 있는 쪽'으로 좁힌다.
      ④ 마커의 뜻을 프롬프트 안에서 밝힌다. 기출의 [페이지 N 이미지 설명]은 시험지
         문장이 아닌데 ⑦은 "원문 그대로" 옮기라고 하므로, 안 알려주면 그 설명이 기출
         원문으로 인용된다(스캔 기출은 텍스트 레이어가 비어 설명밖에 없다). 반대로
         강의록의 [페이지 N 그림 속 글자]는 전사라 원문으로 봐도 된다 — 뭉뚱그리면
         한쪽이 틀리므로 갈라서 적는다. (필기: …)도 ②의 '근거'가 되면 안 되므로 함께.
    """
    return f"""당신은 한국 의과대학 시험 분석 전문가입니다.
[강의록]에 적혀 있는 주제 중 [기출문제]에서 **실제로 출제된 것만** 골라
"강의록 몇 페이지의 주제가 어떤 기출 몇 번 문제로 나왔는지" 대응표를 만드세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [1] 강의록 (자료 라벨 · [페이지 N] 마커 포함)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{lecture_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [2] 기출문제 (자료 라벨 · 문제 번호 포함)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{exam_block}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [3] 표시의 뜻 (위 텍스트를 읽는 법)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- `[페이지 N]` 아래는 PDF에서 그대로 뽑은 **원문**입니다.
- `[페이지 N 그림 속 글자]`(강의록) 아래는 그림 안의 글자를 **그대로 옮긴 것**이므로
  강의록 원문으로 봐도 됩니다.
- `[페이지 N 이미지 설명]`(기출) 아래는 그 쪽의 그림을 보고 **다른 모델이 쓴 설명**이며,
  시험지에 인쇄된 문장이 아닙니다. 문제 번호·페이지를 확인하는 데는 써도 되지만,
  아래 ⑦의 "원문"으로 그대로 인용하면 안 됩니다.
- `(필기: …)`는 사람이 손으로 덧쓴 것(정답 표시·메모)이지 자료에 인쇄된 내용이
  아닙니다. **주제나 출제 내용의 근거로 쓰지 마세요.**
- `(판독불가)`는 읽을 수 없던 자리입니다. 내용을 지어내 채우지 마세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [4] 규칙 (반드시 준수)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
① **용어 규칙 — 가장 중요**
   - "주제"는 **강의록 본문에 그대로 등장하는 표현**만 쓰세요.
   - 강의록에 없는 단어를 쓰지 마세요. 일반 의학 교과서 용어·영어 병기·상위 개념어를
     새로 만들어 붙이지 마세요.
   - 강의록의 표기(띄어쓰기·한자·약어·번호)를 그대로 옮기세요. 다듬거나 요약하지 말고,
     강의록에 있는 어구를 **잘라서** 쓰세요.
   - "출제형태" 문장도 강의록에 있는 용어로만 쓰세요.
② **근거 규칙 — 추측 금지**
   아래 두 가지가 위 텍스트에서 **모두 확인되는** 주제만 출력하세요.
   - 강의록: 그 주제가 적힌 [페이지 N] 의 N
     목차·색인처럼 **이름만 올라 있는** 페이지는 근거가 아닙니다. 그 주제를 실제로
     설명하는 내용이 있는 페이지를 대세요.
     (목차 줄에는 "제목 … 130." 처럼 **다른 쪽의 번호**가 따라붙습니다. 그 번호는
      그 줄이 있는 페이지 번호가 아니며, [페이지 N] 마커만이 페이지 번호입니다)
   - 기출: 그 주제를 묻는 문제의 **문제 번호** (기출 텍스트에 적힌 번호 그대로)
   페이지나 문제 번호를 확인할 수 없으면 그 주제는 **아예 넣지 마세요.**
③ **범위 규칙** — 강의록에 없는 내용만 묻는 기출 문제는 무시하세요.
④ 같은 주제를 여러 기출·여러 문항에서 물었다면 **한 항목에 모아** 넣으세요. (주제 중복 금지)
⑤ 주제는 "골학 전체"처럼 넓게 잡지 말고, **한 문제로 물을 수 있는 단위**로 잡으세요.

⑥ **강의록발췌** — 그 주제가 적힌 강의록 페이지의 내용을 강의록 문장·표현 그대로
   1~3문장으로 옮기거나 간추리세요. 강의록에 없는 설명을 새로 지어내지 마세요.
   페이지 내용을 확인할 수 없으면 빈 문자열로 두세요.
   발췌가 제목 한 줄이거나 "제목 / 쪽번호" 나열뿐이라면 그 페이지는 목차입니다 —
   근거로 쓰지 말고 그 주제를 설명하는 페이지를 다시 찾으세요.
⑦ **기출 문항 상세** — 각 문항마다 다음을 함께 적으세요.
   - "페이지": 그 문항이 적힌 기출 자료의 [페이지 N] 번호. 확인 안 되면 빈 문자열.
   - "원문": 기출 텍스트에 있는 그 문제의 지문·질문을 **요약·의역하지 말고 원문 그대로**
     옮기세요(선택지는 생략 가능). 어떤 문제인지 알아볼 수 있을 만큼만 옮기고,
     120자가 넘으면 거기서 자르고 "…"으로 표시하세요.
     그 문항이 `[페이지 N 이미지 설명]`에만 있어 인쇄된 문장을 옮길 수 없으면,
     설명을 옮기되 **맨 앞에 "(이미지 설명) "을 붙이세요** — 시험지 문장이 아니라는
     표시입니다. `(필기: …)` 부분은 어느 경우에도 "원문"에 넣지 마세요.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## [5] 출력 형식 — 아래 JSON만 (코드블록·설명 문장 금지)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{{
  "주제목록": [
    {{
      "주제": "강의록에 적힌 표현 그대로",
      "강의록": [{{"자료": "강의록1", "페이지": [12, 13]}}],
      "강의록발췌": "12페이지 내용을 강의록 표현 그대로 1~3문장으로",
      "기출": [{{"자료": "기출1", "문항": [
        {{"번호": "4", "페이지": "3", "원문": "문제 지문 원문 그대로(최대 120자)"}}
      ]}}],
      "출제형태": "기출에서 이 주제를 무엇으로 물었는지 한 문장 (강의록 용어로만)"
    }}
  ]
}}
- "자료"에는 위에 표시된 **라벨**(강의록1, 기출1 …)만 쓰세요. 파일명을 쓰지 마세요.
- "강의록.페이지"는 숫자만 담으세요.
- "기출.문항"의 각 원소는 반드시 {{"번호","페이지","원문"}} 객체로 쓰세요 (문자열만 쓰지 마세요).
- 출제가 확인된 주제는 **빠짐없이** 넣으세요. (개수 제한 없음)"""


def file_digest(pdf) -> str:
    """
    업로드된 PDF 한 개의 내용 해시.

    경로(spill_upload가 내려둔 사본)면 1MB씩 읽는다 — 파일 크기와 무관하게 일정 메모리로
    끝난다. bytes·파일 객체도 받는데(테스트·옛 호출부), 파일 객체는 읽은 뒤 되감아
    뒤에서 다시 읽는 쪽이 빈 파일을 보지 않게 한다.
    """
    h = hashlib.sha256()
    if isinstance(pdf, (str, os.PathLike)):
        with open(pdf, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    if hasattr(pdf, "read"):
        h.update(pdf.read() or b"")
        try:
            pdf.seek(0)
        except Exception:
            pass
        return h.hexdigest()
    h.update(pdf or b"")
    return h.hexdigest()


def topic_input_key(lecture_files, exam_files, provider_name: str, model: str,
                    image_model: str) -> str:
    """
    "같은 자료로 같은 분석을 또 하려는 것"을 알아보는 키.

    같은 키의 분석이 보관돼 있으면 LLM을 한 번도 부르지 않고 그 결과를 그대로 쓸 수 있다.
    이 탭은 요금의 대부분이 주제 대조 호출 하나(입력 14만 토큰)에서 나오므로, 같은
    자료를 다시 올렸을 때 그 한 번을 건너뛰는 것이 곧 절감이다.

    무엇을 넣는가:
      ① 파일은 **내용**으로 잡는다 — 이름만 바꿔 올린 같은 파일은 같은 키다. 이름도 함께
         넣는 이유는 결과 화면·보관함에 파일명이 그대로 나오기 때문이다. 업로드 순서를
         유지한다 — 순서가 라벨(강의록1·강의록2)과 표시 순서를 정한다.
      ② 추출한 텍스트가 아니라 **원본 파일**을 해싱한다. 그림 설명은 회차마다 미세하게
         흔들려서(실측 topic_analyses 25→26: 같은 기출인데 3,418자 차이) 추출 결과를
         해싱하면 재실행마다 키가 어긋나 캐시가 사실상 안 맞는다.
      ③ 프롬프트는 빈 블록으로 한 번 만들어 그 **템플릿**을 해싱한다 — 프롬프트를 고치면
         키가 저절로 바뀐다. 버전 번호를 손으로 올리는 방식은 언젠가 까먹는다.
      ④ 결과를 바꾸는 상수(글자 예산·이미지 상한·출력 한도)도 넣는다. 안 넣으면 상한을
         올린 뒤에도 그 전에 잘려 나온 결과가 그대로 재사용된다.

    ※ 여기서 잡는 범위는 'LLM에 무엇을 보내는가'까지다. 응답을 해석하는 규칙
      (parse_topic_analysis)을 고쳐도 키는 그대로다 — 보관된 행은 그때의 규칙으로 만든
      결과이고, 보관함이 이미 그렇게 동작한다(옛 회차를 다시 파싱하지 않는다).
    """
    h = hashlib.sha256()

    def feed(*parts):
        for p in parts:
            h.update(str(p).encode("utf-8"))
            h.update(b"\x00")

    feed(build_topic_analysis_prompt("", ""),
         provider_name, model, image_model,
         TOPIC_SIDE_CHAR_BUDGET, TOPIC_TOTAL_CHAR_BUDGET,
         TOPIC_DOC_MIN_CHARS, TOPIC_MAX_TOKENS,
         sorted(IMAGE_DESCRIBE.items()), sorted(IMAGE_TRANSCRIBE.items()))

    for side, files in (("강의록", lecture_files), ("기출", exam_files)):
        feed(side)
        for name, pdf in files or []:
            feed(name or "", file_digest(pdf))
    return h.hexdigest()


def _resolve_doc_name(value: str, docs: list) -> str:
    """LLM이 준 '자료' 값을 실제 파일명으로 복원. 라벨 → 파일명, 파일명이면 그대로."""
    key = str(value or "").strip()
    if not key:
        return docs[0]["name"] if len(docs) == 1 else ""
    for d in docs:
        if key == d["label"] or key == d["name"]:
            return d["name"]
    # 라벨/파일명 어느 쪽과도 안 맞으면 부분 일치까지 시도 (확장자 누락 등)
    for d in docs:
        if key in d["name"] or d["name"] in key or key in d["label"]:
            return d["name"]
    return key


def _page_sort_key(v: str):
    """'12' → 12, '12-13' → 12, 숫자가 없으면 맨 뒤로."""
    m = re.search(r"\d+", str(v))
    return (0, int(m.group())) if m else (1, 0)


def _normalize_refs(raw_refs, docs: list, item_key: str) -> list:
    """
    [{"자료": 라벨, item_key: [...]}] 형태를 파일명 기준으로 정규화·중복 제거·정렬.
    같은 파일이 여러 번 나오면 하나로 합친다.
    """
    merged = {}
    order = []
    for ref in (raw_refs or []):
        if not isinstance(ref, dict):
            continue
        name = _resolve_doc_name(ref.get("자료") or ref.get("파일") or "", docs)
        if not name:
            continue
        values = ref.get(item_key)
        if isinstance(values, (str, int, float)):
            values = [values]
        if not isinstance(values, list):
            continue
        if name not in merged:
            merged[name] = []
            order.append(name)
        for v in values:
            s = str(v).strip()
            # '12p', '4번' 처럼 단위가 붙어 오는 경우가 있어 숫자/범위만 남긴다
            s = re.sub(r"^(p|page|페이지|문제|문항)\s*", "", s, flags=re.I)
            s = re.sub(r"\s*(p|페이지|쪽|번|번째)$", "", s, flags=re.I).strip()
            if s and s not in merged[name]:
                merged[name].append(s)

    out = []
    for name in order:
        items = sorted(merged[name], key=_page_sort_key)
        if items:
            out.append({"자료": name, item_key: items})
    return out


def _clip_text(s: str, limit: int) -> str:
    s = str(s or "").strip()
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _normalize_exam_refs(raw_refs, exam_docs: list) -> list:
    """
    [{"자료": 라벨, "문항": [{"번호","페이지","원문"} …]}] 를 파일명 기준으로 정규화.
    문항은 (자료, 번호) 기준으로 중복 제거하고(같은 문항이 여러 번 오면 원문이 더 긴 쪽을 채택),
    페이지 → 번호 순으로 정렬한다.
    """
    merged = {}   # name -> {번호: {번호,페이지,원문}}
    order = []
    for ref in (raw_refs or []):
        if not isinstance(ref, dict):
            continue
        name = _resolve_doc_name(ref.get("자료") or ref.get("파일") or "", exam_docs)
        if not name:
            continue
        raw_items = ref.get("문항")
        if isinstance(raw_items, (str, int, float)):
            raw_items = [raw_items]
        if not isinstance(raw_items, list):
            continue
        if name not in merged:
            merged[name] = {}
            order.append(name)
        for it in raw_items:
            # 모델이 규칙을 어기고 문자열/숫자만 줘도 최소한 번호는 살린다
            if isinstance(it, dict):
                num = str(it.get("번호") or it.get("문항") or "").strip()
                page = str(it.get("페이지") or "").strip()
                # 출력 토큰은 입력의 5배 단가라 인용 길이가 그대로 비용이다
                text = _clip_text(it.get("원문") or "", 120)
            else:
                num, page, text = str(it).strip(), "", ""
            num = re.sub(r"^(문제|문항)\s*", "", num, flags=re.I)
            num = re.sub(r"\s*(번|번째)$", "", num, flags=re.I).strip()
            page = re.sub(r"^(p|page|페이지)\s*", "", page, flags=re.I)
            page = re.sub(r"\s*(p|페이지|쪽)$", "", page, flags=re.I).strip()
            if not num:
                continue
            existing = merged[name].get(num)
            if existing is None or (text and len(text) > len(existing.get("원문", ""))):
                merged[name][num] = {"번호": num, "페이지": page or (existing or {}).get("페이지", ""),
                                     "원문": text or (existing or {}).get("원문", "")}

    out = []
    for name in order:
        items = sorted(merged[name].values(), key=lambda it: (_page_sort_key(it["페이지"]), _page_sort_key(it["번호"])))
        if items:
            out.append({"자료": name, "문항": items})
    return out


def _topic_lecture_sort_key(t: dict, lec_order: dict):
    """
    주제 정렬 키 — 강의록 파일 순서(업로드 순) 먼저, 그 안에서는 그 파일에 표시된
    페이지 중 가장 앞선 번호 순. 여러 강의록 파일에 걸친 주제는 순서가 가장
    앞선 파일을 기준으로 삼는다. 강의록 근거가 아예 없으면 맨 뒤로 보낸다.
    """
    lec_refs = t.get("강의록") or []
    if not lec_refs:
        return (len(lec_order) + 1, (1, 0))
    best_idx, best_page = None, (1, 0)
    for r in lec_refs:
        idx = lec_order.get(r["자료"], len(lec_order))
        pages = [_page_sort_key(p) for p in (r.get("페이지") or [])]
        first_page = min(pages) if pages else (1, 0)
        if best_idx is None or idx < best_idx:
            best_idx, best_page = idx, first_page
    return (best_idx, best_page)


def parse_topic_analysis(raw: str, lecture_docs: list, exam_docs: list) -> dict:
    """
    LLM JSON 응답 → 화면에 바로 그릴 수 있는 주제 목록으로 정규화.

    - 자료 라벨을 실제 파일명으로 복원
    - 같은 주제(표기 흔들림 포함)는 근거를 합쳐 하나로
    - 기출 근거(문항 번호)가 없는 항목은 '기출에 나온 주제'가 아니므로 제외하고 개수만 남김
    - 정렬: 강의록 파일 순서(업로드 순) → 그 파일 안에서 페이지 오름차순 (강의록 미확인은 맨 뒤)
    """
    data = safe_parse_json(raw)
    items = data.get("주제목록") if isinstance(data, dict) else None
    if not isinstance(items, list):
        items = []

    topics = []
    by_key = {}
    dropped = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("주제") or "").strip()
        if not name:
            continue
        exam_refs = _normalize_exam_refs(item.get("기출"), exam_docs)
        if not exam_refs:          # 기출 근거 없음 → 이 기능의 대상이 아님
            dropped += 1
            continue
        lecture_refs = _normalize_refs(item.get("강의록"), lecture_docs, "페이지")
        excerpt = _clip_text(item.get("강의록발췌") or "", 400)

        # 표기 흔들림을 흡수한 키로 중복 판정. 괄호로 시작하는 이름 등은 키가 비므로 원문을 쓴다.
        key = _norm_term(name) or name
        if key in by_key:          # 같은 주제 중복 → 근거 병합
            merge_into = by_key[key]
            merge_into["강의록"] = _normalize_refs(
                merge_into["강의록"] + lecture_refs, lecture_docs, "페이지")
            merge_into["기출"] = _normalize_exam_refs(
                merge_into["기출"] + exam_refs, exam_docs)
            if excerpt and excerpt not in merge_into["강의록발췌"]:
                merge_into["강의록발췌"] = (
                    f"{merge_into['강의록발췌']} / {excerpt}" if merge_into["강의록발췌"] else excerpt
                )
            continue

        topic = {
            "주제": name,
            "강의록": lecture_refs,
            "강의록발췌": excerpt,
            "기출": exam_refs,
            "출제형태": str(item.get("출제형태") or "").strip(),
        }
        by_key[key] = topic
        topics.append(topic)

    # 병합 후 문항 수 재계산
    for t in topics:
        t["문항수"] = sum(len(r["문항"]) for r in t["기출"])

    # 정렬: 강의록 파일 순서(업로드·라벨 순) → 그 파일 안에서 페이지 오름차순.
    # 강의록 근거가 없는 주제는 맨 뒤로 보낸다.
    lec_order = {d["name"]: i for i, d in enumerate(lecture_docs)}
    for t in topics:
        t["_sort"] = _topic_lecture_sort_key(t, lec_order)
    topics.sort(key=lambda t: t["_sort"] + (t["주제"],))
    for t in topics:
        t.pop("_sort", None)

    return {
        "topics": topics,
        "dropped": dropped,          # 근거가 없어 버린 항목 수 (조용히 삭제하지 않고 표시)
        "total_questions": sum(t["문항수"] for t in topics),
    }


def run_topic_analysis(lecture_docs: list, exam_docs: list, api_key: str,
                       model: str, provider=None, usage=None) -> dict:
    """추출된 강의록·기출 문서 목록 → 주제 대응표 (LLM 1회 호출)."""
    prompt = build_topic_analysis_prompt(
        build_topic_doc_block(lecture_docs),
        build_topic_doc_block(exam_docs),
    )
    raw = call_llm(prompt, api_key, model, provider=provider,
                   max_tokens=TOPIC_MAX_TOKENS, usage=usage)
    result = parse_topic_analysis(raw, lecture_docs, exam_docs)
    result["raw"] = raw
    return result
