// ══════════════════════════════════════════════
// bone_bank.js — 🦴 골학 문제은행
//   번호가 매겨진 이미지에서 특정 번호 부위의 이름을 맞히는 주관식.
//   카테고리(Skull/Upper Limb/Lower Limb/Vertebrae & Thorax) → 세부 범위를 선택해
//   그 범위에 해당하는 문제만 랜덤 순서로 풀 수 있다 (또는 전체 Random).
//   범위 안의 문제는 중복 없이 1번씩만 출제되며, 다 풀면 완료 화면에서 멈춘다
//   (다시 풀기 / 다른 범위 선택 가능) — 끝없이 반복되지 않음.
//   common.js 이후 로드. 데이터: /static/data/bone_bank.json
//   LLM/서버 없이 순수 정적 데이터로 동작 (비용 0).
//   틀린(또는 원하는) 문제는 '오답에 넣기'로 오답 노트에 저장 (wrong_note.js 재사용).
// ══════════════════════════════════════════════

// 범위 선택 트리 — 카테고리/세부범위 목록·아이콘의 단일 출처.
// bone_bank.json의 각 문제는 category/subcategory 키로 여기에 소속된다.
const BONE_CATEGORIES = [
  {
    key: 'skull', label: 'Skull', icon: '💀',
    subs: [
      { key: 'entire_skull', label: 'Entire Skull', icon: '💀' },
      { key: 'neurocranium', label: 'Neurocranium', icon: '🧠' },
      { key: 'viscerocranium', label: 'Viscerocranium', icon: '🦷' },
    ],
  },
  {
    key: 'upper_limb', label: 'Upper Limb', icon: '💪',
    subs: [
      { key: 'shoulder_girdle', label: 'Shoulder girdle', icon: '🦴' },
      { key: 'upper_arm_forearm', label: 'Upper arm & Forearm', icon: '🦴' },
      { key: 'hand_bones', label: 'Hand bones', icon: '✋' },
    ],
  },
  {
    key: 'lower_limb', label: 'Lower Limb', icon: '🦵',
    subs: [
      { key: 'pelvic_girdle', label: 'Pelvic girdle', icon: '🦴' },
      { key: 'thigh_leg', label: 'Thigh & Leg', icon: '🦴' },
      { key: 'foot_bones', label: 'Foot bones', icon: '🦶' },
    ],
  },
  {
    key: 'vertebrae_thorax', label: 'Vertebrae & Thorax', icon: '🦴',
    subs: [
      { key: 'vertebral_column', label: 'Vertebral column', icon: '🦴' },
      { key: 'thorax', label: 'Thorax', icon: '🫁' },
    ],
  },
];

let boneEntries = [];      // 전체 이미지 단위 데이터 (title, image, original, parts, category, subcategory)
let boneQueue = [];        // 남은 출제 인스턴스 {entry, part} (섞인 순서, 끝에서 pop)
let boneCurrent = null;    // 현재 출제 인스턴스 {entry, part}
let boneAnswered = false;  // 현재 문제 채점 완료 여부
let boneLoaded = false;    // 데이터 로드 여부 (탭 재진입 시 진행상황 유지)
let boneStats = { correct: 0, total: 0 };

let boneView = 'categories';       // 'categories' | 'subcategories' | 'mode' | 'quiz' | 'click' | 'write' | 'done'
let boneActiveCategory = null;     // 현재 열어본 카테고리 객체 (subcategories/quiz 화면용)
let boneScopeLabel = '전체 범위';   // 현재 출제 범위 표시용 문구
let boneScopeEntries = [];         // 현재 선택된 범위에 속한 entries (큐 재섞기용)
let bonePendingScope = null;       // 모드를 고르기 전 임시로 들고 있는 {entries, label}
let boneMode = 'classic';          // 진행 중인 모드: 'classic'(한 문제씩) | 'click'(단어은행 매칭) | 'write'(빈칸 한번에 쓰기)
let boneEntryQueue = [];           // click/write 모드용: 남은 이미지 큐 (엔트리 자체, 섞인 순서)
let boneRoundState = null;         // click/write 모드의 현재 라운드(이미지 1장) 상태

async function loadBoneBank() {
  boneShowView('categories');   // 탭 재진입 시 항상 범위 선택 화면부터
  if (boneLoaded) { renderBoneCategoryGrid(); return; }   // 이미 불러왔으면 재요청 없이 그리드만
  const emptyEl = document.getElementById('bone-empty');
  try {
    const r = await fetch('/static/data/bone_bank.json', { cache: 'no-store' });
    const data = await r.json();
    // image + 유효한 parts(또는 구형 answers)가 있는 항목만 사용
    boneEntries = (data.questions || [])
      .map(normalizeBoneEntry)
      .filter(e => e && e.image && e.parts.length);
    if (!boneEntries.length) {
      emptyEl.innerHTML = '아직 등록된 문제가 없습니다. <code>static/data/bone_bank.json</code> 에 문제를 추가하세요.';
      emptyEl.style.display = 'block';
      return;
    }
    boneLoaded = true;
    emptyEl.style.display = 'none';
    renderBoneCategoryGrid();
  } catch (e) {
    emptyEl.textContent = '문제 데이터를 불러오지 못했습니다.';
    emptyEl.style.display = 'block';
  }
}

// 새 스키마(parts) + 구 스키마(answers 단일) 모두 표준 형태로 변환
function normalizeBoneEntry(q) {
  if (!q || !q.image) return null;
  let parts = [];
  if (Array.isArray(q.parts) && q.parts.length) {
    parts = q.parts.filter(p => p && Array.isArray(p.answers) && p.answers.length);
  } else if (Array.isArray(q.answers) && q.answers.length) {
    // 구형: 이미지 = 뼈 하나 → 번호 없는 단일 부위
    parts = [{ num: null, answers: q.answers }];
  }
  return {
    id: q.id || '',
    title: q.title || '',
    image: q.image,
    original: q.original || '',
    category: q.category || '',
    subcategory: q.subcategory || '',
    parts,
    // 선택: 이미지 전체에 대한 부가 문제 — "이 구조물의 이름은?" / "어느 방향에서 본 모습?"
    structure: Array.isArray(q.structure) && q.structure.length ? q.structure : null,
    view: Array.isArray(q.view) && q.view.length ? q.view : null,
  };
}

// ── 화면 전환: categories / subcategories / quiz / done ──
function boneShowView(view) {
  boneView = view;
  document.getElementById('bone-category-view').style.display = view === 'categories' ? 'block' : 'none';
  document.getElementById('bone-subcategory-view').style.display = view === 'subcategories' ? 'block' : 'none';
  document.getElementById('bone-mode-view').style.display = view === 'mode' ? 'block' : 'none';
  document.getElementById('bone-quiz').style.display = view === 'quiz' ? 'block' : 'none';
  document.getElementById('bone-click-quiz').style.display = view === 'click' ? 'block' : 'none';
  document.getElementById('bone-write-quiz').style.display = view === 'write' ? 'block' : 'none';
  document.getElementById('bone-done').style.display = view === 'done' ? 'block' : 'none';
}

function boneCatTile(icon, label, count, onclick, disabled) {
  const dim = disabled ? 'opacity:0.45;cursor:not-allowed;' : 'cursor:pointer;';
  const handler = disabled ? '' : `onclick="${onclick}"`;
  const countTxt = count == null ? '' : `<div style="font-size:0.72rem;color:#64748b;margin-top:4px;">${count}문항</div>`;
  return `
    <div class="bone-cat-tile" ${handler} style="${dim}">
      <div style="font-size:2rem;">${icon}</div>
      <div style="font-weight:700;font-size:0.92rem;margin-top:6px;">${escHtml(label)}</div>
      ${countTxt}
    </div>`;
}

function boneCountFor(filterFn) {
  let n = 0;
  boneEntries.forEach(e => { if (filterFn(e)) n += boneEntryInstances(e).length; });
  return n;
}

// ── 최상위: 카테고리 4개 + 전체 Random ──
function renderBoneCategoryGrid() {
  boneActiveCategory = null;
  document.getElementById('bone-score').textContent = '';
  const total = boneCountFor(() => true);
  const tiles = BONE_CATEGORIES.map(cat => {
    const n = boneCountFor(e => e.category === cat.key);
    return boneCatTile(cat.icon, cat.label, n, `boneOpenCategory('${cat.key}')`, n === 0);
  }).join('');
  const randomTile = boneCatTile('🎲', 'Random', total, `boneStartScope(() => true, '전체 범위 (Random)')`, total === 0);

  document.getElementById('bone-category-grid').innerHTML = tiles + randomTile;
  boneShowView('categories');
}

// ── 카테고리 클릭 → 세부 범위 화면 ──
function boneOpenCategory(catKey) {
  const cat = BONE_CATEGORIES.find(c => c.key === catKey);
  if (!cat) return;
  boneActiveCategory = cat;

  const subTiles = cat.subs.map(sub => {
    const n = boneCountFor(e => e.category === cat.key && e.subcategory === sub.key);
    return boneCatTile(sub.icon, sub.label, n,
      `boneStartScope(e => e.category==='${cat.key}' && e.subcategory==='${sub.key}', '${cat.label} > ${sub.label}')`,
      n === 0);
  }).join('');
  const catTotal = boneCountFor(e => e.category === cat.key);
  const randomTile = boneCatTile('🎲', `${cat.label} Random`, catTotal,
    `boneStartScope(e => e.category==='${cat.key}', '${cat.label} Random')`, catTotal === 0);

  document.getElementById('bone-subcategory-title').textContent = `${cat.icon} ${cat.label}`;
  document.getElementById('bone-subcategory-grid').innerHTML = subTiles + randomTile;
  boneShowView('subcategories');
}

function boneBackToCategories() {
  renderBoneCategoryGrid();
}

function boneBackToSubcategories() {
  if (boneActiveCategory) boneOpenCategory(boneActiveCategory.key);
  else renderBoneCategoryGrid();
}

// ── 범위 선택 확정 → 출제 모드 선택 화면으로 ──
function boneStartScope(filterFn, label) {
  const filtered = boneEntries.filter(filterFn);
  if (!filtered.length) return;   // 방어: 0문항 타일은 클릭 막혀있지만 이중 확인
  bonePendingScope = { entries: filtered, label };
  document.getElementById('bone-mode-label').textContent = `📍 ${label}`;
  boneShowView('mode');
}

function boneBackToModeChoice() {
  if (bonePendingScope) boneShowView('mode');
  else boneBackToSubcategories();
}

// 모드 선택 확정 → 실제 출제 시작
function boneChooseMode(mode) {
  if (!bonePendingScope) return;
  boneScopeLabel = bonePendingScope.label;
  boneScopeEntries = bonePendingScope.entries;
  boneMode = mode;
  boneStats = { correct: 0, total: 0 };
  boneCurrent = null;
  if (mode === 'classic') {
    boneBuildQueue();
    boneUpdateScore();
    boneShowView('quiz');
    boneNextQuestion();
  } else {
    boneEntryQueue = boneShuffle(boneScopeEntries.slice());
    boneUpdateScore();
    boneNextEntry();
  }
}

// Fisher-Yates 셔플
function boneShuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

// 엔트리 하나가 내는 출제 인스턴스 전부: 번호별 부위 + (있으면) 구조물 이름·방향 문제
function boneEntryInstances(entry) {
  const list = entry.parts.map(part => ({ entry, part, kind: 'part' }));
  if (entry.structure) list.push({ entry, kind: 'structure' });
  if (entry.view) list.push({ entry, kind: 'view' });
  return list;
}

// 선택된 범위의 (이미지 × 부위/구조물/방향)를 하나의 출제 큐로 펼쳐 섞음 → 모든 문제가 골고루 출제
function boneBuildQueue(scopeEntries) {
  if (scopeEntries) boneScopeEntries = scopeEntries;   // 범위가 바뀔 때만 갱신, 이후 재섞기는 같은 범위 유지
  const all = [];
  boneScopeEntries.forEach(entry => all.push(...boneEntryInstances(entry)));
  boneQueue = boneShuffle(all);
  // 직전 문제가 바로 또 나오지 않게 (pop은 끝에서 꺼냄)
  if (boneCurrent && boneQueue.length > 1) {
    const last = boneQueue.length - 1;
    if (boneSameInstance(boneQueue[last], boneCurrent)) {
      [boneQueue[0], boneQueue[last]] = [boneQueue[last], boneQueue[0]];
    }
  }
}

function boneSameInstance(a, b) {
  if (!a || !b || a.entry.id !== b.entry.id) return false;
  const ak = a.kind || 'part', bk = b.kind || 'part';
  if (ak !== bk) return false;
  return ak === 'part' ? a.part.num === b.part.num : true;
}

// 현재 인스턴스의 정답 배열 (부위/구조물/방향 공통 접근)
function boneInstanceAnswers(inst) {
  if (!inst) return [];
  if (inst.kind === 'structure') return inst.entry.structure || [];
  if (inst.kind === 'view') return inst.entry.view || [];
  return (inst.part && inst.part.answers) || [];
}

function boneNextQuestion() {
  if (!boneQueue.length) { boneShowDone(); return; }   // 범위 안 문제를 모두 풀었으면 완료 화면
  boneCurrent = boneQueue.pop();
  boneAnswered = false;
  renderBoneQuestion();
}

// 범위 안 문제를 전부 풀었을 때 표시하는 완료 화면
function boneShowDone() {
  const { correct, total } = boneStats;
  const rate = total ? Math.round((correct / total) * 100) : 0;
  document.getElementById('bone-done-summary').innerHTML =
    `<b>${escHtml(boneScopeLabel)}</b> 범위의 문제를 모두 풀었습니다! 🎉<br>` +
    `맞힘 <b style="color:#16a34a;">${correct}</b> / 총 <b>${total}</b>문제` +
    (total ? ` &nbsp;·&nbsp; 정답률 <b>${rate}%</b>` : '');
  boneShowView('done');
}

// '다시 풀기': 같은 범위·같은 모드를 처음부터 새로 섞어서 다시 시작 (통계 초기화)
function boneRetryScope() {
  boneStats = { correct: 0, total: 0 };
  boneCurrent = null;
  if (boneMode === 'classic') {
    boneBuildQueue();
    boneUpdateScore();
    boneShowView('quiz');
    boneNextQuestion();
  } else {
    boneEntryQueue = boneShuffle(boneScopeEntries.slice());
    boneUpdateScore();
    boneNextEntry();
  }
}

// 현재 문제의 질문 문구 ("N번의 이름은?" / 구조물·방향 문제 / 구형이면 "이 뼈의 이름은?")
function boneQuestionLabel(inst) {
  if (!inst) return '';
  if (inst.kind === 'structure') return '이 그림에 나온 구조물(뼈)의 이름은 무엇입니까?';
  if (inst.kind === 'view') return '이 그림은 어느 방향에서 본 모습입니까?';
  return (inst.part && inst.part.num != null)
    ? `${inst.part.num}번의 이름은?`
    : '이 뼈의 이름은?';
}

function renderBoneQuestion() {
  const inst = boneCurrent;
  const img = document.getElementById('bone-image');
  // 구조물/방향 문제는 특정 번호를 묻는 게 아니므로 번호가 없는 원본 이미지를 보여준다
  // (원본이 없으면 번호 이미지로 대체 — 안내 문구와 실제 화면이 어긋나지 않게).
  const useOriginal = (inst.kind === 'structure' || inst.kind === 'view') && inst.entry.original;
  img.src = useOriginal ? inst.entry.original : inst.entry.image;
  img.alt = '골학 문제 이미지';   // 이미지 제목을 그대로 노출하면 정답 힌트가 되므로 일반 문구만

  document.getElementById('bone-scope-label').textContent = `📍 ${boneScopeLabel}`;
  document.getElementById('bone-question-label').textContent = boneQuestionLabel(inst);

  const input = document.getElementById('bone-input');
  input.value = '';
  input.disabled = false;

  document.getElementById('bone-result').innerHTML = '';

  // '원본 보기' 버튼: 원본 이미지가 있고, 지금 화면에 이미 원본을 보여주는 중이 아닐 때만
  document.getElementById('bone-original-btn').style.display =
    (inst.entry.original && !useOriginal) ? 'inline-block' : 'none';

  // 버튼 상태 초기화
  document.getElementById('bone-check-btn').style.display = 'inline-block';
  document.getElementById('bone-giveup-btn').style.display = 'inline-block';
  document.getElementById('bone-next-btn').style.display = 'none';

  input.focus();
}

// 비교용 정규화: 앞뒤 공백 제거 + 모든 공백 제거 + 소문자
function boneNormalize(s) {
  return (s || '').trim().toLowerCase().replace(/\s+/g, '');
}

function boneCheck() {
  if (boneAnswered) return;
  const input = document.getElementById('bone-input');
  const val = input.value;
  if (!boneNormalize(val)) { input.focus(); return; }   // 빈 답은 무시

  const correct = boneInstanceAnswers(boneCurrent).some(
    a => boneNormalize(a) === boneNormalize(val)
  );
  boneFinish(correct, correct ? '🎉 정답!' : '❌ 오답');
}

function boneGiveUp() {
  if (boneAnswered) return;
  boneFinish(false, '🙈 정답을 확인하세요');
}

// 채점 마무리: 결과 표시 + 정답 공개 + 점수 갱신 + 오답 저장/다음 버튼
function boneFinish(correct, headline) {
  boneAnswered = true;
  boneStats.total += 1;
  if (correct) boneStats.correct += 1;

  const answersTxt = boneInstanceAnswers(boneCurrent).map(escHtml).join(', ');
  const color = correct ? '#16a34a' : '#dc2626';
  document.getElementById('bone-result').innerHTML =
    `<div style="margin-top:12px;display:flex;align-items:center;gap:10px;flex-wrap:wrap;">
       <span style="font-weight:700;color:${color};">${headline}</span>
       <button class="wrong-add-btn" id="bone-wrong-btn" onclick="boneAddWrong()" title="오답 노트에 넣기">🔖 오답에 넣기</button>
     </div>
     <div style="margin-top:4px;font-size:0.9rem;color:#1e293b;">정답: <b>${answersTxt}</b></div>`;

  document.getElementById('bone-input').disabled = true;
  document.getElementById('bone-check-btn').style.display = 'none';
  document.getElementById('bone-giveup-btn').style.display = 'none';
  document.getElementById('bone-next-btn').style.display = 'inline-block';
  boneUpdateScore();
  document.getElementById('bone-next-btn').focus();
}

// 현재 문제를 오답 노트 저장용 question 객체로 변환
function boneToQuestion() {
  const inst = boneCurrent;
  const title = inst.entry.title || '골학';
  const useOriginal = (inst.kind === 'structure' || inst.kind === 'view') && inst.entry.original;
  return {
    '유형': '단답형',
    '문제': `[골학] ${title} — ${boneQuestionLabel(inst)}`,
    '정답': boneInstanceAnswers(inst).join(', '),
    '이미지': useOriginal ? inst.entry.original : inst.entry.image,   // 문제 화면에 보인 이미지 그대로
    '원본이미지': inst.entry.original || '',   // 원본(이름) 이미지
  };
}

// '오답에 넣기' → 오답 노트 저장 모달 (wrong_note.js 재사용)
function boneAddWrong() {
  if (!boneCurrent) return;
  const q = boneToQuestion();
  openWrongModalForQuestion(q, q['문제'], 'bone-wrong-btn');
}

// ── 원본 보기 (이름이 적힌 원본 이미지 모달) ──
// click/write 모드도 같은 모달을 재사용하므로 entry를 직접 받는 형태로 분리해뒀다.
function boneShowOriginalFor(entry) {
  if (!entry || !entry.original) return;
  document.getElementById('bone-original-image').src = entry.original;
  document.getElementById('bone-original-title').textContent = entry.title || '';
  document.getElementById('bone-original-modal').classList.add('open');
}

function boneShowOriginal() {
  if (boneCurrent) boneShowOriginalFor(boneCurrent.entry);
}

function closeBoneOriginal() {
  document.getElementById('bone-original-modal').classList.remove('open');
}

function boneUpdateScore() {
  const { correct, total } = boneStats;
  const rate = total ? Math.round((correct / total) * 100) : 0;
  document.getElementById('bone-score').innerHTML =
    `맞힘 <b style="color:#16a34a;">${correct}</b> / 푼 문제 <b>${total}</b>` +
    (total ? ` &nbsp;·&nbsp; 정답률 <b>${rate}%</b>` : '');
}

// Enter 키: 채점 전이면 확인, 채점 후면 다음 문제
function boneKey(e) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  if (boneAnswered) boneNextQuestion();
  else boneCheck();
}

// ══════════════════════════════════════════════
// click/write 모드 공용 — 이미지 1장 단위로 라운드가 진행된다.
//   (한 이미지에 라벨이 30~40개씩 있어도 '문항 하나'로 묶어서 푸는 것이 이 두 모드의 취지)
// ══════════════════════════════════════════════

function boneNextEntry() {
  if (!boneEntryQueue.length) { boneShowDone(); return; }
  const entry = boneEntryQueue.pop();
  if (boneMode === 'click') {
    renderBoneClickRound(entry);
    boneShowView('click');
  } else {
    renderBoneWriteRound(entry);
    boneShowView('write');
  }
}

// 오답 노트 저장용 question 객체 (click/write 라운드 안의 부위 하나)
function boneRoundWrongQuestion(entry, num, answers) {
  return {
    '유형': '단답형',
    '문제': `[골학] ${entry.title || '골학'} — ${num}번의 이름은?`,
    '정답': answers.join(', '),
    '이미지': entry.image,
    '원본이미지': entry.original || '',
  };
}

// ── 클릭형 매칭: 단어은행에서 골라 빈칸(번호)에 채우기 ──

// 부위 하나를 대표하는 단어(단어은행 카드에 표시할 문구) — 정답 배열의 첫 항목을 쓴다
function boneCanonicalTerm(answers) {
  return (answers && answers[0]) || '';
}

function renderBoneClickRound(entry) {
  const slots = entry.parts.map(p => ({ num: p.num, answers: p.answers, filled: null, correct: null }));
  const bank = boneShuffle(entry.parts.map(p => ({
    text: boneCanonicalTerm(p.answers), sourceNum: p.num, used: false,
  })));
  boneRoundState = { entry, slots, bank, selectedSlotNum: null, graded: false };

  document.getElementById('bone-click-scope-label').textContent = `📍 ${boneScopeLabel}`;
  const img = document.getElementById('bone-click-image');
  img.src = entry.image;
  img.alt = '골학 번호 그림';   // 이미지 제목을 그대로 노출하면 정답 힌트가 되므로 일반 문구만
  document.getElementById('bone-click-original-btn').style.display = entry.original ? 'inline-block' : 'none';
  document.getElementById('bone-click-submit-btn').style.display = 'inline-block';
  document.getElementById('bone-click-next-btn').style.display = 'none';
  document.getElementById('bone-click-summary').innerHTML = '';

  renderBoneClickBoard();
}

function renderBoneClickBoard() {
  const st = boneRoundState;
  document.getElementById('bone-click-slots').innerHTML = st.slots.map(s => {
    let cls = 'bone-slot';
    if (st.graded) cls += s.correct ? ' bone-slot-correct' : ' bone-slot-wrong';
    else if (s.num === st.selectedSlotNum) cls += ' bone-slot-selected';
    const text = s.filled ? escHtml(s.filled.text) : '';
    const wrongBlock = (st.graded && !s.correct)
      ? `<div class="bone-slot-answer">정답: ${escHtml(s.answers.join(', '))}
           <button class="wrong-add-btn" onclick="event.stopPropagation();boneAddWrongClick(${s.num})">🔖</button>
         </div>`
      : '';
    return `
      <div class="${cls}" onclick="boneClickSlot(${s.num})">
        <span class="bone-slot-num">${s.num}</span>
        <span class="bone-slot-text">${text}</span>
        ${wrongBlock}
      </div>`;
  }).join('');

  document.getElementById('bone-click-bank').innerHTML = st.bank.map((c, i) =>
    c.used ? '' : `<span class="bone-chip" onclick="boneClickChip(${i})">${escHtml(c.text)}</span>`
  ).join('') || (st.graded ? '' : '<span style="color:#94a3b8;font-size:0.82rem;">모든 단어를 배치했습니다.</span>');
}

// 슬롯(번호) 클릭 — 채워져 있으면 되돌리고, 비어있으면 '채울 대상'으로 선택
function boneClickSlot(num) {
  const st = boneRoundState;
  if (st.graded) return;
  const slot = st.slots.find(s => s.num === num);
  if (slot.filled) {
    const chip = st.bank.find(c => c.text === slot.filled.text && c.sourceNum === slot.filled.sourceNum);
    if (chip) chip.used = false;
    slot.filled = null;
  }
  st.selectedSlotNum = num;
  renderBoneClickBoard();
}

// 단어은행 카드 클릭 — 선택된 슬롯(없으면 첫 빈 슬롯)에 채운다
function boneClickChip(idx) {
  const st = boneRoundState;
  if (st.graded) return;
  const chip = st.bank[idx];
  if (!chip || chip.used) return;

  let targetNum = st.selectedSlotNum;
  let target = targetNum != null ? st.slots.find(s => s.num === targetNum && !s.filled) : null;
  if (!target) target = st.slots.find(s => !s.filled);
  if (!target) return;

  target.filled = { text: chip.text, sourceNum: chip.sourceNum };
  chip.used = true;
  const nextEmpty = st.slots.find(s => !s.filled);
  st.selectedSlotNum = nextEmpty ? nextEmpty.num : null;
  renderBoneClickBoard();
}

function boneGradeClick() {
  const st = boneRoundState;
  if (st.graded) return;
  st.graded = true;
  st.slots.forEach(s => {
    const val = s.filled ? s.filled.text : '';
    s.correct = !!val && s.answers.some(a => boneNormalize(a) === boneNormalize(val));
  });
  const correctCount = st.slots.filter(s => s.correct).length;
  boneStats.total += st.slots.length;
  boneStats.correct += correctCount;
  boneUpdateScore();

  document.getElementById('bone-click-submit-btn').style.display = 'none';
  document.getElementById('bone-click-next-btn').style.display = 'inline-block';
  document.getElementById('bone-click-summary').innerHTML =
    `<b style="color:${correctCount === st.slots.length ? '#16a34a' : '#dc2626'};">
       ${correctCount} / ${st.slots.length}개 정답
     </b>`;
  renderBoneClickBoard();
  document.getElementById('bone-click-next-btn').focus();
}

function boneAddWrongClick(num) {
  const st = boneRoundState;
  const slot = st.slots.find(s => s.num === num);
  if (!slot) return;
  const q = boneRoundWrongQuestion(st.entry, num, slot.answers);
  openWrongModalForQuestion(q, q['문제'], null);
}

function boneClickShowOriginal() {
  if (boneRoundState) boneShowOriginalFor(boneRoundState.entry);
}

// ── 빈칸 한번에 쓰기: 번호별 입력창을 한 화면에 모두 두고 한 번에 채점 ──

function renderBoneWriteRound(entry) {
  const rows = entry.parts.map(p => ({ num: p.num, answers: p.answers, value: '', correct: null }));
  boneRoundState = { entry, rows, graded: false };

  document.getElementById('bone-write-scope-label').textContent = `📍 ${boneScopeLabel}`;
  const img = document.getElementById('bone-write-image');
  img.src = entry.image;
  img.alt = '골학 번호 그림';   // 이미지 제목을 그대로 노출하면 정답 힌트가 되므로 일반 문구만
  document.getElementById('bone-write-original-btn').style.display = entry.original ? 'inline-block' : 'none';
  document.getElementById('bone-write-submit-btn').style.display = 'inline-block';
  document.getElementById('bone-write-next-btn').style.display = 'none';
  document.getElementById('bone-write-summary').innerHTML = '';

  renderBoneWriteRows();
  const firstInput = document.querySelector('#bone-write-rows .bone-write-input');
  if (firstInput) firstInput.focus();
}

function renderBoneWriteRows() {
  const st = boneRoundState;
  document.getElementById('bone-write-rows').innerHTML = st.rows.map((r, i) => {
    const resultHtml = st.graded
      ? `<span class="bone-write-mark" style="color:${r.correct ? '#16a34a' : '#dc2626'};">${r.correct ? '✅' : '❌'}</span>
         ${!r.correct
            ? `<span class="bone-write-answer">정답: ${escHtml(r.answers.join(', '))}</span>
               <button class="wrong-add-btn" onclick="boneAddWrongWrite(${r.num})">🔖</button>`
            : ''}`
      : '';
    return `
      <div class="bone-write-row">
        <label class="bone-write-num">${r.num}번</label>
        <input type="text" class="bone-write-input" value="${escHtml(r.value)}"
               ${st.graded ? 'disabled' : ''}
               oninput="boneWriteInput(${r.num}, this.value)"
               onkeydown="boneWriteKey(event, ${i})" />
        ${resultHtml}
      </div>`;
  }).join('');
}

function boneWriteInput(num, val) {
  const row = boneRoundState.rows.find(r => r.num === num);
  if (row) row.value = val;
}

// Enter: 다음 입력창으로 이동, 마지막 칸이면 채점
function boneWriteKey(e, idx) {
  if (e.key !== 'Enter') return;
  e.preventDefault();
  if (boneRoundState.graded) { boneNextEntry(); return; }
  const inputs = document.querySelectorAll('#bone-write-rows .bone-write-input');
  if (inputs[idx + 1]) inputs[idx + 1].focus();
  else boneGradeWrite();
}

function boneGradeWrite() {
  const st = boneRoundState;
  if (st.graded) return;
  st.graded = true;
  st.rows.forEach(r => {
    r.correct = !!boneNormalize(r.value) && r.answers.some(a => boneNormalize(a) === boneNormalize(r.value));
  });
  const correctCount = st.rows.filter(r => r.correct).length;
  boneStats.total += st.rows.length;
  boneStats.correct += correctCount;
  boneUpdateScore();

  document.getElementById('bone-write-submit-btn').style.display = 'none';
  document.getElementById('bone-write-next-btn').style.display = 'inline-block';
  document.getElementById('bone-write-summary').innerHTML =
    `<b style="color:${correctCount === st.rows.length ? '#16a34a' : '#dc2626'};">
       ${correctCount} / ${st.rows.length}개 정답
     </b>`;
  renderBoneWriteRows();
  document.getElementById('bone-write-next-btn').focus();
}

function boneAddWrongWrite(num) {
  const st = boneRoundState;
  const row = st.rows.find(r => r.num === num);
  if (!row) return;
  const q = boneRoundWrongQuestion(st.entry, num, row.answers);
  openWrongModalForQuestion(q, q['문제'], null);
}

function boneWriteShowOriginal() {
  if (boneRoundState) boneShowOriginalFor(boneRoundState.entry);
}
