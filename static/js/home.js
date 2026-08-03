// ══════════════════════════════════════════════
// 홈(시작) 화면 — 이어서 하기 + 기능 목록 + 최근 활동
//   common.js 이후 로드. escHtml/switchTab 은 common.js 것을 재사용.
//   서버에 새 API를 추가하지 않고 기존 /sessions, /wrong-folders, /me,
//   /topic-analyses 만 조합한다.
// ══════════════════════════════════════════════

// 홈에서 "이 세션으로 바로 생성"을 누르면 생성기 탭에서 이 세션을 선택한 상태로 연다
let homeResumeSession = null;

// 홈을 다시 부를 때마다 증가. 늦게 도착한 응답이 새 화면을 덮어쓰는 것을 막는다
// (switchTab이 홈에 들어올 때마다 loadHome을 부르므로 탭을 빠르게 오가면 겹친다).
let homeLoadSeq = 0;

async function loadHome() {
  const seq = ++homeLoadSeq;
  let sessions, folders, me, analyses;
  try {
    [sessions, folders, me, analyses] = await Promise.all([
      fetchJson('/sessions').then(d => d.sessions || []),
      fetchJson('/wrong-folders').then(d => d.folders || []),
      fetchJson('/me').catch(() => ({})),   // 로그인 정보는 없어도 홈은 동작한다
      // 최근 활동에 섞어야 하므로 첫 렌더 전에 받는다. 아래 countPapers와 달리
      // 요청이 하나뿐이라 늦출 이유가 없다.
      // 실패해도 홈 전체를 오류로 만들지 않는다 → null (0건과 구분되는 '조회 실패').
      fetchJson('/topic-analyses').then(d => d.analyses || []).catch(() => null),
    ]);
  } catch (err) {
    // 이걸 잡지 않으면 아래 render 함수들이 하나도 실행되지 않는다.
    // 이어서하기·최근활동은 마크업에서 hidden으로 시작하므로 셋 다 안 뜨고,
    // '기능 카드 4장만 덩그러니 있는 화면'이 나가면서 실패한 줄도 알 수 없다.
    showHomeLoadError();
    return;
  }
  if (seq !== homeLoadSeq) return;

  renderHomeGreeting(me, sessions);
  renderResumeCard(sessions[0]);
  renderHomeCounts(sessions, folders);
  renderHomeRecent(sessions, folders, analyses || []);

  // 시험지 수만 뒤늦게 채운다. 세션 수만큼 요청이 나가는 유일한 항목이라
  // 이것을 await로 묶으면 이미 알고 있는 세션 수·오답 수까지 같이 늦어진다.
  // (분석 건수는 위에서 이미 받아왔으므로 다시 조회하지 않는다)
  countPapers(sessions).then(papers => {
    if (seq !== homeLoadSeq) return;
    renderArchiveCount(papers, analyses === null ? null : analyses.length);
  });
}

// 불러오기 자체가 실패한 경우. 실패를 화면 어딘가에는 반드시 남긴다 —
// 아무 말 없이 '기능 카드만 덩그러니 있는 화면'이 나가면 사용자는 저장된 게
// 없는 것으로 오해하고, 실패한 줄도 모른다. (기능 카드는 정적 링크라
//  그대로 눌러 들어갈 수 있으므로 진입 자체를 막지는 않는다)
//
// 부제가 있으면 거기에 쓴다. 부제 외에는 아무것도 건드리지 않는다 —
// 탭을 오가다 실패한 경우 직전에 성공한 화면이 그대로 남아 있는데,
// 인사말만 지우면 이름은 사라지고 이어서하기 카드는 남는 어긋난 상태가 된다.
//
// 부제를 마크업에서 뺀 경우에는 최근 활동 카드를 빌려 쓴다. 저장된 내용을
// 못 읽었다는 말이 들어갈 자리로는 '내 기록'을 보여주는 이 카드가 가장 가깝다.
function showHomeLoadError() {
  const msg = '저장된 내용을 불러오지 못했습니다. 새로고침해 주세요.';

  const sub = document.getElementById('home-subtitle');
  if (sub) {
    sub.textContent = msg;
    sub.style.color = 'var(--danger)';
    return;
  }

  const card = document.getElementById('home-recent-card');
  if (!card) return;                  // 둘 다 없으면 알릴 자리가 없다
  card.classList.remove('hidden');
  const list = document.getElementById('home-recent-list');
  if (list) {
    list.innerHTML = '';
    const p = document.createElement('p');
    p.style.cssText = 'font-size:0.88rem;color:var(--danger);';
    p.textContent = msg;
    list.appendChild(p);
  }
}

// 보관함 요약(시험지 수·문제 수) — 세션별 회차 목록을 합산한다.
// 세션마다 요청이 따로 나가므로 '일부만' 실패할 수 있다. 실패를 0으로 접으면
// 시험지가 30장 있어도 아무것도 안 만든 사용자와 화면이 완전히 같아지므로,
// 성공한 것만 합산하고 실패 건수를 그대로 들고 나간다.
async function countPapers(sessions) {
  if (!sessions.length) return { count: 0, questions: 0, failed: 0, total: 0 };

  const results = await Promise.allSettled(sessions.map(s =>
    fetchJson('/session/' + s.id + '/generations').then(d => d.generations || [])));

  const ok = results.filter(r => r.status === 'fulfilled');
  const failed = results.length - ok.length;
  if (failed) {
    console.warn(`[home] 회차 목록을 ${failed}/${results.length}개 세션에서 불러오지 못했습니다.`,
                 results.filter(r => r.status === 'rejected').map(r => r.reason));
  }

  const all = ok.flatMap(r => r.value);
  return {
    count: all.length,
    questions: all.reduce((n, g) => n + (g.num_questions || 0), 0),
    failed,
    total: results.length,
  };
}

async function fetchJson(url) {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(url + ' ' + resp.status);
  return resp.json();
}

// 인사말 영역은 마크업에서 빼도 되게 두었다 (헤더에 이미 서비스 이름이 있어서
// 함께 두면 같은 말이 두 번 나온다). 없으면 조용히 건너뛴다 —
// 여기서 터지면 이 아래 렌더가 하나도 실행되지 않아 홈이 통째로 빈다.
function renderHomeGreeting(me, sessions) {
  const hello = document.getElementById('home-hello');
  const sub = document.getElementById('home-subtitle');
  if (!hello && !sub) return;

  const name = me && me.user && me.user.name ? me.user.name : null;
  if (hello) {
    hello.textContent = name ? `안녕하세요, ${name}님` : '안녕하세요';
  }
  if (!sub) return;

  sub.style.color = '';   // 직전 로드가 실패해 빨갛게 남아 있을 수 있다
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

// 생성 탭으로 이동. 반드시 설정(입력) 화면부터 보이게 한다 —
// showGenInput()을 거치지 않으면 직전에 만든 결과 화면이 그대로 떠서
// '새로 만들러 왔는데 예전 문제가 보이는' 상태가 된다.
//   fresh=true 면 물려 있던 세션도 해제한다 ('다른 자료로 시작').
function openGenerator(fresh) {
  if (fresh) clearSession();
  showGenInput();
  switchTab('generator');
}

function openSessionFromHome(sess) {
  showGenInput();
  switchTab('generator');
  setActiveSession(sess.id, sess.name);   // question_gen.js — 세션 선택 상태로 전환
  loadSessions();
}

// 보관함의 '분석한 주제' 상세로 바로 들어간다.
// 목록까지 함께 준비하는 이유 — 상세에서 '← 목록으로'를 누르면 목록 화면이 나오는데,
// 한 번도 연 적이 없으면 비어 있다. (둘 다 비동기지만 목록 쪽은 화면을 바꾸지 않아
//  나중에 끝나도 상세 화면을 덮지 않는다)
function openSavedTopicFromHome(a) {
  switchTab('archive');
  openArchiveTopics();     // archive.js — 목록 화면으로 전환 + loadSavedTopics()
  openSavedTopic(a.id);    // topic_archive.js — 다 불러오면 상세 화면으로 전환
}

// 카드 설명은 두 줄 구성이라 <br>로 줄을 맞춘다 (숫자만 들어가므로 안전)
// 보관함 칸은 여기서 건드리지 않는다 — 도착이 늦어 renderArchiveCount가 따로 채운다.
function renderHomeCounts(sessions, folders) {
  document.getElementById('home-gen-desc').innerHTML = sessions.length
    ? `저장된 세션 ${sessions.length}개<br>분석 없이 바로 생성`
    : '강의자료로<br>예상문제 만들기';

  const items = folders.reduce((sum, f) => sum + (f.item_count || 0), 0);
  document.getElementById('home-wrong-desc').innerHTML = folders.length
    ? `폴더 ${folders.length}개<br>문제 ${items}개`
    : '틀린 문제<br>모아 보기';
}

// 보관함 합계 — countPapers와 분석 건수가 도착한 뒤에 호출된다.
// 시험지·분석을 한 줄씩 보여준다 (보관함이 두 갈래이므로).
// 전부 실패한 경우를 '0장'과 반드시 구분한다. 일부만 실패하면 합계가
// 실제보다 적으므로 '이상'을 붙여 확정 수치인 척하지 않게 한다.
//   analyses: 분석 건수. null이면 조회 실패 (0건과 구분).
function renderArchiveCount(papers, analyses) {
  const el = document.getElementById('home-archive-desc');
  const papersDead = papers.failed && papers.failed === papers.total;

  // 시험지 줄 — 셀 수 없으면 아예 빼고 분석 줄만 남긴다
  const more = papers.failed ? ' 이상' : '';
  let paperLine = null;
  if (!papersDead && papers.count) paperLine = `시험지 ${papers.count}장${more}`;
  else if (!papersDead && !papers.failed) paperLine = null;   // 확실히 0장

  const topicLine = analyses ? `분석한 주제 ${analyses}건` : null;

  const lines = [paperLine, topicLine].filter(Boolean);
  if (lines.length) { el.innerHTML = lines.join('<br>'); return; }

  // 양쪽 다 0건이거나 둘 다 못 셌을 때
  el.innerHTML = (papersDead || papers.failed || analyses === null)
    ? '개수를 불러오지<br>못했습니다'
    : '만든 문제와<br>분석한 주제';
}

function renderHomeRecent(sessions, folders, analyses) {
  // 이름을 안 붙인 분석은 보관함과 **같은** '제N회'로 부른다 — 두 화면이 서로
  // 다르게 부르면 같은 것인지 알 수 없다. (규칙도 topic_archive.js와 동일:
  //  전체를 오래된 것부터 세어 제1회. 앞 회차를 지우면 번호가 밀리는 것도 같다)
  const nth = {};
  [...analyses].sort((a, b) => a.id - b.id).forEach((a, i) => { nth[a.id] = i + 1; });

  // 세 종류를 시간순으로 섞어 최근 5건만 (목록이 길어지면 홈이 아니라 보관함이 된다)
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
    ...analyses.map(a => ({
      when: a.created_at, icon: '🔍',
      name: (a.title || '').trim() || `제${nth[a.id]}회`,
      meta: `주제 분석 · 주제 ${a.num_topics || 0}개`,
      onClick: () => openSavedTopicFromHome(a),
    })),
  ].sort((a, b) => String(b.when).localeCompare(String(a.when))).slice(0, 5);

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
