// ══════════════════════════════════════════════
// 홈(시작) 화면 — 이어서 하기 + 기능 목록 + 최근 활동
//   common.js 이후 로드. escHtml/switchTab 은 common.js 것을 재사용.
//   서버에 새 API를 추가하지 않고 기존 /sessions, /wrong-folders, /me 만 조합한다.
// ══════════════════════════════════════════════

// 홈에서 "이 세션으로 바로 생성"을 누르면 생성기 탭에서 이 세션을 선택한 상태로 연다
let homeResumeSession = null;

async function loadHome() {
  const [sessions, folders, me] = await Promise.all([
    fetchJson('/sessions').then(d => d.sessions || []),
    fetchJson('/wrong-folders').then(d => d.folders || []),
    fetchJson('/me').catch(() => ({})),
  ]);

  renderHomeGreeting(me, sessions);
  renderResumeCard(sessions[0]);
  renderHomeCounts(sessions, folders, await countPapers(sessions));
  renderHomeRecent(sessions, folders);
}

// 보관함 요약(시험지 수·문제 수) — 세션별 회차 목록을 합산한다
async function countPapers(sessions) {
  try {
    const lists = await Promise.all(sessions.map(s =>
      fetchJson('/session/' + s.id + '/generations').then(d => d.generations || [])));
    const all = lists.flat();
    return { count: all.length, questions: all.reduce((n, g) => n + (g.num_questions || 0), 0) };
  } catch (err) {
    return { count: 0, questions: 0 };
  }
}

async function fetchJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(url + ' ' + resp.status);
  return resp.json();
}

function renderHomeGreeting(me, sessions) {
  const name = me && me.user && me.user.name ? me.user.name : null;
  document.getElementById('home-hello').textContent =
    name ? `안녕하세요, ${name}님` : '안녕하세요';

  const sub = document.getElementById('home-subtitle');
  if (!sessions.length) {
    sub.textContent = '강의자료와 기출문제를 올리면 예상문제를 만들어 드려요';
  } else if (!name) {
    sub.textContent = '로그인하면 다른 기기에서도 이어서 볼 수 있어요';
  } else {
    sub.textContent = `저장된 세션 ${sessions.length}개 — 분석 없이 바로 생성할 수 있어요`;
  }
}

function renderResumeCard(latest) {
  const resume = document.getElementById('resume-card');
  const welcome = document.getElementById('welcome-card');

  // 세션이 하나도 없으면 '이어서 하기' 대신 첫 사용자 안내를 보여준다
  resume.classList.toggle('hidden', !latest);
  welcome.classList.toggle('hidden', !!latest);
  if (!latest) return;

  homeResumeSession = latest;
  document.getElementById('resume-name').textContent = latest.name || '이름 없는 세션';

  const parts = [relativeDay(latest.created_at)];
  const compo = formatTypeCounts(latest.type_stats);
  if (compo !== '해당 없음') parts.push('기출 ' + compo);
  parts.push('재분석 없이 바로 생성');
  document.getElementById('resume-meta').textContent = parts.filter(Boolean).join(' · ');

  document.getElementById('resume-btn').onclick = () => openSessionFromHome(latest);
}

function openSessionFromHome(sess) {
  switchTab('generator');
  setActiveSession(sess.id, sess.name);   // question_gen.js — 세션 선택 상태로 전환
  loadSessions();
}

// 카드 설명은 두 줄 구성이라 <br>로 줄을 맞춘다 (숫자만 들어가므로 안전)
function renderHomeCounts(sessions, folders, papers) {
  document.getElementById('home-gen-desc').innerHTML = sessions.length
    ? `저장된 세션 ${sessions.length}개<br>분석 없이 바로 생성`
    : '강의자료로<br>예상문제 만들기';

  document.getElementById('home-archive-desc').innerHTML = papers.count
    ? `시험지 ${papers.count}장<br>문제 ${papers.questions}개`
    : '만든 문제<br>다시 보기';

  const items = folders.reduce((sum, f) => sum + (f.item_count || 0), 0);
  document.getElementById('home-wrong-desc').innerHTML = folders.length
    ? `폴더 ${folders.length}개<br>문제 ${items}개`
    : '틀린 문제<br>모아 보기';
}

function renderHomeRecent(sessions, folders) {
  // 세션과 오답 폴더를 시간순으로 섞어 최근 4건만
  const rows = [
    ...sessions.map(s => ({
      when: s.created_at, icon: '📄', name: s.name,
      meta: '문제 생성 세션',
      onClick: () => openSessionFromHome(s),
    })),
    ...folders.map(f => ({
      when: f.created_at, icon: '❌', name: f.name,
      meta: `오답 폴더 · 문제 ${f.item_count || 0}개`,
      onClick: () => { switchTab('wrong'); viewWrongFolder(f.id); },
    })),
  ].sort((a, b) => String(b.when).localeCompare(String(a.when))).slice(0, 4);

  const card = document.getElementById('home-recent-card');
  card.classList.toggle('hidden', !rows.length);
  if (!rows.length) return;

  const list = document.getElementById('home-recent-list');
  list.innerHTML = '';
  rows.forEach(r => {
    const btn = document.createElement('button');
    btn.className = 'recent-row';
    btn.innerHTML = `
      <span class="recent-icon">${r.icon}</span>
      <span class="recent-body">
        <span class="recent-name">${escHtml(r.name || '이름 없음')}</span>
        <span class="recent-meta">${escHtml(relativeDay(r.when))} · ${escHtml(r.meta)}</span>
      </span>
      <span class="recent-arrow">›</span>`;
    btn.onclick = r.onClick;
    list.appendChild(btn);
  });
}

// "2026-07-28 14:12" → 오늘/어제/n일 전 (그보다 오래되면 날짜 그대로)
function relativeDay(stamp) {
  if (!stamp) return '';
  const d = new Date(String(stamp).replace(' ', 'T'));
  if (isNaN(d)) return stamp;

  const startOf = x => new Date(x.getFullYear(), x.getMonth(), x.getDate());
  const days = Math.round((startOf(new Date()) - startOf(d)) / 86400000);
  if (days <= 0) return '오늘';
  if (days === 1) return '어제';
  if (days < 7) return `${days}일 전`;
  return stamp.split(' ')[0];
}

// ── 초기 로드 ── (앱을 켜면 홈부터)
loadHome();
