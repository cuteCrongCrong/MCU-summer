// ══════════════════════════════════════════════
// guide_toast.js — 화면 하단 「처음 방문하셨나요?」 안내 띠
//   /guide (사용 가이드) 로 보내는 것이 전부인 작은 띠다. 다른 스크립트를 부르지도,
//   전역을 건드리지도 않는다 — 이 파일을 지우면 사이트는 그 전과 똑같이 동작한다.
//   마크업도 여기서 만든다 (index.html 에 빈 껍데기를 두지 않는다).
//
// 안 띄우는 경우는 **하나뿐**이다 — 오늘 '오늘 다시 안볼래요'를 누른 경우.
//   '닫기'는 지금 이 화면에서만 치우는 것이라 다음 방문(새로고침 포함)에 다시 뜬다.
//   가이드를 보러 갔다는 이유로도 숨기지 않는다 — 안 뜨는 조건이 여럿이면
//   "왜 안 뜨지?"를 아무도 설명할 수 없게 된다. 끄는 방법은 버튼 하나로 족하다.
//
//   ?guide=1 을 주소 뒤에 붙이면 저장값을 무시하고 강제로 띄운다 (문구 확인용).
// ══════════════════════════════════════════════

(function () {
  var KEY_DAY = 'mcu_guide_toast_day';   // '오늘 다시 안볼래요'를 누른 날짜 (YYYY-M-D)

  // 저장소는 시크릿 모드·차단 설정에서 예외를 던진다. 안내가 한 번 더 뜨는 건
  // 괜찮지만 앱이 멈추면 안 되므로 전부 삼킨다. (auth.js 와 같은 어법)
  function get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } }
  function set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* 무시 */ } }

  function today() {
    var d = new Date();
    return d.getFullYear() + '-' + (d.getMonth() + 1) + '-' + d.getDate();
  }

  var forced = location.search.indexOf('guide=1') !== -1;
  if (!forced && get(KEY_DAY) === today()) return;

  var el = document.createElement('div');
  el.className = 'guide-toast';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-label', '사용법 안내');
  el.innerHTML =
    '<div class="guide-toast-title">👋 처음 방문하셨나요?</div>' +
    '<div class="guide-toast-sub">무엇부터 눌러야 할지 5분이면 감이 옵니다.</div>' +
    // ⚠️ 새 탭(target=_blank)으로 연다. 같은 탭에서 이동하면 돌고 있던 문제 생성이
    //    끊긴다 — 생성은 이 탭의 연결에 매여 있다(question_gen.js streamGenerate).
    //    rel=noopener 는 새 탭이 window.opener 로 이 창을 건드리지 못하게 막는다.
    '<a class="guide-toast-cta" href="/guide" target="_blank" rel="noopener">' +
      '📖 이용 가이드 보러가기</a>' +
    '<div class="guide-toast-foot">' +
      '<button type="button" data-act="day">오늘 다시 안볼래요</button>' +
      '<button type="button" data-act="close">닫기</button>' +
    '</div>';

  document.body.appendChild(el);

  function hide() {
    el.classList.remove('open');
    // 사라지는 동안 클릭을 먹지 않도록 애니메이션이 끝나면 치운다
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 450);
    document.removeEventListener('keydown', onKey);
  }

  // Esc = '닫기'와 같다 — 지금 화면에서만 치운다 (저장하지 않는다)
  function onKey(e) { if (e.key === 'Escape') hide(); }

  el.addEventListener('click', function (e) {
    var act = e.target.getAttribute && e.target.getAttribute('data-act');
    if (!act) return;
    if (act === 'day') set(KEY_DAY, today());   // '닫기'는 아무것도 저장하지 않는다
    hide();
  });

  document.addEventListener('keydown', onKey);

  // 모달(첫 방문 로그인 안내 등)이 떠 있으면 그것부터 답하게 두고 기다린다.
  // 무한정 기다리지는 않는다 — 20초(0.6초 × 33회)면 포기하고 그냥 띄운다.
  //
  // ⚠️ 처음 1.2초는 무조건 기다린다. 로그인 안내 모달은 /me 응답을 받은 뒤에야
  //    열리므로(auth.js), 로드 직후에 검사하면 '모달 없음'으로 보여 띠를 먼저 띄우고
  //    곧바로 모달이 그 위를 덮는다.
  var tries = 0;
  setTimeout(function waitForClearScreen() {
    var modalOpen = document.querySelector('.modal-overlay.open');
    if (modalOpen && tries++ < 33) { setTimeout(waitForClearScreen, 600); return; }
    // 한 프레임 뒤에 클래스를 붙여야 transform 이 '이동'으로 그려진다
    // (붙인 직후 바로 바꾸면 브라우저가 최종 상태로 한 번에 그린다)
    //
    // ⚠️ rAF 만 쓰면 안 된다 — 배경 탭처럼 화면을 그리지 않는 상태에서는 콜백이
    //    아예 안 돌아서 띠가 영영 숨은 채로 남는다. setTimeout 을 나란히 걸어
    //    둘 중 먼저 오는 쪽이 띄우게 한다 (클래스를 두 번 붙여도 문제없다).
    var show = function () { el.classList.add('open'); };
    requestAnimationFrame(function () { requestAnimationFrame(show); });
    setTimeout(show, 60);
  }, 1200);
})();
