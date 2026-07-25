# 🩺 의대 예상문제 생성기

강의자료 + 기출문제 PDF를 업로드하면 Claude AI가 새로운 예상문제를 자동 생성합니다.

## 📁 파일 구조
```
medical_quiz_app/
├── app.py            ← Flask 백엔드 (서버)
├── index.html        ← 프론트엔드 (웹페이지)
├── requirements.txt  ← 필요 패키지 목록
└── README.md
```

## 🚀 실행 방법

### 1. 패키지 설치
```powershell
pip install -r requirements.txt
```

### 2. 서버 실행
```powershell
python app.py
```

### 3. 브라우저 접속
```
http://localhost:5000
```

## 🔑 Claude API Key 발급
1. https://console.anthropic.com 접속
2. 회원가입 → API Keys → Create Key
3. `sk-ant-...` 형태의 키를 복사
4. 웹페이지의 API Key 입력란에 붙여넣기

## ⚙️ 작동 방식
1. PDF 2개 업로드 (강의자료, 기출문제)
2. 백엔드가 텍스트 추출 (pymupdf)
3. Claude API 3회 호출:
   - 1차: 강의자료 핵심 개념 구조화 추출
   - 2차: 기출문제 출제 패턴 분석
   - 3차: 위 결과를 조합해 예상문제 생성
4. 웹페이지에 문제 출력 + 정답/해설 확인 기능

## ⚠️ 주의사항
- API 키는 서버에 저장되지 않고 요청마다 사용됩니다

