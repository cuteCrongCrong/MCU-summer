"""
전북대 LLM 게이트웨이 Provider — OpenAI 호환 엔드포인트를 통해 호출한다.

호출 로직은 OpenAICompatibleProvider와 동일하고, 접속 주소만 다르다.
"""

from providers.openai_compatible import OpenAICompatibleProvider

GATEWAY_BASE_URL = "https://factchat-cloud.mindlogic.ai/v1/gateway"
DEFAULT_MODEL    = "claude-sonnet-4-5"


class JbnuGatewayProvider(OpenAICompatibleProvider):
    name            = "jbnu_gateway"
    label           = "전북대 LLM 게이트웨이"
    base_url        = GATEWAY_BASE_URL
    default_model   = DEFAULT_MODEL
    key_placeholder = "전북대 LLM 플랫폼에서 발급받은 API 키"
