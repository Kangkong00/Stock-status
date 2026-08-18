/* 재고관리 공통 스크립트 (외부 라이브러리 없음) */
(function () {
  "use strict";

  // ---------------------------------------------------------------- 토스트
  function toast(msg, kind, ms) {
    var box = document.getElementById("toasts");
    if (!box) { box = document.createElement("div"); box.id = "toasts"; document.body.appendChild(box); }
    var el = document.createElement("div");
    el.className = "toast " + (kind || "");
    el.textContent = msg;
    box.appendChild(el);
    setTimeout(function () {
      el.style.transition = "opacity .25s"; el.style.opacity = "0";
      setTimeout(function () { el.remove(); }, 260);
    }, ms || (kind === "err" ? 5200 : 2800));
  }

  // ---------------------------------------------------------------- 통신
  function api(url, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "X-Requested-With": "fetch" }, opts.headers || {});
    if (opts.json !== undefined) {
      opts.method = opts.method || "POST";
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    return fetch(url, opts).then(function (r) {
      var ct = r.headers.get("content-type") || "";
      if (ct.indexOf("application/json") < 0) {
        return r.text().then(function (t) {
          throw new Error(r.ok ? "예상치 못한 응답입니다." : (t.slice(0, 200) || ("오류 " + r.status)));
        });
      }
      return r.json().then(function (d) {
        if (!r.ok || d.ok === false) throw new Error(d.error || ("오류 " + r.status));
        return d;
      });
    });
  }

  // ---------------------------------------------------------------- 포맷
  function num(v, digits) {
    if (v === null || v === undefined || v === "") return "-";
    var n = Number(v);
    if (isNaN(n)) return String(v);
    return n.toLocaleString("ko-KR", {
      minimumFractionDigits: 0,
      maximumFractionDigits: digits === undefined ? 2 : digits
    });
  }
  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function today() {
    var d = new Date(), p = function (n) { return String(n).padStart(2, "0"); };
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate());
  }

  // ---------------------------------------------------------------- 모달
  function openModal(id) {
    var m = document.getElementById(id);
    if (!m) return;
    m.hidden = false;
    var f = m.querySelector("input:not([type=hidden]),select,textarea");
    if (f) setTimeout(function () { f.focus(); f.select && f.select(); }, 40);
  }
  function closeModal(id) {
    var m = id ? document.getElementById(id) : document.querySelector(".modal:not([hidden])");
    if (m) m.hidden = true;
  }
  document.addEventListener("click", function (e) {
    var t = e.target;
    if (t.classList && t.classList.contains("modal")) closeModal();
    if (t.dataset && t.dataset.close !== undefined) closeModal(t.dataset.close || null);
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeModal();
  });

  // ---------------------------------------------------------------- 확인창
  function confirmAction(msg) { return window.confirm(msg); }

  // ---------------------------------------------------------------- 폼 직렬화
  function formData(form) {
    var out = {};
    new FormData(form).forEach(function (v, k) {
      if (out[k] !== undefined) {
        if (!Array.isArray(out[k])) out[k] = [out[k]];
        out[k].push(v);
      } else out[k] = v;
    });
    form.querySelectorAll("input[type=checkbox]").forEach(function (cb) {
      if (cb.name && !cb.checked && out[cb.name] === undefined) out[cb.name] = "";
    });
    return out;
  }

  // ---------------------------------------------------------------- 플래시 닫기
  document.addEventListener("click", function (e) {
    if (e.target.classList && e.target.classList.contains("x")) {
      var f = e.target.closest(".flash");
      if (f) f.remove();
    }
  });

  // ---------------------------------------------------------------- 단축키
  document.addEventListener("keydown", function (e) {
    if (e.target.matches("input,select,textarea")) return;
    var go = { "1": "/", "2": "/stock", "3": "/register", "4": "/pending", "5": "/history" };
    if (e.altKey && go[e.key]) { e.preventDefault(); location.href = go[e.key]; }
    if (e.key === "/" ) {
      var s = document.querySelector("input[name=keyword],input[data-search]");
      if (s) { e.preventDefault(); s.focus(); s.select(); }
    }
  });

  // ---------------------------------------------------------------- 자동 제출 방지
  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (f.dataset.once !== undefined) {
      if (f.dataset.sent) { e.preventDefault(); return; }
      f.dataset.sent = "1";
      var b = f.querySelector("[type=submit]");
      if (b) { b.disabled = true; b.textContent = "처리 중..."; }
    }
  });

  window.App = {
    toast: toast, api: api, num: num, esc: esc, today: today,
    openModal: openModal, closeModal: closeModal,
    confirmAction: confirmAction, formData: formData
  };
})();
