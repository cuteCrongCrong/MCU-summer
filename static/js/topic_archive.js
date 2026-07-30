// ══════════════════════════════════════════════
// 보관함 · 분석한 주제 — 기출 주제 분석 결과를 모아 보고 다시 열어 본다.
//   archive.js(생성한 문제)와 같은 구조·같은 카드 모양을 쓴다.
//   결과 화면은 topic_analysis.js의 렌더러를 'saved' 뷰로 재사용한다 (중복 구현 없음).
//   common.js 이후 로드. escHtml/relativeDay/providerLabel 은 기존 것을 재사용.
// ══════════════════════════════════════════════

let savedTopicRows = [];        // 보관된 분석 목록 (메타만)
let savedTopicsLoaded = false;  // 탭을 오갈 때 재요청하지 않기 위한 캐시 플래그

// 새 분석이 저장되면 목록 캐시를 버린다 (topic_analysis.js가 호출)
function invalidateSavedTopics() { savedTopicsLoaded = false; }

async function loadSavedTopics(force) {
  if (savedTopicsLoaded && !force) return renderSavedTopics();

  const grid = document.getElementById('saved-topics-grid');
  grid.className = '';
  grid.innerHTML = '<div style="color:#94a3b8;font-size:0.88rem;">불러오는 중…</div>';
  try {
    const data = await (await fetch('/topic-analyses')).json();
    savedTopicRows = data.analyses || [];
    savedTopicsLoaded = true;
    renderSavedTopics();
  } catch (err) {
    grid.innerHTML = '<div style="color:#dc2626;font-size:0.88rem;">목록을 불러오지 못했습니다.</div>';
  }
}

// 파일 이름 묶음을 한 줄로 ("골학 상지 · 골학 하지" — 많으면 +N개)
function savedTopicFiles(names) {
  const list = (names || []).map(n => topicShortName(n)).filter(Boolean);
  if (!list.length) return '';
  if (list.length <= 2) return list.join(' · ');
  return `${list[0]} · ${list[1]} +${list.length - 2}개`;
}

function renderSavedTopics() {
  const grid = document.getElementById('saved-topics-grid');
  const rows = savedTopicRows;

  const totalTopics = rows.reduce((sum, r) => sum + (r.num_topics || 0), 0);
  document.getElementById('saved-topics-summary').textContent =
    rows.length ? `— 분석 ${rows.length}건 · 주제 ${totalTopics}개` : '';

  if (!rows.length) {
    grid.className = '';
    grid.innerHTML = `
      <div style="text-align:center;padding:28px 16px;color:#94a3b8;">
        <div style="font-size:1.8rem;">🔍</div>
        <div style="margin-top:8px;font-size:0.92rem;">아직 분석한 주제가 없습니다.</div>
        <div style="margin-top:4px;font-size:0.83rem;">기출 주제 분석에서 강의록·기출을 올리면 여기에 모입니다.</div>
      </div>`;
    return;
  }

  grid.className = 'paper-grid';
  grid.innerHTML = rows.map(r => {
    const lec = savedTopicFiles(r.lecture_names);
    const exam = savedTopicFiles(r.exam_names);
    const dropNote = r.dropped
      ? `<span class="paper-short">출처 미확인 ${r.dropped}개 제외</span>` : '';
    return `
      <div class="paper-card">
        <div class="paper-source">📄 ${escHtml(lec || '강의록')}</div>
        <div class="paper-title">${escHtml(r.title || '기출 주제 분석')}${dropNote}</div>
        <div class="paper-meta">${escHtml(relativeDay(r.created_at))} ${escHtml((r.created_at || '').split(' ')[1] || '')} · 주제 ${r.num_topics}개 · 기출 ${r.total_questions}문항</div>
        <div class="paper-meta">📝 ${escHtml(exam || '기출문제')}</div>
        <div class="paper-actions">
          <button class="paper-open" onclick="openSavedTopic(${r.id})">▶ 보기</button>
          <button class="paper-edit" onclick="renameSavedTopic(${r.id})" title="이름 변경">✏️</button>
          <button class="paper-del" onclick="deleteSavedTopic(${r.id})" title="삭제">🗑️</button>
        </div>
      </div>`;
  }).join('');
}

// ── 분석 한 건 열람 ──
let savedTopicDetail = null;   // 열려 있는 분석 (이름 변경 후 헤더만 다시 그릴 때)

function applySavedTopicHeader(aid, a) {
  const row = savedTopicRows.find(r => r.id === aid) || {};
  const title = (row.title || a.title || '').trim();
  document.getElementById('saved-topic-title').textContent = `🔍 ${title || '기출 주제 분석'}`;
  document.getElementById('saved-topic-meta').textContent = [
    a.created_at || '',
    `주제 ${(a.topics || []).length}개`,
    `기출 ${a.total_questions || 0}문항`,
    `${providerLabel(a.provider)} / ${a.model || ''}`,
  ].filter(Boolean).join(' · ');
  document.getElementById('saved-topic-rename-btn').onclick = () => renameSavedTopic(aid, true);
  document.getElementById('saved-topic-delete-btn').onclick = () => deleteSavedTopic(aid, true);
}

async function openSavedTopic(aid) {
  try {
    const resp = await fetch('/topic-analysis/' + aid);
    const a = await resp.json();
    if (!resp.ok || a.error) return alert(a.error || '분석 결과를 불러오지 못했습니다.');

    savedTopicDetail = a;
    applySavedTopicHeader(aid, a);
    topicRenderResult(a, 'saved');   // 분석 탭과 같은 렌더러 ('saved' 뷰로)

    showArchiveSubview('saved-topics-view');   // archive.js — 보관함 안쪽 화면 전환 공용
  } catch (err) {
    alert('분석 결과를 불러오지 못했습니다.');
  }
}

function closeSavedTopicView() {
  showArchiveSubview('saved-topics-list-view');
}

// 이름 변경 — 비우면 서버가 주제 키워드로 제목을 다시 만들어 준다
async function renameSavedTopic(aid, fromDetail) {
  const row = savedTopicRows.find(r => r.id === aid) || {};
  const cur = (row.title || '').trim();
  const next = prompt('이 분석의 이름을 입력하세요.\n(비우면 주제 키워드로 자동 생성됩니다)', cur);
  if (next == null) return;              // 취소

  const form = new FormData();
  form.append('title', next.trim());
  const resp = await fetch('/topic-analysis/' + aid + '/rename', { method: 'POST', body: form });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) return alert(data.error || '이름을 바꾸지 못했습니다.');

  // 비워서 보냈으면 서버가 만든 자동 제목이 돌아온다 — 그 값을 캐시에 반영
  row.title = data.title || '';
  renderSavedTopics();
  if (fromDetail) applySavedTopicHeader(aid, savedTopicDetail);
}

async function deleteSavedTopic(aid, fromDetail) {
  if (!confirm('이 분석 결과를 삭제할까요? 정리된 주제 목록이 함께 사라집니다.')) return;
  await fetch('/topic-analysis/' + aid, { method: 'DELETE' });
  savedTopicRows = savedTopicRows.filter(r => r.id !== aid);
  if (fromDetail) closeSavedTopicView();
  renderSavedTopics();
}
