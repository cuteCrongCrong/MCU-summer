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
