// ══════════════════════════════════════════════
// bone_bank.js — 🦴 골학 문제은행
//   번호가 매겨진 이미지에서 특정 번호 부위의 이름을 맞히는 주관식 (랜덤 무한 출제).
//   common.js 이후 로드. 데이터: /static/data/bone_bank.json
//   LLM/서버 없이 순수 정적 데이터로 동작 (비용 0).
//   틀린(또는 원하는) 문제는 '오답에 넣기'로 오답 노트에 저장 (wrong_note.js 재사용).
// ══════════════════════════════════════════════

let boneEntries = [];      // 이미지 단위 데이터 (title, image, original, parts)
let boneQueue = [];        // 남은 출제 인스턴스 {entry, part} (섞인 순서, 끝에서 pop)
let boneCurrent = null;    // 현재 출제 인스턴스 {entry, part}
let boneAnswered = false;  // 현재 문제 채점 완료 여부
let boneLoaded = false;    // 데이터 로드 여부 (탭 재진입 시 진행상황 유지)
let boneStats = { correct: 0, total: 0 };

async function loadBoneBank() {
  if (boneLoaded) return;             // 이미 불러왔으면 진행상황 유지
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
      return;
    }
    boneLoaded = true;
    boneStats = { correct: 0, total: 0 };
    boneBuildQueue();
    document.getElementById('bone-empty').style.display = 'none';
    document.getElementById('bone-quiz').style.display = 'block';
    boneUpdateScore();
    boneNextQuestion();
  } catch (e) {
    emptyEl.textContent = '문제 데이터를 불러오지 못했습니다.';
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
    parts,
  };
}

// Fisher-Yates 셔플
function boneShuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

// 모든 (이미지 × 부위)를 하나의 출제 큐로 펼쳐 섞음 → 모든 번호가 골고루 출제
function boneBuildQueue() {
  const all = [];
  boneEntries.forEach(entry => entry.parts.forEach(part => all.push({ entry, part })));
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
  return a && b && a.entry.id === b.entry.id && a.part.num === b.part.num;
}

function boneNextQuestion() {
  if (!boneQueue.length) boneBuildQueue();   // 다 풀면 다시 섞어 무한 반복
  boneCurrent = boneQueue.pop();
  boneAnswered = false;
  renderBoneQuestion();
}

// 현재 문제의 질문 문구 ("N번의 이름은?" / 구형이면 "이 뼈의 이름은?")
function boneQuestionLabel(inst) {
  return (inst && inst.part && inst.part.num != null)
    ? `${inst.part.num}번의 이름은?`
    : '이 뼈의 이름은?';
}

function renderBoneQuestion() {
  const inst = boneCurrent;
  const img = document.getElementById('bone-image');
  img.src = inst.entry.image;
  img.alt = (inst.entry.title || '골학') + ' 번호 그림';

  document.getElementById('bone-question-label').textContent = boneQuestionLabel(inst);

  const input = document.getElementById('bone-input');
  input.value = '';
  input.disabled = false;

  document.getElementById('bone-result').innerHTML = '';

  // '원본 보기' 버튼: 원본 이미지가 있을 때만
  document.getElementById('bone-original-btn').style.display =
    inst.entry.original ? 'inline-block' : 'none';

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

  const correct = boneCurrent.part.answers.some(
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

  const answersTxt = boneCurrent.part.answers.map(escHtml).join(', ');
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
  return {
    '유형': '단답형',
    '문제': `[골학] ${title} — ${boneQuestionLabel(inst)}`,
    '정답': inst.part.answers.join(', '),
    '이미지': inst.entry.image,               // 번호 이미지(문제)
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
function boneShowOriginal() {
  if (!boneCurrent || !boneCurrent.entry.original) return;
  document.getElementById('bone-original-image').src = boneCurrent.entry.original;
  document.getElementById('bone-original-title').textContent = boneCurrent.entry.title || '';
  document.getElementById('bone-original-modal').classList.add('open');
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
