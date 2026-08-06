// ══════════════════════════════════════════════
// auth.js — Google 로그인 위젯 (헤더 우측)
//   common.js 이후 로드. /me 로 로그인 상태를 확인해 위젯을 그린다.
//   로그인/로그아웃은 전체 페이지 이동/리로드 → 세션/오답 목록이 자동으로
//   현재 사용자 데이터로 다시 로드됨(스크립트 간 결합 불필요).
// ══════════════════════════════════════════════

async function loadAuth() {
  const el = document.getElementById('auth-widget');
  if (!el) return;
  try {
    const r = await fetch('/me');
    const d = await r.json();
    renderAuth(d);
    authMaybeShowWelcome(d);
  } catch (e) {
    el.innerHTML = '';
  }
}

// Google G 로고 — 마크 정의(<symbol id="google-g">)는 index.html에 한 벌만 두고 여기서 참조한다.
// 흰 타일에 얹는 이유: 헤더가 진한 파랑 그라데이션이라 컬러 로고가 그대로는 묻힌다.
const GOOGLE_G_HTML =
  '<span style="display:inline-flex;align-items:center;justify-content:center;' +
  'width:20px;height:20px;background:#fff;border-radius:4px;flex-shrink:0;">' +
  '<svg width="14" height="14" aria-hidden="true"><use href="#google-g" /></svg></span>';

function renderAuth(d) {
  const el = document.getElementById('auth-widget');
  if (!el) return;
  const btn = 'padding:6px 12px;border:1px solid #cfd6c9;border-radius:8px;' +
              'background:#ffffff;color:#2f5c50;font-size:0.8rem;font-weight:700;' +
              'font-family:inherit;cursor:pointer;';
  if (d && d.user) {
    const u = d.user;
    const avatar = u.picture
      ? `<img src="${escHtml(u.picture)}" alt="" referrerpolicy="no-referrer" style="width:26px;height:26px;border-radius:50%;vertical-align:middle;" />`
      : '👤';
    // 이름 폭은 .auth-name 이 묶는다 — 이름이 없어 이메일이 들어오면 좁은 화면에서
    // 위젯이 헤더 밖으로 밀려 나가기 때문. 잘린 값은 title 로 남겨 둔다.
    const label = u.name || u.email || '사용자';
    el.innerHTML =
      `${avatar}` +
      `<span class="auth-name" title="${escHtml(label)}">${escHtml(label)}</span>` +
      `<button onclick="authLogout()" style="${btn}">로그아웃</button>`;
  } else if (d && d.login_enabled) {
    // 라벨은 '로그인'만 — 좁은 헤더에서 위젯 폭이 곧 제목을 밀어내는 요인이라
    // 'Google로'는 뺐다. 어디로 로그인하는지는 왼쪽 G 로고가 이미 말해 준다.
    el.innerHTML = `<button onclick="authLogin()" style="${btn}display:inline-flex;align-items:center;gap:7px;">${GOOGLE_G_HTML}로그인</button>`;
  } else {
    // 로그인 미설정(secret_config 비어있음) → 위젯 숨김
    el.innerHTML = '';
  }
}

function authLogin() {
  location.href = '/login/google';   // 서버 리다이렉트 플로우 시작
}

async function authLogout() {
  try { await fetch('/logout', { method: 'POST' }); } catch (e) { /* 무시 */ }
  location.reload();                 // 게스트 상태로 다시 로드
}

// ══════════════════════════════════════════════
// 첫 방문 안내 모달 — "로그인하면 계정에 저장, 비로그인은 이 브라우저에만 임시 보관"
//   /me 응답만으로 판단하므로 추가 API가 필요 없다.
//   표시 여부는 localStorage에 남긴다(서버 저장 X — 브라우저마다 한 번씩 뜬다).
// ══════════════════════════════════════════════

// 문구를 크게 고쳐 다시 알려야 할 때는 뒤의 버전을 올린다 (예: _v2).
const WELCOME_KEY = 'mcu_welcome_seen_v1';

// localStorage는 시크릿 모드·차단 설정에서 예외를 던질 수 있다.
// 안내가 매번 뜨는 건 괜찮지만 앱이 멈추면 안 되므로 전부 삼킨다.
function authWelcomeSeen() {
  try { return localStorage.getItem(WELCOME_KEY) === '1'; }
  catch (e) { return false; }
}

function authMarkWelcomeSeen() {
  try { localStorage.setItem(WELCOME_KEY, '1'); } catch (e) { /* 무시 */ }
}

function authMaybeShowWelcome(d) {
  // 로그인 미설정(secret_config 비어있음) → 로그인 버튼 자체가 없으므로 안내도 무의미
  if (!d || !d.login_enabled) return;
  // 이미 로그인한 사용자에게는 띄우지 않는다.
  // 플래그를 심어두면 나중에 로그아웃했을 때 갑자기 팝업이 뜨지 않는다.
  if (d.user) { authMarkWelcomeSeen(); return; }
  if (authWelcomeSeen()) return;
  const modal = document.getElementById('welcome-modal');
  if (modal) modal.classList.add('open');
}

// 두 버튼 모두 "사용자가 선택을 마쳤다"는 뜻이므로 다시 띄우지 않는다.
function authCloseWelcome() {
  authMarkWelcomeSeen();
  const modal = document.getElementById('welcome-modal');
  if (modal) modal.classList.remove('open');
}

function authWelcomeLogin() {
  authMarkWelcomeSeen();
  authLogin();
}

// ── 초기 로드 ──
loadAuth();
