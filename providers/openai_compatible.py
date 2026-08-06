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
    IMAGE_DESC_PROMPT,
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
    # 이미지 호출 기본 한도. 호출부가 llm.py의 모드별 max_output을 넘기므로 보통은
    # 그 값이 쓰이고, 이건 지정하지 않은 호출을 위한 하한이다.
    # 1024였는데 올렸다 — 사고(thinking)를 하는 모델은 사고 토큰도 이 한도에 포함되어,
    # 1024로는 사고가 예산을 다 먹고 전사가 단어 중간에서 잘리는 일이 있었다.
    # (gemini_provider.py가 같은 이유로 max_tokens를 16000으로 올려뒀는데, 이미지 쪽은
    #  빠져 있었다. 게이트웨이는 두 값을 다 상속하므로 모든 모델이 영향을 받았다)
    image_max_tokens = 2048

    def _client(self, api_key: str) -> OpenAI:
        return OpenAI(api_key=api_key, base_url=self.base_url)

    @staticmethod
    def _messages(prompt: str, cache_prefix: str = None) -> list:
        """
        Chat Completions에는 캐시 경계를 지정하는 필드가 없다. 그냥 이어붙인다.
        (자동 프롬프트 캐싱이 있는 서버라면 접두부가 앞에 몰려 있는 것만으로 이득이고,
         없는 서버여도 예전과 완전히 같은 요청이 된다)
        """
        return [{"role": "user", "content": (cache_prefix or "") + prompt}]

    def complete(self, prompt: str, api_key: str, model: str,
                 max_tokens: int = None, usage=None, cache_prefix: str = None) -> str:
        with _translate_errors(self.label):
            response = self._client(api_key).chat.completions.create(
                model=model,
                messages=self._messages(prompt, cache_prefix),
                max_tokens=max_tokens or self.max_tokens,
            )
        if usage is not None:
            usage.add(model, getattr(response, "usage", None))
        return response.choices[0].message.content

    def complete_stream(self, prompt: str, api_key: str, model: str,
                        max_tokens: int = None, usage=None, cache_prefix: str = None):
        """
        응답을 델타 조각으로 흘려보낸다.

        주의 — 스트리밍은 예외가 호출 시점이 아니라 **이터레이션 도중** 발생한다.
        _translate_errors()가 제너레이터 본문 전체를 감싸고 있어야 조각을 받다가
        생긴 오류도 번역된다. (with 블록은 yield 사이에도 계속 활성 상태)
        """
        kwargs = dict(
            model=model,
            messages=self._messages(prompt, cache_prefix),
            max_tokens=max_tokens or self.max_tokens,
            stream=True,
        )
        with _translate_errors(self.label):
            client = self._client(api_key)
            # 스트리밍은 기본적으로 usage를 안 보내준다 → 마지막에 usage 청크를 달라고
            # 요청한다. 이 옵션을 모르는 호환 서버는 400을 내므로, 그때는 옵션 없이
            # 한 번 더 시도한다 (사용량만 포기하고 생성은 정상 진행).
            try:
                stream = client.chat.completions.create(
                    **kwargs, stream_options={"include_usage": True}
                )
            except APIStatusError as e:
                if e.status_code != 400:
                    raise
                stream = client.chat.completions.create(**kwargs)

            with stream:
                reported = False
                for chunk in stream:
                    # usage 전용 청크는 choices가 비어 있다
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage and usage is not None:
                        usage.add(model, chunk_usage)
                        reported = True
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta.content
                    if delta:
                        yield delta
                # 끝까지 usage 청크가 없었으면 '제공사가 안 알려줌'으로 남긴다
                if usage is not None and not reported:
                    usage.add(model, None)

    def describe_image(self, png_bytes: bytes, api_key: str, model: str,
                       usage=None, prompt: str = None,
                       max_tokens: int = None) -> str:
        """
        Vision LLM으로 이미지를 한국어 텍스트로 만든다.
        기본(IMAGE_DESC_PROMPT)은 전체 전사가 아니라 그림·그래프·표·해부도 등 핵심 요약.
        IMAGE_TEXT_PROMPT를 주면 반대로 그림 속 글자만 그대로 옮긴다(강의록 손글씨용).
        엔드포인트가 이미지 입력을 지원하지 않으면 예외 → 호출부에서 폴백 처리.
        """
        b64 = base64.b64encode(png_bytes).decode()
        with _translate_errors(self.label):
            resp = self._client(api_key).chat.completions.create(
                model=model,
                max_tokens=max_tokens or self.image_max_tokens,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt or IMAGE_DESC_PROMPT},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{b64}"}},
                    ],
                }],
            )
        if usage is not None:
            usage.add(model, getattr(resp, "usage", None))
        return (resp.choices[0].message.content or "").strip()

    def list_models(self, api_key: str) -> list:
        with _translate_errors(self.label):
            models = self._client(api_key).models.list()
        return sorted(m.id for m in models.data)
