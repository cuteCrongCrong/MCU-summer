// ══════════════════════════════════════════════
// wrong_note.js — 오답 노트 탭 (폴더 목록 · 저장 모달 · 폴더 보기)
//   common.js 이후 로드. renderQuestions/currentQuestions/escHtml 은 common.js 것을 재사용.
// ══════════════════════════════════════════════

// ── 오답 폴더 보기 닫기 (폴더 목록으로) ──
function closeWrongView() {
  document.getElementById('wrong-view').classList.add('hidden');
  document.getElementById('wrong-card').classList.remove('hidden');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

let wrongFolderCache = [];
let wrongPendingIdx = null;   // 저장 대기 중인 문제의 인덱스

async function loadWrongFolders() {
  const listEl = document.getElementById('wrong-folder-list');
  try {
    const resp = await fetch('/wrong-folders');
    const data = await resp.json();
    wrongFolderCache = data.folders || [];
    renderWrongFolderList();
  } catch (err) {
    listEl.textContent = '오답 폴더를 불러오지 못했습니다.';
  }
}

function renderWrongFolderList() {
  const listEl = document.getElementById('wrong-folder-list');
  if (!wrongFolderCache.length) {
    listEl.innerHTML = '<span style="color:#94a3b8;">아직 오답 폴더가 없습니다. 생성된 문제의 <b>🔖 오답에 넣기</b> 버튼으로 폴더를 만들어 보세요.</span>';
    return;
  }
  listEl.innerHTML = wrongFolderCache.map(f => `
    <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border:1.5px solid #fecaca;background:#fff;border-radius:10px;margin-top:8px;">
      <div style="min-width:0;flex:1;">
        <div style="font-weight:600;color:#1e293b;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">📁 ${escHtml(f.name)}</div>
        <div style="font-size:0.75rem;color:#94a3b8;margin-top:2px;">${escHtml(f.created_at || '')} · 문제 ${f.item_count}개</div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;">
        <button onclick="viewWrongFolder(${f.id})" style="font-size:0.78rem;padding:5px 12px;border:none;border-radius:7px;background:#dc2626;color:#fff;cursor:pointer;">보기</button>
        <button onclick="renameWrongFolderRow(${f.id})" title="이름 변경" style="font-size:0.78rem;padding:5px 8px;border:1px solid #cbd5e1;border-radius:7px;background:#fff;cursor:pointer;">✏️</button>
        <button onclick="deleteWrongFolderRow(${f.id})" title="삭제" style="font-size:0.78rem;padding:5px 8px;border:1px solid #fecaca;border-radius:7px;background:#fff;color:#dc2626;cursor:pointer;">🗑️</button>
      </div>
    </div>`).join('');
}

async function viewWrongFolder(fid) {
  try {
    const resp = await fetch('/wrong-folders/' + fid);
    const f = await resp.json();
    if (!resp.ok || f.error) return alert(f.error || '폴더를 불러오지 못했습니다.');
    if (!f.questions.length) {
      alert('이 폴더에는 아직 담긴 문제가 없습니다.');
      return;
    }
    renderQuestions(f.questions, '', {
      folder: { id: f.id, name: f.name, items: f.items },
      containerId: 'wrong-questions-container',
      titleId: 'wrong-view-title',
    });
    document.getElementById('wrong-card').classList.add('hidden');
    document.getElementById('wrong-view').classList.remove('hidden');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  } catch (err) {
    alert('폴더를 불러오지 못했습니다.');
  }
}

async function renameWrongFolderRow(fid) {
  const cur = (wrongFolderCache.find(f => f.id === fid) || {}).name || '';
  const name = prompt('새 폴더 이름을 입력하세요.', cur);
  if (name == null || !name.trim()) return;
  const form = new FormData();
  form.append('name', name.trim());
  await fetch('/wrong-folders/' + fid + '/rename', { method: 'POST', body: form });
  await loadWrongFolders();
}

async function deleteWrongFolderRow(fid) {
  if (!confirm('이 오답 폴더를 삭제할까요? 담긴 문제가 모두 사라집니다.')) return;
  await fetch('/wrong-folders/' + fid, { method: 'DELETE' });
  await loadWrongFolders();
}

async function removeWrongItem(itemId, folderId) {
  if (itemId == null) return;
  if (!confirm('이 문제를 폴더에서 뺄까요?')) return;
  await fetch('/wrong-items/' + itemId, { method: 'DELETE' });
  await loadWrongFolders();
  // 폴더를 다시 조회해 보기를 갱신 — 비었으면 목록으로 돌아감
  try {
    const resp = await fetch('/wrong-folders/' + folderId);
    const f = await resp.json();
    if (!resp.ok || f.error || !f.questions.length) { closeWrongView(); return; }
    renderQuestions(f.questions, '', {
      folder: { id: f.id, name: f.name, items: f.items },
      containerId: 'wrong-questions-container',
      titleId: 'wrong-view-title',
    });
  } catch (err) {
    closeWrongView();
  }
}

// ── 저장 모달 ──
function openWrongModal(idx) {
  wrongPendingIdx = idx;
  const q = currentQuestions[idx] || {};
  document.getElementById('wrong-modal-preview').textContent =
    `문제 ${idx + 1}. ${(q['문제'] || '').slice(0, 120)}${(q['문제'] || '').length > 120 ? '…' : ''}`;
  document.getElementById('wrong-new-folder-name').value = '';
  renderModalFolders();
  document.getElementById('wrong-modal').classList.add('open');
  // 최신 폴더 목록 확보 후 다시 렌더
  loadWrongFoldersForModal();
}

function closeWrongModal() {
  document.getElementById('wrong-modal').classList.remove('open');
  wrongPendingIdx = null;
}

async function loadWrongFoldersForModal() {
  try {
    const resp = await fetch('/wrong-folders');
    const data = await resp.json();
    wrongFolderCache = data.folders || [];
  } catch (err) { /* 캐시 사용 */ }
  renderModalFolders();
}

function renderModalFolders() {
  const box = document.getElementById('wrong-modal-folders');
  if (!wrongFolderCache.length) {
    box.innerHTML = '<div style="font-size:0.82rem;color:#94a3b8;padding:4px 0;">기존 폴더가 없습니다. 아래에서 새 폴더를 만들어 주세요.</div>';
    return;
  }
  box.innerHTML = wrongFolderCache.map(f => `
    <div class="folder-pick" onclick="saveToFolder(${f.id})">
      <span class="fp-name">📁 ${escHtml(f.name)}</span>
      <span class="fp-count">문제 ${f.item_count}개 · 여기에 담기 →</span>
    </div>`).join('');
}

async function saveToFolder(fid) {
  if (wrongPendingIdx == null) return;
  const q = currentQuestions[wrongPendingIdx] || {};
  const form = new FormData();
  form.append('question', JSON.stringify(q));
  try {
    const resp = await fetch('/wrong-folders/' + fid + '/items', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok || data.error) return alert(data.error || '저장에 실패했습니다.');
    markSaved(wrongPendingIdx);
    if (data.duplicate) alert('이미 이 폴더에 담겨 있는 문제입니다.');
    closeWrongModal();
    loadWrongFolders();
  } catch (err) {
    alert('저장에 실패했습니다.');
  }
}

async function createFolderAndSave() {
  const name = document.getElementById('wrong-new-folder-name').value.trim();
  if (!name) return alert('새 폴더 이름을 입력해주세요.');
  const form = new FormData();
  form.append('name', name);
  try {
    const resp = await fetch('/wrong-folders', { method: 'POST', body: form });
    const data = await resp.json();
    if (!resp.ok || data.error) return alert(data.error || '폴더 생성에 실패했습니다.');
    await saveToFolder(data.id);
  } catch (err) {
    alert('폴더 생성에 실패했습니다.');
  }
}

function markSaved(idx) {
  const btn = document.getElementById('wbtn-' + idx);
  if (btn) {
    btn.classList.add('saved');
    btn.textContent = '✅ 오답에 저장됨';
  }
}

// ── 초기 로드 ──
loadWrongFolders();
