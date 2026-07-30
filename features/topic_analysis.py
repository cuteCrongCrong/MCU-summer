"""
기출 주제 분석 기능 — "강의록 몇 페이지의 주제가 어떤 기출 몇 번 문제로 나왔는가" 대응표.

담당 테이블: 없음 (결과를 저장하지 않고 응답으로만 돌려준다 → DB 스키마 영향 0)
문제 생성기와의 차이:
  - 유형별 문제 수 · 기출 반영 강도 없음 (문제를 만들지 않으므로)
  - 강의록·기출을 **여러 개** 올릴 수 있다 ("어떤 강의록/어떤 기출"을 구분해 보여줘야 하므로)
  - 세션(분석 자산 캐시)을 쓰지 않는다. 세션에는 페이지 번호가 없어서 출처를 만들 수 없다.
LLM 프롬프트·파싱은 llm.py의 '기출 주제 분석' 섹션에 있다. (라우트는 입력 검증·에러 변환만)
"""

from flask import Blueprint, request, jsonify

from providers.base import (
    ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.factory import UnknownProviderError, get_provider
from llm import extract_labeled_docs, run_topic_analysis

topic_bp = Blueprint("topic", __name__)

# 한쪽(강의록/기출)당 업로드 파일 개수 상한.
# 파일 수가 늘수록 문서당 반영 글자수가 줄고(예산 분할) 프롬프트도 커지므로 상한을 둔다.
MAX_FILES_PER_SIDE = 5


def _collect_pdfs(field: str):
    """업로드된 파일 목록에서 빈 항목을 걸러낸다. (name, files) 또는 오류 문구 반환."""
    files = [f for f in request.files.getlist(field) if f and f.filename]
    for f in files:
        if not f.filename.lower().endswith(".pdf"):
            return None, f"PDF 파일만 올릴 수 있습니다: {f.filename}"
    return files, None


@topic_bp.route("/analyze-topics", methods=["POST"])
def analyze_topics():
    try:
        api_key = request.form.get("api_key", "").strip()
        if not api_key:
            return jsonify({"error": "API 키를 입력해주세요."}), 400

        try:
            provider = get_provider(request.form.get("provider"))
        except UnknownProviderError as e:
            return jsonify({"error": str(e)}), 400
        model = request.form.get("model", "").strip() or provider.default_model

        lectures, err = _collect_pdfs("lectures")
        if err:
            return jsonify({"error": err}), 400
        exams, err = _collect_pdfs("exams")
        if err:
            return jsonify({"error": err}), 400

        if not lectures:
            return jsonify({"error": "강의록 PDF를 1개 이상 올려주세요."}), 400
        if not exams:
            return jsonify({"error": "기출문제 PDF를 1개 이상 올려주세요."}), 400
        if len(lectures) > MAX_FILES_PER_SIDE or len(exams) > MAX_FILES_PER_SIDE:
            return jsonify({
                "error": f"강의록·기출은 각각 최대 {MAX_FILES_PER_SIDE}개까지 올릴 수 있습니다."
            }), 400

        # 강의록: 이미지 설명 생략.
        #   주제 이름은 '강의록에 있는 단어'만 써야 하는데, 이미지 설명은 LLM이 새로 쓴
        #   문장이라 강의록에 없는 용어를 끌어들인다. (토큰도 아낀다)
        lecture_docs = extract_labeled_docs(lectures, "강의록", api_key, model,
                                            describe_images=False, provider=provider)
        # 기출: 그림 문제(부위 이름 쓰기 등)를 놓치지 않도록 이미지 설명 포함 (문제 생성기와 동일)
        exam_docs = extract_labeled_docs(exams, "기출", api_key, model,
                                         describe_images=True, provider=provider)

        if not any((d["text"] or "").strip() for d in lecture_docs):
            return jsonify({
                "error": "강의록에서 텍스트를 추출하지 못했습니다. "
                         "스캔 이미지로만 된 PDF는 주제를 읽을 수 없습니다."
            }), 400
        if not any((d["text"] or "").strip() for d in exam_docs):
            return jsonify({
                "error": "기출문제에서 텍스트를 추출하지 못했습니다. "
                         "문제 번호를 읽을 수 없어 출처를 만들 수 없습니다."
            }), 400

        result = run_topic_analysis(lecture_docs, exam_docs, api_key, model, provider)

        # 문서 메타(파일명·페이지수·반영 범위)는 원문 텍스트를 뺀 형태로만 내려보낸다
        def doc_meta(d):
            return {
                "label": d["label"],
                "name": d["name"],
                "pages": d["pages"],
                "source": d["source"],
            }

        return jsonify({
            "success": True,
            "topics": result["topics"],
            "dropped": result["dropped"],
            "total_questions": result["total_questions"],
            "lecture_docs": [doc_meta(d) for d in lecture_docs],
            "exam_docs": [doc_meta(d) for d in exam_docs],
            "raw": result["raw"],
            "model": model,
            "provider": provider.name,
        })

    except ProviderAuthError as e:
        return jsonify({"error": str(e)}), 401
    except ProviderRateLimitError as e:
        return jsonify({"error": str(e)}), 429
    except ProviderError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        # PDF를 열 수 없음 등 사용자가 고칠 수 있는 입력 문제 (llm.extract_labeled_docs)
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"서버 오류: {str(e)}"}), 500
