# 🌐 Google Cloud 무료 배포 가이드

Google Cloud의 **Always Free** VM(Compute Engine)에 올리는 절차입니다.
진짜 서버라서 **코드를 한 줄도 바꾸지 않고** SQLite를 그대로 씁니다.

전체 소요: 처음이면 30~40분

> 일반적인 배포 개념·환경변수 설명은 [배포.md](배포.md)를 먼저 읽으세요.

**Oracle Cloud([배포-오라클.md](배포-오라클.md))와 뭐가 다른가**

|  | GCP 무료 | Oracle 무료 |
|---|---|---|
| RAM | **1GB** | 12GB |
| 리전 | 미국만 (한국↔미국 ~150ms) | 서울 선택 가능 (~5ms) |
| 콘솔 | 구글 OAuth와 **같은 콘솔** | 별도 계정 |
| 인스턴스 확보 | 항상 가능 | ARM 용량 부족이 잦음 |

하드웨어만 보면 Oracle이 낫습니다. GCP를 고르는 이유는 **구글 로그인 자격증명을 이미
Google Cloud Console에서 쓰고 있어 관리 지점이 하나로 합쳐진다**는 점입니다.
지연시간 150ms는 이 앱에서 체감되지 않습니다 — 문제 생성 한 번에 1~3분이 걸리니까요.

대신 **RAM 1GB는 실제 제약**입니다. 아래 [e2-micro 메모리](#e2-micro-메모리-1gb) 절을 꼭 읽으세요.

---

## ⚠️ 시작하기 전에 — 미국 IP에서 LLM 게이트웨이가 열려 있나

이 앱의 핵심 기능은 전북대 LLM 게이트웨이(`factchat-cloud.mindlogic.ai`)를 호출합니다.
**GCP 무료 티어는 미국 리전뿐**이라, 게이트웨이가 국내 IP만 허용한다면
서버를 다 만들어놓고도 문제 생성이 전부 실패합니다.

**5분이면 미리 확인할 수 있습니다:** 미국 서버로 VPN을 켠 상태에서 로컬(`실행.bat`)로
문제 생성을 한 번 돌려보세요. 정상 동작하면 이 가이드를 그대로 진행하면 됩니다.

막히면 → [게이트웨이가 막힐 때](#게이트웨이가-막힐-때--서울-리전으로) 로 가세요.

(설치 스크립트도 마지막 단계에서 이 연결을 자동으로 점검하고 경고를 띄웁니다)

---

## 준비물

- 신용/체크카드 (무료 티어를 쓰더라도 결제 계정 등록은 필요합니다)
- GitHub에 코드가 push되어 있을 것
- 내 PC에 **gcloud CLI** — <https://cloud.google.com/sdk/docs/install>
  (콘솔 웹 UI로만 해도 되지만, 명령 한 줄이 훨씬 빠르고 무료 조건을 틀릴 일이 없습니다)

---

## ⚠️ 무료 조건 — 셋 중 하나만 어겨도 과금됩니다

| 항목 | 반드시 이 값 | 어기면 |
|---|---|---|
| 리전 | `us-west1` / `us-central1` / `us-east1` | 서울 등 다른 리전은 **전액 과금** |
| 머신 타입 | `e2-micro` | e2-small 이상은 과금 |
| 부팅 디스크 | `pd-standard`, 30GB 이하 | **기본값이 `pd-balanced` 라 그냥 만들면 과금됨** ← 가장 흔한 실수 |

이그레스는 월 1GB까지 무료입니다. 이 앱은 정적 자원 전체가 88KB이고 Caddy가 압축해서
보내므로 페이지 1회 로드가 약 30KB — 월 2만 회를 넘겨야 한도에 닿습니다. 신경 안 써도 됩니다.

한국에서 가장 가까운 무료 리전은 **`us-west1`(오레곤)** 입니다. 이 가이드는 이걸 씁니다.

---

## 1단계 — 프로젝트 + 예산 알림

1. <https://console.cloud.google.com> → 프로젝트 선택
   → **구글 로그인 OAuth 클라이언트를 만들어둔 그 프로젝트를 그대로 쓰는 걸 권장합니다.**
   (7단계에서 콘솔을 왔다 갔다 하지 않아도 됩니다)
2. **결제** → 결제 계정 연결 (카드 등록)
3. **결제 → 예산 및 알림 → 예산 만들기** → 금액 **$1**, 알림 50%/90%/100%

> 3번은 건너뛰지 마세요. 무료 조건을 하나 틀렸을 때 **한 달 뒤 청구서로 알게 되는 대신
> 며칠 안에 메일로 알게 됩니다.** 실제로 지출이 없으면 메일도 오지 않습니다.

4. Compute Engine API 활성화 (VM을 처음 만들 때 자동으로 물어봅니다)

---

## 2단계 — VM 만들기

내 PC에서 (`<프로젝트ID>` 를 본인 것으로 바꾸세요):

```bash
gcloud compute instances create mcu-app --project=<프로젝트ID> --zone=us-west1-b --machine-type=e2-micro --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-type=pd-standard --boot-disk-size=30GB --tags=http-server,https-server
```

출력의 `EXTERNAL_IP` 를 적어둡니다. (예: `34.82.xxx.xxx`)

> **콘솔에서 직접 만든다면** 위 표의 세 값(리전 `us-west1`, 머신 `e2-micro`,
> 부팅 디스크 **표준 영구 디스크** 30GB)을 손으로 맞추고,
> 네트워크 태그에 `http-server`, `https-server` 를 넣으세요.
> 부팅 디스크 유형은 기본이 균형 있는 영구 디스크(`pd-balanced`)라 **반드시 바꿔야 합니다.**

**외부 IP는 고정(static) 예약을 하지 않습니다.** 예약 IP는 무료 티어 적용이 불확실하고
미사용 시 시간당 과금됩니다. 임시 IP는 **재부팅으로는 바뀌지 않고**, VM을 정지→시작할 때만
바뀝니다. 그 경우에 대비해 6단계에서 DuckDNS 자동 갱신을 켜면 됩니다.

---

## 3단계 — 방화벽 열기

GCP는 VM 안의 OS 방화벽이 아니라 **VPC 방화벽 규칙**이 트래픽을 막습니다.
(Oracle과 반대라, 설치 스크립트가 iptables를 건드리지 않습니다)

```bash
gcloud compute firewall-rules create mcu-allow-web --allow=tcp:80,tcp:443 --target-tags=http-server,https-server
```

2단계에서 `--tags=http-server,https-server` 를 줬기 때문에 이 규칙이 VM에 붙습니다.

---

## 4단계 — 도메인 연결 (무료)

HTTPS 인증서를 받으려면 IP가 아니라 **도메인**이 필요합니다.
구글 로그인도 도메인이 있어야 콜백을 등록할 수 있습니다.

<https://www.duckdns.org> 접속 → 구글 계정으로 로그인 →
원하는 이름 입력 (예: `mcu-club`) → **add domain**
→ `current ip` 칸에 2단계의 **외부 IP**를 넣고 **update ip**

화면에 보이는 **token** 을 복사해두세요. 6단계에서 IP 자동 갱신에 씁니다. (선택)

이제 `mcu-club.duckdns.org` 가 서버를 가리킵니다. 무료이고 만료도 없습니다.

---

## 5단계 — 서버 접속

SSH 키를 따로 만들 필요가 없습니다. gcloud가 알아서 처리합니다.

```bash
gcloud compute ssh mcu-app --zone=us-west1-b
```

> 처음 실행하면 키를 자동 생성하며 암호를 물어봅니다. 그냥 Enter를 두 번 쳐도 됩니다.
> Windows에서 이 명령이 안 되면 콘솔의 VM 목록에서 **SSH** 버튼을 눌러 브라우저로 접속하세요.

---

## 6단계 — 설치 스크립트 실행

서버에 접속한 상태에서:

```bash
curl -fsSL https://raw.githubusercontent.com/cuteCrongCrong/MCU-summer/main/deploy/gcp/setup.sh -o setup.sh
```

```bash
sudo bash setup.sh mcu-club.duckdns.org
```

(마지막 인자를 **본인 도메인**으로 바꾸세요)

4단계의 DuckDNS 토큰이 있다면, IP가 바뀌어도 자동으로 따라가게 하려면 이렇게:

```bash
sudo DUCKDNS_TOKEN=여기에토큰 bash setup.sh mcu-club.duckdns.org
```

스크립트가 하는 일:

1. 파이썬·git·sqlite3·Caddy 설치
2. **swap 2GB 생성** ← RAM 1GB를 보완하는 안전망
3. 전용 사용자 `mcu` 로 `/opt/mcu` 에 코드 설치
4. DB를 `/var/lib/mcu/sessions.db` 에 분리 (코드와 데이터 분리)
5. `FLASK_SECRET_KEY` 자동 생성 → `/etc/mcu/mcu.env`
   (e2-micro에 맞춰 `MAX_UPLOAD_MB=30`, `SERVER_THREADS=4` 로 설정)
6. systemd 등록 → **재부팅해도 자동 시작**
7. Caddy가 Let's Encrypt 인증서를 자동 발급 → **https 적용**
8. **LLM 게이트웨이 도달 여부 점검** → 막혀 있으면 경고를 띄웁니다

끝나면 `https://mcu-club.duckdns.org` 로 접속됩니다.

> 인증서 발급에 10~60초 걸립니다. 바로 안 되면 잠깐 기다렸다 새로고침하세요.

---

## 7단계 — 구글 로그인 연결 (선택)

로그인 없이도 모든 기능이 동작하므로 나중에 해도 됩니다.

**7-1.** Google Cloud Console → 사용자 인증 정보 → OAuth 클라이언트 ID
→ **승인된 리디렉션 URI** 에 추가:

```
https://mcu-club.duckdns.org/login/google/callback
```

기존 `http://localhost:5000/login/google/callback` 은 **지우지 마세요.**
로컬 개발에 계속 필요합니다.

**7-2.** 서버에서 값을 채웁니다:

```bash
sudo nano /etc/mcu/mcu.env
```

`GOOGLE_CLIENT_ID=` 와 `GOOGLE_CLIENT_SECRET=` 뒤에 값을 붙여넣고 저장
(`Ctrl+O` → `Enter` → `Ctrl+X`), 그다음:

```bash
sudo systemctl restart mcu
```

---

## e2-micro 메모리 (1GB)

이 가이드에서 **유일하게 Oracle보다 조심해야 하는 부분**입니다.

[llm.py](llm.py)의 PDF 처리 경로는 메모리를 많이 씁니다:

- 업로드된 PDF **전체**를 메모리에 올립니다 (`file_storage.read()`)
- 이미지 페이지를 150DPI로 최대 15장 렌더합니다 (A4 1장 ≈ 6.5MB)
- 그 작업을 4개 동시에 돌립니다

그래서 설치 스크립트가 세 가지로 대응합니다:

| 대응 | 값 | 이유 |
|---|---|---|
| swap | 2GB | RAM이 모자랄 때 죽는 대신 느려지게 |
| `MAX_UPLOAD_MB` | 30 (기본은 100) | 애초에 큰 파일을 못 올리게 |
| `SERVER_THREADS` | 4 (기본은 16) | 동시에 처리하는 건수를 제한 |

**메모리 상태 확인:**

```bash
free -h
```

**OOM으로 죽은 적이 있는지 확인:**

```bash
sudo journalctl -k --no-pager | grep -i 'out of memory'
```

죽어도 `Restart=always` 덕분에 서비스는 몇 초 만에 되살아납니다. 다만 **그 요청 하나는 실패**합니다.

### 그래도 부족하면 → e2-small (유료, 월 $15~18)

머신 타입만 바꾸면 됩니다. **디스크·설정·DB는 그대로 유지**됩니다.

```bash
gcloud compute instances stop mcu-app --zone=us-west1-b
```

```bash
gcloud compute instances set-machine-type mcu-app --zone=us-west1-b --machine-type=e2-small
```

```bash
gcloud compute instances start mcu-app --zone=us-west1-b
```

RAM이 2GB가 되므로 `/etc/mcu/mcu.env` 의 `MAX_UPLOAD_MB` 와 `SERVER_THREADS` 를
원래 기본값(100 / 16)에 가깝게 올려도 됩니다. 수정 후 `sudo systemctl restart mcu`.

> ⚠️ VM을 정지→시작하면 **외부 IP가 바뀝니다.** DuckDNS를 다시 갱신하거나,
> 6단계의 `DUCKDNS_TOKEN` 자동 갱신을 켜두세요.

---

## 게이트웨이가 막힐 때 — 서울 리전으로

미국 IP가 차단돼 문제 생성이 실패한다면, 무료 티어는 포기하고 서울 리전으로 갑니다.
**이 가이드의 스크립트는 그대로 쓰고, VM 생성 명령의 리전·머신 타입만 바꾸면 됩니다.**

```bash
gcloud compute instances create mcu-app --project=<프로젝트ID> --zone=asia-northeast3-a --machine-type=e2-small --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud --boot-disk-type=pd-balanced --boot-disk-size=30GB --tags=http-server,https-server
```

월 $18 수준입니다. 대신 한국 IP + RAM 2GB라 메모리 걱정도 같이 사라집니다.
3단계 이후는 전부 동일하며, `--zone` 만 `asia-northeast3-a` 로 바꿔서 실행하세요.

> 같은 돈이면 Oracle Cloud 서울 리전이 무료입니다([배포-오라클.md](배포-오라클.md)).
> 유료로 갈 거라면 그쪽도 한 번 비교해보세요.

---

## 평소 운영

### 코드 업데이트 (로컬에서 push한 뒤)

```bash
sudo bash /opt/mcu/deploy/gcp/update.sh
```

DB와 설정은 건드리지 않고 코드만 갱신 후 재시작합니다.

### 상태·로그 확인

```bash
sudo systemctl status mcu
```

```bash
sudo journalctl -u mcu -f
```

### 백업 (권장)

```bash
sudo bash /opt/mcu/deploy/gcp/backup.sh
```

매일 자동 백업하려면 `sudo crontab -e` 후 아래 한 줄 추가:

```
0 4 * * * /bin/bash /opt/mcu/deploy/gcp/backup.sh >> /var/log/mcu-backup.log 2>&1
```

> WAL 모드를 쓰므로 `.db` 파일만 복사하면 **깨진 백업**이 됩니다.
> 반드시 이 스크립트(내부적으로 `sqlite3 .backup` 사용)를 쓰세요.

Cloud Storage에 백업본을 같이 올리고 싶다면(US 리전 5GB 무료)
[deploy/gcp/backup.sh](deploy/gcp/backup.sh) 상단 주석의 절차를 따르세요.

### VM 정지 (쓰지 않는 기간)

무료 티어라 켜둬도 비용은 없지만, 정지해두고 싶다면:

```bash
gcloud compute instances stop mcu-app --zone=us-west1-b
```

> 정지 후 다시 시작하면 **외부 IP가 바뀝니다.** DuckDNS를 갱신하세요.
> 디스크는 정지 중에도 30GB 무료 한도 안에 있으므로 데이터는 유지됩니다.

---

## 배포 후 확인 목록

| # | 확인할 것 | 방법 | 기대 |
|---|---|---|---|
| 1 | swap 적용 | `free -h` | Swap 2.0Gi |
| 2 | 서비스 기동 | `sudo systemctl status mcu` | active (running) |
| 3 | HTTPS | `https://<도메인>/healthz` | `{"status":"ok"}` + 자물쇠 |
| 4 | **문제 생성** | PDF 올려서 끝까지 생성 | 문제가 나옴 ← 게이트웨이 판정 |
| 5 | 메모리 한계 | 25~30MB PDF로 생성 | OOM 없이 완주 |
| 6 | 게스트 데이터 | 비로그인으로 오답노트 저장 → 브라우저 껐다 재접속 | 데이터 남아 있음 |
| 7 | 구글 로그인 | 로그인 → 복귀 | `redirect_uri_mismatch` 없음 |
| 8 | 재부팅 복구 | `sudo reboot` 후 재접속 | 자동 기동 |
| 9 | 비용 | 며칠 뒤 결제 → 비용 | $0 |

---

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| 접속 자체가 안 됨 (타임아웃) | 3단계 방화벽 규칙을 만들었는지, VM에 `http-server` 태그가 붙었는지 |
| `502 Bad Gateway` | 앱이 죽음 → `sudo journalctl -u mcu -n 50` |
| 인증서 발급 실패 | DuckDNS IP가 VM의 현재 외부 IP와 일치하는지, 80 포트가 열렸는지 |
| 구글 로그인 `redirect_uri_mismatch` | 7-1의 URI를 **https로** 정확히 등록했는지 |
| 문제 생성이 항상 실패 | 게이트웨이 차단 의심 → `curl -I https://factchat-cloud.mindlogic.ai/` 로 확인 |
| 큰 PDF에서만 끊김 | 메모리 부족 → 위 [e2-micro 메모리](#e2-micro-메모리-1gb) 절 |
| 며칠 뒤 요금이 청구됨 | 무료 조건 표의 세 값(리전·머신·디스크 유형)을 다시 확인 |
| 재부팅 후 안 뜸 | `sudo systemctl enable mcu` 확인 |
| VM 재시작 후 접속 안 됨 | 외부 IP가 바뀜 → DuckDNS 갱신 |

### 시크릿 키에 대한 경고

`/etc/mcu/mcu.env` 의 `FLASK_SECRET_KEY` 를 **바꾸지 마세요.**
바뀌는 순간 모든 쿠키 서명이 무효가 되어, 비로그인 사용자들이 자기가 만든
세션·오답노트에 영영 접근할 수 없게 됩니다.
(설치 스크립트를 다시 돌려도 기존 키는 그대로 유지됩니다)

---

## 비용이 정말 0인지

콘솔 → **결제** → **보고서** 에서 확인합니다.

무료 조건을 지켰다면 e2-micro 인스턴스·30GB 표준 디스크·외부 IP 모두 $0으로 잡힙니다.
1단계에서 예산 알림을 걸어뒀다면, 뭔가 잘못됐을 때 며칠 안에 메일이 옵니다.

가장 흔한 과금 원인은 **부팅 디스크 유형**입니다. 확인:

```bash
gcloud compute disks describe mcu-app --zone=us-west1-b --format="value(type)"
```

끝이 `pd-standard` 가 아니면 과금 중입니다. 디스크 유형은 나중에 바꿀 수 없으므로,
VM을 지우고 2단계부터 다시 만드는 편이 빠릅니다. (DB는 미리 백업하세요)
