# 🤝 협업 가이드 (팀 4인)

내 기능과 팀원들 기능(로그인 · 오답노트 등)을 **충돌 없이 하나로 합치기** 위한 규칙 모음입니다.
한 파일을 여러 명이 동시에 고치는 지금 구조에서는 **규칙 > 실력**입니다. 아래를 지키면 병합 지옥을 피할 수 있어요.

---

## 1. 브랜치 전략 (가장 중요)

```
main  ← 항상 실행되는 안정 버전. 코드는 여기에 직접 커밋 금지 🚫
 ├─ feature/login          (로그인 담당)
 ├─ feature/wrong-note     (오답노트 담당)
 ├─ feature/question-gen   (문제 생성 담당)
 └─ feature/<기능이름>      (기타)
```

- 각자 **자기 `feature/...` 브랜치에서만** 작업한다.
- `main`에는 **Pull Request(PR)** 로만 합친다. **최소 1명 리뷰** 후 merge.
  (예외: 아래 **문서만 바꾸는 경우**)
- 기능 브랜치는 병합되면 삭제하고, 새 작업은 새 브랜치를 판다.

### 예외 — 문서(`.md`)만 바꿀 때는 `main`에 바로 push해도 된다

오타 수정이나 설명 보강까지 PR을 기다리면 문서가 낡은 채로 방치됩니다.
문서는 잘못돼도 앱이 죽지 않고 되돌리기도 쉬우니, 이 경우만 예외로 둡니다.

| 바꾼 것 | 어떻게 |
|---|---|
| **`.md` 파일만** (README·가이드·CONTRIBUTING 등) | ✅ `main`에 바로 push |
| 코드 (`.py` `.js` `.html` `.css`) | 🚫 PR + 리뷰 |
| 스크립트 (`.sh` `.bat`) · 설정 (`requirements.txt` `render.yaml` `.gitignore`) | 🚫 PR + 리뷰 |

```bash
git checkout main && git pull origin main
```

```bash
git add 문서이름.md && git commit -m "docs: 오타 수정"
```

```bash
git push origin main
```

**지킬 것 두 가지**

1. **문서와 코드를 한 커밋에 섞지 마세요.** 섞이면 코드가 리뷰 없이 들어갑니다.
   섞였다면 그 커밋은 PR로 올리세요.
   커밋 전에 `git status` 로 `.md` 만 올라가는지 확인하는 습관.
2. **push 전에 반드시 `git pull`.** 안 하면 거절당하거나 남의 작업 위에 얹힙니다.

> 문서라도 **팀 규칙 자체를 바꾸는 변경**(이 CONTRIBUTING.md의 규칙 조항 등)은
> 팀에 먼저 알리고 합의한 뒤 올리세요. 형식은 문서지만 내용은 약속입니다.

### 처음 한 번 (각자 자기 컴퓨터에서)
```bash
git checkout main
git pull origin main
git checkout -b feature/login        # 자기 기능 이름으로
```

### 매일 습관 (충돌을 "조금씩 자주" 없애기)
```bash
# 작업 시작 전: main의 최신 내용을 내 브랜치로 가져오기
git checkout main && git pull origin main
git checkout feature/login && git merge main   # 여기서 충돌 나면 지금 해결 (작을 때!)

# 작업 후: 자주 커밋 & 푸시
git add -A && git commit -m "feat: 로그인 폼 추가"
git push origin feature/login
```

> 💡 핵심: **하루에 한 번 이상 `git merge main`**. 일주일 몰아서 합치면 충돌이 산더미가 됩니다.

---

## 2. 커밋하면 안 되는 파일 (`.gitignore`)

이미 `.gitignore`를 추가해 두었습니다. 아래는 **절대 공유하지 않습니다.**

| 파일 | 이유 |
|---|---|
| `sessions.db` | 실행 시 자동 생성되는 **로컬 DB**. 바이너리라 병합 불가 → 충돌 주범. 각자 로컬에만 둔다. |
| `__pycache__/`, `*.pyc` | 파이썬 캐시. 사람마다 달라 불필요한 충돌 발생. |
| `전북대 llm api.txt`, `.env`, `*.key` | **API 키 등 비밀정보. 한 번이라도 push하면 기록에 영구히 남는다.** |
| `.claude/settings.local.json` | 개인 에디터 설정. |

### ⚠️ 이미 올라간 파일 추적 해제 (한 번만, 담당자 1명이 실행 후 push)
```bash
git rm --cached sessions.db
git rm -r --cached __pycache__
git rm --cached .claude/settings.local.json
git commit -m "chore: gitignore 추가 및 DB/캐시/개인설정 추적 해제"
git push origin main
```
> 이 커밋을 pull하면 각자의 로컬 `sessions.db`가 지워질 수 있습니다.
> DB는 앱을 쓰면 다시 생기는 **임시 데이터**라 괜찮지만, 저장해둔 세션이 아깝다면 pull 전에 `sessions.db`를 백업해 두세요.

---

## 3. DB 스키마 규칙 (백엔드 충돌 방지 핵심) 🗄️

모두가 같은 `sessions.db` 스키마를 공유하므로, 여기 규칙이 제일 중요합니다.

### 테이블 소유권 (누가 어떤 테이블을 책임지는가)

| 테이블 | 담당 기능 | 비고 |
|---|---|---|
| `sessions`, `generations` | 문제 생성 | 분석 결과·생성 이력 (`provider` 컬럼 = 어떤 LLM 제공사로 만들었는지) |
| `wrong_folders`, `wrong_items` | 오답노트 | 폴더 + 담긴 문제 |
| `users` (예정) | 로그인 | 아래 "연결 계약" 참고 |

### 규칙
1. **새 테이블은 자기 것만** 만든다. `CREATE TABLE IF NOT EXISTS`로 작성.
2. **기존 테이블에 컬럼 추가**가 필요하면 `_ensure_column()`(이미 `app.py`에 있음)으로 마이그레이션한다. `CREATE TABLE`을 고치지 말 것 — 기존 DB엔 반영 안 됨.
   ```python
   _ensure_column(conn, "sessions", "user_id", "INTEGER")
   ```
3. **공용 테이블(`sessions`,`generations`) 스키마 변경은 반드시 팀 채팅에 먼저 공유** 후, 한 사람이 대표로 수정한다.
4. `init_db()`는 공용 함수다. 여기에 자기 테이블 생성 코드를 추가할 땐 **파일 맨 아래 자기 블록**에 몰아서 넣어 서로 다른 줄을 건드리게 한다.

---

## 4. 기능 간 "연결 계약" (미리 합의할 것) 🔌

기능이 서로 데이터를 주고받는 지점만 합의해두면, 나머지는 독립적으로 진행할 수 있습니다.

### (A) 로그인 ↔ 세션 / 오답노트
- 로그인 붙이면 "이 사용자의 세션/오답만" 보여줘야 함.
- 방법: `sessions`와 `wrong_folders`에 `user_id INTEGER` 컬럼을 `_ensure_column`으로 추가.
- `list_sessions()`, `list_wrong_folders()` 등에 `user_id` 인자를 추가하는 **함수 시그니처 변경은 팀 공지 필수** (호출부가 여러 곳).

### (B) 오답노트 ↔ 문제 생성 (이미 연결됨 — 형식 고정)
- 오답노트는 생성된 문제 dict를 그대로 저장한다. **이 키 이름을 절대 바꾸지 말 것:**
  ```
  문제 · 선택지 · 정답 · 해설 · 함정포인트 · 유형
  ```
- 프런트의 `renderQuestions()`가 이 키를 읽어 렌더링하므로, 문제 생성 쪽에서 키를 바꾸면 오답노트가 깨진다.

### (C) 문제 유형 4분류 (공용 상수)
- `객관식 · 빈칸채우기 · 단답형 · 서술형` 은 백엔드(`QUESTION_TYPES`)·프런트(`TYPE_BADGE`) 양쪽에 있다.
- 유형을 추가/변경하려면 **양쪽을 같이** 고치고 팀에 공지한다.

### (D) LLM 제공사(provider) ↔ 세션/이력
- LLM 호출은 전부 `providers/` 계층을 거친다. `llm.py`·라우트는 구체 클래스를 import하지 말고
  `get_provider(name)`만 쓴다. (현재: 전북대 게이트웨이 · OpenAI · Anthropic · Google Gemini)
- **새 제공사 추가 절차** — OpenAI 호환 엔드포인트면 20줄이면 끝난다.
  1. `providers/<이름>_provider.py` 에 클래스 작성
     (OpenAI 호환이면 `OpenAICompatibleProvider` 상속 후 `base_url`만 지정,
      아니면 `Provider`를 직접 구현 — Anthropic이 그 예)
  2. `providers/factory.py`의 `_REGISTRY`에 **한 줄** 등록
  3. 프런트는 손댈 필요 없음 — `/providers` 응답을 그대로 렌더링하므로 버튼이 자동 생성된다
- **SDK 예외는 반드시 공통 예외로 번역**한다 (`providers/base.py`의 `ProviderAuthError` /
  `ProviderRateLimitError` / `ProviderError`). 라우트가 특정 SDK를 알면 안 되기 때문.
- `sessions`·`generations`에 `provider TEXT` 컬럼이 있다. **다중 제공사 지원 이전 데이터는
  NULL**이므로, 읽을 때는 `db.LEGACY_PROVIDER`(`jbnu_gateway`)로 간주한다
  (`question_gen.py`의 `_row_provider()`). 직접 `row["provider"]`를 읽지 말 것.
- 저장된 세션을 재사용할 때는 **그때 선택한 제공사**로 문제를 생성한다. 세션의 분석 자산
  (개념·예시문제·형식)은 순수 텍스트라 제공사와 무관하게 재사용 가능하며, `provider` 컬럼은
  "무엇으로 만들었는지"를 남기는 기록용이다. `model` 컬럼도 같은 규칙.

---

## 5. 코드 배치 규칙 (같은 줄을 안 건드리게) 🧩

파일 분리 전까지는, 아래 습관만으로도 충돌이 크게 준다.

### 백엔드 `app.py`
- 새 라우트/함수는 **관련 섹션의 끝**에 추가한다. (`# ── 오답 노트 ──` 처럼 주석 구획 유지)
- 기존 함수 **시그니처를 바꾸면** 반드시 팀 공지.

### 프런트 `index.html`
- 새 탭은 `switchTab()`의 배열 `['generator','wrong']`에 **항목만 추가** (예: `'login'`). 이 한 줄은 공용이니 병합 시 주의.
- 새 기능의 HTML은 **자기 `<div class="tab-panel" id="tab-xxx">`** 로 감싸 독립시킨다.
- 새 JS 함수는 **접두사**를 붙여 이름 충돌을 막는다: `authLogin()`, `wrongSave()` 등.
- 공용 유틸(`escHtml`, `delay` 등)은 **재정의하지 말고 재사용**한다.

---

## 6. 지금 병합 순서 (권장 로드맵) 🗺️

현재 상태: 오답노트 병합됨 · **모듈 분리 리팩터링 완료(7번)** · 로그인 미착수.

1. **이 리팩터링 + 인프라를 `main`에 반영** — `.gitignore`·DB/캐시 추적 해제·문서·파일 분리. (한 사람이 커밋 & 푸시)
2. **전원 `git pull`** 로 새 구조를 받는다 → 각자 `feature/...` 브랜치 생성.
3. **로그인 담당자**는 `features/auth.py` + `static/js/auth.js` **새 파일을 추가**하는 방식으로 작업.
   기존 파일은 거의 안 건드리므로(아래 8번 통합 지점만 주의) 충돌이 거의 없다.

---

## 7. 모듈 분리 구조 (✅ 완료 — 현재 구조) 🏗️

로그인이 아직 시작 전이라, **처음부터 분리된 구조에서 각자 작업**하도록 지금 리팩터링을 마쳤습니다.
각 기능이 **자기 파일 하나**를 담당하므로 이후로는 서로 다른 파일을 고쳐 충돌이 거의 사라집니다.

```
app.py                    # 앱 생성 + Blueprint 등록만 (거의 안 바뀜)
db.py                     # get_conn / _ensure_column / init_db (스키마 단일 소유처)
llm.py                    # LLM 호출·PDF 추출·프롬프트·파싱·분석 파이프라인
features/
  question_gen.py         # 세션/이력 CRUD + /generate, /sessions, /models ... (gen_bp)
  wrong_note.py           # 오답 CRUD + /wrong-folders, /wrong-items ...        (wrong_bp)
  auth.py                 # (로그인 담당자가 추가) /login, /logout ...          (auth_bp)
index.html                # HTML 뼈대 (CSS/JS는 아래 static 참조)
static/
  css/style.css
  js/common.js            # escHtml·탭 전환·문제 렌더링 등 공용 (제일 먼저 로드)
  js/question_gen.js
  js/wrong_note.js
  js/auth.js              # (로그인 담당자가 추가)
```

### 로그인 기능 추가 방법 (충돌 없이)
1. **백엔드** — `features/auth.py` 생성:
   ```python
   from flask import Blueprint, request, jsonify
   from db import get_conn
   auth_bp = Blueprint("auth", __name__)

   @auth_bp.route("/login", methods=["POST"])
   def login(): ...
   ```
   그리고 `app.py`에 **두 줄만** 추가 (등록부에 나란히):
   ```python
   from features.auth import auth_bp
   app.register_blueprint(auth_bp)
   ```
2. **DB** — 새 `users` 테이블 + `user_id` 컬럼은 `db.py`의 `init_db()`에 추가 (2번·3번 규칙).
3. **프런트** — `static/js/auth.js` 생성 후, `index.html` 스크립트 목록에 **한 줄** 추가:
   ```html
   <script src="/static/js/auth.js"></script>
   ```
   로그인 탭 UI가 필요하면 `<div class="tab-panel" id="tab-login">` 추가 + `common.js`의 `switchTab` 배열에 `'login'` 추가.

---

## 8. 충돌이 났을 때 (당황 금지) 🆘

```bash
git merge main
# CONFLICT (content): index.html ...

# 1) 충돌 파일을 연다. <<<<<<< ======= >>>>>>> 사이가 충돌 구간.
#    <<<<<<< HEAD        ← 내 코드
#    =======
#    >>>>>>> main        ← 상대 코드
# 2) 둘 중 하나를 고르거나 둘 다 살려 손으로 합친다. 마커 3줄은 지운다.
# 3) 저장 후:
git add index.html
git commit            # 병합 완료
```
- **판단 안 서면 상대(그 코드 작성자)에게 물어보고 합친다.** 임의로 지우지 말 것.
- 겁나면 병합 취소: `git merge --abort` 로 원상복구 후 다시 시도.

---

## 9. 커밋 메시지 컨벤션

| 접두사 | 용도 |
|---|---|
| `feat:` | 새 기능 |
| `fix:` | 버그 수정 |
| `docs:` | 문서 |
| `refactor:` | 동작 변화 없는 구조 개선 |
| `chore:` | 설정·빌드 등 잡무 |

예) `feat: 로그인 세션 유지 기능 추가`

---

## 10. 테스트 (`tests/`) 🧪

별도 설치 없이 그냥 실행한다. pytest 같은 프레임워크를 쓰지 않으므로 `requirements.txt`에 추가할 것도 없다.

```bash
python tests/test_usage.py
```

통과하면 `전부 통과`, 실패하면 어느 항목이 왜 틀렸는지 출력하고 종료 코드 1을 낸다.

| 파일 | 무엇을 지키는가 |
|---|---|
| `tests/test_usage.py` | LLM 토큰 사용량 수집 (`providers/usage.py`) |

**`providers/` 를 건드렸으면 push 전에 한 번 돌려보자.** API 키 없이도 돌아간다 — LLM SDK를 가짜로 바꿔서 확인하기 때문에 토큰도 0원도 안 든다.

프로바이더를 새로 추가할 때는 `complete` · `complete_stream` · `describe_image` 세 곳에서 `usage`에 기록하는지 확인할 것. 안 하면 화면의 토큰 사용량이 조용히 0으로 나온다.

### LLM을 호출하는 기능을 새로 만들 때 (사용량 배선) 💸

사용량은 **호출 경로를 따라 손으로 넘겨줘야** 잡힌다. 빠뜨려도 아무 오류가 안 나고 화면에 0으로 보이기 때문에, 새 기능이 LLM을 부른다면 아래를 확인하자. (`features/topic_analysis.py`가 그대로 이 모양이다)

1. 라우트 맨 위에서 `usage = UsageCollector()` — `try` **바깥**에 둔다. 오류로 끝나도 그때까지 쓴 만큼은 알려줘야 하므로.
2. 크레딧 과금 제공사용으로 첫 LLM 호출 **직전에** `credits_before = credits_snapshot(provider, api_key)`.
3. 단계가 바뀔 때마다 `usage.set_stage("...")` — 키는 `providers/usage.py`의 `STAGE_LABELS`에 함께 추가한다.
4. `llm.py` 함수에 `usage=usage`를 끝까지 넘긴다. 중간 함수 하나만 빠뜨려도 그 아래 호출이 통째로 안 잡힌다.
5. 성공·오류 응답 **양쪽**에 `usage`(토큰)와 `credits`(크레딧)를 실는다.
6. 프런트에서는 `common.js`의 `renderSpend(data, { boxId, noun })`로 그리고, 오류 문구에는 `spendSuffix(data)`를 덧붙인다. 결과 화면에 `<details class="usage-box" id="...">` 빈 상자만 두면 된다.
