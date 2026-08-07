// ══════════════════════════════════════════════
// 보관함 — 「생성한 문제」와 「분석한 주제」로 갈라지는 탭.
//   이 파일은 갈림길(허브) + '생성한 문제' 쪽을 담당한다.
//   '분석한 주제' 쪽은 topic_archive.js.
//   common.js 이후 로드. renderQuestions/escHtml/formatTypeCounts 는 common.js 것을 재사용.
//   서버에 새 API를 추가하지 않고 기존 /sessions, /session/<id>/generations,
//   /generation/<gid> 만 조합한다.
// ══════════════════════════════════════════════

let archiveRows = [];        // 전체 회차 (세션 정보를 붙여 평탄화)
let archiveLoaded = false;   // 탭을 오갈 때 재요청하지 않기 위한 캐시 플래그

// ── 갈림길(허브) ──
// 보관함 탭에 들어오면 항상 여기부터 보여준다. 안쪽 화면 4개를 한 번에 정리하므로
// 어느 화면에서 '← 보관함'을 눌러도 상태가 어긋나지 않는다.
const ARCHIVE_SUBVIEWS = [
  'archive-hub-view', 'archive-list-view', 'archive-view',
  'saved-topics-list-view', 'saved-topics-view',
];

function showArchiveSubview(id) {
  ARCHIVE_SUBVIEWS.forEach(v =>
    document.getElementById(v).classList.toggle('hidden', v !== id));
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function showArchiveHub() {
  showArchiveSubview('archive-hub-view');
  updateArchiveHubCounts();
}

function openArchivePapers() {
  showArchiveSubview('archive-list-view');
  loadArchive();
}

function openArchiveTopics() {
  showArchiveSubview('saved-topics-list-view');
  loadSavedTopics();
}

// 허브 카드 두 장의 설명을 실제 개수로 채운다.
// 실패하면 마크업의 기본 문구를 그대로 두고 넘어간다 — 카드는 눌러서 들어갈 수 있으므로
// 개수를 못 세는 것 때문에 진입을 막을 이유가 없다.
async function updateArchiveHubCounts() {
  const papersEl = document.getElementById('hub-papers-desc');
  const topicsEl = document.getElementById('hub-topics-desc');

  fetchJson('/sessions')
    .then(d => countPapers(d.sessions || []))     // home.js의 합산 함수 재사용
    .then(p => {
      if (p.failed && p.failed === p.total) return;
      if (!p.count) return;
      const more = p.failed ? ' 이상' : '';
      papersEl.innerHTML = `시험지 ${p.count}장${more}<br>문제 ${p.questions}개${more}`;
    })
    .catch(() => {});

  fetchJson('/topic-analyses')
    .then(d => {
      const rows = d.analyses || [];
      if (!rows.length) return;
      const topics = rows.reduce((n, r) => n + (r.num_topics || 0), 0);
      topicsEl.innerHTML = `분석 ${rows.length}건<br>주제 ${topics}개`;
    })
    .catch(() => {});
}

async function loadArchive(force) {
  if (archiveLoaded && !force) return renderArchive();

  // ↻ 새로고침으로 다시 받는 길. 새 시험지는 맨 앞에 들어와 보던 페이지가
  // 통째로 밀리므로 1페이지부터 보여준다.
  if (force) setPage('papers', 1);

  const grid = document.getElementById('archive-grid');
  grid.innerHTML = '<div style="color:#94a3b8;font-size:0.88rem;">불러오는 중…</div>';
  document.getElementById('archive-pager').innerHTML = '';
  try {
    const sessions = (await (await fetch('/sessions')).json()).sessions || [];
    // 세션마다 회차 목록을 병렬로 받아 한 배열로 합친다 (home.js와 같은 조합 패턴)
    const perSession = await Promise.all(sessions.map(async s => {
      const d = await (await fetch('/session/' + s.id + '/generations')).json();
      return (d.generations || []).map(g => ({ ...g, session_id: s.id, session_name: s.name }));
    }));

    // 정렬은 created_at이 아니라 id 내림차순 — created_at이 분 단위라
    // 같은 분에 만든 두 회차의 순서가 흔들린다. id는 생성 순서와 정확히 일치한다.
    archiveRows = perSession.flat().sort((a, b) => b.id - a.id);
    archiveLoaded = true;
    renderSessionFilter(sessions);
    renderArchive();
  } catch (err) {
    grid.innerHTML = '<div style="color:#dc2626;font-size:0.88rem;">목록을 불러오지 못했습니다.</div>';
    document.getElementById('archive-pager').innerHTML = '';
  }
}

// 강의자료 필터를 바꾸면 목록 자체가 달라져 보던 페이지 번호가 의미를 잃는다.
// (다른 목록의 3페이지는 아무 뜻이 없다. 범위를 벗어나는 것만이면 pageSlice가
//  당겨 주지만, 그래도 새 목록은 처음부터 보여주는 것이 맞다)
function archiveFilterChanged() {
  setPage('papers', 1);
  renderArchive();
}

// 세션이 2개 이상일 때만 필터를 보여준다 (1개면 고를 게 없다)
function renderSessionFilter(sessions) {
  const row = document.getElementById('archive-filter-row');
  const sel = document.getElementById('archive-session-filter');
  const used = sessions.filter(s => archiveRows.some(r => r.session_id === s.id));
  row.classList.toggle('hidden', used.length < 2);
  if (used.length < 2) return;

  const prev = sel.value;
  sel.innerHTML = '<option value="">전체 강의자료</option>' + used.map(s => {
    const n = archiveRows.filter(r => r.session_id === s.id).length;
    return `<option value="${s.id}">${escHtml(s.name)} (${n}장)</option>`;
  }).join('');
  if (prev) sel.value = prev;
}

// 시험지 id → 같은 세션 안에서 몇 번째로 만든 것인지 (오래된 것이 제1회).
// 앞 회차를 지우면 번호가 밀리므로, 카드에는 날짜를 항상 함께 둔다.
// ⚠️ 세는 대상은 화면에 그릴 몫이 아니라 archiveRows 전체다. 필터나 페이지로
//    잘린 배열로 세면 '제N회'가 페이지마다 1부터 다시 시작한다.
// 목록과 열람 화면이 같은 번호를 부르도록 한 곳에 둔다.
function paperOrdinals() {
  const ordinal = {};
  const seen = {};
  [...archiveRows].sort((a, b) => a.id - b.id).forEach(r => {
    seen[r.session_id] = (seen[r.session_id] || 0) + 1;
    ordinal[r.id] = seen[r.session_id];
  });
  return ordinal;
}

function renderArchive() {
  const grid = document.getElementById('archive-grid');
  const pager = document.getElementById('archive-pager');
  const sel = document.getElementById('archive-session-filter');
  const filter = sel && !document.getElementById('archive-filter-row').classList.contains('hidden')
    ? sel.value : '';
  const rows = filter ? archiveRows.filter(r => String(r.session_id) === filter) : archiveRows;

  const total = archiveRows.reduce((sum, r) => sum + (r.num_questions || 0), 0);
  document.getElementById('archive-summary').textContent =
    archiveRows.length ? `— 시험지 ${archiveRows.length}장 · 총 ${total}문제` : '';

  if (!rows.length) {
    grid.className = '';
    grid.innerHTML = `
      <div style="text-align:center;padding:28px 16px;color:#94a3b8;">
        <div style="font-size:1.8rem;">🗂️</div>
        <div style="margin-top:8px;font-size:0.92rem;">아직 만든 문제가 없습니다.</div>
        <div style="margin-top:4px;font-size:0.83rem;">문제 생성기에서 문제를 만들면 여기에 모입니다.</div>
      </div>`;
    pager.innerHTML = '';
    return;
  }

  const ordinal = paperOrdinals();

  // 여기서부터는 이 페이지 몫(items)만 그린다 (common.js — 분석한 주제 목록과 공용)
  const { page, totalPages, items } = pageSlice('papers', rows);

  grid.className = 'paper-grid';
  grid.innerHTML = items.map(r => {
    const tt = r.type_targets || {};
    const hasTt = Object.keys(TYPE_BADGE).some(t => (tt[t] || 0) > 0);
    const badges = hasTt
      ? Object.keys(TYPE_BADGE).filter(t => (tt[t] || 0) > 0)
          .map(t => `<span class="type-badge ${TYPE_BADGE[t][0]}">${TYPE_BADGE[t][1]} ${tt[t]}</span>`).join('')
      : '';
    // 요청한 개수보다 적게 저장됐으면 알려준다 (파싱 실패로 유실된 경우)
    const short = (r.count && r.num_questions < r.count)
      ? `<span class="paper-short">요청 ${r.count}개 중 ${r.num_questions}개</span>` : '';

    // 이름을 붙였으면 그 이름이 제목, 회차 번호는 메타로 내린다.
    const nth = `제${ordinal[r.id]}회`;
    const title = (r.title || '').trim();
    // 인쇄 버튼의 이름표. 카드마다 같은 '문제지'가 여러 개 놓이므로
    // 화면 낭독기에는 어느 시험지의 것인지까지 읽어 준다.
    const who = escHtml(title || nth);
    return `
      <div class="paper-card">
        <div class="paper-source">📄 ${escHtml(r.session_name || '이름 없는 강의자료')}</div>
        <div class="paper-title"><span>${escHtml(title || nth)}</span
          ><button class="title-edit-btn" onclick="renamePaper(${r.id})"
                   title="이름 변경" aria-label="이름 변경">✏️</button
          ><span class="paper-print-row"
            ><button class="paper-print-btn" onclick="printPaperFromList(${r.id},'q',this)"
                     title="문제지 인쇄 / PDF 저장" aria-label="${who} 문제지 저장">🖨 문제지</button
            ><button class="paper-print-btn" onclick="printPaperFromList(${r.id},'a',this)"
                     title="정답·해설 인쇄 / PDF 저장" aria-label="${who} 정답·해설 저장">🖨 정답·해설</button
          ></span>${short}</div>
        <div class="paper-meta">${title ? escHtml(nth) + ' · ' : ''}${escHtml(relativeDay(r.created_at))} ${escHtml((r.created_at || '').split(' ')[1] || '')} · ${r.num_questions}문항</div>
        ${badges ? `<div class="paper-badges">${badges}</div>` : ''}
        <div class="paper-actions">
          <button class="paper-open" onclick="openPaper(${r.id})">▶ 풀기</button>
          <button class="paper-del" onclick="deletePaper(${r.id})" title="삭제">🗑️</button>
        </div>
      </div>`;
  }).join('');

  pager.innerHTML = pagerHtml('papers', page, totalPages);
}

// ── 시험지 한 장 열람 ──

// 지금 상세 화면에 열려 있는 시험지. 인쇄 버튼이 쓴다.
// (인쇄는 화면 DOM이 아니라 이 questions 배열을 재료로 쓴다 — print.js 머리말 참고)
let currentPaper = null;

// 상세 헤더(제목·메타)를 그린다. 이름 변경은 목록에서만 하므로 여기서
// 다시 그릴 일은 없고, openPaper가 열 때 한 번 부른다.
function applyPaperHeader(gid, g) {
  const row = archiveRows.find(r => r.id === gid) || {};
  const title = (row.title || g.title || '').trim();
  // 이름이 없으면 목록 카드와 같은 '제N회'로 부른다. 강의자료 이름만 띄우면
  // 같은 자료로 만든 회차끼리 구분이 안 돼 어느 시험지를 연 건지 알 수 없었다.
  const nth = paperOrdinals()[gid];
  document.getElementById('archive-paper-title').textContent =
    `📄 ${title || (nth ? `제${nth}회` : row.session_name || '시험지')}`;
  // 제목 자리에 쓰이지 않은 쪽을 메타 줄로 내린다 (목록 카드와 같은 어법)
  document.getElementById('archive-paper-meta').textContent =
    [title && nth ? `제${nth}회` : '', row.session_name || '', g.created_at || '',
     `${(g.questions || []).length}문항`, `강도 ${g.weight}/10`,
     `${providerLabel(g.provider)} / ${g.model || ''}`].filter(Boolean).join(' · ');
}

async function openPaper(gid) {
  try {
    const resp = await fetch('/generation/' + gid);
    const g = await resp.json();
    if (!resp.ok || g.error) return alert(g.error || '시험지를 불러오지 못했습니다.');

    applyPaperHeader(gid, g);
    // 인쇄용. buildPaperMeta 는 print.js 것으로, 화면 헤더와 같은 재료를 쓴다.
    currentPaper = {
      gid,
      meta: buildPaperMeta(gid, g, archiveRows.find(r => r.id === gid) || {}),
      questions: g.questions || [],
    };

    // ns를 주지 않으면 생성기·오답노트 카드와 DOM id가 겹친다
    renderQuestions(g.questions, g.raw, {
      containerId: 'archive-questions-container',
      titleId: null,
      ns: 'arc-',
      paged: true,          // 생성 결과 화면과 같게 — 10문제씩 + 번호 바로가기
    });
    // 그때 쓴 토큰·크레딧 (기록 이전 회차는 값이 없어 상자째 숨는다).
    // 세션을 재사용한 회차는 분석을 건너뛰어 사용량이 작게 나오는데, 그게 사실이다.
    renderSpend(g, { boxId: 'archive-usage-box', noun: '생성' });
    showArchiveSubview('archive-view');
  } catch (err) {
    alert('시험지를 불러오지 못했습니다.');
  }
}

function closeArchiveView() {
  showArchiveSubview('archive-list-view');
  currentPaper = null;   // 인쇄(print.js)가 참조하는 '지금 연 시험지' 상태도 정리
}

// ── 인쇄 (print.js) ──
// 이미 메모리에 있는 questions 만 쓴다. fetch 를 끼우면 await 뒤의 print() 가
// 사용자 제스처 컨텍스트를 잃어 iOS 에서 차단된다.
function printPaper(kind) {
  if (!currentPaper) return alert('시험지를 먼저 열어 주세요.');
  openPrintPreview(kind, currentPaper.questions, currentPaper.meta);
}

// 목록 카드에서 바로 인쇄 — '풀기'로 한 번 들어가야만 저장할 수 있던 것을 없앤다.
// 목록에는 문항이 없으므로 여기서만 fetch 한다. 위의 제스처 주의는 여기 해당하지 않는다:
// openPrintPreview 는 오버레이를 열 뿐이고 실제 window.print() 는 그 안의 버튼
// (= 진짜 사용자 제스처)이 부른다. 그래서 await 를 앞에 끼워도 iOS 가 막지 않는다.
const paperCache = {};   // gid → /generation 응답. 문항은 한 번 만들어지면 바뀌지 않는다.

async function printPaperFromList(gid, kind, btn) {
  if (btn && btn.disabled) return;          // 응답 오기 전 연타 방지
  if (btn) btn.disabled = true;
  try {
    let g = paperCache[gid];
    if (!g) {
      const resp = await fetch('/generation/' + gid);
      g = await resp.json();
      if (!resp.ok || g.error) return alert(g.error || '시험지를 불러오지 못했습니다.');
      paperCache[gid] = g;
    }
    // meta 는 캐시하지 않고 매번 만든다 — 이름을 바꾸면 archiveRows 만 갱신되므로,
    // 캐시해 두면 인쇄물 머리말에 옛 제목이 남는다.
    openPrintPreview(kind, g.questions || [], buildPaperMeta(gid, g, archiveRows.find(r => r.id === gid) || {}));
  } catch (err) {
    alert('시험지를 불러오지 못했습니다.');
  } finally {
    if (btn) btn.disabled = false;
  }
}

// 이름 변경 — 빈 값으로 두면 이름을 지워 '제N회' 표시로 되돌린다.
// 목록 카드의 제목 옆 연필에서만 호출된다(상세 화면에는 버튼이 없다).
async function renamePaper(gid) {
  const row = archiveRows.find(r => r.id === gid) || {};
  const cur = (row.title || '').trim();
  const next = prompt('이 문제 세트의 이름을 입력하세요.\n(비우면 「제N회」로 표시됩니다)', cur);
  if (next == null) return;              // 취소

  const form = new FormData();
  form.append('title', next.trim());
  const resp = await fetch('/generation/' + gid + '/rename', { method: 'POST', body: form });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) return alert(data.error || '이름을 바꾸지 못했습니다.');

  row.title = next.trim();               // 캐시도 갱신 (목록 재요청 없이 반영)
  renderArchive();
}

// 삭제도 목록 카드에서만 한다. 상세 화면(풀기)에는 버튼이 없으므로
// 삭제 시점에 상세 화면이 열려 있는 경우는 없다.
// 페이지 번호는 건드리지 않는다 — 보던 자리에 머무는 것이 자연스럽고,
// 마지막 장에 남은 하나를 지워 페이지가 사라지는 경우는 pageSlice가 당겨 준다.
async function deletePaper(gid) {
  if (!confirm('이 시험지를 삭제할까요? 담긴 문제도 함께 사라집니다.')) return;
  await fetch('/generation/' + gid, { method: 'DELETE' });
  archiveRows = archiveRows.filter(r => r.id !== gid);
  delete paperCache[gid];                // 지운 시험지를 캐시로 다시 인쇄하는 일이 없게
  renderArchive();
}
