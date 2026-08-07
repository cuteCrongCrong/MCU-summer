// ══════════════════════════════════════════════
// topic_analysis.js — 기출 주제 분석 탭
//   "강의록 몇 페이지의 주제가 어떤 기출 몇 번 문제로 나왔는가" 를 정리해 보여준다.
//   common.js 이후 로드. escHtml/setStep 과 진행 상태 도구(createProgress·sseEvents)는
//   common.js 것을 재사용한다 — 진행률 계산·SSE 파싱이 문제 생성 탭과 같은 규칙이다.
//   이름 충돌을 막기 위해 전역·함수 모두 topic 접두사 사용. (CONTRIBUTING.md 5번)
//   문제 생성기와 제공사/키/모델 입력을 공유하지 않고 각자 들고 있다 — 탭끼리 독립시켜
//   한쪽 수정이 다른 쪽을 깨지 않게 하기 위함. (그래서 상태 이름이 전부 따로다)
// ══════════════════════════════════════════════

const TOPIC_MAX_FILES = 7;   // llm.py TOPIC_MAX_FILES_PER_SIDE와 같은 값으로 유지

// ── 파일 드래그앤드롭 (여러 개 지원) ──
function topicSetupDrop(dropId, inputId, nameId) {
  const drop = document.getElementById(dropId);
  const input = document.getElementById(inputId);
  const nameEl = document.getElementById(nameId);

  const show = () => {
    const files = Array.from(input.files || []);
    if (!files.length) { nameEl.textContent = ''; return; }
    const names = files.map(f => f.name).join(', ');
    nameEl.textContent = `✅ ${files.length}개 — ${names}`;
    nameEl.style.color = files.length > TOPIC_MAX_FILES ? '#dc2626' : '';
  };

  input.addEventListener('change', show);
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('drag-over');
    const pdfs = Array.from(e.dataTransfer.files || [])
      .filter(f => f.name.toLowerCase().endsWith('.pdf'));
    if (!pdfs.length) return;
    const dt = new DataTransfer();
    pdfs.forEach(f => dt.items.add(f));
    input.files = dt.files;
    show();
  });
}
topicSetupDrop('topic-drop-lecture', 'topic-lecture-file', 'topic-lecture-name');
topicSetupDrop('topic-drop-exam',    'topic-exam-file',    'topic-exam-name');

// ── 설정 화면 / 결과 화면 전환 ──
function topicShowResult() {
  document.getElementById('topic-input-view').classList.add('hidden');
  document.getElementById('topic-result-view').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function topicShowInput() {
  document.getElementById('topic-result-view').classList.add('hidden');
  document.getElementById('topic-input-view').classList.remove('hidden');
  document.getElementById('topic-status-box').classList.add('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function topicShowError(msg) {
  const box = document.getElementById('topic-error-box');
  box.textContent = '⚠️ ' + msg;
  box.style.display = 'block';
}


// ── LLM 제공사 (/providers) ──
let topicProviders = [];
let topicCurrentProvider = null;
const topicApiKeyByProvider = {};   // 제공사별 키를 메모리에만 (localStorage 저장 안 함)

async function topicLoadProviders() {
  try {
    const resp = await fetch('/providers');
    const data = await resp.json();
    topicProviders = data.providers || [];
    if (!topicProviders.length) return;
    document.getElementById('topic-provider-select').innerHTML = topicProviders.map(p =>
      `<option value="${escHtml(p.name)}">${escHtml(p.label)}</option>`
    ).join('');
    topicSelectProvider(data.default || topicProviders[0].name);
  } catch (err) {
    topicShowError('LLM 제공사 목록을 불러오지 못했습니다.');
  }
}

function topicSelectProvider(name) {
  const info = topicProviders.find(p => p.name === name);
  if (!info) return;

  const keyInput = document.getElementById('topic-api-key');
  if (topicCurrentProvider) topicApiKeyByProvider[topicCurrentProvider] = keyInput.value;

  topicCurrentProvider = name;
  document.getElementById('topic-provider-select').value = name;
  document.getElementById('topic-api-key-label').textContent = `🔑 ${info.label} API Key`;
  keyInput.placeholder = info.key_placeholder || 'API 키 입력';
  keyInput.value = topicApiKeyByProvider[name] || '';
  renderKeyHelp(info, 'topic-key-help');

  topicPopulateModels([info.default_model]);
  topicLoadModels();
}

// ── 모델 목록 (/models) ──
async function topicLoadModels() {
  const apiKey = document.getElementById('topic-api-key').value.trim();
  const msg = document.getElementById('topic-model-load-msg');
  if (!apiKey) {
    msg.textContent = '(API 키 입력 시 자동 로드)';
    msg.style.color = '#94a3b8';
    return;
  }
  msg.textContent = '불러오는 중…';
  msg.style.color = '#4f8a76';
  try {
    const url = '/models?provider=' + encodeURIComponent(topicCurrentProvider || '');
    const resp = await fetch(url, { headers: { 'X-Api-Key': apiKey } });
    const data = await resp.json();
    const models = data.models || [];
    if (models.length) {
      topicPopulateModels(models);
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

function topicPopulateModels(models) {
  const sel = document.getElementById('topic-model-select');
  const prev = sel.value;
  const info = topicProviders.find(p => p.name === topicCurrentProvider) || {};
  const defaultModel = info.default_model;
  sel.innerHTML = models.map(m => {
    const label = (m === defaultModel) ? `${m} (기본)` : m;
    return `<option value="${escHtml(m)}">${escHtml(label)}</option>`;
  }).join('');
  if (models.includes(prev))              sel.value = prev;
  else if (models.includes(defaultModel)) sel.value = defaultModel;
}

// ── 진행 상태 ──
// 서버의 stage.key 와 화면의 step 요소 id(topic-step-*)가 1:1로 대응한다.
// 단계가 셋뿐인 이유: 주제 대응표는 LLM 호출 한 번이라 그 안을 쪼갤 수 없다.
const TOPIC_STAGES = ['extract', 'topics', 'finish'];
const TOPIC_STEP_ICONS = { extract: '📄', topics: '📚', finish: '🔗' };

// 가중치 — PDF 그림을 한 장씩 읽는 추출이 가장 오래 걸리고, 정리·보관은 금방 끝난다.
// 계산·표시는 common.js 의 createProgress 가 한다 (문제 생성 탭과 공용).
const topicProgress = createProgress({
  barId: 'topic-overall-bar', pctId: 'topic-overall-pct', stepPrefix: 'topic-step-',
  weights: { extract: 55, topics: 40, finish: 5 },
});

function topicResetSteps() {
  TOPIC_STAGES.forEach(key => {
    const el = document.getElementById('topic-step-' + key);
    el.className = 'step-item wait';
    el.querySelector('.step-icon').textContent = TOPIC_STEP_ICONS[key];
    const note = el.querySelector('.step-note');
    if (note) note.remove();
  });
  topicProgress.reset(TOPIC_STAGES);
}

// ── 메인 분석 함수 ──
async function topicAnalyze() {
  const lectureFiles = Array.from(document.getElementById('topic-lecture-file').files || []);
  const examFiles    = Array.from(document.getElementById('topic-exam-file').files || []);
  const apiKey       = document.getElementById('topic-api-key').value.trim();
  const model        = document.getElementById('topic-model-select').value;

  if (!apiKey)             return alert('API 키를 입력해주세요.');
  if (!lectureFiles.length) return alert('강의록 PDF를 1개 이상 올려주세요.');
  if (!examFiles.length)    return alert('기출문제 PDF를 1개 이상 올려주세요.');
  if (lectureFiles.length > TOPIC_MAX_FILES || examFiles.length > TOPIC_MAX_FILES) {
    return alert(`강의록·기출은 각각 최대 ${TOPIC_MAX_FILES}개까지 올릴 수 있습니다.`);
  }

  document.getElementById('topic-analyze-btn').disabled = true;
  document.getElementById('topic-status-box').classList.remove('hidden');
  document.getElementById('topic-error-box').style.display = 'none';
  topicRenderSpend(null, 'main');   // 지난 회차의 사용량 표가 남아 보이지 않게
  topicResetSteps();

  const form = new FormData();
  form.append('api_key', apiKey);
  form.append('model', model);
  form.append('provider', topicCurrentProvider || '');
  form.append('title', document.getElementById('topic-title').value.trim());
  lectureFiles.forEach(f => form.append('lectures', f));
  examFiles.forEach(f => form.append('exams', f));

  try {
    let out = await topicRunAnalysis(form);

    // 분량 상한을 넘는 파일이 있으면 서버가 추출까지만 하고 멈춰 물어본다.
    // 추출 결과는 서버에 보관돼 있으므로, 진행하면 토큰만 보내 이어서 돈다.
    if (out && out.needsConfirm) {
      const go = await confirmTruncation(out.payload.warnings || [], out.payload);
      if (!go) {
        // 추출은 실제로 끝났다(그래서 경고가 나왔다) — 단계 표시도 그대로 남긴다
        setStep('topic-step-extract', 'done');
        topicProgress.completeStage('extract');
        topicRenderSpend(out.payload, 'main');   // 추출까지 쓴 양은 알려준다
        return;
      }
      const again = new FormData();
      again.append('api_key', apiKey);
      again.append('title', document.getElementById('topic-title').value.trim());
      again.append('extract_token', out.payload.extract_token);
      await topicRunAnalysis(again);
    }
  } catch (err) {
    topicShowError('서버 연결에 실패했습니다. Flask 서버가 실행 중인지 확인하세요.\n' + err.message);
  } finally {
    document.getElementById('topic-analyze-btn').disabled = false;
  }
}

// 요청 한 번을 끝까지 처리한다.
// 분량 초과로 멈추면 {needsConfirm, payload}, 그 밖에는 아무것도 돌려주지 않는다.
async function topicRunAnalysis(form) {
  if (!canReadStream()) return topicFallbackAnalyze(form);

  const resp = await fetch('/analyze-topics/stream', { method: 'POST', body: form });

  // 스트림이 시작되기 전 오류(키 누락 등)는 평범한 JSON으로 온다
  if (!resp.ok) {
    topicShowError(await describeHttpError(resp, '/analyze-topics/stream'));
    return;
  }

  let finished = false;     // done/error 없이 스트림이 끊겼는지 판별

  // 프레임 끊기는 common.js 의 sseEvents 가 한다 (문제 생성 탭과 공용)
  for await (const ev of sseEvents(resp)) {
    if (ev.type === 'stage') {
      setStep('topic-step-' + ev.key, ev.status);
      if (ev.status === 'active') {
        topicProgress.setStageProgress(ev.key, 0, ev.total || 0);
      } else if (ev.status === 'done') {
        topicProgress.completeStage(ev.key);
      }
    } else if (ev.type === 'progress') {
      topicProgress.setStageProgress(ev.key, ev.done, ev.total);
    } else if (ev.type === 'done') {
      finished = true;
      topicFinish(ev.payload);
    } else if (ev.type === 'needs_confirm') {
      return { needsConfirm: true, payload: ev.payload };
    } else if (ev.type === 'error') {
      // 오류로 끝나도 그때까지 쓴 토큰·크레딧은 이미 소모됐으므로 문구에 덧붙인다
      topicShowError((ev.message || '알 수 없는 오류가 발생했습니다.') + spendSuffix(ev));
      return;
    }
  }

  // done/error 없이 연결이 끊긴 경우 — 조용히 멈춘 것처럼 보이지 않도록 알린다
  if (!finished) {
    topicShowError('분석이 끝나기 전에 서버와의 연결이 끊겼습니다. '
                 + '서버 상태를 확인하고 다시 시도해주세요.');
  }
}

// 스트리밍을 못 쓰는 환경 — 예전처럼 한 번에 받아서 그린다.
// 진행률은 중간값을 알 수 없으므로 요청 전후로 0% → 100%만 움직인다.
async function topicFallbackAnalyze(form) {
  TOPIC_STAGES.forEach(k => setStep('topic-step-' + k, 'active'));
  const resp = await fetch('/analyze-topics', { method: 'POST', body: form });
  const data = await resp.json().catch(() => null);
  TOPIC_STAGES.forEach(k => {
    setStep('topic-step-' + k, 'done');
    topicProgress.completeStage(k);
  });

  if (!data) {
    topicShowError(`서버 오류 ${resp.status} ${resp.statusText}`);
    return;
  }
  if (!resp.ok || data.error) {
    topicShowError((data.error || '알 수 없는 오류가 발생했습니다.') + spendSuffix(data));
    return;
  }
  if (data.needs_confirm) return { needsConfirm: true, payload: data };
  topicFinish(data);
}

function topicFinish(data) {
  topicRenderResult(data, 'main');   // 사용량 상자도 여기서 함께 그려진다
  topicShowResult();
  // 방금 분석한 것이 보관함에 저장됐으므로 목록 캐시를 버린다
  // (안 버리면 보관함에 들어가도 직전에 받아둔 옛 목록이 그대로 보인다)
  if (data.analysis_id) invalidateSavedTopics();
}

// ── 결과 렌더링 ──
// 같은 렌더러를 두 화면이 함께 쓴다: 분석 탭의 결과 화면(main)과
// 보관함 '분석한 주제'의 열람 화면(saved). 둘이 동시에 DOM에 살아 있으므로
// 대상 element id를 뷰로 묶어 넘긴다 (renderQuestions의 ns와 같은 이유).
// 화면 상태(data / countText)도 뷰별로 따로 담는다 — 공유하면 한쪽을 열었을 때
// 다른 쪽 검색창이 엉뚱한 목록을 걸러낸다.
const TOPIC_VIEWS = {
  main: {
    sourceId: 'topic-source-content', listId: 'topic-list',
    emptyId: 'topic-list-empty', countId: 'topic-count-label',
    searchId: 'topic-search', usageId: 'topic-usage-box',
    summaryId: 'topic-summary-box',
  },
  saved: {
    sourceId: 'saved-topic-source-content', listId: 'saved-topic-list',
    emptyId: 'saved-topic-list-empty', countId: 'saved-topic-count-label',
    searchId: 'saved-topic-search', usageId: 'saved-topic-usage-box',
    summaryId: 'saved-topic-summary-box',
  },
};

function topicView(key) { return TOPIC_VIEWS[key || 'main'] || TOPIC_VIEWS.main; }

// 분석에 쓴 토큰·크레딧 (common.js의 공용 렌더러를 해당 뷰의 상자에 그린다).
// 보관된 분석에는 서버가 저장해둔 값이 실려 온다 — 기록을 남기기 전에 만든
// 분석은 값이 없어(null) 상자째 숨겨진다.
function topicRenderSpend(data, viewKey) {
  renderSpend(data, { boxId: topicView(viewKey).usageId, noun: '분석' });
}

function topicRenderResult(data, viewKey) {
  const view = topicView(viewKey);
  view.data = data;
  topicRenderSpend(data, viewKey);
  document.getElementById(view.sourceId).innerHTML =
    topicRenderDocs(data.lecture_docs || [], '📄 강의록') +
    topicRenderDocs(data.exam_docs || [], '📝 기출문제');
  document.getElementById(view.searchId).value = '';
  topicRenderList(data.topics || [], viewKey);

  // 방금 분석한 결과 화면에서만 저장 여부를 안내한다 (보관함에서 연 화면은 이미 저장된 것이므로 불필요)
  const statusEl = document.getElementById('topic-save-status');
  if (statusEl && viewKey !== 'saved') {
    statusEl.textContent = data.analysis_id
      ? '💾 이 결과는 보관함에 자동 저장되었습니다.'
      : '';
  }
}

// 분석에 쓰인 파일 목록 (파일명 · 페이지 수 · 반영 범위)
function topicRenderDocs(docs, label) {
  if (!docs.length) return '';
  const rows = docs.map(d => {
    const s = d.source || {};
    const nf = n => (n || 0).toLocaleString('ko-KR');
    const cut = s.truncated
      ? `<span style="color:#b45309;">추출 ${nf(s.chars)}자 중 ${nf(s.used)}자(${s.coverage}%) 반영 — 분량 초과로 중간 생략</span>`
      : `<span style="color:#15803d;">추출 ${nf(s.chars)}자 전체 반영</span>`;
    return `
      <div class="topic-doc-row">
        <span class="topic-doc-name">${escHtml(d.name)}</span>
        <span class="topic-doc-meta">${d.pages ? d.pages + '페이지 인식 · ' : ''}${cut}</span>
      </div>`;
  }).join('');
  return `<div class="topic-doc-group">
    <div class="tag-group-label">${label} ${docs.length}개</div>
    ${rows}
  </div>`;
}

// 기출 문항 하나를 "3쪽 4번"처럼 표시 (페이지를 못 찾았으면 번호만)
function topicExamItemLabel(it) {
  return (it['페이지'] ? `${it['페이지']}쪽 ` : '') + `${it['번호']}번`;
}

// 주제 이름 뒤에 붙는 출처 부분 — "강의록 12p, 13p - 기출 3쪽 4번, 5쪽 9번"
// (앞에 "주제: "를 붙이면 "주제: 강의록 몇p - 기출 몇번" 한 줄 형식이 된다.
//  파일명은 줄이지 않은 전체 이름을 쓴다 — 여러 개 있어도 헷갈리지 않게)
function topicRefLine(t) {
  const lec = (t['강의록'] || []).map(r =>
    `${topicShortName(r['자료'])} ${r['페이지'].map(p => p + 'p').join(', ')}`).join(' / ');
  const exam = (t['기출'] || []).map(r =>
    `${topicShortName(r['자료'])} ${r['문항'].map(topicExamItemLabel).join(', ')}`).join(' / ');
  return `${lec || '페이지 미확인'} - ${exam}`;
}

// 파일명에서 확장자만 떼어 읽기 편하게 (복사용 한 줄 요약은 이 전체 이름을 쓴다)
function topicShortName(name) {
  return String(name || '').replace(/\.pdf$/i, '');
}

// 화면 표시용: 파일명이 길면 가운데를 줄인다.
// 주제마다 같은 파일명이 반복되므로 그냥 두면 목록이 파일명으로 가득 찬다.
// 앞뒤를 남기는 이유 — '2023학년도 1학기…중간고사'처럼 연도와 시험명이 둘 다 필요하기 때문.
const TOPIC_NAME_MAX = 20;
function topicDisplayName(name) {
  const s = topicShortName(name);
  if (s.length <= TOPIC_NAME_MAX) return s;
  return s.slice(0, 11).trim() + '…' + s.slice(-7).trim();
}

function topicRenderList(topics, viewKey) {
  const view = topicView(viewKey);
  const listEl = document.getElementById(view.listId);
  const emptyEl = document.getElementById(view.emptyId);
  const countEl = document.getElementById(view.countId);
  const summaryEl = document.getElementById(view.summaryId);
  const data = view.data || {};

  const dropNote = data.dropped
    ? ` · 출처(페이지·문항 번호)를 확인할 수 없어 제외한 항목 ${data.dropped}개`
    : '';
  view.countText = topics.length
    ? `주제 ${topics.length}개 · 기출 ${data.total_questions || 0}문항${dropNote}`
    : '';
  countEl.textContent = view.countText;

  if (!topics.length) {
    listEl.innerHTML = '';
    // 요약 폴드는 목록 밖(제목 바로 밑)에 있으므로 직접 지워야 이전 결과가 남지 않는다
    summaryEl.innerHTML = '';
    view.summaryLines = [];
    emptyEl.innerHTML = `강의록과 기출에서 함께 확인되는 주제를 찾지 못했습니다.
      <div style="font-size:0.8rem;color:#94a3b8;margin-top:6px;">
        같은 과목·같은 범위의 강의록과 기출인지 확인해 보세요.
        스캔 이미지로만 된 PDF는 페이지·문항 번호를 읽을 수 없습니다.${dropNote ? '<br />' + escHtml(dropNote.replace(/^ · /, '')) : ''}
      </div>`;
    return;
  }
  emptyEl.innerHTML = '';

  listEl.innerHTML = topics.map((t, i) => {
    const lecRefs = (t['강의록'] || []).map(r => `
      <span class="topic-src">
        <span class="topic-src-name" title="${escHtml(r['자료'])}">${escHtml(topicDisplayName(r['자료']))}</span>
        ${r['페이지'].map(p => `<span class="topic-chip topic-chip-lec">${escHtml(p)}p</span>`).join('')}
      </span>`).join('');
    const examRefs = (t['기출'] || []).map(r => `
      <span class="topic-src">
        <span class="topic-src-name" title="${escHtml(r['자료'])}">${escHtml(topicDisplayName(r['자료']))}</span>
        ${r['문항'].map(it => `<span class="topic-chip topic-chip-exam">${escHtml(topicExamItemLabel(it))}</span>`).join('')}
      </span>`).join('');

    // 강의록 발췌 — 해당 페이지에 실제로 적힌 내용을 강의록 표현 그대로 정리한 것
    const excerptHtml = t['강의록발췌'] ? `
      <div class="topic-excerpt">
        <div class="topic-excerpt-label">📄 강의록 내용</div>
        ${escHtml(t['강의록발췌'])}
      </div>` : '';

    // 기출 원문 — 문항마다 지문을 그대로 인용 (원문이 확인된 문항만)
    const examQuotes = (t['기출'] || []).flatMap(r =>
      (r['문항'] || []).filter(it => it['원문']).map(it => `
        <div class="topic-exam-quote">
          <div class="topic-exam-quote-head">${escHtml(topicDisplayName(r['자료']))} · ${escHtml(topicExamItemLabel(it))}</div>
          <div class="topic-exam-quote-text">“${escHtml(it['원문'])}”</div>
        </div>`)
    ).join('');

    // 검색용 텍스트 (주제 + 파일명 + 출제형태 + 발췌 + 기출 원문)
    const searchText = [
      t['주제'], t['출제형태'], t['강의록발췌'],
      ...(t['강의록'] || []).map(r => r['자료']),
      ...(t['기출'] || []).flatMap(r => [r['자료'], ...(r['문항'] || []).map(it => it['원문'] || '')]),
    ].join(' ').toLowerCase();

    return `
    <div class="topic-item" data-search="${escHtml(searchText)}">
      <div class="topic-head">
        <span class="topic-rank">${i + 1}</span>
        <span class="topic-name">${escHtml(t['주제'])}</span>
        <span class="topic-hits">기출 ${t['문항수']}문항</span>
      </div>
      <div class="topic-trace">
        <span class="topic-trace-side">
          <span class="topic-trace-label">📄 강의록</span>
          ${lecRefs || '<span class="topic-src-name" style="color:#b45309;">페이지 미확인</span>'}
        </span>
        <span class="topic-arrow">→</span>
        <span class="topic-trace-side">
          <span class="topic-trace-label">📝 기출</span>
          ${examRefs}
        </span>
      </div>
      ${excerptHtml}
      ${examQuotes}
      ${t['출제형태'] ? `<div class="topic-note">${escHtml(t['출제형태'])}</div>` : ''}
    </div>`;
  }).join('');

  // 한 줄 요약 (복사용) — "주제: 강의록 몇p - 기출 몇번" 평문 한 줄 = 카드 한 장.
  // "📌 기출에 나온 주제" 제목 바로 밑에 접어둔다 — 전체를 훑거나 복사하려는 사람이
  // 긴 주제 목록을 끝까지 스크롤하지 않아도 되게. 형식은 아래 주제 목록과 같은
  // 테두리 있는 흰 카드 행이되 굵게 강조하지는 않는다.
  // 복사 텍스트는 DOM을 다시 읽지 않고 이 배열을 그대로 쓴다.
  const lines = topics.map(t => `${t['주제']}: ${topicRefLine(t)}`);
  view.summaryLines = lines;
  const rowsHtml = lines.map(l => `<div class="topic-summary-row">${escHtml(l)}</div>`).join('');
  const key = viewKey || 'main';
  summaryEl.innerHTML = `
    <details class="topic-summary-fold">
      <summary>📋 한 줄 요약 보기 (복사용)</summary>
      <button class="check-btn" style="margin:10px 0;" onclick="topicCopyLines('${key}')">📋 전체 복사</button>
      <div class="topic-summary-list">${rowsHtml}</div>
    </details>`;
}

function topicCopyLines(viewKey) {
  const view = topicView(viewKey);
  const text = (view.summaryLines || []).join('\n');
  if (!text) return;
  navigator.clipboard.writeText(text)
    .then(() => alert('한 줄 요약을 복사했습니다.'))
    .catch(() => alert('복사에 실패했습니다. 텍스트를 직접 선택해 복사해주세요.'));
}

// ── 주제 걸러보기 (주제명·파일명·출제형태 대상) ──
function topicFilter(viewKey) {
  const view = topicView(viewKey);
  const q = document.getElementById(view.searchId).value.trim().toLowerCase();
  const items = document.querySelectorAll('#' + view.listId + ' .topic-item');
  let shown = 0;
  items.forEach(el => {
    const hit = !q || (el.dataset.search || '').includes(q);
    el.classList.toggle('hidden', !hit);
    if (hit) shown++;
  });
  // 검색 중에는 몇 개가 걸렸는지, 지우면 원래 요약 문구로 되돌린다
  document.getElementById(view.countId).textContent =
    q ? `${shown} / ${items.length}개 표시` : (view.countText || '');
}

// ── 초기 로드 ──
topicLoadProviders();
