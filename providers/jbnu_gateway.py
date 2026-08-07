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

GATEWAY_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"
DEFAULT_MODEL    = "claude-sonnet-4-5"

# 이미지(그림 설명·글자 전사) 전용 모델 — 게이트웨이에서는 사용자가 고르지 않는다.
#
# 저가 모델로 내려도 되는 근거는 실측이다. image_desc_cache에서 cache_key가 같은 행
# (= 같은 PNG·같은 프롬프트·같은 출력 한도)끼리 짝지어 비교한 강의록 전사 결과:
#     gemini-3.6-flash 783자 · gpt-5.6-luna 776자   (53쪽, 동일 조건)
#     같은 문서에서 claude-sonnet-4-6 806자 · gpt-5.6-luna 790자 (54쪽)
# 체급 차이가 거의 없다. 출력 한도를 풀고 나서 갈린 것은 모델이 아니라 한도였다
# (llm.py의 DESCRIBE_MAX_OUTPUT 주석 참고). 그러면 가장 싼 것을 쓰는 게 맞다.
IMAGE_MODEL = "gpt-5.6-luna"

# 이미지 호출이 실패했을 때 한 번 더 물어볼 모델.
# 게이트웨이는 여러 제공사의 모델을 한 키로 부를 수 있어서, 한쪽이 죽어도 다른 쪽이 산다.
# 제공사를 일부러 다르게 골랐다 — 같은 제공사의 다른 모델은 장애가 같이 나기 쉽다.
# 위 실측에서 전사량이 luna와 동률이라, 폴백으로 넘어가도 결과가 얇아지지 않는다.
IMAGE_FALLBACK_MODEL = "gemini-3.6-flash"

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
    image_fallback_model = IMAGE_FALLBACK_MODEL

    # ── 여기 채워주세요 ──────────────────────────────────────────────
    # 다른 프로바이더는 스스로 키를 발급받지만, 게이트웨이는 학교에서 배부하는
    # 방식이라 절차를 코드에서 알 수 없어 비워 뒀다.
    #
    # key_help_steps가 빈 리스트인 동안에는 화면에서 도움말 자체가 숨겨진다.
    # 아래 두 줄을 채우면 그 순간부터 키 입력란 아래에 접힌 도움말로 나타난다.
    # (다른 프로바이더 예시는 openai_provider.py 참고)
    #
    #   - key_help_url:   신청·발급 페이지 주소. 없으면 "" 그대로 두면 버튼이 빠진다
    #   - key_help_steps: 한 문장 = 한 단계. 평문으로 쓸 것 (HTML 태그 금지)
    #   - key_help_note:  한도·유효기간 같은 주의사항 한 줄 (선택)
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
