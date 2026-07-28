"""
골학 문제은행 OCR (관리자용 일괄 처리 라이브러리).

사용자는 이미지를 올리지 않는다. 팀이 static/images/bones/ 폴더에
'부위 이름이 적힌 이미지'를 넣어두면, 이 모듈이 그 이미지를 읽어:
  1) Vision LLM으로 각 라벨의 '텍스트 + 위치'를 인식하고
  2) 라벨 글자 위에 '번호'를 덮어씌운 이미지(SVG)를 생성하고
  3) static/data/bone_bank.json 에 문제 항목(번호→이름)을 추가한다.

실행은 웹 UI가 아니라 커맨드라인 스크립트로 한다 →  build_bone_bank.py 참고.
의존성: 기존 openai 게이트웨이(Vision) + pymupdf(fitz). 추가 설치 없음.
좌표 규약: LLM에게 0~1000 정규화 [x1,y1,x2,y2] (좌상단 원점)로 라벨 '글자' 박스를 받는다.
"""

import os
import json
import base64

import fitz  # pymupdf

from llm import GATEWAY_BASE_URL, DEFAULT_MODEL, safe_parse_json
from openai import OpenAI

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IMAGES_DIR = os.path.join(_BASE_DIR, "static", "images", "bones")
DATA_JSON = os.path.join(_BASE_DIR, "static", "data", "bone_bank.json")

MAX_LABELS = 40  # 한 이미지에서 인식할 라벨 상한
# 원본(소스) 이미지로 인정하는 확장자. 생성물(_numbered.svg)·SVG는 제외.
SOURCE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


# ──────────────────────────────────────────────
# Vision OCR (라벨 텍스트 + 박스 + 한글명)
# ──────────────────────────────────────────────

def ocr_labels(png_bytes: bytes, api_key: str, model: str) -> list:
    """이미지 속 모든 텍스트 라벨 → [{name, box[0-1000], ko[]}]. 게이트웨이 미지원 시 예외."""
    b64 = base64.b64encode(png_bytes).decode()
    client = OpenAI(api_key=api_key, base_url=GATEWAY_BASE_URL)
    prompt = (
        "이 이미지는 해부학 그림이며, 각 부위를 가리키는 '텍스트 라벨'이 붙어 있습니다.\n"
        "이미지에 보이는 모든 텍스트 라벨을 빠짐없이 찾아, 아래 JSON 형식으로만 반환하세요 "
        "(코드블록·설명 없이 JSON만).\n\n"
        '{"labels": [\n'
        '  {"name": "라벨 텍스트 원문 그대로", '
        '"box": [x1, y1, x2, y2], '
        '"ko": ["해당 부위의 한국어 해부학 명칭", "동의어"]},\n'
        '  ...\n'
        ']}\n\n'
        "규칙:\n"
        "- name: 이미지에 쓰인 글자를 원문 그대로 (영어면 영어 그대로).\n"
        "- box: 그 '라벨 글자'만 감싸는 사각형. 지시선(선)은 포함하지 말 것. "
        "좌표는 이미지 가로/세로를 각각 0~1000으로 정규화한 정수, 좌상단이 (0,0), "
        "[왼쪽 x, 위 y, 오른쪽 x, 아래 y] 순서.\n"
        "- ko: name이 가리키는 부위의 한국어 명칭(들). 확실치 않으면 빈 배열 [].\n"
        "- 여러 줄에 걸친 라벨(예: 두 줄로 쓰인 이름)은 하나로 합쳐 한 항목으로.\n"
        f"- 라벨이 아주 많으면 대표적인 것부터 최대 {MAX_LABELS}개까지만."
    )
    resp = client.chat.completions.create(
        model=model,
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ],
        }],
    )
    raw = (resp.choices[0].message.content or "").strip()
    data = safe_parse_json(raw)
    labels = data.get("labels") if isinstance(data, dict) else None
    return labels if isinstance(labels, list) else []


# ──────────────────────────────────────────────
# 라벨 정규화 + 번호 부여
# ──────────────────────────────────────────────

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def normalize_labels(labels: list) -> list:
    """유효 박스만, 0~1000 보정, 읽기순(위→아래, 좌→우) 정렬 후 1번부터 번호 부여."""
    cleaned = []
    for lab in labels:
        if not isinstance(lab, dict):
            continue
        name = str(lab.get("name") or "").strip()
        box = lab.get("box")
        if not name or not isinstance(box, (list, tuple)) or len(box) != 4:
            continue
        try:
            x1, y1, x2, y2 = [float(v) for v in box]
        except (TypeError, ValueError):
            continue
        if max(x1, y1, x2, y2) <= 1.5:               # 0~1 소수 → 0~1000
            x1, y1, x2, y2 = x1 * 1000, y1 * 1000, x2 * 1000, y2 * 1000
        x1, x2 = sorted((x1, x2))
        y1, y2 = sorted((y1, y2))
        x1, y1, x2, y2 = (_clamp(x1, 0, 1000), _clamp(y1, 0, 1000),
                          _clamp(x2, 0, 1000), _clamp(y2, 0, 1000))
        if x2 - x1 < 3 or y2 - y1 < 3:
            continue
        ko = lab.get("ko")
        ko = [str(k).strip() for k in ko if str(k).strip()] if isinstance(ko, list) else []
        cleaned.append({"name": name, "ko": ko, "box": [x1, y1, x2, y2]})

    cleaned.sort(key=lambda l: (round(l["box"][1] / 40), l["box"][0]))
    cleaned = cleaned[:MAX_LABELS]
    for i, lab in enumerate(cleaned, start=1):
        lab["num"] = i
    return cleaned


def labels_to_parts(labels: list) -> list:
    """번호 라벨 → parts [{num, answers[]}]. 원문+한글명을 정답으로 (중복 제거)."""
    parts = []
    for lab in labels:
        answers, seen = [], set()
        for a in [lab["name"]] + lab.get("ko", []):
            key = a.lower().replace(" ", "")
            if a and key not in seen:
                seen.add(key)
                answers.append(a)
        parts.append({"num": lab["num"], "answers": answers})
    return parts


# ──────────────────────────────────────────────
# 번호 이미지(SVG) 생성 — 원본 위에 '라벨 가림 + 번호' 오버레이
# ──────────────────────────────────────────────

def build_numbered_svg(png_bytes: bytes, labels: list, w: int, h: int) -> str:
    """원본 PNG를 base64로 내장(<img> 로드에서도 렌더) + 각 라벨 자리에 흰 사각형+번호."""
    b64 = base64.b64encode(png_bytes).decode()
    badges = []
    for lab in labels:
        x1, y1, x2, y2 = lab["box"]
        px1, py1 = x1 / 1000 * w, y1 / 1000 * h
        px2, py2 = x2 / 1000 * w, y2 / 1000 * h
        pad = max(2.0, (py2 - py1) * 0.15)
        rx1, ry1 = px1 - pad, py1 - pad
        rw, rh = (px2 - px1) + pad * 2, (py2 - py1) + pad * 2
        cx, cy = px1 + (px2 - px1) / 2, py1 + (py2 - py1) / 2
        fs = _clamp(rh * 0.85, 13, 34)
        badges.append(
            f'<rect x="{rx1:.1f}" y="{ry1:.1f}" width="{rw:.1f}" height="{rh:.1f}" '
            f'rx="5" fill="#ffffff" stroke="#2563eb" stroke-width="1.5"/>'
            f'<text x="{cx:.1f}" y="{cy + fs * 0.35:.1f}" text-anchor="middle" '
            f'font-size="{fs:.1f}" font-weight="700" fill="#2563eb">{lab["num"]}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'font-family="\'Malgun Gothic\',\'Apple SD Gothic Neo\',sans-serif">'
        f'<image href="data:image/png;base64,{b64}" x="0" y="0" width="{w}" height="{h}"/>'
        f'<g>{"".join(badges)}</g>'
        f'</svg>'
    )


# ──────────────────────────────────────────────
# bone_bank.json 읽기/쓰기
# ──────────────────────────────────────────────

def load_bank() -> dict:
    try:
        with open(DATA_JSON, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("questions", [])
    return data


def append_to_bank(entry: dict):
    """문제 항목을 bone_bank.json questions 배열에 추가 (다른 키는 보존)."""
    data = load_bank()
    data["questions"].append(entry)
    os.makedirs(os.path.dirname(DATA_JSON), exist_ok=True)
    with open(DATA_JSON, "w", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, indent=2))


# ──────────────────────────────────────────────
# 이미지 1장 / 폴더 전체 처리
# ──────────────────────────────────────────────

def process_image(src_path: str, api_key: str, model: str, title: str = None) -> dict:
    """
    소스 이미지(이름이 적힌 원본) 한 장을 OCR → 번호 SVG 생성 후 문제 항목(dict) 반환.
    (bone_bank.json에 추가는 하지 않음 — 호출부에서 append_to_bank)
    번호 이미지는 <stem>_numbered.svg 로 저장, 원본(원본 보기용)은 소스 파일 그대로 사용.
    """
    fname = os.path.basename(src_path)
    stem = os.path.splitext(fname)[0]

    with open(src_path, "rb") as f:
        raw = f.read()
    pix = fitz.Pixmap(raw)
    if pix.alpha:
        pix = fitz.Pixmap(fitz.csRGB, pix)   # 알파 제거 (흰 배경 평탄화)
    w, h = pix.width, pix.height
    png = pix.tobytes("png")

    labels = normalize_labels(ocr_labels(png, api_key, model))
    if not labels:
        raise ValueError("라벨(부위 이름)을 찾지 못함")

    num_name = f"{stem}_numbered.svg"
    with open(os.path.join(IMAGES_DIR, num_name), "w", encoding="utf-8") as f:
        f.write(build_numbered_svg(png, labels, w, h))

    return {
        "id": stem,
        "title": title or stem,
        "image": f"/static/images/bones/{num_name}",   # 번호 이미지(문제)
        "original": f"/static/images/bones/{fname}",    # 원본(이름) 이미지
        "parts": labels_to_parts(labels),
    }


def process_folder(api_key: str, model: str = DEFAULT_MODEL):
    """
    static/images/bones/ 의 '새로 추가된 소스 이미지'를 찾아 일괄 처리.
    이미 bone_bank.json 에 등록됐거나(파일명/ id 기준), 생성물(_numbered.svg),
    '_'로 시작하는 파일은 건너뜀. 반환: (처리한 id 목록, [(파일, 사유)] 스킵 목록)
    """
    bank = load_bank()
    existing_ids = {q.get("id") for q in bank["questions"]}
    referenced = set()
    for q in bank["questions"]:
        for k in ("image", "original"):
            v = q.get(k)
            if v:
                referenced.add(os.path.basename(v))

    processed, errors = [], []
    for name in sorted(os.listdir(IMAGES_DIR)):
        path = os.path.join(IMAGES_DIR, name)
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(name)
        if ext.lower() not in SOURCE_EXTS:          # svg 등 비대상
            continue
        if name.startswith("_"):                    # _placeholder 등
            continue
        if name in referenced or stem in existing_ids:  # 이미 처리됨
            continue
        try:
            entry = process_image(path, api_key, model)
            append_to_bank(entry)
            existing_ids.add(entry["id"])
            processed.append(entry["id"])
        except Exception as e:
            errors.append((name, str(e)))
    return processed, errors
