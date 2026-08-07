"""
전북대 LLM 게이트웨이 Provider — OpenAI 호환 엔드포인트를 통해 호출한다.

호출 로직은 OpenAICompatibleProvider와 동일하고, 접속 주소만 다르다.

여기에만 크레딧(잔액) 조회가 있다. 게이트웨이는 토큰이 아니라 크레딧으로 과금하므로,
사용량을 토큰 수로 보여줘도 실제 소모와 연결되지 않는다. 대신 생성 전후로 잔액을
조회해 그 차이를 "이번 생성에 쓴 크레딧"으로 보여준다.
  문서: https://docs.mindlogic.ai/docs/jbnu/api-gateway/reference/credits
"""

import requests

from providers.base import ProviderAuthError, ProviderError, ProviderRateLimitError
from providers.openai_compatible import OpenAICompatibleProvider

# ══════════════════════════════════════════════════════════════════
#  ▼ 자주 바꾸는 값 — 모델을 갈아끼울 땐 여기 네 줄만 고치면 된다 ▼
#    (고른 근거·실측은 바로 아래 '모델 선택 근거'에 적어 두었다)
# ══════════════════════════════════════════════════════════════════

# 화면에서 사용자가 고르는 '사용할 모델'의 기본 선택값 (= 문제를 만드는 모델)
DEFAULT_MODEL        = "gpt-5.6-sol"

# 기출 주제 분석 탭 — '그림 설명용' 모델. 이미지 호출에만 쓰인다.
#   (주제↔문항 대조는 사용자가 고른 모델이 그대로 한다)
IMAGE_MODEL          = "gpt-5.6-luna"

# 문제 생성 탭 — '분석용' 모델. 그림 읽기 + 개념·형식 분석을 맡는다.
#   (문제를 만드는 것은 사용자가 고른 모델이다)
ANALYSIS_MODEL       = "gpt-5.6-luna"

# 이미지 호출이 실패했을 때 한 번 더 물어볼 모델 (이미지 전용 — 텍스트 분석엔 폴백이 없다)
IMAGE_FALLBACK_MODEL = "gemini-3.6-flash"

# ══════════════════════════════════════════════════════════════════
#  모델 선택 근거 — 값을 바꾸기 전에 읽을 것
# ══════════════════════════════════════════════════════════════════
#
# ① IMAGE_MODEL — 저가 모델로 내려도 되는 근거는 실측이다. image_desc_cache에서
#    cache_key가 같은 행(= 같은 PNG·프롬프트·출력 한도)끼리 짝지은 강의록 전사 글자수:
#        gemini-3.6-flash 783 · gpt-5.6-luna 776   (53쪽)
#        claude-sonnet-4-6 806 · gpt-5.6-luna 790  (54쪽)
#    체급 차이가 거의 없다. 갈린 것은 모델이 아니라 출력 한도였다
#    (llm.py의 DESCRIBE_MAX_OUTPUT 주석). 그러면 가장 싼 것을 쓰는 게 맞다.
#
# ② ANALYSIS_MODEL — 값이 ①과 같지만 근거는 따로다. 이쪽은 텍스트 추론(기출 분석·
#    대표문제 추출)까지 포함해 '옮겨 적기'라는 ①의 논리가 안 통한다. 같은 자료로 비교
#    (session 63 vs 64, 생성 모델은 양쪽 다 claude-opus-5 고정):
#        크레딧      luna 225.63  ·  terra 397.82   ← terra는 이미지 호출값이 빠진 수치
#        대표문제    luna 5개(4유형 전부) · terra 4개(서술형 누락)
#        빈출포인트  luna 11 · terra 8   |   유형통계  luna 107문항 · terra 98문항
#        금지 규칙 위반((필기:·판독불가·이미지 설명 누출)은 양쪽 다 0
#    싼 쪽이 더 나았으므로 luna로 고정한다.
#    ⚠️ 이 측정은 기출이 38% 잘린 입력(coverage 61~63%)에서 나왔다. 예산을 올린 뒤
#       (llm.py GEN_SIDE_CHAR_BUDGET) 재측정하지 않았으므로 다시 재면 뒤집힐 수 있다.
#
# ③ IMAGE_FALLBACK_MODEL — 제공사를 일부러 다르게 골랐다. 같은 제공사의 다른 모델은
#    장애가 같이 나기 쉽다. ①의 실측에서 전사량이 luna와 동률이라 폴백으로 넘어가도
#    결과가 얇아지지 않는다.

GATEWAY_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"

CREDITS_URL     = GATEWAY_BASE_URL + "/credits/"
CREDITS_TIMEOUT = 10        # 초 — 잔액 조회가 생성 전체를 붙잡지 않게

# 응답 섹션 → 화면 라벨. 문서상 세 섹션 모두 quota/used/remaining을 가진다.
CREDIT_SECTIONS = (
    ("monthly_allocated", "월별 할당"),
    ("purchased",         "충전"),
    ("total",             "전체"),
)


def _num(v):
    """quota/used/remaining은 float. 숫자가 아니면(누락·null) None으로 떨어뜨린다."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    return float(v)


class JbnuGatewayProvider(OpenAICompatibleProvider):
    name            = "jbnu_gateway"
    label           = "전북대 LLM 게이트웨이"
    base_url        = GATEWAY_BASE_URL
    default_model   = DEFAULT_MODEL
    key_placeholder = "전북대 LLM 플랫폼에서 발급받은 API 키"

    image_model          = IMAGE_MODEL
    analysis_model       = ANALYSIS_MODEL
    image_fallback_model = IMAGE_FALLBACK_MODEL

    key_help_url    = "https://gpt.jbnu.ai/dashboard/developers"
    key_help_steps  = [
        "gpt.jbnu.ai에 로그인합니다.",
        "왼쪽 메뉴 최하단의 \"API Gateway\"로 들어갑니다.",
        "\"API 키 관리\"에서 [ + API 키 생성 ] 버튼을 누르고, 이름은 아무거나 적습니다.",
        "생성된 API 키를 복사해 메모장에 저장해둡니다. 창을 닫으면 키를 다시 확인할 수 없습니다.",
        "복사한 키를 위 칸에 붙여넣으세요."
    ]
    key_help_note   = ""

    supports_credits = True

    def get_credits(self, api_key: str) -> dict:
        """
        크레딧 잔액 조회 — GET /v1/gateway/credits/ (파라미터 없이 키로 사용자 식별).

        반환 형태 (숫자를 못 읽은 필드는 None):
          {"monthly_allocated": {"quota","used","remaining","renewal_date"},
           "purchased":         {"quota","used","remaining"},
           "total":             {"quota","used","remaining"},
           "sections": [{"key","label","quota","used","remaining"}, ...]}

        sections는 화면에서 표로 그리기 위한 순서 고정 사본이다.
        """
        try:
            resp = requests.get(
                CREDITS_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=CREDITS_TIMEOUT,
            )
        except requests.RequestException:
            raise ProviderError(
                f"{self.label}에 연결하지 못했습니다. 네트워크를 확인해주세요."
            )

        if resp.status_code in (401, 403):
            raise ProviderAuthError(
                f"{self.label} API 키가 올바르지 않습니다. 키를 확인해주세요."
            )
        if resp.status_code == 429:
            raise ProviderRateLimitError(
                f"{self.label} 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."
            )
        if resp.status_code == 404:
            raise ProviderError(
                f"{self.label}가 크레딧 조회를 지원하지 않습니다. (엔드포인트 없음)"
            )
        if not resp.ok:
            raise ProviderError(f"크레딧 조회 실패({resp.status_code})")

        try:
            data = resp.json()
        except ValueError:
            raise ProviderError("크레딧 응답을 해석하지 못했습니다.")
        if not isinstance(data, dict):
            raise ProviderError("크레딧 응답 형식이 예상과 다릅니다.")

        return self._shape_credits(data)

    @staticmethod
    def _shape_credits(data: dict) -> dict:
        """응답을 화면에서 바로 쓸 형태로 정리. 없는 필드는 None으로 남긴다."""
        out, sections = {}, []
        for key, label in CREDIT_SECTIONS:
            raw = data.get(key)
            raw = raw if isinstance(raw, dict) else {}
            part = {
                "quota":     _num(raw.get("quota")),
                "used":      _num(raw.get("used")),
                "remaining": _num(raw.get("remaining")),
            }
            if key == "monthly_allocated":
                rd = raw.get("renewal_date")
                part["renewal_date"] = rd if isinstance(rd, str) else None
            out[key] = part
            sections.append({"key": key, "label": label, **part})
        out["sections"] = sections
        return out
