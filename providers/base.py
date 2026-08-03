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

    # API 키 발급 안내 — 키 입력란 아래 접힌 도움말로 표시된다.
    # key_help_steps가 비어 있으면 도움말 자체가 화면에서 숨겨지므로,
    # 절차를 아직 모르는 프로바이더는 빈 리스트로 두면 된다.
    key_help_url    = ""    # 발급 페이지 주소. 비면 '발급 페이지 열기' 버튼이 빠진다
    key_help_steps  = []    # 번호 목록으로 뿌릴 문장들 (평문 — HTML 태그 넣지 말 것)
    key_help_note   = ""    # 비용·무료 등급 등 한 줄 주의사항 (선택)

    # 크레딧(잔액) 조회를 지원하는가 — 화면에 조회 UI를 띄울지 결정한다.
    # 표준 API가 아니라 제공사가 따로 만든 기능이라 대부분 False다.
    supports_credits = False

    # usage 인자 — providers.usage.UsageCollector를 넘기면 구현체가 호출마다
    # 토큰 사용량을 기록한다. None이면 아무것도 하지 않으므로, 사용량이 필요 없는
    # 호출부는 기존처럼 그냥 부르면 된다.

    @abstractmethod
    def complete(self, prompt: str, api_key: str, model: str,
                 max_tokens: int = None, usage=None) -> str:
        """프롬프트 하나를 보내고 텍스트 응답을 받는다.
        max_tokens=None 이면 프로바이더 기본값을 쓴다."""

    def complete_stream(self, prompt: str, api_key: str, model: str,
                        max_tokens: int = None, usage=None):
        """
        complete()와 같지만 응답을 조각(문자열)으로 나눠 yield 한다.
        조각 경계는 의미 단위가 아니므로, 호출부가 이어붙여 해석해야 한다.

        기본 구현은 complete()를 그대로 한 조각으로 내보낸다 —
        스트리밍을 지원하지 않는 프로바이더도 같은 인터페이스로 쓸 수 있게.
        """
        yield self.complete(prompt, api_key, model, max_tokens, usage=usage)

    @abstractmethod
    def describe_image(self, png_bytes: bytes, api_key: str, model: str,
                       usage=None) -> str:
        """이미지를 vision 모델에 보내 한국어 설명을 받는다."""

    @abstractmethod
    def list_models(self, api_key: str) -> list:
        """사용 가능한 모델 id 목록을 반환한다."""

    def get_credits(self, api_key: str) -> dict:
        """
        API 키에 연결된 계정의 크레딧 잔액을 조회한다.

        표준 API가 아니라 제공사가 따로 제공하는 기능이므로,
        supports_credits = True 로 선언한 프로바이더만 구현한다.
        (라우트가 supports_credits를 먼저 확인하므로 여기까지 오지 않는다)
        """
        raise NotImplementedError(f"{self.label}는 크레딧 조회를 지원하지 않습니다.")
