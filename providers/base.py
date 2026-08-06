"""
Provider 추상 인터페이스 — LLM 백엔드를 교체 가능하게 만드는 계층.

llm.py는 구체적으로 어떤 프로바이더(전북대 게이트웨이/OpenAI/Anthropic 등)를
쓰는지 몰라도 되고, 이 세 메서드만 알면 된다.

각 프로바이더 SDK는 예외 클래스가 서로 다르므로(openai.AuthenticationError vs
anthropic.AuthenticationError), 구현체가 아래 공통 예외로 번역해서 던진다.
→ 라우트는 프로바이더를 몰라도 같은 방식으로 사용자에게 안내할 수 있다.
"""

from abc import ABC, abstractmethod


# ──────────────────────────────────────────────
# 이미지 페이지용 프롬프트 (프로바이더 공통)
# 어느 구현체를 쓰든 같은 문구여야 결과가 일관되므로 여기 한 곳에만 둔다.
# ──────────────────────────────────────────────

# ① 그림이 '무엇인지' 설명하게 한다. 기출용 —
#    그림 문제(부위 이름 쓰기 등)를 살리려면 그림의 의미가 텍스트로 남아야 하므로
#    모델의 해석이 필요하다.
IMAGE_DESC_PROMPT = (
    "이 이미지는 의대 강의자료 또는 기출문제의 한 페이지/그림입니다. "
    "무엇을 나타내는 이미지인지 한국어로 간결히 설명하세요. "
    "의학적으로 중요한 내용(그래프·표·해부도·검사 소견·수치 등)이 있으면 핵심을 요약하고, "
    "이미지 안에 글자가 보이면 핵심 텍스트도 함께 옮겨 적으세요. "
    "설명 외의 사족은 쓰지 마세요."
)

# ② 그림 속 '글자만' 그대로 옮긴다. 강의록용 —
#    손글씨·판서는 살리되 모델이 지어낸 문장은 한 줄도 섞이면 안 된다.
#    주제 분석이 "주제명은 강의록에 있는 표현만" 규칙으로 도는데, 설명 문장이 강의록
#    텍스트에 섞이면 그 표현까지 '강의록에 있는 것'이 되어 규칙이 무력해지기 때문.
IMAGE_TEXT_PROMPT = (
    "이 이미지는 의대 강의자료의 한 페이지입니다. "
    "이미지 안에 보이는 글자를 적힌 그대로 옮겨 적으세요. 손으로 쓴 글씨도 포함합니다. "
    "표기(띄어쓰기·한자·약어·영문 병기)를 바꾸지 말고, 읽는 순서대로 줄을 나눠 적으세요. "
    "그림이 무엇을 나타내는지에 대한 설명·해석·요약은 절대 쓰지 마세요. "
    "글자를 알아볼 수 없으면 그 부분만 (판독불가)로 표시하세요. "
    "옮길 글자가 하나도 없으면 아무것도 쓰지 마세요."
)


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

    # cache_prefix 인자 — prompt 앞에 붙는 '여러 요청에서 똑같이 반복되는 부분'.
    #   프롬프트 캐싱을 지원하는 제공사(Anthropic)는 여기에 캐시 경계를 잡아
    #   두 번째 요청부터 접두부를 훨씬 싸게 처리한다.
    #   지원하지 않는 제공사는 그냥 앞에 이어붙이면 되므로 결과는 어디서나 같다.
    #   → 호출부는 제공사를 몰라도 "이 부분이 고정이다"만 알려주면 된다.

    @abstractmethod
    def complete(self, prompt: str, api_key: str, model: str,
                 max_tokens: int = None, usage=None, cache_prefix: str = None) -> str:
        """프롬프트 하나를 보내고 텍스트 응답을 받는다.
        max_tokens=None 이면 프로바이더 기본값을 쓴다."""

    def complete_stream(self, prompt: str, api_key: str, model: str,
                        max_tokens: int = None, usage=None, cache_prefix: str = None):
        """
        complete()와 같지만 응답을 조각(문자열)으로 나눠 yield 한다.
        조각 경계는 의미 단위가 아니므로, 호출부가 이어붙여 해석해야 한다.

        기본 구현은 complete()를 그대로 한 조각으로 내보낸다 —
        스트리밍을 지원하지 않는 프로바이더도 같은 인터페이스로 쓸 수 있게.
        """
        yield self.complete(prompt, api_key, model, max_tokens, usage=usage,
                            cache_prefix=cache_prefix)

    @abstractmethod
    def describe_image(self, png_bytes: bytes, api_key: str, model: str,
                       usage=None, prompt: str = None,
                       max_tokens: int = None) -> str:
        """
        이미지를 vision 모델에 보내 한국어 텍스트를 받는다.

        prompt: 생략하면 IMAGE_DESC_PROMPT(그림이 무엇인지 설명).
                IMAGE_TEXT_PROMPT를 주면 그림 속 글자만 그대로 옮긴다.
        max_tokens: 생략하면 프로바이더 기본값. 하는 일에 따라 필요량이 크게 달라서
                호출부가 지정한다 — 그림 설명은 짧지만, 글자 전사는 빽빽한 슬라이드
                한 장을 통째로 옮겨야 한다.
                ⚠️ 사고(thinking)를 하는 모델은 그 토큰도 이 한도에 포함된다.
                   빠듯하게 잡으면 사고가 예산을 먹고 본문이 단어 중간에서 잘린다.
        """

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
