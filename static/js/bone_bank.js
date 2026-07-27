// ══════════════════════════════════════════════
// bone_bank.js — 🦴 뼈 문제은행 (그림 보고 이름 맞히기, 주관식, 랜덤 무한 출제)
//   common.js 이후 로드. 데이터: /static/data/bone_bank.json
//   LLM/서버 없이 순수 정적 데이터로 동작 (비용 0).
// ══════════════════════════════════════════════

let boneAll = [];          // 전체 문제
let boneQueue = [];        // 남은 문제 (섞인 순서, 끝에서 pop)
let boneCurrent = null;    // 현재 문제
let boneAnswered = false;  // 현재 문제 채점 완료 여부
let boneLoaded = false;    // 데이터 로드 여부 (탭 재진입 시 진행상황 유지)
let boneStats = { correct: 0, total: 0 };

async function loadBoneBank() {
  if (boneLoaded) return;             // 이미 불러왔으면 진행상황 유지
  const emptyEl = document.getElementById('bone-empty');
  try {
    const r = await fetch('/static/data/bone_bank.json');
    const data = await r.json();
    // image·answers가 제대로 있는 문제만 사용
    boneAll = (data.questions || []).filter(
      q => q && q.image && Array.isArray(q.answers) && q.answers.length
    );
    if (!boneAll.length) {
      emptyEl.innerHTML = '아직 등록된 문제가 없습니다. <code>static/data/bone_bank.json</code> 에 문제를 추가하세요.';
      return;
    }
    boneLoaded = true;
    boneStats = { correct: 0, total: 0 };
    boneRefillQueue();
    document.getElementById('bone-empty').style.display = 'none';
    document.getElementById('bone-quiz').style.display = 'block';
    boneUpdateScore();
    boneNextQuestion();
  } catch (e) {
    emptyEl.textContent = '문제 데이터를 불러오지 못했습니다.';
  }
}

// Fisher-Yates 셔플 (배열을 섞어 반환)
function boneShuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

function boneRefillQueue() {
  boneQueue = boneShuffle(boneAll.slice());
  // 직전에 푼 문제가 바로 또 나오지 않게 (pop은 배열 끝에서 꺼냄)
  if (boneCurrent && boneQueue.length > 1 &&
      boneQueue[boneQueue.length - 1].id === boneCurrent.id) {
    const last = boneQueue.length - 1;
    [boneQueue[0], boneQueue[last]] = [boneQueue[last], boneQueue[0]];
  }
}

function boneNextQuestion() {
  if (!boneQueue.length) boneRefillQueue();   // 다 풀면 다시 섞어 무한 반복
  boneCurrent = boneQueue.pop();
  boneAnswered = false;
  renderBoneQuestion();
}

function renderBoneQuestion() {
  const q = boneCurrent;
  const img = document.getElementById('bone-image');
  img.src = q.image;
  img.alt = '뼈 그림';

  const input = document.getElementById('bone-input');
  input.value = '';
  input.disabled = false;

  document.getElementById('bone-result').innerHTML = '';
  // 힌트 접기
  const hint = document.getElementById('bone-hint');
  hint.classList.add('hidden');
  hint.textContent = q.hint ? ('💡 ' + q.hint) : '';
  document.getElementById('bone-hint-btn').style.display = q.hint ? 'inline-block' : 'none';
  // 버튼 상태
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

  const correct = boneCurrent.answers.some(
    a => boneNormalize(a) === boneNormalize(val)
  );
  boneFinish(correct, correct ? '🎉 정답!' : '❌ 오답');
}

function boneGiveUp() {
  if (boneAnswered) return;
  boneFinish(false, '🙈 정답을 확인하세요');
}

// 채점 마무리: 결과 표시 + 정답 공개 + 점수 갱신 + 다음 버튼
function boneFinish(correct, headline) {
  boneAnswered = true;
  boneStats.total += 1;
  if (correct) boneStats.correct += 1;

  const answersTxt = boneCurrent.answers.map(escHtml).join(', ');
  const color = correct ? '#16a34a' : '#dc2626';
  document.getElementById('bone-result').innerHTML =
    `<div style="margin-top:12px;font-weight:700;color:${color};">${headline}</div>` +
    `<div style="margin-top:4px;font-size:0.9rem;color:#1e293b;">정답: <b>${answersTxt}</b></div>`;

  document.getElementById('bone-input').disabled = true;
  document.getElementById('bone-check-btn').style.display = 'none';
  document.getElementById('bone-giveup-btn').style.display = 'none';
  document.getElementById('bone-next-btn').style.display = 'inline-block';
  boneUpdateScore();
  document.getElementById('bone-next-btn').focus();
}

function boneToggleHint() {
  document.getElementById('bone-hint').classList.toggle('hidden');
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
