"""
OpenAI 직접 연동 Provider — 사용자가 자신의 OpenAI API 키로 문제를 생성할 수 있게 한다.

호출 로직은 게이트웨이와 동일(OpenAI SDK)하고, base_url을 지정하지 않아
OpenAI 공식 엔드포인트로 나간다.
"""

from providers.openai_compatible import OpenAICompatibleProvider

DEFAULT_MODEL = "gpt-5.6"

# models.list()는 음성·이미지·임베딩 모델까지 모두 돌려주므로,
# 문제 생성에 쓸 수 있는 채팅 모델만 남긴다.
_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")
_EXCLUDE_KEYWORDS = (
    "instruct", "audio", "realtime", "transcribe", "tts", "image",
    "embedding", "moderation", "search", "dall-e", "whisper", "codex",
)


class OpenAIProvider(OpenAICompatibleProvider):
    name            = "openai"
    label           = "OpenAI"
    base_url        = None          # OpenAI 공식 엔드포인트
    default_model   = DEFAULT_MODEL
    key_placeholder = "sk-... (platform.openai.com에서 발급)"

    key_help_url    = "https://platform.openai.com/api-keys"
    key_help_steps  = [
        "platform.openai.com에 로그인합니다. (ChatGPT 계정과 같은 계정으로 로그인됩니다)",
        "왼쪽 메뉴에서 API keys를 엽니다.",
        "Create new secret key 버튼을 누르고, 이름은 아무거나 적어도 됩니다.",
        "키는 이때 딱 한 번만 보입니다. 창을 닫으면 다시 볼 수 없으니 바로 복사해서 위 칸에 붙여넣으세요.",
        "Billing 메뉴에서 결제 수단을 등록하고 크레딧을 충전해야 실제 호출이 됩니다.",
    ]
    key_help_note   = "ChatGPT Plus 구독료와 API 요금은 별개입니다. Plus를 쓰고 있어도 API 크레딧은 따로 충전해야 합니다."

    # 최신 모델은 추론 토큰까지 max_tokens에 포함되므로 넉넉히 잡는다.
    max_tokens = 16000

    def list_models(self, api_key: str) -> list:
        ids = super().list_models(api_key)
        chat = [
            m for m in ids
            if m.startswith(_CHAT_PREFIXES)
            and not any(k in m for k in _EXCLUDE_KEYWORDS)
        ]
        # 필터가 전부 걸러내면(모델명 규칙이 바뀐 경우) 원본을 그대로 보여준다.
        return chat or ids
