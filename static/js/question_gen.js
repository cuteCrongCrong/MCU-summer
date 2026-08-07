// ══════════════════════════════════════════════
// question_gen.js — 문제 생성기 탭 (업로드·세션·이력·모델·생성)
//   common.js 이후 로드. escHtml/renderQuestions/TYPE_BADGE 등은 common.js 것을 재사용.
// ══════════════════════════════════════════════

const GEN_MAX_FILES = 7;   // llm.py MAX_FILES_PER_SIDE와 같은 값으로 유지

// ── 파일 드래그앤드롭 UX (여러 개 지원) ──
function setupDrop(dropId, inputId, nameId) {
  const drop = document.getElementById(dropId);
  const input = document.getElementById(inputId);
  const nameEl = document.getElementById(nameId);

  const show = () => {
    const files = Array.from(input.files || []);
    if (!files.length) { nameEl.textContent = ''; return; }
    nameEl.textContent = `✅ ${files.length}개 — ${files.map(f => f.name).join(', ')}`;
    // 상한을 넘으면 빨갛게 — 생성 버튼을 누르기 전에 알아채도록
    nameEl.style.color = files.length > GEN_MAX_FILES ? '#dc2626' : '';
  };

  input.addEventListener('change', show);
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('drag-over');
    const pdfs = Array.from(e.dataTransfer.files).filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if (!pdfs.length) return;
    const dt = new DataTransfer();
    pdfs.forEach(f => dt.items.add(f));
    input.files = dt.files;
    show();
  });
}
setupDrop('drop-lecture', 'lecture-file', 'lecture-name');
setupDrop('drop-exam',    'exam-file',    'exam-name');

// ── 기출 반영 강도 슬라이더 ──
function updateWeight() {
  const w = parseInt(document.getElementById('weight').value);
  document.getElementById('weight-value').textContent = w;
  let hint;
  if (w >= 8)      hint = '🔒 기출 개념·문투·형식을 거의 그대로 재현하고, 기출과 겹치는 주제 위주로 출제합니다.';
  else if (w >= 4) hint = '⚖️ 기출 경향과 형식을 균형 있게 반영하되, 강의자료 개념도 폭넓게 활용합니다.';
  else             hint = '🌱 기출은 참고만 하고 강의자료 핵심 개념 위주로 자유롭게 출제합니다. 형식은 느슨하게 맞춥니다.';
  document.getElementById('weight-hint').textContent = hint;
}
updateWeight();

// ── 유형별 문제 수 직접 설정 (자동/수동 전환) ──
const MANUAL_COUNT_TYPES = ['객관식', '빈칸채우기', '단답형', '서술형'];

function toggleManualCount() {
  const manual = document.getElementById('manual-count-toggle').checked;
  document.getElementById('auto-count-row').classList.toggle('hidden', manual);
  // 희소 유형 보존은 자동 배분에만 적용된다 — 수동 모드에서는 같이 숨긴다
  document.getElementById('preserve-types-row').classList.toggle('hidden', manual);
  document.getElementById('manual-count-row').classList.toggle('hidden', !manual);
  if (manual) updateManualTotal();
}

function updateManualTotal() {
  const total = MANUAL_COUNT_TYPES.reduce(
    (sum, t) => sum + (parseInt(document.getElementById('count-' + t).value) || 0), 0
  );
  document.getElementById('manual-total-value').textContent = total;
  return total;
}

// ── 문제 생성기: 설정 화면 / 결과 화면 전환 ──
function showGenResult() {
  document.getElementById('gen-input-view').classList.add('hidden');
  document.getElementById('gen-result-view').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function showGenInput() {
  document.getElementById('gen-result-view').classList.add('hidden');
  document.getElementById('gen-input-view').classList.remove('hidden');
  document.getElementById('status-box').style.display = 'none';  // 완료된 진행 상태 숨김
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

// ── 현재 활성 세션 (설정 시 재분석 없이 생성) ──
let currentSessionId = null;

function setActiveSession(id, name) {
  currentSessionId = id;
  const bar = document.getElementById('active-session-bar');
  document.getElementById('active-session-text').innerHTML =
    `✅ 현재 세션: <b>${escHtml(name || ('#' + id))}</b> — 재분석 없이 바로 생성합니다.`;
  bar.style.display = 'flex';
  document.getElementById('generate-btn').textContent = '✨ 이 세션으로 문제 생성 (분석 생략)';
  // 세션 모드 진입 → 남아있는 파일 입력 비워 실수로 재분석되지 않게
  ['lecture-file','exam-file'].forEach(id => { document.getElementById(id).value = ''; });
  document.getElementById('lecture-name').textContent = '';
  document.getElementById('exam-name').textContent = '';
  updateSessionListHighlight();
}

function clearSession() {
  currentSessionId = null;
  document.getElementById('active-session-bar').style.display = 'none';
  document.getElementById('generate-btn').textContent = '✨ 예상문제 생성하기';
  updateSessionListHighlight();
}

// ── 메인 생성 함수 ──
async function generate() {
  const lectureFiles = Array.from(document.getElementById('lecture-file').files || []);
  const examFiles    = Array.from(document.getElementById('exam-file').files || []);
  const apiKey      = document.getElementById('api-key').value.trim();
  const weight      = document.getElementById('weight').value;
  const model       = document.getElementById('model-select').value;

  const manualMode = document.getElementById('manual-count-toggle').checked;
  let count, manualTargets = null;
  if (manualMode) {
    manualTargets = {};
    MANUAL_COUNT_TYPES.forEach(t => {
      manualTargets[t] = parseInt(document.getElementById('count-' + t).value) || 0;
    });
    count = updateManualTotal();
  } else {
    count = document.getElementById('count').value;
  }

  // 새 파일을 올렸다면 세션보다 파일 분석을 우선 (세션 자동 해제)
  if (lectureFiles.length && examFiles.length && currentSessionId) clearSession();

  const useSession = !!currentSessionId;

  // 유효성 검사
  if (!apiKey) return alert('API 키를 입력해주세요.');
  if (manualMode && (count < 1 || count > 30)) return alert('유형별 문제 수의 합계는 1~30개 사이여야 합니다.');
  if (!useSession) {
    if (!lectureFiles.length) return alert('강의자료 PDF를 업로드하거나, 저장된 세션을 선택해주세요.');
    if (!examFiles.length)    return alert('기출문제 PDF를 업로드하거나, 저장된 세션을 선택해주세요.');
    if (lectureFiles.length > GEN_MAX_FILES || examFiles.length > GEN_MAX_FILES) {
      return alert(`강의자료·기출은 각각 최대 ${GEN_MAX_FILES}개까지 올릴 수 있습니다.`);
    }
  }

  // UI 초기화
  document.getElementById('generate-btn').disabled = true;
  document.getElementById('status-box').style.display = 'block';
  clearErrors();
  document.getElementById('result-box').style.display   = 'none';
  document.getElementById('cancel-btn').style.display   = 'inline-block';
  renderSpend(null);   // 지난 회차의 사용량 표가 남아 보이지 않게
  resetSteps(useSession);

  // FormData 구성
  const form = new FormData();
  form.append('api_key', apiKey);
  form.append('count', count);
  if (manualTargets) form.append('type_targets', JSON.stringify(manualTargets));
  // 수동 모드면 서버가 무시하지만, 상태를 그대로 보내 화면과 요청을 일치시킨다
  if (document.getElementById('preserve-types').checked) form.append('preserve_types', '1');
  form.append('weight', weight);
  form.append('model', model);
  form.append('provider', currentProvider || '');
  form.append('title', document.getElementById('gen-title').value.trim());
  if (useSession) {
    form.append('session_id', currentSessionId);
  } else {
    // 같은 키로 여러 번 append → 서버에서 getlist('lecture')로 받는다
    lectureFiles.forEach(f => form.append('lecture', f));
    examFiles.forEach(f => form.append('exam', f));
  }

  genAbort = new AbortController();
  try {
    const result = await streamGenerate(form, genAbort.signal);
    // 분량 상한을 넘는 파일이 있으면 서버가 추출까지만 하고 멈춰 있다.
    // 확인을 받으면 파일 대신 토큰만 보내 이어서 돈다 (재추출 = Vision 재과금 방지).
    if (result && result.needsConfirm) {
      const go = await confirmTruncation(result.payload.warnings || [], result.payload);
      if (!go) {
        renderSpend(result.payload);   // 추출까지 쓴 양은 알려준다
      } else {
        const again = new FormData();
        for (const [k, v] of form.entries()) {
          if (k !== 'lecture' && k !== 'exam') again.append(k, v);
        }
        again.append('extract_token', result.payload.extract_token);
        resetSteps(false);
        await streamGenerate(again, genAbort.signal);
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      showError('생성을 중지했습니다.');
    } else if (err instanceof TypeError) {
      // fetch 자체가 실패(네트워크 끊김·서버 다운)했을 때만 TypeError가 난다.
      // 응답은 왔지만 화면에 그리다 난 오류는 아래 else에서 실제 원인을 보여준다.
      showError('서버 연결에 실패했습니다. Flask 서버가 실행 중인지 확인하세요.\n' + err.message);
    } else {
      showError('결과를 표시하는 중 오류가 발생했습니다 (서버 응답은 정상적으로 받았습니다).\n' + err.message);
    }
  } finally {
    genAbort = null;
    document.getElementById('generate-btn').disabled = false;
    document.getElementById('cancel-btn').style.display = 'none';
  }
}

// ── 스트리밍 (SSE) ──
// EventSource는 GET만 지원하는데 PDF를 POST로 보내야 하므로 fetch로 직접 읽는다.
let genAbort = null;

function cancelGenerate() {
  if (genAbort) genAbort.abort();
}

// 서버의 stage.key 와 화면의 step 요소 id 가 1:1로 대응한다
const STAGE_STEPS = ['extract', 'concepts', 'format', 'generate'];
const STEP_ICONS = { extract: '📄', concepts: '🧠', format: '🔍', generate: '✏️' };

// 단계별 진행 상황 — 전체 진행률 막대는 아래 가중치로 합산한다.
// (오래 걸리는 단계일수록 크게 — 실제 소요 시간에 대략 비례)
// 계산·표시는 common.js 의 createProgress 가 한다 (기출 주제 분석 탭과 공용).
const genProgress = createProgress({
  barId: 'overall-bar', pctId: 'overall-pct', stepPrefix: 'step-',
  weights: { extract: 15, concepts: 20, format: 20, generate: 45 },
});

function resetSteps(useSession) {
  // 저장된 세션을 재사용하면 분석 단계는 아예 실행되지 않는다 → 숨기고 진행률에서도 제외
  const active = useSession ? ['generate'] : STAGE_STEPS.slice();

  STAGE_STEPS.forEach(key => {
    const el = document.getElementById('step-' + key);
    el.style.display = active.includes(key) ? '' : 'none';
    el.className = 'step-item wait';
    el.querySelector('.step-icon').textContent = STEP_ICONS[key];
    const note = el.querySelector('.step-note');
    if (note) note.remove();
  });
  genProgress.reset(active);
}

// 스트리밍을 못 쓰는 환경 — 예전처럼 한 번에 받아서 그린다
async function fallbackGenerate(form, signal) {
  genProgress.stages.forEach(k => setStep('step-' + k, 'active'));
  const resp = await fetch('/generate', { method: 'POST', body: form, signal });
  genProgress.stages.forEach(k => {
    setStep('step-' + k, 'done');
    genProgress.completeStage(k);
  });

  if (!resp.ok) {
    showError(await describeHttpError(resp, '/generate'));
    return;
  }
  const data = await resp.json().catch(() => ({}));
  if (data.error) {
    showError(data.error + spendSuffix(data));
    return;
  }
  // 스트리밍 경로와 같은 형태로 호출부에 넘긴다 (분량 초과 → 확인 후 재요청)
  if (data.needs_confirm) {
    return { needsConfirm: true, payload: data };
  }
  if (!data.reused && data.session_id) {
    setActiveSession(data.session_id, data.session_name);
    loadSessions();
  }
  document.getElementById('result-box').style.display = 'block';
  renderQuestions(data.questions, data.raw, { paged: true });
  applyGenerationResult(data);
  showGenResult();
  archiveLoaded = false;   // 보관함 캐시 무효화 — 다음에 열 때 새 결과가 보이도록
}

// 방금 생성한 회차 — 결과 화면에서 이름을 붙일 때 대상이 된다
let lastGeneration = { id: null, title: '', ordinal: 0 };

// 결과 화면 제목. 이름을 붙였으면 그 이름으로, 아니면 '제N회'.
// '생성된 예상문제'는 어느 시험지인지 알려주지 않아 보관함 목록과 부르는 이름이
// 어긋났다 — 목록은 이름이 없으면 '제N회'로 부른다. 여기서도 같게 맞춘다.
// 회차 번호를 못 받은 경우(저장 실패 등)에만 예전 문구로 돌아간다.
// renderQuestions가 제목을 기본값으로 되돌리므로 반드시 그 뒤에 부른다.
function setResultTitle(title, ordinal) {
  const name = (title || '').trim() || (ordinal ? `제${ordinal}회` : '생성된 예상문제');
  document.getElementById('result-title').textContent = `📋 ${name}`;
}

// 생성 직후 결과를 화면에 반영 (제목 + 이름 변경 버튼 상태)
// 키 입력란 아래 한 줄 — 생성 전에 잔액을 확인하는 용도
async function loadCredits() {
  const info = currentProviderInfo();
  const bar  = document.getElementById('credits-bar');
  if (!info || !info.supports_credits) {      // 지원하지 않는 제공사면 아예 숨긴다
    bar.hidden = true;
    return;
  }
  bar.hidden = false;

  const text   = document.getElementById('credits-bar-text');
  const apiKey = document.getElementById('api-key').value.trim();
  if (!apiKey) {
    text.textContent = 'API 키를 입력하면 잔액을 조회합니다.';
    return;
  }
  text.textContent = '조회 중…';
  try {
    const resp = await fetch('/credits?provider=' + encodeURIComponent(currentProvider || ''),
                             { headers: { 'X-Api-Key': apiKey } });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.error) {
      text.textContent = '잔액 조회 실패 — ' + (data.error || resp.status);
      return;
    }
    const t = data.total || {};
    text.textContent = `남은 크레딧 ${fmtCredit(t.remaining)}`
      + (t.quota != null ? ` / 할당 ${fmtCredit(t.quota)}` : '')
      + (t.used  != null ? ` (누적 사용 ${fmtCredit(t.used)})` : '');
  } catch (err) {
    text.textContent = '잔액을 조회하지 못했습니다.';
  }
}

function applyGenerationResult(payload) {
  renderSpend(payload);
  lastGeneration = {
    id: payload.generation_id,
    title: (payload.title || '').trim(),
    ordinal: payload.ordinal || 0,      // 이름이 없을 때 '제N회'로 부르는 번호
  };
  setResultTitle(lastGeneration.title, lastGeneration.ordinal);
  // 제목 옆 아이콘 버튼 — 문구 대신 툴팁으로 상태를 알린다
  const btn = document.getElementById('result-rename-btn');
  const label = lastGeneration.title ? '이름 바꾸기' : '이름 붙이기';
  btn.style.display = lastGeneration.id ? '' : 'none';
  btn.title = label;
  btn.setAttribute('aria-label', label);
}

// 문제를 보고 나서 이름을 정하는 경우 — 결과 화면에서 바로 붙인다
async function renameCurrentResult() {
  if (!lastGeneration.id) return;
  const next = prompt('이 문제 세트의 이름을 입력하세요.\n(비우면 「제N회」로 표시됩니다)',
                      lastGeneration.title);
  if (next == null) return;

  const form = new FormData();
  form.append('title', next.trim());
  const resp = await fetch('/generation/' + lastGeneration.id + '/rename',
                           { method: 'POST', body: form });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) return alert(data.error || '이름을 바꾸지 못했습니다.');

  // ordinal 을 같이 넘긴다 — 이름을 지워 비우면 다시 '제N회'로 돌아가야 한다
  applyGenerationResult({ generation_id: lastGeneration.id, title: next.trim(),
                          ordinal: lastGeneration.ordinal });
  archiveLoaded = false;   // 보관함에도 반영되도록 캐시 무효화
}

async function streamGenerate(form, signal) {
  if (!canReadStream()) return fallbackGenerate(form, signal);

  const resp = await fetch('/generate/stream', { method: 'POST', body: form, signal });

  // 스트림이 시작되기 전 오류(키 누락 등)는 평범한 JSON으로 온다
  if (!resp.ok) {
    showError(await describeHttpError(resp, '/generate/stream'));
    return;
  }

  let finished = false;     // done/error 없이 스트림이 끊겼는지 판별

  // 프레임 끊기는 common.js 의 sseEvents 가 한다 (기출 주제 분석 탭과 공용)
  for await (const ev of sseEvents(resp)) {
    if (ev.type === 'stage') {
      setStep('step-' + ev.key, ev.status);
      if (ev.status === 'active') {
        genProgress.setStageProgress(ev.key, 0, ev.total || 0);
      } else if (ev.status === 'done') {
        genProgress.completeStage(ev.key);
      }
    } else if (ev.type === 'progress') {
      genProgress.setStageProgress(ev.key, ev.done, ev.total);
    } else if (ev.type === 'analysis') {
      // 분석 결과 자체는 화면에 표시하지 않는다(결과 화면은 문제만 보여준다).
      // 다만 이 이벤트로 새로 저장된 세션을 알 수 있으므로 그것만 반영하고,
      // 문제 컨테이너를 미리 열어둔다. 화면 전환은 문제까지 다 나온 뒤 done에서.
      const a = ev.payload;
      if (!a.reused && a.session_id) {
        setActiveSession(a.session_id, a.session_name);
        loadSessions();
      }
      document.getElementById('result-box').style.display = 'block';
    } else if (ev.type === 'question') {
      // 문제는 모아뒀다가 done에서 한 번에 보여준다 (진행률만 실시간)
      genProgress.setStageProgress('generate', ev.index, ev.total);
    } else if (ev.type === 'done') {
      renderQuestions(ev.payload.questions, ev.payload.raw, { paged: true });
      applyGenerationResult(ev.payload);
      showGenResult();
      archiveLoaded = false;   // 보관함 캐시 무효화 — 다음에 열 때 새 결과가 보이도록
      finished = true;
    } else if (ev.type === 'needs_confirm') {
      // 분량 초과 — 추출까지만 하고 멈춘 상태. 호출부가 확인을 받아 이어서 진행한다.
      return { needsConfirm: true, payload: ev.payload };
    } else if (ev.type === 'error') {
      showError((ev.message || '생성 중 오류가 발생했습니다.') + spendSuffix(ev));
      return;
    }
  }

  // done/error 없이 연결이 끊긴 경우 — 조용히 멈춘 것처럼 보이지 않도록 알린다
  if (!finished) {
    showError('생성이 끝나기 전에 서버와의 연결이 끊겼습니다. 서버 상태를 확인하고 다시 시도해주세요.');
  }
}

// ── 세션 관리 ──
let sessionCache = [];
// 세션이 쌓여도 생성 동선(업로드·생성 버튼)이 아래로 밀리지 않도록 기본은 최근 3개만.
const SESSION_PREVIEW_COUNT = 3;
let sessionListExpanded = false;

function toggleSessionList() {
  sessionListExpanded = !sessionListExpanded;
  renderSessionList();
}

async function loadSessions() {
  const listEl = document.getElementById('session-list');
  try {
    const resp = await fetch('/sessions');
    const data = await resp.json();
    sessionCache = data.sessions || [];
    renderSessionList();
  } catch (err) {
    listEl.textContent = '세션 목록을 불러오지 못했습니다.';
  }
}

function renderSessionList() {
  const listEl = document.getElementById('session-list');
  if (!sessionCache.length) {
    listEl.innerHTML = '<span style="color:#94a3b8;">저장된 세션이 없습니다. 파일을 분석하면 자동으로 저장됩니다.</span>';
    return;
  }
  // 최근 것부터 3개만 (서버가 id 내림차순으로 준다).
  // 선택된 세션이 그 밖에 있으면 함께 보여준다 — '선택됨'이 안 보이면 혼란스럽다.
  let visible = sessionCache;
  let hiddenCount = 0;
  if (!sessionListExpanded && sessionCache.length > SESSION_PREVIEW_COUNT) {
    visible = sessionCache.slice(0, SESSION_PREVIEW_COUNT);
    if (currentSessionId && !visible.some(s => s.id === currentSessionId)) {
      const active = sessionCache.find(s => s.id === currentSessionId);
      if (active) visible = visible.concat(active);
    }
    hiddenCount = sessionCache.length - visible.length;
  }

  listEl.innerHTML = visible.map(s => {
    const ts = s.type_stats || {};
    const compo = ts['총문항'] ? formatTypeCounts(ts) : '';
    const active = (s.id === currentSessionId);
    return `
    <div class="session-row" data-id="${s.id}" style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1.5px solid ${active ? '#6ee7b7' : '#e2e8f0'};background:${active ? '#ecfdf5' : '#fff'};border-radius:10px;margin-top:8px;">
      <div style="min-width:0;flex:1;">
        <div style="font-weight:600;color:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(s.name)}</div>
        <div style="font-size:0.75rem;color:#94a3b8;margin-top:2px;">${escHtml(s.created_at || '')} · ${escHtml(providerLabel(s.provider))} / ${escHtml(s.model || '')}${compo ? ' · ' + compo : ''}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;">
        <button onclick="useSessionRow(${s.id})" style="font-size:0.78rem;padding:5px 10px;border:none;border-radius:7px;background:#2f5c50;color:#fff;cursor:pointer;">${active ? '선택됨' : '불러오기'}</button>
        <button onclick="renameSessionRow(${s.id})" title="이름 변경" style="font-size:0.78rem;padding:5px 8px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;cursor:pointer;">✏️</button>
        <button onclick="deleteSessionRow(${s.id})" title="삭제" style="font-size:0.78rem;padding:5px 8px;border:1px solid #fecaca;border-radius:7px;background:#fff;color:#dc2626;cursor:pointer;">🗑️</button>
      </div>
    </div>`;
  }).join('');

  if (hiddenCount > 0) {
    listEl.innerHTML += `
      <button type="button" class="session-more-btn" onclick="toggleSessionList()">
        ▾ 이전 세션 ${hiddenCount}개 더 보기
      </button>`;
  } else if (sessionListExpanded && sessionCache.length > SESSION_PREVIEW_COUNT) {
    listEl.innerHTML += `
      <button type="button" class="session-more-btn" onclick="toggleSessionList()">
        ▴ 최근 ${SESSION_PREVIEW_COUNT}개만 보기
      </button>`;
  }
}

function updateSessionListHighlight() {
  renderSessionList();
}

// 세션을 이번 생성의 활성 세션으로 지정한다.
// 예전에는 여기서 분석 요약을 결과 화면에 띄웠지만(showGenResult), 분석 요약을
// 없앤 뒤로는 띄울 것이 없어 빈 결과 화면이 된다. 그래서 화면을 전환하지 않고
// 입력 화면에 머문다 — setActiveSession이 초록색 현재 세션 바를 띄우고,
// 생성 버튼 문구를 바꾸고, 목록의 버튼을 '선택됨'으로 만들어 준다.
//
// fetch는 표시할 데이터가 아니라 세션이 실제로 존재하는지(그리고 내 것인지)
// 확인하는 용도로만 남긴다. 다른 탭에서 지운 세션을 고르는 경우를 잡는다.
async function useSessionRow(id) {
  try {
    const resp = await fetch('/session/' + id);
    const s = await resp.json();
    if (!resp.ok || s.error) return alert(s.error || '세션을 불러오지 못했습니다.');
    setActiveSession(s.id, s.name);
  } catch (err) {
    alert('세션을 불러오지 못했습니다.');
  }
}

async function renameSessionRow(id) {
  const cur = (sessionCache.find(s => s.id === id) || {}).name || '';
  const name = prompt('새 세션 이름을 입력하세요.', cur);
  if (name == null || !name.trim()) return;
  const form = new FormData();
  form.append('name', name.trim());
  await fetch('/session/' + id + '/rename', { method: 'POST', body: form });
  await loadSessions();
  if (id === currentSessionId) setActiveSession(id, name.trim());
}

async function deleteSessionRow(id) {
  if (!confirm('이 세션을 삭제할까요? 저장된 분석 결과와 생성 이력이 모두 사라집니다.')) return;
  await fetch('/session/' + id, { method: 'DELETE' });
  if (id === currentSessionId) clearSession();
  await loadSessions();
}

// 생성 이력 조회·열람·삭제는 '🗂️ 생성한 문제' 탭(archive.js)으로 옮겼다.
// 생성 탭은 '만들기'만 담당한다.

// ── LLM 제공사 (/providers) ──
// 제공사마다 키 형식이 달라서, 전환해도 서로 섞이지 않도록 메모리에만 따로 담아둔다.
// (localStorage에는 저장하지 않음 — 화면에 안내한 "브라우저에 저장되지 않음" 약속 유지)
let providers = [];
let currentProvider = null;
const apiKeyByProvider = {};

async function loadProviders() {
  try {
    const resp = await fetch('/providers');
    const data = await resp.json();
    providers = data.providers || [];
    if (!providers.length) return;
    renderProviders();
    selectProvider(data.default || providers[0].name);
  } catch (err) {
    showError('LLM 제공사 목록을 불러오지 못했습니다.');
  }
}

function renderProviders() {
  document.getElementById('provider-select').innerHTML = providers.map(p =>
    `<option value="${escHtml(p.name)}">${escHtml(p.label)}</option>`
  ).join('');
}

function currentProviderInfo() {
  return providers.find(p => p.name === currentProvider) || null;
}

// 저장된 세션·이력에 어떤 제공사로 만든 것인지 표시 (목록이 먼저 그려질 수 있어 폴백 둠)
function providerLabel(name) {
  if (!name) return '';
  const info = providers.find(p => p.name === name);
  return info ? info.label : name;
}

function selectProvider(name) {
  const info = providers.find(p => p.name === name);
  if (!info) return;

  // 전환 전 입력해둔 키를 이전 제공사 쪽에 보관
  const keyInput = document.getElementById('api-key');
  if (currentProvider) apiKeyByProvider[currentProvider] = keyInput.value;

  currentProvider = name;
  document.getElementById('provider-select').value = name;   // 코드로 호출된 경우도 동기화

  document.getElementById('api-key-label').textContent = `🔑 ${info.label} API Key`;
  keyInput.placeholder = info.key_placeholder || 'API 키 입력';
  keyInput.value = apiKeyByProvider[name] || '';
  renderKeyHelp(info, 'key-help');
  loadCredits();

  // 제공사가 바뀌면 이전 모델 목록은 무효 → 기본 모델만 남기고 다시 조회
  populateModels([info.default_model]);
  loadModels();
}

// ── 모델 목록 (/models 자동 로드) ──
async function loadModels() {
  const apiKey = document.getElementById('api-key').value.trim();
  const msg = document.getElementById('model-load-msg');
  if (!apiKey) {
    msg.textContent = '(API 키 입력 시 자동 로드)';
    msg.style.color = '#94a3b8';
    return;
  }
  msg.textContent = '불러오는 중…';
  msg.style.color = '#4f8a76';
  loadCredits();          // 키가 들어온 시점 — 잔액도 같이 갱신
  try {
    const url = '/models?provider=' + encodeURIComponent(currentProvider || '');
    const resp = await fetch(url, { headers: { 'X-Api-Key': apiKey } });
    const data = await resp.json();
    const models = data.models || [];
    if (models.length) {
      populateModels(models);
      msg.textContent = data.error ? '기본 목록 (조회 실패)' : `${models.length}개 모델`;
      msg.style.color = data.error ? '#d97706' : '#16a34a';
    } else {
      msg.textContent = '사용 가능한 모델이 없습니다';
      msg.style.color = '#d97706';
    }
  } catch (err) {
    msg.textContent = '모델 목록을 불러오지 못했습니다';
    msg.style.color = '#dc2626';
  }
}

function populateModels(models) {
  const sel = document.getElementById('model-select');
  const prev = sel.value;                       // 기존 선택 유지 시도
  const defaultModel = (currentProviderInfo() || {}).default_model;
  sel.innerHTML = models.map(m => {
    const label = (m === defaultModel) ? `${m} (기본)` : m;
    return `<option value="${escHtml(m)}">${escHtml(label)}</option>`;
  }).join('');
  // 선택값 복원: 이전 선택 > 기본모델 > 첫 항목
  if (models.includes(prev))                sel.value = prev;
  else if (models.includes(defaultModel))   sel.value = defaultModel;
}

function showError(msg) {
  // 결과 화면으로 넘어간 뒤(스트리밍 도중 오류)에는 입력 화면의 오류 상자가 보이지 않으므로
  // 현재 보이는 화면 쪽에 표시한다.
  const onResult = !document.getElementById('gen-result-view').classList.contains('hidden');
  const box = document.getElementById(onResult ? 'result-error-box' : 'error-box');
  box.textContent = '⚠️ ' + msg;
  box.style.display = 'block';
}

function clearErrors() {
  ['error-box', 'result-error-box'].forEach(id => {
    document.getElementById(id).style.display = 'none';
  });
}

// ── 초기 로드 ──
loadSessions();
loadProviders();   // 제공사 선택 → 기본 제공사로 모델 목록까지 이어서 로드
