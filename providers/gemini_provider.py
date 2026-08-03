"""
Google Gemini 연동 Provider.

제미나이는 OpenAI 호환 엔드포인트(/chat/completions)를 제공하므로
Anthropic처럼 별도 어댑터를 쓸 필요 없이 base_url만 바꿔주면 된다.
  https://ai.google.dev/gemini-api/docs/openai
"""

from providers.openai_compatible import OpenAICompatibleProvider

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
DEFAULT_MODEL   = "gemini-3.6-flash"


class GeminiProvider(OpenAICompatibleProvider):
    name            = "gemini"
    label           = "Google Gemini"
    base_url        = GEMINI_BASE_URL
    default_model   = DEFAULT_MODEL
    key_placeholder = "AIza... (aistudio.google.com에서 발급)"

    key_help_url    = "https://aistudio.google.com/apikey"
    key_help_steps  = [
        "aistudio.google.com에 Google 계정으로 로그인합니다.",
        "API 키 만들기(Get API key) 버튼을 누릅니다.",
        "프로젝트를 고르라고 하면 아무거나 골라도 되고, 새로 만들어도 됩니다.",
        "만들어진 키를 복사해서 위 칸에 붙여넣으세요. (여기서는 나중에 다시 볼 수 있습니다)",
    ]
    key_help_note   = "무료 등급이 있어서 결제 수단 없이도 바로 써볼 수 있습니다. 다만 분당·일당 요청 수에 제한이 있습니다."

    # 제미나이도 사고(thinking) 토큰이 출력 한도에 포함되므로 넉넉히 잡는다.
    max_tokens = 16000

    def list_models(self, api_key: str) -> list:
        # 제미나이는 모델 id를 'models/gemini-...' 형태로 돌려준다.
        # 접두사를 떼야 드롭다운에 깔끔하게 보이고, 그대로 호출해도 동작한다.
        ids = super().list_models(api_key)
        return sorted(m.split("/", 1)[-1] if m.startswith("models/") else m for m in ids)
