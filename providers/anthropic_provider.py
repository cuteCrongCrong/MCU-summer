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
    IMAGE_DESC_PROMPT,
)

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


def _check_refusal(response):
    """
    안전 필터가 요청을 거절하면 stop_reason='refusal'로 200이 오고 content가 빈다.
    빈 응답 대신 이유를 알 수 있는 오류로 바꿔준다. (스트리밍도 최종 메시지로 확인)
    """
    if getattr(response, "stop_reason", None) == "refusal":
        raise ProviderError(
            "모델이 이 자료에 대한 응답을 거부했습니다. "
            "다른 모델을 선택하거나 자료 범위를 좁혀서 다시 시도해주세요."
        )


def _extract_text(response) -> str:
    """content 블록 중 텍스트만 이어붙인다."""
    _check_refusal(response)
    parts = [b.text for b in response.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


class AnthropicProvider(Provider):
    name            = "anthropic"
    label           = "Anthropic (Claude)"
    default_model   = DEFAULT_MODEL
    key_placeholder = "sk-ant-... (console.anthropic.com에서 발급)"

    key_help_url    = "https://console.anthropic.com/settings/keys"
    key_help_steps  = [
        "console.anthropic.com에 로그인합니다. (Claude.ai 계정과 같은 계정으로 로그인됩니다)",
        "Settings → API keys로 들어갑니다.",
        "Create Key 버튼을 누르고, 이름은 아무거나 적어도 됩니다.",
        "키는 이때 딱 한 번만 보입니다. 창을 닫으면 다시 볼 수 없으니 바로 복사해서 위 칸에 붙여넣으세요.",
        "Billing 메뉴에서 크레딧을 구매해야 실제 호출이 됩니다.",
    ]
    key_help_note   = "Claude Pro 구독료와 API 요금은 별개입니다. Pro를 쓰고 있어도 API 크레딧은 따로 구매해야 합니다."

    def _client(self, api_key: str) -> anthropic.Anthropic:
        return anthropic.Anthropic(api_key=api_key)

    def complete(self, prompt: str, api_key: str, model: str,
                 max_tokens: int = None, usage=None) -> str:
        # thinking·effort를 지정하지 않는 이유: 사용자가 드롭다운에서 아무 모델이나
        # 고를 수 있는데, 이 옵션들은 모델마다 지원 여부가 달라 400을 낸다.
        # 생략하면 모든 모델에서 안전하게 동작한다.
        with _translate_errors():
            response = self._client(api_key).messages.create(
                model=model,
                max_tokens=max_tokens or MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
        # 거절(refusal)로 터지기 전에 기록한다 — 거절도 토큰은 쓴다
        if usage is not None:
            usage.add(model, getattr(response, "usage", None))
        return _extract_text(response)

    def complete_stream(self, prompt: str, api_key: str, model: str,
                        max_tokens: int = None, usage=None):
        """
        Messages API의 스트리밍 헬퍼(messages.stream)를 쓴다.
        OpenAI 호환 계층과 달리 델타를 직접 뒤질 필요 없이 text_stream이 텍스트만 준다.

        주의 — 예외가 이터레이션 도중 발생하므로 _translate_errors()가
        제너레이터 본문 전체를 감싸야 한다.
        """
        with _translate_errors():
            with self._client(api_key).messages.stream(
                model=model,
                max_tokens=max_tokens or MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    yield text
                # 거절은 텍스트 없이 끝나므로 최종 메시지에서 확인해야 알 수 있다.
                # 최종 메시지에 usage가 실려오므로 거절 검사 전에 먼저 기록한다.
                final = stream.get_final_message()
                if usage is not None:
                    usage.add(model, getattr(final, "usage", None))
                _check_refusal(final)

    def describe_image(self, png_bytes: bytes, api_key: str, model: str,
                       usage=None, prompt: str = None) -> str:
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
                        {"type": "text", "text": prompt or IMAGE_DESC_PROMPT},
                    ],
                }],
            )
        if usage is not None:
            usage.add(model, getattr(response, "usage", None))
        return _extract_text(response)

    def list_models(self, api_key: str) -> list:
        # models.list()는 이터레이션하면 자동으로 페이지를 넘긴다.
        with _translate_errors():
            return sorted(m.id for m in self._client(api_key).models.list())
