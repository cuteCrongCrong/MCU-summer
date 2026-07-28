// ══════════════════════════════════════════════
// question_gen.js — 문제 생성기 탭 (업로드·세션·이력·모델·생성·분석요약)
//   common.js 이후 로드. escHtml/renderQuestions/TYPE_BADGE 등은 common.js 것을 재사용.
// ══════════════════════════════════════════════

// ── 파일 드래그앤드롭 UX ──
function setupDrop(dropId, inputId, nameId) {
  const drop = document.getElementById(dropId);
  const input = document.getElementById(inputId);
  const nameEl = document.getElementById(nameId);

  input.addEventListener('change', () => {
    if (input.files[0]) nameEl.textContent = '✅ ' + input.files[0].name;
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('drag-over');
    const file = e.dataTransfer.files[0];
    if (file && file.name.endsWith('.pdf')) {
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      nameEl.textContent = '✅ ' + file.name;
    }
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
  loadHistory(id);
}

function clearSession() {
  currentSessionId = null;
  document.getElementById('active-session-bar').style.display = 'none';
  document.getElementById('generate-btn').textContent = '✨ 예상문제 생성하기';
  document.getElementById('history-card').style.display = 'none';
  updateSessionListHighlight();
}

// ── 메인 생성 함수 ──
async function generate() {
  const lectureFile = document.getElementById('lecture-file').files[0];
  const examFile    = document.getElementById('exam-file').files[0];
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
  if (lectureFile && examFile && currentSessionId) clearSession();

  const useSession = !!currentSessionId;

  // 유효성 검사
  if (!apiKey) return alert('API 키를 입력해주세요.');
  if (manualMode && (count < 1 || count > 30)) return alert('유형별 문제 수의 합계는 1~30개 사이여야 합니다.');
  if (!useSession) {
    if (!lectureFile) return alert('강의자료 PDF를 업로드하거나, 저장된 세션을 선택해주세요.');
    if (!examFile)    return alert('기출문제 PDF를 업로드하거나, 저장된 세션을 선택해주세요.');
  }

  // UI 초기화
  document.getElementById('generate-btn').disabled = true;
  document.getElementById('status-box').style.display = 'block';
  document.getElementById('error-box').style.display  = 'none';
  document.getElementById('analysis-box').style.display = 'none';
  document.getElementById('result-box').style.display   = 'none';
  ['step1','step2','step3','step4'].forEach(s => setStep(s, 'wait'));

  // 단계 애니메이션 (실제로는 서버에서 한번에 처리 — UI 피드백용)
  setStep('step1', 'active');
  await delay(400);
  setStep('step1', 'done'); setStep('step2', 'active');

  // FormData 구성
  const form = new FormData();
  form.append('api_key', apiKey);
  form.append('count', count);
  if (manualTargets) form.append('type_targets', JSON.stringify(manualTargets));
  form.append('weight', weight);
  form.append('model', model);
  if (useSession) {
    form.append('session_id', currentSessionId);
  } else {
    form.append('lecture', lectureFile);
    form.append('exam', examFile);
  }

  try {
    const resp = await fetch('/generate', { method: 'POST', body: form });

    setStep('step2', 'done'); setStep('step3', 'active');
    await delay(300);
    setStep('step3', 'done'); setStep('step4', 'active');
    await delay(300);
    setStep('step4', 'done'); setStep('step5', 'active');

    const data = await resp.json();

    setStep('step5', 'done');

    if (!resp.ok || data.error) {
      showError(data.error || '알 수 없는 오류가 발생했습니다.');
      return;
    }

    // 새 분석이면 세션이 저장됨 → 활성 세션으로 설정하고 목록 갱신
    if (!data.reused && data.session_id) {
      setActiveSession(data.session_id, data.session_name);
      loadSessions();
    }

    renderAnalysis(data.concepts, data.sample_questions, data.format_analysis, data.exam_concepts, data.priority_topics, data.type_stats, data.type_targets, data.source_info);
    document.getElementById('result-box').style.display = 'block';
    renderQuestions(data.questions, data.raw);
    showGenResult();   // 결과를 다음 페이지로 표시

    // 생성 이력 갱신 (방금 결과가 저장됨)
    if (currentSessionId) loadHistory(currentSessionId);

  } catch (err) {
    showError('서버 연결에 실패했습니다. Flask 서버가 실행 중인지 확인하세요.\n' + err.message);
  } finally {
    document.getElementById('generate-btn').disabled = false;
  }
}

// ── 세션 관리 ──
let sessionCache = [];

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
  listEl.innerHTML = sessionCache.map(s => {
    const ts = s.type_stats || {};
    const compo = ts['총문항'] ? formatTypeCounts(ts) : '';
    const active = (s.id === currentSessionId);
    return `
    <div class="session-row" data-id="${s.id}" style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1.5px solid ${active ? '#6ee7b7' : '#e2e8f0'};background:${active ? '#ecfdf5' : '#fff'};border-radius:10px;margin-top:8px;">
      <div style="min-width:0;flex:1;">
        <div style="font-weight:600;color:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${escHtml(s.name)}</div>
        <div style="font-size:0.75rem;color:#94a3b8;margin-top:2px;">${escHtml(s.created_at || '')} · ${escHtml(s.model || '')}${compo ? ' · ' + compo : ''}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;">
        <button onclick="useSessionRow(${s.id})" style="font-size:0.78rem;padding:5px 10px;border:none;border-radius:7px;background:#2563eb;color:#fff;cursor:pointer;">${active ? '선택됨' : '불러오기'}</button>
        <button onclick="renameSessionRow(${s.id})" title="이름 변경" style="font-size:0.78rem;padding:5px 8px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;cursor:pointer;">✏️</button>
        <button onclick="deleteSessionRow(${s.id})" title="삭제" style="font-size:0.78rem;padding:5px 8px;border:1px solid #fecaca;border-radius:7px;background:#fff;color:#dc2626;cursor:pointer;">🗑️</button>
      </div>
    </div>`;
  }).join('');
}

function updateSessionListHighlight() {
  renderSessionList();
}

async function useSessionRow(id) {
  // 세션 상세를 불러와 분석 결과를 즉시 표시 + 활성 세션 설정
  try {
    const resp = await fetch('/session/' + id);
    const s = await resp.json();
    if (!resp.ok || s.error) return alert(s.error || '세션을 불러오지 못했습니다.');
    setActiveSession(s.id, s.name);
    renderAnalysis(s.concepts, s.sample_questions, s.format_analysis, s.exam_concepts, s.priority_topics, s.type_stats, {}, s.source_info);
    document.getElementById('result-box').style.display = 'none';  // 아직 생성된 문제 없음 (분석 미리보기만)
    showGenResult();
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

// ── 생성 이력 ──
async function loadHistory(sid) {
  const card = document.getElementById('history-card');
  const listEl = document.getElementById('history-list');
  card.style.display = 'block';
  listEl.textContent = '불러오는 중…';
  try {
    const resp = await fetch('/session/' + sid + '/generations');
    const data = await resp.json();
    renderHistory(data.generations || []);
  } catch (err) {
    listEl.textContent = '생성 이력을 불러오지 못했습니다.';
  }
}

function renderHistory(gens) {
  const listEl = document.getElementById('history-list');
  if (!gens.length) {
    listEl.innerHTML = '<span style="color:#94a3b8;">아직 생성 이력이 없습니다. 문제를 생성하면 여기에 저장됩니다.</span>';
    return;
  }
  listEl.innerHTML = gens.map(g => {
    const tt = g.type_targets || {};
    const hasTt = Object.keys(TYPE_BADGE).some(t => (tt[t] || 0) > 0);
    const compo = hasTt ? formatTypeCounts(tt) : '';
    return `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1.5px solid #e2e8f0;background:#fff;border-radius:10px;margin-top:8px;">
      <div style="min-width:0;flex:1;">
        <div style="font-weight:600;color:#1e293b;">${escHtml(g.created_at || '')} · ${g.num_questions}문제</div>
        <div style="font-size:0.75rem;color:#94a3b8;margin-top:2px;">강도 ${g.weight}/10 · ${escHtml(g.model || '')}${compo ? ' · ' + compo : ''}</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;">
        <button onclick="viewGeneration(${g.id})" style="font-size:0.78rem;padding:5px 12px;border:none;border-radius:7px;background:#0ea5e9;color:#fff;cursor:pointer;">보기</button>
        <button onclick="deleteGeneration(${g.id})" title="삭제" style="font-size:0.78rem;padding:5px 8px;border:1px solid #fecaca;border-radius:7px;background:#fff;color:#dc2626;cursor:pointer;">🗑️</button>
      </div>
    </div>`;
  }).join('');
}

async function viewGeneration(gid) {
  try {
    const resp = await fetch('/generation/' + gid);
    const g = await resp.json();
    if (!resp.ok || g.error) return alert(g.error || '이력을 불러오지 못했습니다.');
    document.getElementById('analysis-box').style.display = 'none';  // 이력엔 분석 요약 없음
    document.getElementById('result-box').style.display = 'block';
    renderQuestions(g.questions, g.raw);
    showGenResult();
  } catch (err) {
    alert('이력을 불러오지 못했습니다.');
  }
}

async function deleteGeneration(gid) {
  if (!confirm('이 생성 이력을 삭제할까요?')) return;
  await fetch('/generation/' + gid, { method: 'DELETE' });
  if (currentSessionId) loadHistory(currentSessionId);
}

// ── 모델 목록 (/models 자동 로드) ──
const DEFAULT_MODEL = 'claude-sonnet-4-5';

async function loadModels() {
  const apiKey = document.getElementById('api-key').value.trim();
  const msg = document.getElementById('model-load-msg');
  if (!apiKey) {
    msg.textContent = '(API 키 입력 시 자동 로드)';
    msg.style.color = '#94a3b8';
    return;
  }
  msg.textContent = '불러오는 중…';
  msg.style.color = '#0ea5e9';
  try {
    const resp = await fetch('/models', { headers: { 'X-Api-Key': apiKey } });
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
  sel.innerHTML = models.map(m => {
    const label = (m === DEFAULT_MODEL) ? `${m} (기본)` : m;
    return `<option value="${escHtml(m)}">${escHtml(label)}</option>`;
  }).join('');
  // 선택값 복원: 이전 선택 > 기본모델 > 첫 항목
  if (models.includes(prev))                sel.value = prev;
  else if (models.includes(DEFAULT_MODEL))  sel.value = DEFAULT_MODEL;
}

function showError(msg) {
  const box = document.getElementById('error-box');
  box.textContent = '⚠️ ' + msg;
  box.style.display = 'block';
}

// ── 분석 결과 렌더링 ──
function renderAnalysis(concepts, sampleQuestions, formatAnalysis, examConcepts, priorityTopics, typeStats, typeTargets, sourceInfo) {
  const box = document.getElementById('analysis-box');
  const content = document.getElementById('analysis-content');
  concepts = concepts || {};
  examConcepts = examConcepts || {};
  priorityTopics = priorityTopics || [];
  typeStats = typeStats || {};
  typeTargets = typeTargets || {};
  sourceInfo = sourceInfo || {};

  // 접을 수 있는 세부 항목 (details/summary). body가 비면 항목 자체를 생략.
  const aSection = (summaryHtml, bodyHtml, open) =>
    bodyHtml
      ? `<details class="a-section"${open ? ' open' : ''}><summary>${summaryHtml}</summary><div class="a-body">${bodyHtml}</div></details>`
      : '';

  const tagGroup = (label, items, cls) => {
    if (!items || !items.length) return '';
    const tags = items.map(i => `<span class="tag ${cls}">${escHtml(i)}</span>`).join('');
    return `<div class="tag-group">
      <div class="tag-group-label">${label}</div>
      <div class="tags">${tags}</div>
    </div>`;
  };

  // ① 원문 반영 범위
  const sourceBody = renderSourceInfo(sourceInfo);

  // ② 기출 유형 구성
  let typeBody = '';
  if (typeStats['총문항']) {
    const hasTarget = Object.keys(TYPE_BADGE).some(t => (typeTargets[t] || 0) > 0);
    const targetTxt = hasTarget
      ? ` &nbsp;→&nbsp; <b>생성 구성:</b> ${formatTypeCounts(typeTargets)}`
      : '';
    typeBody = `
      <div style="font-size:0.86rem;color:#1e3a8a;">
        ${formatTypeCounts(typeStats)} (총 ${typeStats['총문항']})${targetTxt}
        <div style="font-size:0.75rem;color:#64748b;margin-top:4px;">${escHtml(typeStats['판별근거'] || '')}</div>
      </div>`;
  }

  // ③ 우선 출제 주제 (강의 ∩ 기출)
  const priorityBody = priorityTopics.length
    ? `<div class="tags">${priorityTopics.map(t => `<span class="tag" style="background:#fde68a;color:#92400e;">${escHtml(t)}</span>`).join('')}</div>`
    : '';

  // ④ 강의자료 핵심 개념
  const lectureBody =
    tagGroup('핵심 질환', concepts['핵심질환'], 'tag-blue') +
    tagGroup('핵심 개념', concepts['핵심개념'], 'tag-purple') +
    tagGroup('중요 수치', concepts['중요수치'], 'tag-green') +
    tagGroup('감별 진단', concepts['감별진단포인트'], 'tag-orange');

  // ⑤ 기출 출제 경향
  const examBody =
    tagGroup('기출 출제 개념', examConcepts['기출출제개념'], 'tag-orange') +
    tagGroup('빈출 포인트', examConcepts['빈출포인트'], 'tag-green') +
    `<div style="font-weight:700;font-size:0.85rem;margin:14px 0 10px;color:#0369a1;">🔍 기출 형식 키워드</div>` +
    renderFormatKeywords(formatAnalysis || '');

  // ⑥ 기출문제 예시 (Few-shot)
  const sampleBody = (sampleQuestions || '').trim()
    ? `<pre style="background:#f1f5f9;border-radius:8px;padding:14px;font-size:0.8rem;line-height:1.7;white-space:pre-wrap;margin:0;">${escHtml(sampleQuestions)}</pre>`
    : '';

  content.innerHTML =
    aSection('📥 원문 반영 범위', sourceBody, true) +
    aSection('📊 기출 유형 구성', typeBody, true) +
    aSection('⭐ 우선 출제 주제 <span style="font-weight:400;color:#92400e;font-size:0.8rem;">(강의자료 ∩ 기출 — 가중치 높음)</span>', priorityBody, true) +
    aSection('📚 강의자료 핵심 개념', lectureBody, false) +
    aSection('📈 기출 출제 경향', examBody, false) +
    aSection('📋 추출된 기출문제 예시 (Few-shot 참조용)', sampleBody, false);

  box.style.display = 'block';
}

// ── 형식 키워드 렌더링 ("라벨: 키워드1, 키워드2" 라인 → 태그 그룹) ──
function renderFormatKeywords(text) {
  const lines = text.split('\n').map(l => l.trim()).filter(Boolean);
  let html = '';
  for (const line of lines) {
    const ci = line.indexOf(':');
    if (ci < 0) continue;
    const label = line.slice(0, ci).trim();
    const rest  = line.slice(ci + 1).trim();
    if (!rest) continue;
    const kws = rest.split(/[,、]/).map(k => k.trim()).filter(Boolean);
    const tags = kws.map(k => `<span class="tag tag-purple">${escHtml(k)}</span>`).join('');
    html += `<div class="tag-group">
      <div class="tag-group-label">${escHtml(label)}</div>
      <div class="tags">${tags}</div>
    </div>`;
  }
  // 라벨:키워드 형식이 전혀 없으면 원문 그대로 표시 (폴백)
  return html || `<div style="font-size:0.85rem;color:#475569;line-height:1.8;white-space:pre-wrap;">${escHtml(text)}</div>`;
}

// ── 초기 로드 ──
loadSessions();
loadModels();
