"""
OpenAI 호환 엔드포인트를 쓰는 프로바이더의 공통 구현.

전북대 게이트웨이·OpenAI 모두 OpenAI SDK로 호출하고 base_url만 다르므로,
호출 로직을 여기 한 곳에 두고 하위 클래스는 접속 정보와 메타데이터만 선언한다.
"""

import base64
from contextlib import contextmanager

from openai import OpenAI, APIStatusError, APIConnectionError

from providers.base import (
    Provider, ProviderError, ProviderAuthError, ProviderRateLimitError,
)

# 이미지 페이지가 '무엇인지' 설명하게 하는 프롬프트 (프로바이더 공통)
IMAGE_DESC_PROMPT = (
    "이 이미지는 의대 강의자료 또는 기출문제의 한 페이지/그림입니다. "
    "무엇을 나타내는 이미지인지 한국어로 간결히 설명하세요. "
    "의학적으로 중요한 내용(그래프·표·해부도·검사 소견·수치 등)이 있으면 핵심을 요약하고, "
    "이미지 안에 글자가 보이면 핵심 텍스트도 함께 옮겨 적으세요. "
    "설명 외의 사족은 쓰지 마세요."
)


def _looks_like_key_problem(text: str) -> bool:
    """
    상태 코드로 못 거르는 키 오류를 문구로 잡는다.
    OpenAI 호환이라도 제공사마다 응답이 달라서 필요함
    (예: xAI는 잘못된 키에 401이 아니라 400 + 'Incorrect API key provided'를 반환).
    """
    low = (text or "").lower()
    return ("api key" in low) or ("api_key" in low) or ("apikey" in low)


@contextmanager
def _translate_errors(label: str):
    """OpenAI SDK 예외를 프로바이더 공통 예외로 번역 (라우트가 SDK를 몰라도 되게)."""
    try:
        yield
    except APIConnectionError:
        raise ProviderError(f"{label} 서버에 연결하지 못했습니다. 네트워크를 확인해주세요.")
    except APIStatusError as e:
        detail = (getattr(e, "message", "") or str(e)).strip()
        status = e.status_code
        if status in (401, 403) or _looks_like_key_problem(detail):
            raise ProviderAuthError(f"{label} API 키가 올바르지 않습니다. 키를 확인해주세요.")
        if status == 429:
            raise ProviderRateLimitError(
                f"{label} 요청 한도를 초과했습니다. 잠시 후 다시 시도해주세요."
            )
        if status == 404:
            raise ProviderError(
                f"{label}에 없는 모델입니다. 모델 목록을 새로고침한 뒤 다시 선택해주세요."
            )
        raise ProviderError(f"{label} 요청 오류({status}): {detail}")


class OpenAICompatibleProvider(Provider):
    # 하위 클래스에서 재정의 (base_url=None 이면 OpenAI 기본 엔드포인트)
    base_url = None

    # 문제 생성은 30문항 + 해설까지 나올 수 있어 넉넉히 잡는다.
    max_tokens       = 4096
    image_max_tokens = 1024

    def _client(self, api_key: str) -> OpenAI:
        return OpenAI(api_key=api_key, base_url=self.base_url)

    def complete(self, prompt: str, api_key: str, model: str,
                 max_tokens: int = None) -> str:
        with _translate_errors(self.label):
            response = self._client(api_key).chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens or self.max_tokens,
            )
        return response.choices[0].message.content

    def complete_stream(self, prompt: str, api_key: str, model: str,
                        max_tokens: int = None):
        """
        응답을 델타 조각으로 흘려보낸다.

        주의 — 스트리밍은 예외가 호출 시점이 아니라 **이터레이션 도중** 발생한다.
        _translate_errors()가 제너레이터 본문 전체를 감싸고 있어야 조각을 받다가
        생긴 오류도 번역된다. (with 블록은 yield 사이에도 계속 활성 상태)
        """
        with _translate_errors(self.label):
            with self._client(api_key).chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens or self.max_tokens,
                stream=True,
            ) as stream:
                for chunk in stream:
                    # 마지막 usage 전용 청크 등 choices가 빈 경우가 있다
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta

    def describe_image(self, png_bytes: bytes, api_key: str, model: str) -> str:
        """
        Vision LLM으로 이미지가 '무엇인지' 한국어로 설명 생성.
        전체 전사가 아니라, 그림·그래프·표·해부도·검사 소견 등 핵심 내용을 요약.
        엔드포인트가 이미지 입력을 지원하지 않으면 예외 → 호출부에서 폴백 처리.
        """
        b64 = base64.b64encode(png_bytes).decode()
        with _translate_errors(self.label):
            resp = self._client(api_key).chat.completions.create(
                model=model,
                max_tokens=self.image_max_tokens,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_DESC_PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }],
            )
        return (resp.choices[0].message.content or "").strip()

    def list_models(self, api_key: str) -> list:
        with _translate_errors(self.label):
            models = self._client(api_key).models.list()
        return sorted(m.id for m in models.data)
