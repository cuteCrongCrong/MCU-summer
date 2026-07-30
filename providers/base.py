"""
Provider 추상 인터페이스 — LLM 백엔드를 교체 가능하게 만드는 계층.

llm.py는 구체적으로 어떤 프로바이더(전북대 게이트웨이/OpenAI/Anthropic 등)를
쓰는지 몰라도 되고, 이 세 메서드만 알면 된다.

각 프로바이더 SDK는 예외 클래스가 서로 다르므로(openai.AuthenticationError vs
anthropic.AuthenticationError), 구현체가 아래 공통 예외로 번역해서 던진다.
→ 라우트는 프로바이더를 몰라도 같은 방식으로 사용자에게 안내할 수 있다.
"""

from abc import ABC, abstractmethod


class ProviderError(Exception):
    """프로바이더 호출 실패 — 메시지는 사용자에게 그대로 보여줄 수 있는 한국어."""


class ProviderAuthError(ProviderError):
    """API 키가 잘못됐거나 권한이 없음."""


class ProviderRateLimitError(ProviderError):
    """요청 한도 초과."""


class Provider(ABC):
    # 하위 클래스에서 재정의 (화면 표시·기본값용 메타데이터)
    name            = ""
    label           = ""
    default_model   = ""
    key_placeholder = ""

    @abstractmethod
    def complete(self, prompt: str, api_key: str, model: str,
                 max_tokens: int = None) -> str:
        """프롬프트 하나를 보내고 텍스트 응답을 받는다.
        max_tokens=None 이면 프로바이더 기본값을 쓴다."""

    def complete_stream(self, prompt: str, api_key: str, model: str,
                        max_tokens: int = None):
        """
        complete()와 같지만 응답을 조각(문자열)으로 나눠 yield 한다.
        조각 경계는 의미 단위가 아니므로, 호출부가 이어붙여 해석해야 한다.

        기본 구현은 complete()를 그대로 한 조각으로 내보낸다 —
        스트리밍을 지원하지 않는 프로바이더도 같은 인터페이스로 쓸 수 있게.
        """
        yield self.complete(prompt, api_key, model, max_tokens)

    @abstractmethod
    def describe_image(self, png_bytes: bytes, api_key: str, model: str) -> str:
        """이미지를 vision 모델에 보내 한국어 설명을 받는다."""

    @abstractmethod
    def list_models(self, api_key: str) -> list:
        """사용 가능한 모델 id 목록을 반환한다."""
