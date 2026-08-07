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

// ── LaTeX 표기를 읽히는 평문으로 ──
// 이 앱에는 수식 렌더러(KaTeX·MathJax)가 없다. 그런데 생화학·생리학처럼 수식이
// 나오는 과목을 넣으면 LLM이 '$\Delta G^\circ < 0$' 같은 LaTeX를 그대로 내보내고,
// escHtml 을 거쳐 글자 그대로 화면에 찍힌다. (한글 글꼴이 역슬래시를 ₩ 로 그려서
// 더 이상하게 보인다 — 데이터는 U+005C 로 멀쩡하다.)
//
// 완전한 LaTeX 파서가 아니다. 의대 문항에 실제로 나오는 표기만 다룬다 —
// 그리스 문자, 비교·연산 기호, 단위를 감싸는 \text{}, 간단한 위·아래 첨자.
// 분수·적분처럼 못 바꾸는 것은 건드리지 않고 원문 그대로 남긴다.
// 잘못 바꾸느니 원문이 낫다.
//
// 표시 전용이다. 저장된 데이터는 바꾸지 않으므로, 렌더링을 제대로 붙이게 되면
// 이 함수만 걷어내면 된다.
// 두 갈래로 나눠 둔다. LaTeX 는 명령어 뒤 공백 하나를 '구분자'로 먹는데,
// 그걸 그대로 따르면 '\Delta G' 는 'ΔG'(맞음)가 되지만 '\approx 4.18' 은
// '≈4.18'(붙어버림)이 된다. 글자처럼 붙는 것과 좌우에 공백이 있어야 읽히는
// 연산·비교 기호를 달리 취급한다.
const MATH_LETTERS = {          // 뒤 공백을 먹는다 → ΔG
  Delta: 'Δ', delta: 'δ', alpha: 'α', beta: 'β', gamma: 'γ', lambda: 'λ',
  mu: 'μ', pi: 'π', sigma: 'σ', omega: 'ω', Sigma: 'Σ', Omega: 'Ω',
  circ: '°', degree: '°', percent: '%', infty: '∞',
};
const MATH_OPS = {              // 좌우에 공백을 준다 → 1 cal ≈ 4.184 J
  approx: '≈', times: '×', cdot: '·', div: '÷', pm: '±', mp: '∓',
  leq: '≤', le: '≤', geq: '≥', ge: '≥', neq: '≠', ne: '≠', equiv: '≡',
  rightarrow: '→', to: '→', leftarrow: '←', leftrightarrow: '↔', Rightarrow: '⇒',
};
const SUP_MAP = { '0':'⁰','1':'¹','2':'²','3':'³','4':'⁴','5':'⁵','6':'⁶','7':'⁷','8':'⁸','9':'⁹','+':'⁺','-':'⁻' };
const SUB_MAP = { '0':'₀','1':'₁','2':'₂','3':'₃','4':'₄','5':'₅','6':'₆','7':'₇','8':'₈','9':'₉','+':'₊','-':'₋' };

function mathToText(s) {
  if (!s) return '';
  let t = String(s);
  if (t.indexOf('\\') < 0 && t.indexOf('$') < 0) return t;   // 흔한 경우엔 그냥 통과

  // \text{ cal} \mathrm{J} → 안쪽 내용만 (단위를 감싸는 용도로 가장 많이 쓴다)
  t = t.replace(/\\(?:text|mathrm|mathit|mathbf|rm|bf|it)\s*\{([^{}]*)\}/g, '$1');
  // ^\circ → ° (표준 상태 표기 ΔG°'. \circ 단독도 이 앱 맥락에서는 각도가 아니라 도(度)다)
  t = t.replace(/\^\s*\\circ/g, '°');
  // 위 첨자: ^{2} ^2 ^{+}
  t = t.replace(/\^\{([^{}]+)\}|\^(\S)/g, (m, br, one) => {
    const body = br != null ? br : one;
    return [...body].every(c => SUP_MAP[c]) ? [...body].map(c => SUP_MAP[c]).join('') : m;
  });
  // 아래 첨자: 숫자면 유니코드(ΔG₁), 글자면 밑줄만 남긴다(K'_eq — 붙여 쓰면 읽기 어렵다)
  t = t.replace(/_\{([^{}]+)\}/g, (m, body) =>
    [...body].every(c => SUB_MAP[c]) ? [...body].map(c => SUB_MAP[c]).join('') : '_' + body);
  t = t.replace(/_(\d)/g, (m, d) => SUB_MAP[d]);
  // 기호 치환. 명령어 뒤 공백 하나까지 함께 집어 LaTeX 의 구분자 규칙을 따른다.
  // 모르는 명령은 공백까지 원문 그대로 되돌린다.
  t = t.replace(/\\([a-zA-Z]+) ?/g, (m, name) => {
    if (Object.prototype.hasOwnProperty.call(MATH_LETTERS, name)) return MATH_LETTERS[name];
    if (Object.prototype.hasOwnProperty.call(MATH_OPS, name)) return ' ' + MATH_OPS[name] + ' ';
    return m;
  });
  // 간격 명령(\, \; \! \quad)은 공백으로
  t = t.replace(/\\[,;:!]/g, ' ').replace(/\\q?quad/g, '  ');
  // 남은 $ 구분자 제거 ($$ 먼저)
  t = t.replace(/\$\$/g, '').replace(/\$/g, '');
  return t.replace(/[ \t]{2,}/g, ' ');
}

// 문항 텍스트를 화면에 넣을 때 쓴다 (평문 변환 → HTML 이스케이프 순서).
function escMath(s) { return escHtml(mathToText(s)); }

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
// viewOpts.paged = true 면 10문제씩 끊어 보여주고 번호 바로가기 줄을 붙인다
//   (파일 아래쪽 '문제 페이지 나누기' 절). 시험지 화면(생성 결과·보관함)에만 준다.
const questionsByNs = {};              // ns → 그 컨테이너에 그려진 문제 배열
function getQuestions(ns) { return questionsByNs[ns || ''] || []; }

// ── 문제 dict 판정 (화면 카드와 인쇄 문서가 같은 규칙을 쓰게 하려고 분리) ──
// print.js 의 buildPrintDoc 도 이 셋을 쓴다. 유형 판별 규칙이 갈리면
// 화면과 종이의 문제 유형이 달라지므로 반드시 여기 한 곳만 고칠 것.

// 빈칸 표시(____ · □□ · ( ))를 찾는 정규식. 인쇄에서는 이걸 손글씨용 밑줄로 바꾼다.
const BLANK_MARK_RE = /_{2,}|□{2,}|\(\s*\)/g;

function typeInfoOf(q) {
  const choices = Array.isArray(q['선택지']) ? q['선택지'] : [];
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
  // 번호 바로가기 줄은 컨테이너 '밖'에 있어서 innerHTML='' 로 지워지지 않는다.
  // 지난 회차의 줄이 남으면 없어진 문제 번호를 가리키므로 여기서 같이 치운다.
  clearQuestionPages(ns);

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

  // 번호 바로가기 줄 + 페이지 버튼 (요청한 화면만). 페이지 버튼을 원본 접기보다
  // 먼저 붙여, 컨테이너 안이 [카드들 → 페이지 버튼 → 원본 접기] 순서가 되게 한다.
  if (viewOpts.paged) setupQuestionPages(ns, container, questions.length);

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
      ? `<button class="check-btn" style="margin-top:10px;" data-src="${escHtml(q['원본이미지'])}" data-title="${escMath(q['문제'] || '')}" onclick="viewOriginalImage(this.dataset.src, this.dataset.title)">🖼️ 원본 보기</button>`
      : '';

    const answerBlock = `
      <div class="answer-section" id="${ns}ans-${idx}">
        <div class="answer-badge">✅ 정답: ${escMath(q['정답'] || '-')}</div>
        ${q['해설'] ? `<div class="explain-box"><strong>💡 해설</strong>\n${escMath(q['해설'])}</div>` : ''}
        ${origBtn}
      </div>`;

    if (isObjective) {
      const answerIdx = answerIndexOf(q);   // 판별 실패 시 -1 (정답 표시가 안 된다)
      const choiceHtml = choices.map((c, i) =>
        `<li data-idx="${i}" onclick="selectChoice(this, '${ns}', ${idx}, ${answerIdx})">${escMath(c)}</li>`
      ).join('');
      card.innerHTML = `
        ${headerHtml}
        ${imageHtml}
        <div class="q-text">${escMath(q['문제'] || '')}</div>
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
        <div class="q-text">${escMath(q['문제'] || '')}</div>
        <div class="blank-input-row">${blankInputsHtml}</div>
        <button class="check-btn" onclick="checkBlanks('${ns}', ${idx})">정답 확인</button>
        ${answerBlock}
      `;
    } else {
      // 주관식(단답형/서술형/빈칸 1개): 선택지 없이 답안 작성란 + 정답 공개
      card.innerHTML = `
        ${headerHtml}
        ${imageHtml}
        <div class="q-text">${escMath(q['문제'] || '')}</div>
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
  // 화면에 보이는 것과 같은 기준으로 비교한다. 정답이 '$\Delta G$' 로 저장돼 있으면
  // 화면에는 'ΔG' 로 보이는데, 사용자가 본 대로 'ΔG' 를 쳐도 원문과 달라 오답이 된다.
  const norm = s => mathToText(s || '').trim().replace(/\s+/g, '').toLowerCase();

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

// ══════════════════════════════════════════════
// 목록 페이지 나누기 — 보관함의 두 목록(시험지 · 분석한 주제) 공용
//   카드가 계속 쌓이면 옛날 것을 찾으려 끝까지 스크롤해야 한다.
//   두 목록은 같은 .paper-grid 마크업을 쓰므로 자르는 규칙도 한 곳에 둔다.
//   ⚠️ 문제 카드 목록(renderQuestions)에는 쓰지 않는다 — 고른 선택지·입력한 답·
//      채점 색이 전부 DOM 에만 있어서, 잘라 다시 그리면 답안이 날아간다.
//      문제 쪽 페이지 나누기는 파일 맨 아래 '문제 페이지 나누기' 절에 따로 있다
//      (카드는 전부 만들어 두고 보이기/숨기기만 바꾸는 방식).
// ══════════════════════════════════════════════

// 목록 key → 한 페이지 개수 + 그 목록을 다시 그리는 함수.
// render 를 화살표로 감싼 것은 호출 시점에 찾게 하려는 것이다 — 이 파일은
// archive.js/topic_archive.js 보다 먼저 로드돼서 직접 적으면 undefined 다.
const PAGED_LISTS = {
  papers: { perPage: 8, render: () => renderArchive() },
  topics: { perPage: 8, render: () => renderSavedTopics() },
};

const pageByList = {};                 // key → 지금 보고 있는 페이지 (1-based)

function setPage(key, n) { pageByList[key] = Math.max(1, n); }

// 현재 페이지 몫만 잘라 준다. 페이지가 범위를 벗어나 있으면 마지막 페이지로
// 당겨온다 — 마지막 장에 남은 카드를 지웠을 때 빈 화면이 뜨는 것을 막는다.
function pageSlice(key, rows) {
  const perPage = PAGED_LISTS[key].perPage;
  const totalPages = Math.max(1, Math.ceil(rows.length / perPage));
  const page = Math.min(pageByList[key] || 1, totalPages);
  pageByList[key] = page;
  const start = (page - 1) * perPage;
  return { page, totalPages, items: rows.slice(start, start + perPage) };
}

// 버튼 줄에 실을 번호들. 7장까지는 전부 늘어놓고, 그보다 많으면
// 처음·마지막·현재±1 만 남기고 사이를 '…' 로 접는다 (좁은 화면에서 줄이 넘친다).
function pagerItems(page, totalPages) {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, i) => i + 1);
  const from = Math.max(2, page - 1);
  const to = Math.min(totalPages - 1, page + 1);
  const out = [1];
  if (from > 2) out.push('…');
  for (let i = from; i <= to; i++) out.push(i);
  if (to < totalPages - 1) out.push('…');
  out.push(totalPages);
  return out;
}

// 페이지 버튼 줄. 한 장뿐이면 빈 문자열 — 목록이 짧으면 지금까지와 똑같이 보인다.
function pagerHtml(key, page, totalPages) {
  if (totalPages <= 1) return '';

  const arrow = (target, glyph, label) =>
    `<button class="pager-btn pager-arrow" onclick="goPage('${key}', ${target})"` +
    `${target < 1 || target > totalPages ? ' disabled' : ''}` +
    ` title="${label}" aria-label="${label}">${glyph}</button>`;

  const nums = pagerItems(page, totalPages).map(n =>
    n === '…'
      ? '<span class="pager-gap" aria-hidden="true">…</span>'
      : `<button class="pager-btn" onclick="goPage('${key}', ${n})"` +
        `${n === page ? ' aria-current="page"' : ` aria-label="${n}페이지로"`}>${n}</button>`
  ).join('');

  return `<nav class="pager" aria-label="페이지">`
    + arrow(page - 1, '◀', '이전 페이지') + nums + arrow(page + 1, '▶', '다음 페이지')
    + `</nav>`;
}

// 페이지를 바꾸고 그 목록만 다시 그린다. 카드가 통째로 바뀌므로 맨 위로 올려 준다
// (보관함 안쪽 화면을 옮길 때와 같은 어법 — archive.js showArchiveSubview).
function goPage(key, n) {
  setPage(key, n);
  PAGED_LISTS[key].render();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ══════════════════════════════════════════════
// 진행 상태 (퍼센트 바 + 단계 목록) — 문제 생성기·기출 주제 분석 공용
//   두 탭이 같은 모양의 상자를 쓰고 서버도 같은 이벤트(stage/progress)를 보낸다.
//   막대 id 가 탭마다 다르므로 계산기를 탭마다 하나씩 만들어 쓴다.
// ══════════════════════════════════════════════

// weights: 단계 key → 가중치. 오래 걸리는 단계일수록 크게 (실제 소요 시간에 대략 비례).
// stepPrefix: 단계 <li> 의 id 접두사 ('step-' / 'topic-step-').
function createProgress(opts) {
  const weights    = opts.weights;
  const stepPrefix = opts.stepPrefix || 'step-';
  let progress = {};      // key → 0~1
  let active   = [];      // 이번 실행에서 실제로 도는 단계

  // 단계 줄 오른쪽의 '3 / 14' · '완료' 표시
  function setNote(key, text) {
    const el = document.getElementById(stepPrefix + key);
    if (!el) return;
    let note = el.querySelector('.step-note');
    if (!note) {
      note = document.createElement('span');
      note.className = 'step-note';
      el.appendChild(note);
    }
    note.textContent = text;
  }

  function render() {
    const totalWeight = active.reduce((s, k) => s + weights[k], 0) || 1;
    const acc = active.reduce((s, k) => s + weights[k] * (progress[k] || 0), 0);
    const pct = Math.round(acc / totalWeight * 100);
    document.getElementById(opts.barId).style.width = pct + '%';
    document.getElementById(opts.pctId).textContent = pct + '%';
  }

  return {
    // 이번 실행에서 도는 단계들 (세션 재사용처럼 일부를 건너뛰는 경우가 있다)
    get stages() { return active; },

    reset(stages) {
      active = (stages || []).slice();
      progress = {};
      active.forEach(k => {
        const note = document.getElementById(stepPrefix + k);
        const el = note && note.querySelector('.step-note');
        if (el) el.remove();
      });
      render();
    },

    setStageProgress(key, done, total) {
      if (!(key in weights)) return;
      progress[key] = total > 0 ? Math.min(1, done / total) : 0;
      setNote(key, total > 0 ? `${done} / ${total}` : '진행 중…');
      render();
    },

    completeStage(key) {
      progress[key] = 1;
      setNote(key, '완료');     // 진행률 이벤트가 없던 단계에도 표시가 남도록
      render();
    },

    render,
  };
}

// 스트림을 읽을 수 있는 환경인지 — 요청을 보내기 '전에' 판단해야 한다.
// 요청 후에 폴백하면 서버가 이미 일을 시작해 토큰을 두 번 쓰게 된다.
function canReadStream() {
  // 'body'는 getter이므로 프로토타입에서 값을 '읽으면' 안 된다 (Illegal invocation).
  // 존재 여부만 확인한다.
  return typeof ReadableStream !== 'undefined' && 'body' in Response.prototype;
}

// SSE 본문을 이벤트 객체 하나씩으로 바꿔 주는 async 이터레이터.
//   for await (const ev of sseEvents(resp)) { ... }
// 도중에 빠져나가야 하면(분량 초과 확인 등) 그냥 break/return 하면 된다.
// 이벤트 해석은 탭마다 다르므로 여기서는 프레임을 끊어 주기만 한다.
async function* sseEvents(resp) {
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE는 빈 줄로 이벤트를 구분한다
    let sep;
    while ((sep = buffer.indexOf('\n\n')) !== -1) {
      const frame = buffer.slice(0, sep).trim();
      buffer = buffer.slice(sep + 2);
      if (frame.startsWith('data: ')) yield JSON.parse(frame.slice(6));
    }
  }
}

// HTTP 오류를 '알 수 없는 오류'로 뭉개지 않고 원인을 알려준다.
// (응답이 JSON이 아닌 경우 — 404 HTML, 디버그 트레이스백 등 — 이 경로로 온다)
// endpoint: 404 안내에 실을 경로 ('/generate/stream' 등)
async function describeHttpError(resp, endpoint) {
  const text = await resp.text().catch(() => '');
  try {
    const parsed = JSON.parse(text);
    if (parsed.error) return parsed.error;
  } catch (e) { /* JSON이 아니면 아래에서 상태 코드로 안내 */ }

  if (resp.status === 404) {
    return `서버에서 스트리밍 기능(${endpoint || '스트림'})을 찾을 수 없습니다.\n`
         + 'Flask 서버가 예전 코드로 실행 중일 수 있습니다. 서버를 껐다가 다시 실행해주세요.';
  }
  const snippet = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim().slice(0, 300);
  return `서버 오류 ${resp.status} ${resp.statusText}` + (snippet ? `\n${snippet}` : '');
}

// ══════════════════════════════════════════════
// 문제 페이지 나누기 + 번호 바로가기 — 시험지 화면(생성 결과 · 보관함) 공용
//   30문제짜리 시험지는 한 화면에 다 담기지 않아, 뒤쪽 문제로 가려면 계속 굴려야 했다.
//   ① 10문제씩 끊어 보여주고 아래에 페이지 버튼을 둔다.
//   ② 넓은 화면에서 비어 있던 본문 왼쪽 여백에 문제 번호를 늘어놓아, 몇 번이든
//      한 번에 간다(다른 페이지의 번호를 누르면 그 페이지로 넘어간 뒤 그 문제로).
//      번호 줄은 넓은 화면에만 나온다 — 좁은 화면은 CSS 가 통째로 감춘다(.q-rail).
//      만드는 쪽은 화면 폭을 따지지 않는다. 폭이 바뀌면 다시 그려야 하기 때문이다.
//
//   ⚠️ 위의 목록 페이저(PAGED_LISTS)와 달리 **다시 그리지 않는다.** 고른 선택지·
//      입력한 답·채점 색·열어 둔 정답이 전부 DOM 에만 있어서, 페이지를 넘길 때마다
//      다시 그리면 앞 페이지에 쓴 답이 통째로 날아간다. 그래서 카드는 처음에 전부
//      만들어 두고 .q-off 로 보이기/숨기기만 바꾼다. 돌아오면 답안이 그대로 있다.
// ══════════════════════════════════════════════

const QUESTIONS_PER_PAGE = 10;
const qPagerByNs = {};   // ns → {containerId, total, totalPages} — 페이지를 나눈 컨테이너
const qPageByNs  = {};   // ns → 지금 보고 있는 페이지 (1-based)

// renderQuestions 가 카드를 다 그린 뒤 부른다.
function setupQuestionPages(ns, container, total) {
  if (total < 2) return;   // 한 문제뿐이면 번호줄도 페이지도 의미가 없다

  qPagerByNs[ns] = {
    containerId: container.id,
    total,
    totalPages: Math.ceil(total / QUESTIONS_PER_PAGE),
  };

  // ① 아래 페이지 버튼 (내용은 applyQuestionPage 가 채운다)
  const pager = document.createElement('div');
  pager.className = 'q-pager';
  pager.id = `${ns}qpager`;
  container.appendChild(pager);

  // ② 번호 바로가기 줄 — 컨테이너 '앞'(밖)에 둔다. 본문 왼쪽 여백에
  //    고정(position:fixed)하려면 카드 흐름 밖이어야 하기 때문이다.
  //    탭·서브뷰가 숨겨지면(display:none) 이 줄도 같이 사라진다 —
  //    조상이 안 그려지면 fixed 자손도 안 그려진다.
  const rail = document.createElement('nav');
  rail.className = 'q-rail';
  rail.id = `${ns}qrail`;
  rail.setAttribute('aria-label', '문제 번호 바로가기');
  rail.innerHTML =
    `<span class="q-rail-label">문제</span><div class="q-rail-nums">`
    + Array.from({ length: total }, (_, i) =>
        `<button type="button" class="q-rail-btn" id="${ns}qnav-${i}"`
        + ` onclick="jumpToQuestion('${ns}', ${i})"`
        + ` aria-label="${i + 1}번 문제로 이동">${i + 1}</button>`).join('')
    + `</div>`;
  container.parentNode.insertBefore(rail, container);
  updateQRailTop();          // 지금 스크롤 위치에 맞는 세로 자리로 (아래 절)

  applyQuestionPage(ns, 1);
}

// ── 번호 줄의 세로 자리 — 헤더 아래에서 시작해, 헤더가 밀려 올라가면 화면 맨 위 ──
//   CSS 로는 못 한다. fixed 는 스크롤을 모르고, sticky 는 흐름 안에 있어야 하는데
//   이 줄은 본문 옆 여백에 띄우느라 흐름 밖에 있다. 그래서 top 만 스크롤에 맞춰
//   다시 준다 (가로 자리는 CSS 가 잡은 그대로 둔다).
//
//   헤더 높이를 상수로 박지 않고 매번 재는 이유 — 응원 문구 길이와 화면 폭에 따라
//   헤더가 한 줄 더 늘거나 줄어든다(문구 바꾸기 버튼도 있다). 재면 늘 맞는다.
const Q_RAIL_TOP_MIN = 16;      // 헤더가 사라진 뒤 화면 맨 위에서 띄울 간격
const Q_RAIL_HEADER_GAP = 18;   // 헤더 아래끝과 줄 사이 간격
const qRailMedia = window.matchMedia('(min-width: 960px)');   // style.css 의 표시 기준과 같은 값
let qRailTick = false;

function updateQRailTop() {
  if (!qRailMedia.matches) return;      // 줄이 안 뜨는 폭 — 잴 것도 없다
  const rails = document.querySelectorAll('.q-rail');
  if (!rails.length) return;

  // 헤더 아래끝의 '화면상' 위치. 스크롤을 내리면 줄어들다 음수가 되고,
  // 그때부터 아래 max 가 Q_RAIL_TOP_MIN 을 골라 줄이 화면 맨 위에 붙는다.
  const header = document.querySelector('header');
  const headerBottom = header ? header.getBoundingClientRect().bottom : 0;
  const top = Math.max(Q_RAIL_TOP_MIN, headerBottom + Q_RAIL_HEADER_GAP) + 'px';
  rails.forEach(r => { r.style.top = top; });
}

// 스크롤 이벤트는 초당 수십 번 온다 — 프레임당 한 번으로 묶는다.
function queueQRailTop() {
  if (qRailTick) return;
  qRailTick = true;
  requestAnimationFrame(() => { qRailTick = false; updateQRailTop(); });
}
window.addEventListener('scroll', queueQRailTop, { passive: true });
window.addEventListener('resize', queueQRailTop);   // 폭이 바뀌면 헤더 높이도 바뀐다

// 다음 회차를 그리기 전 정리. 줄은 컨테이너 밖에 있어 innerHTML='' 로는 안 지워진다.
function clearQuestionPages(ns) {
  delete qPagerByNs[ns];
  delete qPageByNs[ns];
  const rail = document.getElementById(`${ns}qrail`);
  if (rail) rail.remove();
}

// 페이지를 실제로 바꾼다 (스크롤은 건드리지 않는다).
function applyQuestionPage(ns, n) {
  const info = qPagerByNs[ns];
  if (!info) return;
  const page = Math.min(Math.max(1, n), info.totalPages);
  qPageByNs[ns] = page;

  const container = document.getElementById(info.containerId);
  container.querySelectorAll('.question-card').forEach((card, i) => {
    card.classList.toggle('q-off', Math.floor(i / QUESTIONS_PER_PAGE) + 1 !== page);
  });

  const pager = document.getElementById(`${ns}qpager`);
  if (pager) pager.innerHTML = questionPagerHtml(ns, page, info);
  markRailPage(ns, page, info);
}

// 아래 페이지 버튼. 보이는 문제가 통째로 바뀌므로 맨 위로 올려 준다
// (보관함 목록 goPage 와 같은 어법 — 새 페이지의 1번부터 읽게 된다).
function goQuestionPage(ns, n) {
  applyQuestionPage(ns, n);
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// 번호 바로가기. 다른 페이지의 번호면 그 페이지로 넘어간 뒤 그 카드로 간다.
function jumpToQuestion(ns, idx) {
  if (!qPagerByNs[ns]) return;
  applyQuestionPage(ns, Math.floor(idx / QUESTIONS_PER_PAGE) + 1);
  const card = document.getElementById(`${ns}qcard-${idx}`);
  if (card) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// 번호 접기 규칙(pagerItems)은 목록 페이저와 같은 것을 쓴다 — 페이지가 많아도
// 줄이 넘치지 않는다. 앞뒤 버튼은 글자를 넣어 '다음 페이지'임을 분명히 한다.
function questionPagerHtml(ns, page, info) {
  if (info.totalPages <= 1) return '';
  const from = (page - 1) * QUESTIONS_PER_PAGE + 1;
  const to   = Math.min(info.total, page * QUESTIONS_PER_PAGE);

  const step = (target, label) =>
    `<button class="pager-btn pager-step" onclick="goQuestionPage('${ns}', ${target})"`
    + `${target < 1 || target > info.totalPages ? ' disabled' : ''}>${label}</button>`;

  const nums = pagerItems(page, info.totalPages).map(n =>
    n === '…'
      ? '<span class="pager-gap" aria-hidden="true">…</span>'
      : `<button class="pager-btn" onclick="goQuestionPage('${ns}', ${n})"`
        + `${n === page ? ' aria-current="page"' : ` aria-label="${n}페이지로"`}>${n}</button>`
  ).join('');

  return `<div class="q-pager-info">${page} / ${info.totalPages} 페이지 · ${from}–${to}번</div>`
    + `<nav class="pager" aria-label="문제 페이지">`
    + step(page - 1, '◀ 이전 페이지') + nums + step(page + 1, '다음 페이지 ▶')
    + `</nav>`;
}

// 지금 페이지에 속한 번호를 강조한다. 나머지 번호도 그대로 누를 수 있다(페이지 이동).
function markRailPage(ns, page, info) {
  const rail = document.getElementById(`${ns}qrail`);
  if (!rail) return;
  const first = (page - 1) * QUESTIONS_PER_PAGE;
  const last  = Math.min(info.total, page * QUESTIONS_PER_PAGE) - 1;
  for (let i = 0; i < info.total; i++) {
    const btn = document.getElementById(`${ns}qnav-${i}`);
    if (btn) btn.classList.toggle('on', i >= first && i <= last);
  }

  // 번호가 많으면 줄 안이 넘쳐 세로로 굴러간다 (칸 수는 화면 폭에 따라 CSS 가 정한다).
  // 지금 페이지의 첫 번호를 줄 안에서 보이는 자리로 끌어온다.
  // scrollIntoView 는 쓰지 않는다 — 줄뿐 아니라 페이지 전체까지 같이 움직인다.
  const box = rail.querySelector('.q-rail-nums');
  const btn = document.getElementById(`${ns}qnav-${first}`);
  if (!box || !btn) return;
  const b = box.getBoundingClientRect(), t = btn.getBoundingClientRect();
  box.scrollTop += t.top - b.top - 4;
}

// ══════════════════════════════════════════════
// 사용량 표시 (토큰 / 크레딧) — 문제 생성기·기출 주제 분석 공용
//   두 탭이 각자 자기 결과 화면에 같은 모양의 접힌 상자를 띄운다.
//   그래서 대상 상자 id와 작업 이름('생성'/'분석')을 인자로 받는다.
//   계산은 서버(providers/usage.py)에서 끝났고 여기서는 그리기만 한다.
// ══════════════════════════════════════════════

// 크레딧은 소수가 나올 수 있어 정수/소수를 나눠 표기한다.
function fmtCredit(v) {
  if (v == null) return '—';
  if (Number.isInteger(v)) return v.toLocaleString('ko-KR');
  const max = Math.abs(v) < 1 ? 4 : 2;
  return v.toLocaleString('ko-KR', { minimumFractionDigits: 2, maximumFractionDigits: max });
}

// 제공사에 따라 크레딧과 토큰 중 맞는 쪽을 보여준다.
// 전북대 게이트웨이는 크레딧으로 과금하므로 토큰 수를 보여주지 않는다.
// opts: { boxId: 상자 element id, noun: '생성' | '분석' }
function renderSpend(payload, opts) {
  const o = Object.assign({ boxId: 'usage-box', noun: '생성' }, opts || {});
  if (payload && payload.credits) return renderCredits(payload.credits, o);
  return renderUsage(payload && payload.usage, o);
}

// 결과 화면 — 이번 작업에 쓴 크레딧 + 남은 총 크레딧
function renderCredits(credits, opts) {
  const o = Object.assign({ boxId: 'usage-box', noun: '생성' }, opts || {});
  const box = document.getElementById(o.boxId);
  if (!box) return;
  box.hidden = false;
  box.open = false;

  // 보관된 기록에는 '쓴 크레딧'만 있고 잔액이 없다 — 잔액은 조회한 그 순간의 값이라
  // 나중에 보면 틀린 숫자가 되기 때문(providers/usage.py의 credits_for_history).
  // 그래서 잔액이 없으면 잔액 관련 표시를 통째로 뺀다.
  const hasBalance = credits.remaining != null || (credits.sections || []).length > 0;

  const spent = credits.spent_known
    ? `이번 ${o.noun}에 쓴 크레딧 ${fmtCredit(credits.spent)}`
    : '이번 사용분은 계산하지 못했습니다';
  const summaryText = `💳 ${spent}`
    + (hasBalance ? ` <span class="usage-note">· 남은 크레딧 ${fmtCredit(credits.remaining)}</span>` : '');

  let body = '';
  if (hasBalance) {
    const rows = (credits.sections || []).map(s => `<tr>
        <td>${escHtml(s.label)}</td>
        <td>${fmtCredit(s.quota)}</td>
        <td>${fmtCredit(s.used)}</td>
        <td><b>${fmtCredit(s.remaining)}</b></td>
      </tr>`).join('');

    body = `<table class="usage-table">
        <thead><tr><th>구분</th><th>할당</th><th>누적 사용</th><th>남음</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;

    if (credits.renewal_date) {
      body += `<div class="usage-models">월별 할당 갱신: ${escHtml(credits.renewal_date)}</div>`;
    }
    if (!credits.spent_known) {
      body += `<div class="usage-warn">${o.noun} 전 잔액을 읽지 못해 이번 사용분을 계산할 수 없었습니다.
               위 표의 누적 사용량은 정상입니다.</div>`;
    }
    body += `<div class="usage-foot">플랫폼이 알려준 잔액입니다.
             정산이 늦게 반영되면 이번 사용분이 실제보다 적게 보일 수 있습니다.</div>`;
  }

  // 잔액 없는 보관 기록은 요약 한 줄이 전부다 — 펼칠 내용이 없으므로
  // 본문도 펼침 화살표도 두지 않는다 (눌러도 아무것도 안 나오면 오히려 헷갈린다)
  box.classList.toggle('no-body', !body);
  box.innerHTML = `<summary>${summaryText}</summary>`
                + (body ? `<div class="usage-body">${body}</div>` : '');
}

// ── 토큰 사용량 (이번 작업) ──
function renderUsage(usage, opts) {
  const o = Object.assign({ boxId: 'usage-box', noun: '생성' }, opts || {});
  const box = document.getElementById(o.boxId);
  if (!box) return;
  box.classList.remove('no-body');   // 같은 상자에 크레딧을 그렸던 흔적을 지운다
  if (!usage || !usage.calls) {          // 호출 자체가 없었으면 숨긴다
    box.hidden = true;
    box.innerHTML = '';
    box.open = false;
    return;
  }
  box.hidden = false;

  const num = n => (n || 0).toLocaleString('ko-KR');
  // 제공사가 usage를 안 준 호출 — 0으로 합치면 안 쓴 것처럼 보이므로 따로 알린다
  const allUnreported = usage.unreported_calls >= usage.calls;
  const rowNote = r => (r.unreported_calls
    ? ` <span class="usage-note">(${r.unreported_calls}회 미보고)</span>` : '');

  const summaryText = allUnreported
    ? `🔢 토큰 사용량 — 제공사가 알려주지 않음 (호출 ${num(usage.calls)}회)`
    : `🔢 이번 ${o.noun}에 쓴 토큰 ${num(usage.total)}개 <span class="usage-note">· 호출 ${num(usage.calls)}회</span>`;

  let body = '';
  if (allUnreported) {
    body = `<div class="usage-warn">이 제공사는 응답에 토큰 사용량을 담아 보내지 않습니다.
            호출은 ${num(usage.calls)}회 있었지만 몇 토큰을 썼는지는 알 수 없습니다.</div>`;
  } else {
    const rows = usage.by_stage.map(r => `<tr>
        <td>${escHtml(r.label)}${rowNote(r)}</td>
        <td>${num(r.calls)}</td>
        <td>${num(r.input)}</td>
        <td>${num(r.output)}</td>
        <td><b>${num(r.total)}</b></td>
      </tr>`).join('');

    body = `<table class="usage-table">
        <thead><tr><th>단계</th><th>호출</th><th>입력</th><th>출력</th><th>합계</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr>
          <td>전체</td><td>${num(usage.calls)}</td>
          <td>${num(usage.input)}</td><td>${num(usage.output)}</td>
          <td><b>${num(usage.total)}</b></td>
        </tr></tfoot>
      </table>`;

    if (usage.by_model.length > 1) {
      body += `<div class="usage-models">모델별: ` + usage.by_model.map(m =>
        `${escHtml(m.model)} ${num(m.total)}`).join(' · ') + `</div>`;
    }
    if (usage.unreported_calls) {
      body += `<div class="usage-warn">${num(usage.unreported_calls)}회는 제공사가 사용량을
               알려주지 않아 위 합계에 빠져 있습니다. 실제 사용량은 더 많습니다.</div>`;
    }
  }

  box.innerHTML = `<summary>${summaryText}</summary>`
                + `<div class="usage-body">${body}`
                + `<div class="usage-foot">제공사가 응답에 담아 보낸 값입니다. 청구 기준과 다를 수 있습니다.</div>`
                + `</div>`;
}

// 오류·중단으로 끝나면 결과 화면이 안 보일 수 있어, 쓴 만큼을 오류 문구에 덧붙인다.
// (중단해도 그 시점까지의 크레딧·토큰은 이미 소모됐으므로 알려주는 게 맞다)
function spendSuffix(ev) {
  const c = ev && ev.credits;
  if (c) {
    const left = `남은 크레딧 ${fmtCredit(c.remaining)}`;
    return c.spent_known
      ? `\n여기까지 쓴 크레딧: ${fmtCredit(c.spent)} (${left})`
      : `\n${left}`;
  }
  return usageSuffix(ev && ev.usage);
}

function usageSuffix(usage) {
  if (!usage || !usage.calls) return '';
  const num = n => (n || 0).toLocaleString('ko-KR');
  if (usage.unreported_calls >= usage.calls) {
    return `\nLLM 호출 ${num(usage.calls)}회 — 토큰 사용량은 제공사가 알려주지 않았습니다.`;
  }
  return `\n여기까지 쓴 토큰: ${num(usage.total)}개 (호출 ${num(usage.calls)}회)`;
}

// ── API 키 발급 도움말 (키 입력란 아래 접힌 안내) — 문제 생성기·기출 주제 분석 공용 ──
// 내용은 제공사가 /providers로 알려준다 (providers/base.py의 key_help_* 필드).
// 발급 절차를 아직 모르는 제공사(전북대 게이트웨이)는 steps가 비어 있다 → 도움말째로 숨긴다.
function renderKeyHelp(info, boxId) {
  const box = document.getElementById(boxId || 'key-help');
  if (!box) return;
  const steps = (info && info.key_help_steps) || [];

  box.open = false;            // 제공사를 바꾸면 접힌 상태로 되돌린다
  if (!steps.length) {
    box.hidden = true;
    box.innerHTML = '';
    return;
  }
  box.hidden = false;

  const note = info.key_help_note
    ? `<div class="key-help-note">💡 ${escHtml(info.key_help_note)}</div>` : '';
  // 발급 페이지는 외부 사이트 → 새 탭으로 열고, opener를 넘기지 않는다
  const link = info.key_help_url
    ? `<a class="key-help-link" href="${escHtml(info.key_help_url)}" target="_blank" rel="noopener noreferrer">🔗 키 발급 페이지 열기</a>` : '';

  box.innerHTML =
    `<summary>❓ ${escHtml(info.label)} API 키 발급 방법</summary>` +
    `<div class="key-help-body">` +
      `<ol>${steps.map(s => `<li>${escHtml(s)}</li>`).join('')}</ol>` +
      note + link +
    `</div>`;
}

// ══════════════════════════════════════════════
// 분량 초과 경고 모달 (주제 분석·문제 생성 공용)
//   서버가 needs_confirm 을 주면 어느 파일의 몇 번째 줄이 버려지는지 보여주고
//   진행 여부를 묻는다. 확인을 받아야만 실제 분석/생성이 돈다.
// ══════════════════════════════════════════════

let _cutResolve = null;

// 모달의 두 버튼과 배경 클릭이 부른다 (index.html)
function resolveCutModal(ok) {
  document.getElementById('cut-modal').classList.remove('open');
  const done = _cutResolve;
  _cutResolve = null;
  if (done) done(ok);
}

/**
 * 여기까지(추출) 쓴 양을 한 줄로. 취소해도 이미 나간 비용이라 숨기지 않는다.
 *
 * 크레딧 과금 제공사(전북대 게이트웨이)만 할당량(quota)이 있어 비율을 낼 수 있다.
 * 토큰 과금 제공사는 총량 개념이 없으므로 절대값으로 보여준다.
 */
function cutSpendText(payload) {
  const c = payload && payload.credits;
  if (c && c.spent_known && c.quota > 0 && c.remaining != null) {
    const after  = (c.remaining / c.quota) * 100;
    const before = ((c.remaining + c.spent) / c.quota) * 100;
    return `${before.toFixed(1)}% → ${after.toFixed(1)}% 소모`;
  }
  const u = payload && payload.usage;
  if (!u || !u.calls) return '';
  if (u.total) return `토큰 ${u.total.toLocaleString('ko-KR')}개 소모`;
  // 제공사가 토큰을 안 알려준 경우 — 호출 수만 안다
  return `LLM 호출 ${u.calls.toLocaleString('ko-KR')}회`;
}

/**
 * 경고를 띄우고 사용자의 선택을 기다린다.
 * @param {Array}  warnings 서버가 준 목록 — {side, name, coverage,
 *                          from_page, from_words, to_page, to_words}
 * @param {Object} spend    같은 응답의 {usage, credits} — 추출까지 쓴 양
 * @returns {Promise<boolean>} 진행하면 true
 */
function confirmTruncation(warnings, spend) {
  // 쪽 번호는 마커가 없으면 null로 온다 (스캔 PDF 등) → 그때는 어절만 보여준다
  const at = (page, words) =>
    (page ? `<b>${page}쪽</b> ` : '') + `“${escHtml(words || '…')}”`;

  const rows = warnings.map(w => `
    <div class="cut-row">
      <div class="cut-file">
        <b>${escHtml(w.side)}</b>
        <span class="cut-name">${escHtml(w.name)}</span>
        <span class="cut-pct">${w.coverage}%만 반영</span>
      </div>
      <div class="cut-range">
        ${at(w.from_page, w.from_words)}<span class="cut-arrow">→</span>${at(w.to_page, w.to_words)}
        <span style="color:#94a3b8;">사이를 버립니다</span>
      </div>
    </div>`).join('');

  document.getElementById('cut-modal-sub').textContent =
    `${warnings.length}개 파일이 배정된 글자수를 넘습니다. 아래 구간이 결과에 들어가지 않습니다.`;
  document.getElementById('cut-modal-list').innerHTML = rows;
  document.getElementById('cut-modal-spend').textContent = cutSpendText(spend);
  document.getElementById('cut-modal').classList.add('open');

  return new Promise(resolve => { _cutResolve = resolve; });
}
