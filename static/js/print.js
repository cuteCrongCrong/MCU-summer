// ══════════════════════════════════════════════
// print.js — 시험지 인쇄 / PDF 저장
//   common.js 이후 로드. escHtml / typeInfoOf / answerIndexOf / blankAnswersOf 재사용.
//
// ⚠️ 이 파일의 가장 중요한 성질:
//   buildPrintDoc 은 **화면 DOM을 한 번도 읽지 않는다.** 인자로 받은 문제 배열만 쓴다.
//   화면 카드는 사용자가 '정답 확인'을 누르면 정답이 열리고 선택지에 채점 색이 남는데,
//   DOM을 긁어 인쇄하면 그 흔적이 종이에 그대로 나온다("문제만"인데 정답이 찍힌다).
//   원본 배열로 새로 그리면 정답 문자열이 애초에 문서에 들어가지 않는다.
//   → 숨기는 게 아니라 만들지 않는다. 이 차이가 콘솔 한 줄로 검증된다:
//        const qs = getQuestions('arc-');
//        const doc = buildPrintDoc('q', qs, meta);
//        qs.every(q => !doc.includes(q['정답']));   // ← true 여야 한다
//   common.js 의 buildQuestionCard 를 고칠 때 이 파일도 같이 봐야 한다.
// ══════════════════════════════════════════════

const PRINT_EXCERPT_LEN = 40;   // 정답지에서 문항을 알아보게 할 본문 앞부분 길이

// 인쇄 문서 제목·머리말에 쓸 메타를 화면 헤더와 같은 재료로 만든다
function buildPaperMeta(gid, g, row) {
  row = row || {};
  const title = (row.title || g.title || '').trim();
  return {
    gid,
    title: title || (row.session_name || '시험지'),
    sname: row.session_name || '',
    created_at: g.created_at || '',
    n: (g.questions || []).length,
  };
}

// 파일명으로 못 쓰는 문자를 치운다 (document.title 이 기본 파일명 제안이 된다)
function sanitizeFilename(s) {
  return String(s).replace(/[\\/:*?"<>|]/g, '_').replace(/\s+/g, ' ').trim();
}

// 본문의 빈칸 표시를 손글씨용 밑줄로 바꾼다.
// ⚠️ escHtml 이후에 적용해야 한다 — 먼저 하면 삽입한 <span> 이 이스케이프된다.
function blanksToUnderline(escaped, total) {
  let i = 0;
  return escaped.replace(BLANK_MARK_RE, () => {
    i += 1;
    const sup = total >= 2 ? `<sup>${i}</sup>` : '';
    return `<span class="p-blank"></span>${sup}`;
  });
}

function printHeadBand(kind, meta) {
  const label = kind === 'q' ? '문제지' : '정답 · 해설';
  const sub = [meta.sname.split(' · ')[0], meta.created_at,
               `${meta.n}문항`, `#${meta.gid}`].filter(Boolean).join(' · ');
  return `<div class="p-band">
      <div class="p-title">${escHtml(meta.title)} <span class="p-kind">${label}</span></div>
      <div class="p-meta">${escHtml(sub)}</div>
    </div>`;
}

// ── 문제지 ──────────────────────────────────────
// 정답·해설·함정포인트를 **문자열로도 넣지 않는다**.
function buildQuestionSheet(questions, meta) {
  const items = questions.map((q, i) => {
    const t = typeInfoOf(q);
    const label = t.rawType || (t.isObjective ? '객관식' : '단답형');
    // 객관식은 답을 헤더 우측 박스에 쓰고, 나머지는 본문 아래 괘선/밑줄에 쓴다
    const box = t.isObjective ? '<span class="p-abox"></span>' : '';
    const body = blanksToUnderline(escMath(q['문제'] || ''), t.blankCount);
    const choices = t.choices.length
      ? `<ul class="p-choices">${t.choices.map(c => `<li>${escMath(c)}</li>`).join('')}</ul>`
      : '';
    let slot = '';
    if (label.includes('서술')) slot = '<div class="p-rule"></div>'.repeat(3);
    else if (!t.isObjective && !t.isBlank) slot = '<div class="p-rule"></div>';
    return `<div class="p-item">
        <div class="p-ihead"><span class="p-no">${i + 1}</span>
          <span class="p-type">${escHtml(label)}</span>${box}</div>
        <div class="p-body">${body}</div>${choices}${slot}
      </div>`;
  }).join('');
  return `<div class="p-sheet memo">${printHeadBand('q', meta)}${items}
      <div class="p-end">— 끝 (총 ${questions.length}문항) —</div></div>`;
}

// ── 정답 · 해설지 ───────────────────────────────
function answerText(q) {
  const t = typeInfoOf(q);
  if (t.isObjective) {
    const k = answerIndexOf(q);
    // -1 = 판별 실패(정답에 기호 외 문자가 섞인 경우). 원문을 그대로 싣는다.
    if (k >= 0 && k < t.choices.length) return escMath(t.choices[k]);
    return escMath(q['정답'] || '-');
  }
  if (t.isBlank) {
    const bl = blankAnswersOf(q);
    if (bl.length >= 2) {
      return bl.map((v, j) => `<b>빈칸${j + 1}</b> ${escMath(v)}`).join(' &nbsp;/&nbsp; ');
    }
  }
  return escMath(q['정답'] || '-');
}

function buildAnswerSheet(questions, meta) {
  // 빠른 정답표 — 채점할 때 이것만 훑으면 끝난다. grid 보다 인쇄 페이지 나눔이 안정적이라 table.
  const cells = questions.map((q, i) => {
    const t = typeInfoOf(q);
    // 자르기 전에 평문으로 바꾼다 — LaTeX 원문을 22자에서 자르면
    // '$\text{ ca' 처럼 명령어 중간이 잘려 더 읽기 어려워진다.
    let s = mathToText(t.isBlank ? blankAnswersOf(q).join(' / ') : (q['정답'] || ''));
    s = s.trim();
    if (s.length > 22) s = s.slice(0, 22) + '…';
    return `<td><b>${i + 1}</b> ${escHtml(s)}</td>`;
  });
  const rows = [];
  for (let k = 0; k < cells.length; k += 2) {
    const pair = cells.slice(k, k + 2);
    rows.push('<tr>' + pair.join('') + (pair.length < 2 ? '<td></td>' : '') + '</tr>');
  }

  const details = questions.map((q, i) => {
    const t = typeInfoOf(q);
    const label = t.rawType || (t.isObjective ? '객관식' : '단답형');
    // 번호만으로는 무엇에 대한 답인지 알 수 없어 본문 앞부분을 함께 싣는다.
    // CSS 로 자르면 글자 높이 중간에서 잘리므로 문자열을 직접 자른다.
    // 여기도 자르기 전에 평문으로 (40자에서 LaTeX 명령어 중간이 잘리지 않게)
    const flat = mathToText(q['문제'] || '').replace(/\s+/g, ' ').trim();
    const ex = flat.length > PRINT_EXCERPT_LEN
      ? flat.slice(0, PRINT_EXCERPT_LEN) + '…' : flat;
    return `<div class="p-ans">
        <div class="p-ihead"><span class="p-no">${i + 1}</span>
          <span class="p-type">${escHtml(label)}</span>
          <span class="p-ex">${escHtml(ex)}</span></div>
        <div class="p-ansline"><b>정답</b> ${answerText(q)}</div>
        ${q['해설'] ? `<div class="p-exp">${escMath(q['해설'])}</div>` : ''}
        ${q['함정포인트'] ? `<div class="p-trap">${escMath(q['함정포인트'])}</div>` : ''}
      </div>`;
  }).join('');

  return `<div class="p-sheet">${printHeadBand('a', meta)}
      <div class="p-sub">빠른 정답</div>
      <table class="p-key"><tbody>${rows.join('')}</tbody></table>
      <div class="p-sub">해설</div>${details}</div>`;
}

// kind: 'q' 문제지 / 'a' 정답·해설지
function buildPrintDoc(kind, questions, meta) {
  questions = questions || [];
  return kind === 'q'
    ? buildQuestionSheet(questions, meta)
    : buildAnswerSheet(questions, meta);
}

// ── 미리보기 오버레이 ───────────────────────────
// 새 창을 쓰지 않는 이유: 팝업 차단 폴백(iframe)이 두 번째 구현 경로가 되고,
// about:blank 가 종이에 찍히는 고유 실패가 있다. 오버레이는 그 둘이 없고,
// 오버레이가 열려 있는 상태 자체가 인쇄 모드라 afterprint 리스너도 필요 없다
// (삼성 인터넷은 beforeprint/afterprint 를 지원하지 않는다).
let _printSavedTitle = null;

function openPrintPreview(kind, questions, meta) {
  if (!questions || !questions.length) return alert('인쇄할 문제가 없습니다.');
  const root = document.getElementById('print-root');
  const label = kind === 'q' ? '문제지' : '정답해설';
  root.innerHTML = `
    <div class="p-toolbar">
      <span class="p-tname">${escHtml(meta.title)} — ${label}</span>
      <button class="p-btn-main" onclick="doPrint()">🖨 인쇄 / PDF로 저장</button>
      <button class="p-btn" onclick="closePrintPreview()">닫기</button>
      <div class="p-hint">인쇄 창에서 <b>「머리글 및 바닥글」</b>을 끄면 URL·날짜가 빠집니다.
        대신 <b>페이지 번호도 사라집니다.</b>
        카카오톡·인스타그램 안에서 열면 인쇄 창이 안 열릴 수 있어요 — Safari나 Chrome으로 열어 주세요.</div>
    </div>
    <div class="p-page">${buildPrintDoc(kind, questions, meta)}</div>`;
  root.classList.remove('hidden');
  document.body.classList.add('print-mode');
  if (_printSavedTitle === null) _printSavedTitle = document.title;
  // 파일명은 '제안'일 뿐이다 — 사용자가 저장 창에서 언제든 바꾼다. UI에 약속하지 말 것.
  document.title = sanitizeFilename(`${meta.title}_${label}_g${meta.gid}`);
  root.scrollTop = 0;
}

// 오버레이 안 버튼 클릭 = 진짜 사용자 제스처. await 를 끼우면 iOS 가 차단한다.
function doPrint() { window.print(); }

function closePrintPreview() {
  document.body.classList.remove('print-mode');
  const root = document.getElementById('print-root');
  root.classList.add('hidden');
  root.innerHTML = '';
  if (_printSavedTitle !== null) { document.title = _printSavedTitle; _printSavedTitle = null; }
}

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.body.classList.contains('print-mode')) closePrintPreview();
});
