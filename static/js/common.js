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
function switchTab(name) {
  ['home', 'generator', 'wrong', 'bones'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('hidden', t !== name);
    document.getElementById('tabbtn-' + t).classList.toggle('active', t === name);
  });
  document.getElementById('tab-bar').classList.toggle('hidden', name === 'home');
  if (name === 'home')  loadHome();
  if (name === 'wrong') loadWrongFolders();
  if (name === 'bones') loadBoneBank();
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
let currentQuestions = [];
function renderQuestions(questions, raw, viewOpts) {
  viewOpts = viewOpts || {};
  const folder = viewOpts.folder || null;
  const containerId = viewOpts.containerId || 'questions-container';
  const titleId = viewOpts.titleId || 'result-title';
  currentQuestions = questions || [];

  const container = document.getElementById(containerId);
  container.innerHTML = '';

  // 결과 영역 제목 (오답 폴더 보기면 폴더명으로 교체)
  const titleEl = document.getElementById(titleId);
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

  questions.forEach((q, idx) => container.appendChild(buildQuestionCard(q, idx, folder)));

  // 원본 펼치기 (오답 폴더 보기 모드에서는 원본 응답이 없으므로 생략)
  if (raw) appendRawSection(container, raw);
}

// 원본 응답 접기 영역 — 렌더링 완료 시점에 붙인다 (스트리밍은 done 이벤트에서)
function appendRawSection(container, raw) {
  const raw_section = document.createElement('details');
  raw_section.innerHTML = `<summary>📄 LLM 원본 응답 보기</summary><pre>${escHtml(raw)}</pre>`;
  container.appendChild(raw_section);
}

// 문제 카드 1장을 만든다
function buildQuestionCard(q, idx, folder) {
    const choices = q['선택지'] || [];
    // 유형 판별: 명시된 유형 우선, 없으면 선택지 유무로 추정
    const rawType = (q['유형'] || '').replace(/\s/g, '');
    const isObjective = (rawType ? rawType.includes('객관') : choices.length > 0);
    const isBlank = rawType.includes('빈칸');
    // 빈칸(____, □□, ( )) 개수와 '정답'을 ' | '로 나눈 개수가 일치할 때만
    // 빈칸별 입력칸으로 분리. 안 맞으면(구형 데이터·형식 이탈) 기존 textarea로 대체.
    const blankCount = isBlank ? ((q['문제'] || '').match(/_{2,}|□{2,}|\(\s*\)/g) || []).length : 0;
    const blankAnswers = isBlank ? (q['정답'] || '').split('|').map(s => s.trim()).filter(Boolean) : [];
    const useBlankInputs = isBlank && blankCount >= 2 && blankAnswers.length === blankCount;

    const card = document.createElement('div');
    card.className = 'question-card';
    card.id = `qcard-${idx}`;

    const typeBadge = typeBadgeHtml(rawType, isObjective);

    // 헤더: 문제 번호 + (넣기 / 빼기) 버튼
    let actionBtn;
    if (folder) {
      const item = (folder.items || [])[idx];
      const itemId = item ? item.id : null;
      actionBtn = `<button class="wrong-remove-btn" onclick="removeWrongItem(${itemId}, ${folder.id})" title="이 폴더에서 빼기">🗑️ 폴더에서 빼기</button>`;
    } else {
      actionBtn = `<button class="wrong-add-btn" id="wbtn-${idx}" onclick="openWrongModal(${idx})" title="오답 노트에 넣기">🔖 오답에 넣기</button>`;
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
      <div class="answer-section" id="ans-${idx}">
        <div class="answer-badge">✅ 정답: ${escHtml(q['정답'] || '-')}</div>
        ${q['해설'] ? `<div class="explain-box"><strong>💡 해설</strong>\n${escHtml(q['해설'])}</div>` : ''}
        ${q['함정포인트'] ? `<div class="trap-box"><strong>⚠️ 함정포인트</strong>\n${escHtml(q['함정포인트'])}</div>` : ''}
        ${origBtn}
      </div>`;

    if (isObjective) {
      const answerNum = (q['정답'] || '').replace(/[^①②③④⑤\d]/g, '');
      const answerIdx = ['①','②','③','④','⑤'].indexOf(answerNum.length === 1 && isNaN(answerNum) ? answerNum : ['①','②','③','④','⑤'][parseInt(answerNum)-1]);
      const choiceHtml = choices.map((c, i) =>
        `<li data-idx="${i}" onclick="selectChoice(this, ${idx}, ${answerIdx})">${escHtml(c)}</li>`
      ).join('');
      card.innerHTML = `
        ${headerHtml}
        ${imageHtml}
        <div class="q-text">${escHtml(q['문제'] || '')}</div>
        <ul class="choices" id="choices-${idx}">${choiceHtml}</ul>
        <button class="check-btn" onclick="checkAnswer(${idx}, ${answerIdx})">정답 확인</button>
        ${answerBlock}
      `;
    } else if (useBlankInputs) {
      // 빈칸채우기(빈칸 2개 이상): 빈칸별로 입력칸을 분리하고 각각 채점
      const blankInputsHtml = blankAnswers.map((_, i) => `
        <div class="blank-input-item">
          <span class="blank-label">빈칸 ${i + 1}</span>
          <input type="text" class="blank-input" id="blank-${idx}-${i}" placeholder="답을 입력하세요" />
        </div>`).join('');
      card.innerHTML = `
        ${headerHtml}
        <div class="q-text">${escHtml(q['문제'] || '')}</div>
        <div class="blank-input-row">${blankInputsHtml}</div>
        <button class="check-btn" onclick="checkBlanks(${idx})">정답 확인</button>
        ${answerBlock}
      `;
    } else {
      // 주관식(단답형/서술형/빈칸 1개): 선택지 없이 답안 작성란 + 정답 공개
      card.innerHTML = `
        ${headerHtml}
        ${imageHtml}
        <div class="q-text">${escHtml(q['문제'] || '')}</div>
        <textarea class="subj-input" id="subj-${idx}" placeholder="답을 작성해 보세요..." rows="3"></textarea>
        <button class="check-btn" onclick="revealAnswer(${idx})">정답 확인</button>
        ${answerBlock}
      `;
    }
    return card;
}

function selectChoice(el, qIdx, answerIdx) {
  const list = document.getElementById(`choices-${qIdx}`);
  list.querySelectorAll('li').forEach(li => li.classList.remove('selected'));
  el.classList.add('selected');
}

function revealAnswer(qIdx) {
  document.getElementById(`ans-${qIdx}`).style.display = 'block';
}

// 빈칸채우기(빈칸 2개 이상): 빈칸별 입력값을 정답과 각각 비교해 O/X 표시
function checkBlanks(qIdx) {
  const q = currentQuestions[qIdx] || {};
  const answers = (q['정답'] || '').split('|').map(s => s.trim()).filter(Boolean);
  const norm = s => (s || '').trim().replace(/\s+/g, '').toLowerCase();

  answers.forEach((ans, i) => {
    const input = document.getElementById(`blank-${qIdx}-${i}`);
    if (!input) return;
    input.classList.remove('correct', 'wrong');
    input.classList.add(norm(input.value) === norm(ans) ? 'correct' : 'wrong');
    input.disabled = true;
  });

  document.getElementById(`ans-${qIdx}`).style.display = 'block';
}

function checkAnswer(qIdx, answerIdx) {
  const list = document.getElementById(`choices-${qIdx}`);
  const items = list.querySelectorAll('li');
  const selected = list.querySelector('li.selected');

  items.forEach((li, i) => {
    li.onclick = null; // 클릭 잠금
    if (i === answerIdx) li.classList.add('correct');
  });
  if (selected && parseInt(selected.dataset.idx) !== answerIdx) {
    selected.classList.add('wrong');
  }

  document.getElementById(`ans-${qIdx}`).style.display = 'block';
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
