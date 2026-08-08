"""
프로바이더 레지스트리 — 이름(문자열)으로 구현체를 얻는 단일 진입점.

새 프로바이더를 추가할 때 손댈 곳은 아래 _REGISTRY 한 줄뿐이다.
라우트·llm.py는 구체 클래스를 import하지 않고 get_provider(name)만 쓴다.
"""

from providers.anthropic_provider import AnthropicProvider
from providers.gemini_provider import GeminiProvider
from providers.jbnu_gateway import JbnuGatewayProvider
from providers.openai_provider import OpenAIProvider

# 등록 순서 = 화면에 표시되는 순서
_REGISTRY = {
    JbnuGatewayProvider.name: JbnuGatewayProvider,
    OpenAIProvider.name:      OpenAIProvider,
    AnthropicProvider.name:   AnthropicProvider,
    GeminiProvider.name:      GeminiProvider,
}

DEFAULT_PROVIDER = JbnuGatewayProvider.name


class UnknownProviderError(ValueError):
    """등록되지 않은 프로바이더 이름을 요청했을 때."""


def get_provider(name: str = None):
    """이름으로 프로바이더 인스턴스 반환. 빈 값이면 기본 프로바이더."""
    key = (name or DEFAULT_PROVIDER).strip()
    cls = _REGISTRY.get(key)
    if cls is None:
        known = ", ".join(_REGISTRY)
        raise UnknownProviderError(
            f"알 수 없는 프로바이더 '{key}'. 사용 가능: {known}"
        )
    return cls()


def list_providers() -> list:
    """화면(프로바이더 선택 UI)에 필요한 메타데이터 목록."""
    return [
        {
            "name":            cls.name,
            "label":           cls.label,
            "default_model":   cls.default_model,
            "key_placeholder": cls.key_placeholder,
            "key_help_url":    cls.key_help_url,
            "key_help_steps":  cls.key_help_steps,
            "key_help_note":   cls.key_help_note,
            "supports_credits": cls.supports_credits,
            # 기출 주제 분석 탭의 기본 선택 모델. 빈 값이면 화면이 default_model로 떨어진다.
            # 이건 내려보낸다 — 드롭다운의 첫 선택값이라 화면이 알아야 하는 값이다.
            "topic_default_model": cls.topic_default_model,
            # image_model·analysis_model·image_fallback_model은 일부러 안 내려보낸다 —
            # 화면이 고를 수 있는 값이 아니라 서버가 알아서 쓰는 값이라, 보내봐야 쓰는 곳이 없다.
        }
        for cls in _REGISTRY.values()
    ]
