"""
깨진 한글 텍스트 레이어를 잡아내고 되살리는지 확인한다.

실제 사고 — 기출 주제 분석 결과에 "C6넍carotid tubercle냹閵ꌩ멙鱉ꩡ덹" 같은 문구가
그대로 실려 나왔다. 인코딩 문제가 아니라 PDF 텍스트 레이어다. ToUnicode CMap이 없는
한글 서브셋 폰트를 만나면 MuPDF가 글리프 번호를 유니코드 값인 양 내보내서, 한글 전체가
**일정한 오프셋만큼 밀린** 다른 문자로 나온다 (이번 건은 -5707).

이게 화면까지 새어 나온 이유는 글자 '수'가 멀쩡하기 때문이다. read_pdf_pages는
"텍스트가 20자 미만이거나 큰 그림이 있을 때"만 이미지로 넘기므로, 깨진 쪽은 잘 뽑힌
페이지로 판정돼 쓰레기가 그대로 프롬프트에 실린다. 그 뒤로는 SSE·DB·화면이 전부
무손실이라 아무도 못 걸러낸다.

여기서 고정하는 것:

  ① 감지    — 깨진 쪽을 잡고, 멀쩡한 쪽은 절대 건드리지 않는다 (오탐이 더 비싸다)
  ② 되살리기 — 오프셋을 역산해 LLM 호출 없이 원문을 되돌린다
  ③ 안전장치 — **틀린 오프셋으로 되살리느니 포기한다.** 쓰레기 글자는 눈에 띄지만
               잘못 되살린 글은 아무도 못 알아챈다
  ④ 배선    — read_pdf_pages가 되살리거나(이미지 호출 0회), 못 되살리면 텍스트를
               버리고 이미지로 넘긴다

LLM 호출 없이 돈다. API 키도 요금도 필요 없다:

    python tests/test_broken_text.py
"""

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# 한글 윈도우 기본 코드페이지(cp949)로는 이 파일이 찍는 문자를 쓸 수 없다
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import fitz

import llm

_failures = []


def check(name, cond, extra=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  → {extra}" if not cond else ""))
    if not cond:
        _failures.append(name)


# 실제 사고 문장을 포함한 기출 한 쪽 분량. 되살리기는 글자가 많을수록 또렷해지므로
# 한 줄짜리로 재면 안 된다 — 짧은 표본은 ③에서 따로 다룬다.
PAGE = ("{n}. 다음 사진에서 화살표가 가리키는 구조물의 이름을 쓰시오.\n"
        "C6의 carotid tubercle을 가리키는 사진(C6이라고 표시되어 있었습니다)\n"
        "{m}. 위 구조물이 임상적으로 중요한 이유를 두 가지 서술하시오.\n"
        "{k}. 경동맥 압박점으로 이용되는 해부학적 근거를 설명하시오.\n"
        "갑상선기능항진증 환자에서 맥박이 빨라지고 체중이 감소한다.\n")

CLEAN_PAGES = [PAGE.format(n=i * 3 + 1, m=i * 3 + 2, k=i * 3 + 3) for i in range(4)]


def bend(text, offset):
    """고장을 흉내낸다 — 한글 음절만 밀린다 (ASCII·괄호는 다른 폰트라 멀쩡했다)."""
    return "".join(chr(ord(c) - offset)
                   if llm.HANGUL_FIRST <= ord(c) <= llm.HANGUL_LAST else c
                   for c in text)


def scramble(text, seed=7):
    """오프셋이 아닌 고장 — CMap이 통째로 뒤섞인 경우. 되살리기가 포기해야 한다."""
    rnd, table, out = random.Random(seed), {}, []
    for c in text:
        if llm.HANGUL_FIRST <= ord(c) <= llm.HANGUL_LAST:
            table.setdefault(c, chr(rnd.randrange(llm.HANGUL_FIRST, llm.HANGUL_LAST + 1)))
            out.append(table[c])
        else:
            out.append(c)
    return "".join(out)


def make_pdf(pages_text):
    """텍스트 레이어만 있는 PDF.

    korea-s(Adobe-Korea1)를 쓰는 이유는 폰트 파일 없이 한글이 왕복하기 때문이다.
    기본 helv는 한글 자리에 점만 남고, china-s/japan-s는 아예 떨어뜨린다.
    """
    doc = fitz.open()
    for body in pages_text:
        page, y = doc.new_page(), 72
        for line in body.splitlines():
            page.insert_text((50, y), line, fontname="korea-s", fontsize=10)
            y += 20
    data = doc.tobytes()
    doc.close()
    return data


def pdf_text(data):
    """PDF에서 MuPDF가 실제로 뽑아내는 글자 (쪽 이어 붙임)."""
    doc = fitz.open(stream=data, filetype="pdf")
    out = [doc[i].get_text() for i in range(doc.page_count)]
    doc.close()
    return out


def test_detect():
    """① 깨진 쪽을 잡되, 멀쩡한 쪽은 건드리지 않는다.

    오탐이 미탐보다 비싸다 — 멀쩡한 쪽을 깨진 것으로 보면 글자를 버리고 이미지로
    넘기므로 요금이 나가고 이미지 예산까지 잡아먹는다. 그래서 '한국어처럼 보이지만
    통계가 튀는' 쪽들을 오탐 표본으로 박아둔다.
    """
    print("\n[① 감지]")
    body = "".join(CLEAN_PAGES)

    # 오프셋이 작으면 밀린 글자가 한글 구간에 그대로 떨어져 '비한글 비율'로는 안 잡힌다
    # (KS적중이 대신 무너진다). 크면 한자 구간으로 넘어가 셀 음절이 없어져 그 반대가
    # 된다. 두 신호가 서로의 사각을 메우는지 보려면 양쪽 끝을 다 넣어야 한다.
    for offset in (28, 56, 84, 112, 256, 1280, 4096, 5707, 8192, 12288, 16384):
        check(f"-{offset} 로 밀린 쪽을 잡는다", llm.korean_text_looks_broken(bend(body, offset)))
    check("임의 치환된 쪽을 잡는다", llm.korean_text_looks_broken(scramble(body)))

    check("멀쩡한 기출 쪽은 통과", not llm.korean_text_looks_broken(body))
    check("영문 전용 쪽은 통과",
          not llm.korean_text_looks_broken("The carotid tubercle of C6. " * 20))
    check("빈 쪽은 통과", not llm.korean_text_looks_broken(""))
    # ①②③④⑤ 는 카테고리가 '숫자'라 한글 판정에서 빠져야 한다. 안 빼면 5지선다가
    # 빽빽한 쪽이 통째로 '한글 아닌 문자'로 잡힌다.
    check("5지선다 ①②③④⑤ 가 빽빽한 쪽은 통과",
          not llm.korean_text_looks_broken(
              "다음 중 옳은 것은? ① 상완골 ② 요골 ③ 척골 ④ 견갑골 ⑤ 쇄골\n" * 12))
    # 뼈 이름만 나열된 쪽은 받침이 몰려 있다. 받침 분포를 신호로 쓰면 여기서 걸린다.
    check("뼈 이름만 나열된 쪽은 통과",
          not llm.korean_text_looks_broken(
              "상완골 견갑골 쇄골 척골 요골 대퇴골 경골 비골 슬개골 늑골\n" * 10))
    check("한자 병기가 빽빽한 쪽은 통과",
          not llm.korean_text_looks_broken(
              "경추(頸椎) 6번의 전결절(前結節)은 총경동맥(總頸動脈)을 압박한다.\n" * 8))
    check("기호·단위가 많은 쪽은 통과",
          not llm.korean_text_looks_broken(
              "α-세포와 β-세포의 비율은 20±5%이며 37℃에서 측정한다. “정상”은 ≥ 4.0\n" * 8))


def test_repair():
    """② 오프셋을 역산해 원문을 그대로 되돌린다 (LLM 호출 없음)."""
    print("\n[② 되살리기]")
    body = "".join(CLEAN_PAGES)

    for offset in (28, 56, 84, 112, 256, 1280, 4096, 5707, 8192, 12288, 16384):
        broken = bend(body, offset)
        found = llm.find_hangul_offset(broken)
        check(f"-{offset} 을 찾아 원문까지 되돌린다",
              found == offset and llm.repair_hangul(broken, found) == body,
              f"찾은 값 {found}")

    # 감지가 헛짚어도 안전하다 — 지금 글자가 이미 1등이면 0이 나와 손대지 않는다.
    check("멀쩡한 한국어에는 0 (손대지 않음)", llm.find_hangul_offset(body) == 0)
    check("0 으로 되돌리면 글자 하나 안 바뀐다", llm.repair_hangul(body, 0) == body)


def test_repair_refuses():
    """③ 애매하면 포기한다 — 이게 이 기능에서 제일 중요한 성질이다.

    틀린 오프셋으로 되살리면 '그럴듯한 딴 글'이 된다. 실제 사고 문장 한 줄만 놓고 보면
    5707과 5763이 **정확히 동점**이라, 여유를 안 따지면 "C6의…"가 "C6자…"로 조용히
    바뀐다. 쓰레기 글자는 눈에 띄지만 잘못 되살린 글은 아무도 못 알아챈다.
    """
    print("\n[③ 안전장치 — 애매하면 포기]")
    sample = "C6넍carotid tubercle냹閵ꌩ멙鱉ꩡ덹(C6넩ꄱ隕 븑겑鷍꽩 넽꽽걪鱽鲙)"

    check("실제 사고 문장 한 줄은 표본이 모자라 포기",
          llm.find_hangul_offset(sample) is None)
    check("같은 문장을 되풀이해도 (2등과 동점) 포기",
          llm.find_hangul_offset(sample * 4) is None)
    check("일부만 깨진 쪽(폰트 섞임)은 포기",
          llm.find_hangul_offset(bend("".join(CLEAN_PAGES), 5707)
                                 + "".join(CLEAN_PAGES)) is None)
    check("오프셋이 아닌 임의 치환은 포기",
          llm.find_hangul_offset(scramble("".join(CLEAN_PAGES))) is None)
    check("영문만 있는 쪽은 포기",
          llm.find_hangul_offset("The quick brown fox jumps over the lazy dog. " * 30)
          is None)

    # 무작위 대량 시험 — 여유(REPAIR_MIN_MARGIN)를 0으로 되돌리면 여기서 터진다.
    # 임계값을 낮추고 싶어질 때 '구조율은 오르지만 오답이 섞인다'를 눈으로 보라고 남긴다.
    rnd = random.Random(20260809)
    pool = "".join(CLEAN_PAGES) * 8
    rescued = wrong = refused = 0
    for _ in range(200):
        size = rnd.choice([100, 200, 400, 800, 1600])
        start = rnd.randrange(0, max(1, len(pool) - size))
        src = pool[start:start + size]
        offset = rnd.choice([28, 56, 84, 112, 256, 1280, 4096, 5707, 8192,
                             rnd.randrange(1, 16000)])
        broken = bend(src, offset)
        found = llm.find_hangul_offset(broken)
        if found is None:
            refused += 1
        elif found == offset and llm.repair_hangul(broken, found) == src:
            rescued += 1
        else:
            wrong += 1
    check(f"무작위 200건에 틀린 되살리기가 없다 (구조 {rescued} · 거부 {refused})",
          wrong == 0, f"오답 {wrong}건")


def test_read_pdf_pages():
    """④ read_pdf_pages 배선 — 되살리거나, 못 되살리면 텍스트를 버리고 이미지로."""
    print("\n[④ read_pdf_pages 배선]")

    # ── 되살릴 수 있는 깨짐: 이미지 호출 0회 ──
    data = make_pdf([bend(t, 5707) for t in CLEAN_PAGES])
    check("시뮬레이션이 실제로 깨진 PDF를 만든다",
          llm.korean_text_looks_broken(pdf_text(data)[0]))

    pages, jobs = llm.read_pdf_pages(data, "key", llm.IMAGE_DESCRIBE)
    joined = "\n".join(p["text"] for p in pages)
    check("되살렸으므로 이미지 대상이 0쪽 (= Vision 값이 안 나간다)",
          len(jobs) == 0, f"{len(jobs)}쪽")
    check("실제 사고 문구가 정상 한글로 돌아왔다", "carotid tubercle을" in joined)
    check("쓰레기 글자가 남지 않았다", "넍" not in joined and "鱉" not in joined)
    check("되살린 글에는 깨짐 판정이 안 걸린다", not llm.korean_text_looks_broken(joined))
    # 원문 전체와 비교하지 않는 이유: korea-s 에는 밀린 코드포인트(이 문자·일부 한자)의
    # 글리프가 없어 PDF를 만들 때 그 글자가 통째로 빠진다. 시뮬레이션의 한계이지
    # 되살리기의 한계가 아니라서(②에서 문자열로 완전 일치를 재고 있다), 여기서는
    # "PDF가 실제로 담고 있는 것"을 기준으로 정확도를 본다.
    expected = [llm.repair_hangul(t, 5707) for t in pdf_text(data)]
    check("PDF가 담은 글자를 한 자도 빠짐없이 되돌렸다",
          [p["text"] for p in pages] == expected)

    # ── 못 되살리는 깨짐: 글자를 버리고 이미지로 ──
    scrambled = make_pdf([scramble(t) for t in CLEAN_PAGES])
    pages, jobs = llm.read_pdf_pages(scrambled, "key", llm.IMAGE_DESCRIBE)
    check("되살리기 실패 → 4쪽 모두 이미지 대상", len(jobs) == 4, f"{len(jobs)}쪽")
    # 안 버리면 Vision이 옮겨 적은 글과 나란히 프롬프트에 실려, LLM이 쓰레기 쪽도
    # 원문으로 믿고 주제·문항을 뽑는다. 이 검사가 이 기능의 핵심이다.
    check("깨진 텍스트를 전부 버렸다", all(p["text"] == "" for p in pages))

    # 깨진 쪽은 '글자를 읽어야' 하므로 기출 기본값(110)이 아니라 전사용(150)으로 굽는다.
    wide = fitz.Pixmap(jobs[0]["png_path"]).width
    doc = fitz.open(stream=scrambled, filetype="pdf")
    want = doc[0].get_pixmap(dpi=llm.TRANSCRIBE_RENDER_DPI).width
    plain = doc[0].get_pixmap(dpi=llm.DESCRIBE_RENDER_DPI).width
    doc.close()
    check(f"전사용 해상도로 구웠다 ({plain}px 아니라 {want}px)", wide == want, f"{wide}px")
    llm.discard_spills(pages)

    # ── 이미지 예산이 모자라면 기존 경고 경로를 그대로 탄다 ──
    pages, jobs = llm.read_pdf_pages(scrambled, "key", llm.IMAGE_DESCRIBE, max_images=2)
    cov = llm.image_coverage(pages, jobs)
    check("상한에 걸린 쪽이 기존 경고로 남는다",
          cov["candidates"] == 4 and cov["processed"] == 2
          and cov["skipped_pages"] == [3, 4], str(cov))
    llm.discard_spills(pages)

    # ── Vision을 못 쓰는 경우: 그래도 쓰레기는 안 내보낸다 ──
    pages, jobs = llm.read_pdf_pages(scrambled, None, None)
    check("키가 없어도 쓰레기 대신 빈 텍스트",
          not jobs and all(p["text"] == "" for p in pages))

    # ── 회귀: 멀쩡한 PDF는 하나도 달라지지 않는다 ──
    pages, jobs = llm.read_pdf_pages(make_pdf(CLEAN_PAGES), "key", llm.IMAGE_DESCRIBE)
    joined = "\n".join(p["text"] for p in pages)
    check("멀쩡한 PDF는 이미지 대상 0쪽", len(jobs) == 0, f"{len(jobs)}쪽")
    check("멀쩡한 PDF의 본문은 그대로",
          "carotid tubercle을 가리키는 사진" in joined
          and all(p["text"].strip() for p in pages))


if __name__ == "__main__":
    print("깨진 한글 텍스트 레이어")
    test_detect()
    test_repair()
    test_repair_refuses()
    test_read_pdf_pages()

    print()
    if _failures:
        print(f"실패 {len(_failures)}건: " + ", ".join(_failures))
        sys.exit(1)
    print("전부 통과")
