// ══════════════════════════════════════════════
// auth.js — 구글 로그인 위젯 (헤더 우측)
//   common.js 이후 로드. /me 로 로그인 상태를 확인해 위젯을 그린다.
//   로그인/로그아웃은 전체 페이지 이동/리로드 → 세션/오답 목록이 자동으로
//   현재 사용자 데이터로 다시 로드됨(스크립트 간 결합 불필요).
// ══════════════════════════════════════════════

async function loadAuth() {
  const el = document.getElementById('auth-widget');
  if (!el) return;
  try {
    const r = await fetch('/me');
    renderAuth(await r.json());
  } catch (e) {
    el.innerHTML = '';
  }
}

function renderAuth(d) {
  const el = document.getElementById('auth-widget');
  if (!el) return;
  const btn = 'padding:6px 12px;border:1px solid rgba(255,255,255,0.6);border-radius:8px;' +
              'background:rgba(255,255,255,0.15);color:#fff;font-size:0.8rem;font-weight:700;' +
              'font-family:inherit;cursor:pointer;';
  if (d && d.user) {
    const u = d.user;
    const avatar = u.picture
      ? `<img src="${escHtml(u.picture)}" alt="" referrerpolicy="no-referrer" style="width:26px;height:26px;border-radius:50%;vertical-align:middle;" />`
      : '👤';
    el.innerHTML =
      `${avatar}` +
      `<span style="margin:0 8px;font-weight:600;vertical-align:middle;">${escHtml(u.name || u.email || '사용자')}</span>` +
      `<button onclick="authLogout()" style="${btn}">로그아웃</button>`;
  } else if (d && d.login_enabled) {
    el.innerHTML = `<button onclick="authLogin()" style="${btn}">🔐 구글로 로그인</button>`;
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

// ── 초기 로드 ──
loadAuth();
