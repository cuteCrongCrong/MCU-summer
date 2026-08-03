// ══════════════════════════════════════════════
// common.js — 여러 탭이 함께 쓰는 공용 유틸 + 문제 카드 렌더링
//   · 다른 스크립트(question_gen.js, wrong_note.js)보다 먼저 로드됩니다.
//   · 여기의 함수/전역은 모든 기능에서 재사용하세요 (재정의 금지).
// ══════════════════════════════════════════════

// ── 공용 유틸 ──
function delay(ms) { return new Promise(r => setTimeout(r, ms)); }

function escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ── 진행 단계 아이콘 업데이트 ──
function setStep(stepId, state) {
  const el = document.getElementById(stepId);
  el.className = 'step-item ' + state;
  if (state === 'done')   el.querySelector('.step-icon').textContent = '✅';
  if (state === 'active') el.querySelector('.step-icon').textContent = '⏳';
}

// ── 상단 탭 전환 ──
// 새 탭을 추가하려면 아래 배열에 id 접미사를 추가하세요. (예: 'login')
// 'home'은 시작 화면 — 탭 바를 숨기고, 다른 탭으로 들어가면 다시 보인다.
const TAB_TITLES = {
  generator: '📝 문제 생성기',
  archive:   '🗂️ 보관함',
  wrong:     '❌ 오답 노트',
  bones:     '🦴 골학 문제은행',
  topics:    '🔍 기출 주제 분석',
};

function switchTab(name) {
  ['home', 'generator', 'archive', 'wrong', 'bones', 'topics'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('hidden', t !== name);
  });
  // 홈에서는 상단 바를 숨기고, 탭 안에서는 '← 홈' + 현재 위치를 보여준다
  document.getElementById('tab-bar').classList.toggle('hidden', name === 'home');
  document.getElementById('tab-bar-title').textContent = TAB_TITLES[name] || '';

  if (name === 'home')    loadHome();
  // 보관함은 목록이 아니라 갈림길(생성한 문제 / 분석한 주제)부터 보여준다
  if (name === 'archive') showArchiveHub();
  if (name === 'wrong')   loadWrongFolders();
  if (name === 'bones')   loadBoneBank();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ── 유형(4분류) → 배지 색·라벨 (백엔드 QUESTION_TYPES와 일치시킬 것) ──
const TYPE_BADGE = {
  '객관식':   ['type-obj',   '객관식'],
  '빈칸채우기': ['type-blank', '빈칸 채우기'],
  '단답형':   ['type-short', '단답형'],
  '서술형':   ['type-essay', '서술형'],
};

// 유형별 개수를 "객관식 6 · 단답형 3" 형태로 (0인 유형 생략)
function formatTypeCounts(obj) {
  obj = obj || {};
  const parts = [];
  for (const key in TYPE_BADGE) {
    const n = obj[key] || 0;
    if (n > 0) parts.push(`${TYPE_BADGE[key][1]} ${n}`);
  }
  return parts.length ? parts.join(' · ') : '해당 없음';
}

function typeBadgeHtml(rawType, isObjective) {
  for (const key in TYPE_BADGE) {
    if (rawType && rawType.includes(key)) {
      const [cls, label] = TYPE_BADGE[key];
      return `<span class="type-badge ${cls}">${label}</span>`;
    }
  }
  // 라벨이 없거나 구형 '주관식' → 선택지 유무로
  return isObjective
    ? `<span class="type-badge type-obj">객관식</span>`
    : `<span class="type-badge type-short">단답형</span>`;
}

// 원문 반영 범위 배너 (전체 읽음 / 일부만 반영)
function renderSourceInfo(sourceInfo) {
  const docs = [['lecture', '📄 강의자료'], ['exam', '📝 기출문제']];
  const rows = [];
  let anyTrunc = false;
  for (const [key, label] of docs) {
    const s = sourceInfo[key];
    if (!s || typeof s.chars !== 'number') continue;
    const nf = n => n.toLocaleString('ko-KR');
    if (s.truncated) {
      anyTrunc = true;
      rows.push(`<div style="margin-top:4px;">⚠️ <b>${label}</b>: 추출 ${nf(s.chars)}자 중 <b>약 ${nf(s.used)}자(${s.coverage}%)</b>만 반영 — 분량 초과로 앞·뒤 일부만 사용, 중간 생략`);
    } else {
      rows.push(`<div style="margin-top:4px;">✅ <b>${label}</b>: 추출 ${nf(s.chars)}자 <b>전체 반영</b>`);
    }
  }
  if (!rows.length) return '';
  const bg = anyTrunc ? '#fffbeb' : '#f0fdf4';
  const bd = anyTrunc ? '#fcd34d' : '#86efac';
  const fg = anyTrunc ? '#92400e' : '#166534';
  return `
    <div style="background:${bg};border:1.5px solid ${bd};border-radius:10px;padding:12px 16px;font-size:0.84rem;color:${fg};">
      <b>${anyTrunc ? '⚠️ 일부 문서가 잘렸습니다' : '✅ 파일 전체가 반영되었습니다'}</b>
      ${rows.join('')}
      ${anyTrunc ? `<div style="font-size:0.75rem;color:#a16207;margin-top:6px;">더 많이 반영하려면 파일을 나눠 올리거나, 서버의 상한(현재 ${(100000).toLocaleString('ko-KR')}자)을 높이면 됩니다.</div>` : ''}
    </div>`;
}

// ── 문제 카드 렌더링 (생성 결과 · 오답 폴더 보기 공용) ──
// viewOpts.folder = {id, name, items} 이면 오답 폴더 보기 모드 (넣기 버튼 대신 빼기 버튼)
// viewOpts.containerId / titleId 로 렌더링 대상 지정 (기본: 생성기 결과 영역)
// viewOpts.ns = 카드 DOM id 접두사. 여러 컨테이너가 동시에 카드를 들고 있어도
//   서로의 카드를 집지 않도록 반드시 컨테이너마다 다른 값을 준다. (기본 '' = 생성기)
//   ⚠️ ns 없이 두 컨테이너를 함께 쓰면 getElementById가 문서 앞쪽 카드를 집어
//      '정답 확인'이 엉뚱한 카드를 여는 버그가 난다.
const questionsByNs = {};              // ns → 그 컨테이너에 그려진 문제 배열
function getQuestions(ns) { return questionsByNs[ns || ''] || []; }

// ── 문제 dict 판정 (화면 카드와 인쇄 문서가 같은 규칙을 쓰게 하려고 분리) ──
// print.js 의 buildPrintDoc 도 이 셋을 쓴다. 유형 판별 규칙이 갈리면
// 화면과 종이의 문제 유형이 달라지므로 반드시 여기 한 곳만 고칠 것.

// 빈칸 표시(____ · □□ · ( ))를 찾는 정규식. 인쇄에서는 이걸 손글씨용 밑줄로 바꾼다.
const BLANK_MARK_RE = /_{2,}|□{2,}|\(\s*\)/g;

function typeInfoOf(q) {
  const choices = q['선택지'] || [];
  const rawType = (q['유형'] || '').replace(/\s/g, '');
  const isObjective = (rawType ? rawType.includes('객관') : choices.length > 0);
  const isBlank = rawType.includes('빈칸');
  // 빈칸 개수와 '정답'을 ' | '로 나눈 개수가 일치할 때만 빈칸별로 분리한다.
  const blankCount = isBlank ? ((q['문제'] || '').match(BLANK_MARK_RE) || []).length : 0;
  const blankAnswers = isBlank ? blankAnswersOf(q) : [];
  return {
    choices, rawType, isObjective, isBlank, blankCount, blankAnswers,
    useBlankInputs: isBlank && blankCount >= 2 && blankAnswers.length === blankCount,
  };
}

function blankAnswersOf(q) {
  return (q['정답'] || '').split('|').map(s => s.trim()).filter(Boolean);
}

// 객관식 정답이 몇 번째 선택지인지 (0-based). 판별 실패하면 -1.
// ⚠️ -1 이 나올 수 있다 — '정답'에 기호 외 문자가 섞이면(예: '④ 12번 갈비뼈') 실패한다.
//   호출부는 반드시 -1 을 처리해야 한다. 화면 카드는 이 경우 정답 표시가 안 되고,
//   인쇄 정답지는 선택지 본문 대신 원문을 그대로 싣는다.
function answerIndexOf(q) {
  const CIRCLED = ['①', '②', '③', '④', '⑤'];
  const answerNum = (q['정답'] || '').replace(/[^①②③④⑤\d]/g, '');
  const mark = (answerNum.length === 1 && isNaN(answerNum))
    ? answerNum : CIRCLED[parseInt(answerNum) - 1];
  return CIRCLED.indexOf(mark);
}

function renderQuestions(questions, raw, viewOpts) {
  viewOpts = viewOpts || {};
  const folder = viewOpts.folder || null;
  const containerId = viewOpts.containerId || 'questions-container';
  // titleId: null 을 명시하면 제목을 건드리지 않는다 (자체 헤더를 쓰는 화면용)
  const titleId = ('titleId' in viewOpts) ? viewOpts.titleId : 'result-title';
  const ns = viewOpts.ns || '';
  questionsByNs[ns] = questions || [];

  const container = document.getElementById(containerId);
  container.innerHTML = '';

  // 결과 영역 제목 (오답 폴더 보기면 폴더명으로 교체)
  const titleEl = titleId ? document.getElementById(titleId) : null;
  if (titleEl) {
    titleEl.textContent = folder
      ? `❌ 오답 노트 — ${folder.name}`
      : '📋 생성된 예상문제';
  }

  if (!questions || questions.length === 0) {
    // 파싱 실패 시 원본 텍스트 그대로 표시
    container.innerHTML = `
      <div class="question-card">
        <p style="color:#64748b;margin-bottom:12px;">문제 파싱에 실패했습니다. 아래 원본 텍스트를 확인하세요.</p>
        <pre style="white-space:pre-wrap;font-size:0.85rem;line-height:1.7;">${escHtml(raw)}</pre>
      </div>`;
    return;
  }

  questions.forEach((q, idx) => container.appendChild(buildQuestionCard(q, idx, folder, ns)));

  // 원본 펼치기 (오답 폴더 보기 모드에서는 원본 응답이 없으므로 생략)
  if (raw) appendRawSection(container, raw);
}

// 원본 응답 접기 영역 — 렌더링 완료 시점에 붙인다 (스트리밍은 done 이벤트에서)
function appendRawSection(container, raw) {
  const raw_section = document.createElement('details');
  raw_section.innerHTML = `<summary>📄 LLM 원본 응답 보기</summary><pre>${escHtml(raw)}</pre>`;
  container.appendChild(raw_section);
}

// 문제 카드 1장을 만든다. ns는 이 카드가 속한 컨테이너의 id 접두사.
function buildQuestionCard(q, idx, folder, ns) {
    ns = ns || '';
    // 유형 판별·빈칸 분리 규칙은 typeInfoOf 한 곳에 있다 (인쇄 문서와 공유)
    const { choices, rawType, isObjective, blankAnswers, useBlankInputs } = typeInfoOf(q);

    const card = document.createElement('div');
    card.className = 'question-card';
    card.id = `${ns}qcard-${idx}`;

    const typeBadge = typeBadgeHtml(rawType, isObjective);

    // 헤더: 문제 번호 + (넣기 / 빼기) 버튼
    let actionBtn;
    if (folder) {
      const item = (folder.items || [])[idx];
      const itemId = item ? item.id : null;
      actionBtn = `<button class="wrong-remove-btn" onclick="removeWrongItem(${itemId}, ${folder.id})" title="이 폴더에서 빼기">🗑️ 폴더에서 빼기</button>`;
    } else {
      actionBtn = `<button class="wrong-add-btn" id="${ns}wbtn-${idx}" onclick="openWrongModal('${ns}', ${idx})" title="오답 노트에 넣기">🔖 오답에 넣기</button>`;
    }
    const headerHtml =
      `<div class="q-header"><span class="q-number">문제 ${idx + 1} ${typeBadge}</span>${actionBtn}</div>`;

    // 문제에 그림이 있으면 표시 (골학 문제은행 오답 등)
    const imageHtml = q['이미지']
      ? `<div style="text-align:center;margin:6px 0 12px;"><img class="q-image" src="${escHtml(q['이미지'])}" alt="문제 그림" /></div>`
      : '';
    // 원본(이름 표시) 그림이 있으면 정답 영역에 '원본 보기' 버튼
    const origBtn = q['원본이미지']
      ? `<button class="check-btn" style="margin-top:10px;" data-src="${escHtml(q['원본이미지'])}" data-title="${escHtml(q['문제'] || '')}" onclick="viewOriginalImage(this.dataset.src, this.dataset.title)">🖼️ 원본 보기</button>`
      : '';

    const answerBlock = `
      <div class="answer-section" id="${ns}ans-${idx}">
        <div class="answer-badge">✅ 정답: ${escHtml(q['정답'] || '-')}</div>
        ${q['해설'] ? `<div class="explain-box"><strong>💡 해설</strong>\n${escHtml(q['해설'])}</div>` : ''}
        ${q['함정포인트'] ? `<div class="trap-box"><strong>⚠️ 함정포인트</strong>\n${escHtml(q['함정포인트'])}</div>` : ''}
        ${origBtn}
      </div>`;

    if (isObjective) {
      const answerIdx = answerIndexOf(q);   // 판별 실패 시 -1 (정답 표시가 안 된다)
      const choiceHtml = choices.map((c, i) =>
        `<li data-idx="${i}" onclick="selectChoice(this, '${ns}', ${idx}, ${answerIdx})">${escHtml(c)}</li>`
      ).join('');
      card.innerHTML = `
        ${headerHtml}
        ${imageHtml}
        <div class="q-text">${escHtml(q['문제'] || '')}</div>
        <ul class="choices" id="${ns}choices-${idx}">${choiceHtml}</ul>
        <button class="check-btn" onclick="checkAnswer('${ns}', ${idx}, ${answerIdx})">정답 확인</button>
        ${answerBlock}
      `;
    } else if (useBlankInputs) {
      // 빈칸채우기(빈칸 2개 이상): 빈칸별로 입력칸을 분리하고 각각 채점
      const blankInputsHtml = blankAnswers.map((_, i) => `
        <div class="blank-input-item">
          <span class="blank-label">빈칸 ${i + 1}</span>
          <input type="text" class="blank-input" id="${ns}blank-${idx}-${i}" placeholder="답을 입력하세요" />
        </div>`).join('');
      card.innerHTML = `
        ${headerHtml}
        <div class="q-text">${escHtml(q['문제'] || '')}</div>
        <div class="blank-input-row">${blankInputsHtml}</div>
        <button class="check-btn" onclick="checkBlanks('${ns}', ${idx})">정답 확인</button>
        ${answerBlock}
      `;
    } else {
      // 주관식(단답형/서술형/빈칸 1개): 선택지 없이 답안 작성란 + 정답 공개
      card.innerHTML = `
        ${headerHtml}
        ${imageHtml}
        <div class="q-text">${escHtml(q['문제'] || '')}</div>
        <textarea class="subj-input" id="${ns}subj-${idx}" placeholder="답을 작성해 보세요..." rows="3"></textarea>
        <button class="check-btn" onclick="revealAnswer('${ns}', ${idx})">정답 확인</button>
        ${answerBlock}
      `;
    }
    return card;
}

// 아래 채점 함수들은 카드가 속한 컨테이너를 ns로 받는다.
// (ns 없이 id만 쓰면 다른 탭에 남아 있는 같은 번호의 카드를 집는다)
function selectChoice(el, ns, qIdx, answerIdx) {
  const list = document.getElementById(`${ns}choices-${qIdx}`);
  list.querySelectorAll('li').forEach(li => li.classList.remove('selected'));
  el.classList.add('selected');
}

function revealAnswer(ns, qIdx) {
  document.getElementById(`${ns}ans-${qIdx}`).style.display = 'block';
}

// 빈칸채우기(빈칸 2개 이상): 빈칸별 입력값을 정답과 각각 비교해 O/X 표시
function checkBlanks(ns, qIdx) {
  const q = getQuestions(ns)[qIdx] || {};
  const answers = (q['정답'] || '').split('|').map(s => s.trim()).filter(Boolean);
  const norm = s => (s || '').trim().replace(/\s+/g, '').toLowerCase();

  answers.forEach((ans, i) => {
    const input = document.getElementById(`${ns}blank-${qIdx}-${i}`);
    if (!input) return;
    input.classList.remove('correct', 'wrong');
    input.classList.add(norm(input.value) === norm(ans) ? 'correct' : 'wrong');
    input.disabled = true;
  });

  document.getElementById(`${ns}ans-${qIdx}`).style.display = 'block';
}

function checkAnswer(ns, qIdx, answerIdx) {
  const list = document.getElementById(`${ns}choices-${qIdx}`);
  const items = list.querySelectorAll('li');
  const selected = list.querySelector('li.selected');

  items.forEach((li, i) => {
    li.onclick = null; // 클릭 잠금
    if (i === answerIdx) li.classList.add('correct');
  });
  if (selected && parseInt(selected.dataset.idx) !== answerIdx) {
    selected.classList.add('wrong');
  }

  document.getElementById(`${ns}ans-${qIdx}`).style.display = 'block';
}

// 원본(이름 표시) 이미지를 공용 모달로 표시 — 골학 문제은행/오답 노트 공용
function viewOriginalImage(src, title) {
  if (!src) return;
  const modal = document.getElementById('bone-original-modal');
  if (!modal) return;
  document.getElementById('bone-original-image').src = src;
  document.getElementById('bone-original-title').textContent = title || '';
  modal.classList.add('open');
}
