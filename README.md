# 🩺 의대 예상문제 생성기

강의자료 + 기출문제 PDF를 업로드하면 LLM이 **기출 유형·형식을 반영한 새 예상문제**를 자동 생성합니다.
(전북대 LLM 플랫폼 게이트웨이 사용)

## ✨ 주요 기능
- 강의자료·기출문제 PDF 분석 → 예상문제 생성 (객관식/빈칸채우기/단답형/서술형 4분류)
- 기출 유형 비율에 맞춰 문제 유형 자동 배분 + 기출 반영 강도(1~10) 조절
- 기출문제의 그림/사진은 Vision LLM으로 설명을 남겨 문제에 활용
- **세션 저장**: 분석 결과를 저장해 재분석 없이 바로 재생성 (토큰 절약)
- **생성 이력**·**오답 노트**(폴더별 문제 모으기)

## 📁 파일 구조 (기능별 모듈 분리)
```
yama/
├── app.py                ← Flask 진입점 (앱 생성 + Blueprint 등록)
├── serve.py              ← 배포용 실행 진입점 (waitress WSGI)
├── config.py             ← 비밀값·배포 설정 로더 (환경변수)
├── db.py                 ← DB 연결·스키마 (init_db)
├── llm.py                ← LLM 호출·PDF 추출·프롬프트·파싱·분석
├── features/
│   ├── question_gen.py   ← 문제 생성 라우트 (/generate, /sessions, /models ...)
│   ├── wrong_note.py     ← 오답 노트 라우트 (/wrong-folders ...)
│   └── auth.py           ← Google 로그인 + 게스트 익명 id
├── index.html            ← 프론트엔드 뼈대
├── static/
│   ├── css/style.css
│   └── js/{common,question_gen,wrong_note}.js
├── deploy/               ← 클라우드별 자동 설치 스크립트
│   ├── oracle/           ← 오라클 클라우드 ({setup,update,backup}.sh, mcu.service, Caddyfile)
│   └── gcp/              ← GCP (구성 동일)
├── render.yaml           ← Render 배포 설정
├── sessions.db           ← 로컬 DB (자동 생성, git 제외)
├── requirements.txt      ← 필요 패키지
├── 설치.bat / 실행.bat    ← 더블클릭 설치·실행 (Windows)
├── 배포.md               ← 배포 가이드 (개요·공통 절차)
├── 배포-GCP.md           ← GCP 배포 가이드 (현재 운영 중인 방식)
├── 배포-오라클.md         ← 오라클 배포 가이드 (미사용 · 대비책)
├── 서버_가이드.md         ← 🖥️ 배포된 서버 다루기 (팀원용)
├── README.md
└── CONTRIBUTING.md       ← 🤝 팀 협업 규칙 (병합·브랜치·DB·기능 추가법)
```

## 🚀 실행 방법

### 1. 패키지 설치
```powershell
pip install -r requirements.txt
```
(또는 Windows에서 `설치.bat` 더블클릭)

### 2. 서버 실행
```powershell
python app.py
```
(또는 `실행.bat` 더블클릭 — 브라우저가 자동으로 열립니다)

### 3. 브라우저 접속
```
http://localhost:5000
```

## 🔑 API Key
- 전북대 LLM 플랫폼에서 발급받은 키를 웹페이지의 **API Key 입력란**에 붙여넣습니다.
- 키를 입력하면 사용 가능한 **모델 목록이 자동 로드**됩니다.

## ⚙️ 작동 방식
1. PDF 2개 업로드 (강의자료 · 기출문제)
2. 텍스트 추출 (pymupdf) — 기출의 이미지 페이지는 Vision LLM으로 설명 생성
3. 분석 LLM 호출 (강의 핵심개념 · 기출 개념/유형 · 예시추출 · 형식분석) → **세션 저장**
4. 분석 자산을 조합해 예상문제 생성 → 화면 출력 + 정답/해설/오답노트

## 📚 문서 안내

| 문서 | 언제 보나 |
|---|---|
| [팀원_가이드.md](팀원_가이드.md) | 코드를 짜서 GitHub에 올릴 때 |
| [서버_가이드.md](서버_가이드.md) | **올린 코드를 실제 사이트에 반영할 때** · 서버가 이상할 때 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 브랜치·DB 스키마·기능 추가 규칙 |
| [배포.md](배포.md) | 배포 개념·환경변수 전반 |
| [배포-GCP.md](배포-GCP.md) | 서버를 처음부터 새로 만들 때 (**현재 이 방식으로 운영 중**) |
| [배포-오라클.md](배포-오라클.md) | 미사용. GCP의 RAM 1GB가 부족해지면 옮길 곳 |

> 💡 **평소에 볼 문서는 위의 두 개**입니다 — 코드 올릴 땐 `팀원_가이드`, 사이트에 반영할 땐 `서버_가이드`.
> 배포 문서 3개는 서버를 새로 만들 일이 생겼을 때만 보면 됩니다.

## ⚠️ 주의사항
- API 키는 서버에 저장되지 않고 요청마다 사용됩니다.
- `sessions.db`·API 키 파일은 **git에 커밋하지 않습니다** (`.gitignore` 참고).

## 🌐 인터넷에 공개하려면
배포 절차·환경변수·주의사항은 **[배포.md](배포.md)** 를 참고하세요.
로컬 실행 방식(`실행.bat`)은 배포와 무관하게 지금 그대로 동작합니다.

```bash
python serve.py
```

## 🤝 팀 작업 중이라면
브랜치·병합·DB 스키마 충돌 방지 규칙은 **[CONTRIBUTING.md](CONTRIBUTING.md)** 를 먼저 읽어주세요.
