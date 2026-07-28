"""
Anthropic(Claude) 직접 연동 Provider.

OpenAI 호환 계층을 상속하지 않는 유일한 프로바이더 — Chat Completions가 아니라
Messages API라서 요청/응답 구조가 다르다:
  - max_tokens 필수
  - 응답이 content 블록 리스트 (텍스트만 골라내야 함)
  - 이미지는 image_url이 아니라 source.type=base64 형식
"""

import base64
from contextlib import contextmanager

import anthropic

from providers.base import (
    Provider, ProviderError, ProviderAuthError, ProviderRateLimitError,
)
from providers.openai_compatible import IMAGE_DESC_PROMPT

DEFAULT_MODEL = "claude-opus-5"

# Claude 최신 모델은 thinking이 기본으로 켜져 있고, max_tokens가
# thinking + 응답 텍스트를 합쳐서 제한한다 → 넉넉히 잡지 않으면 답이 잘린다.
MAX_TOKENS       = 16000
IMAGE_MAX_TOKENS = 4096


@contextmanager
def _translate_errors():
    """Anthropic SDK 예외를 프로바이더 공통 예외로 번역."""
    try:
        yield
    except anthropic.AuthenticationError:
        raise ProviderAuthError("Anthropic API 키가 올바르지 않습니다. 키를 확인해주세요.")
    except anthropic.PermissionDeniedError:
        raise ProviderAuthError("이 API 키로는 해당 모델을 사용할 수 없습니다.")
    except anthropic.RateLimitError:
        raise ProviderRateLimitError(
            "Anthropic 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."
        )
    except anthropic.NotFoundError:
        raise ProviderError("존재하지 않는 모델입니다. 모델 목록을 새로고침해주세요.")


def _extract_text(response) -> str:
    """
    content 블록 중 텍스트만 이어붙인다.
    안전 필터가 요청을 거절하면 stop_reason='refusal'로 200이 오고 content가 비므로,
    빈 문자열 대신 이유를 알 수 있는 오류로 바꿔준다.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        raise ProviderError(
            "모델이 이 자료에 대한 응답을 거부했습니다. "
            "다른 모델을 선택하거나 자료 범위를 좁혀서 다시 시도해주세요."
        )
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


class AnthropicProvider(Provider):
    name            = "anthropic"
    label           = "Anthropic (Claude)"
    default_model   = DEFAULT_MODEL
    key_placeholder = "sk-ant-... (console.anthropic.com에서 발급)"

    def _client(self, api_key: str) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key=api_key)

    def complete(self, prompt: str, api_key: str, model: str) -> str:
        # thinking·effort를 지정하지 않는 이유: 사용자가 드롭다운에서 아무 모델이나
        # 고를 수 있는데, 이 옵션들은 모델마다 지원 여부가 달라 400을 낸다.
        # 생략하면 모든 모델에서 안전하게 동작한다.
        with _translate_errors():
            response = self._client(api_key).messages.create(
                model=model,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        return _extract_text(response)

    def describe_image(self, png_bytes: bytes, api_key: str, model: str) -> str:
        b64 = base64.b64encode(png_bytes).decode()
        with _translate_errors():
            response = self._client(api_key).messages.create(
                model=model,
                max_tokens=IMAGE_MAX_TOKENS,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        }},
                        {"type": "text", "text": IMAGE_DESC_PROMPT},
                    ],
                }],
            )
        return _extract_text(response)

    def list_models(self, api_key: str) -> list:
        # models.list()는 이터레이션하면 자동으로 페이지를 넘긴다.
        with _translate_errors():
            return sorted(m.id for m in self._client(api_key).models.list())
