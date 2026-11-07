/* Chat page: send without reload, Enter to send, auto-grow, jump pill, code copy, history drawer
   and search, and the answer in the making (Server-Sent Events, word by word). Plain JS.
   Streamed text is only ever added as text nodes; the final reply is the server's HTML. */
(function () {
  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var jump = document.getElementById("chat-jump");
  if (!log || !form || !input) { return; }
  var stick = true;
  var seq = 0;
  var sseFailed = !window.EventSource;
  var stream = null;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function nearBottom() {
    return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120;
  }
  function toBottom() { window.scrollTo(0, document.documentElement.scrollHeight); }
  function busy() { return !!log.querySelector(".chat-pending"); }

  function grow() {
    var line = parseFloat(getComputedStyle(input).lineHeight) || 22;
    input.style.height = "auto";
    var max = line * 8;
    input.style.height = Math.min(input.scrollHeight, max) + "px";
    input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
  }

  function codeButtons() {
    log.querySelectorAll(".chat-ai pre:not([data-c])").forEach(function (pre) {
      pre.setAttribute("data-c", "1");
      pre.id = pre.id || "chat-code-" + (++seq);
      var wrap = document.createElement("div");
      wrap.className = "chat-code";
      pre.parentNode.insertBefore(wrap, pre);
      wrap.appendChild(pre);
      var b = document.createElement("button");
      b.type = "button";
      b.className = "chat-copy";
      b.textContent = "Copy";
      b.setAttribute("data-copy", "#" + pre.id);
      wrap.appendChild(b);
    });
  }

  /* ---------- the answer in the making ---------- */

  function startStream(pend) {
    var url = pend.getAttribute("data-stream");
    if (!url) { return; }
    pend.setAttribute("data-live", "1");
    var steps = pend.querySelector(".chat-steps");
    var label = pend.querySelector(".chat-think-label");
    var clock = pend.querySelector(".chat-think-time");
    var out = pend.querySelector(".chat-stream");
    var wordMs = parseInt(pend.getAttribute("data-word-ms"), 10) || 32;
    var t0 = Date.now() - (parseInt(pend.getAttribute("data-since"), 10) || 0) * 1000;
    var st = { ok: true, finishing: false };
    var queue = [];
    var timer = null;
    var ended = false;
    var block = null;
    var caret = document.createElement("span");
    caret.className = "chat-caret";
    caret.setAttribute("aria-hidden", "true");
    var es = new EventSource(url);
    stream = st;
    if (steps) { steps.textContent = ""; }

    var tick = window.setInterval(function () {
      if (!document.body.contains(pend)) { window.clearInterval(tick); return; }
      if (clock && !pend.classList.contains("is-answering")) {
        clock.textContent = Math.max(0, Math.round((Date.now() - t0) / 1000)) + "s";
      }
    }, 1000);

    function newBlock() {
      block = document.createElement("div");
      block.className = "chat-sline";
      out.appendChild(block);
      block.appendChild(caret);
    }
    function answering() {
      if (pend.classList.contains("is-answering")) { return; }
      pend.classList.add("is-answering");
      var secs = Math.max(1, Math.round((Date.now() - t0) / 1000));
      if (label) { label.textContent = "Thought for " + secs + "s"; }
      if (clock) { clock.textContent = ""; }
    }
    function pump() {
      timer = null;
      if (!queue.length) { if (ended) { finish(); } return; }
      var n = reduced ? queue.length : Math.max(1, Math.ceil(queue.length / 40));
      while (n-- > 0 && queue.length) {
        var tok = queue.shift();
        if (tok === null || !block) { newBlock(); if (tok === null) { continue; } }
        var w = document.createElement("span");
        w.className = "chat-w";
        w.textContent = tok;
        block.insertBefore(w, caret);
      }
      timer = window.setTimeout(pump, wordMs);
    }
    function push(text) {
      var lines = String(text).split("\n");
      lines.forEach(function (line, i) {
        if (i > 0) { queue.push(null); }
        var words = line.match(/\s*\S+\s*/g) || [];
        words.forEach(function (w) { queue.push(w); });
      });
      if (queue.length) { answering(); out.hidden = false; }
      if (!timer) { timer = window.setTimeout(pump, 0); }
    }
    function finish() {
      st.finishing = true;
      window.clearInterval(tick);
      if (caret.parentNode) { caret.parentNode.removeChild(caret); }
      if (window.htmx && document.body.contains(pend)) {
        window.htmx.ajax("GET", pend.getAttribute("data-poll"), { target: "#chat-pending", swap: "outerHTML", source: "#chat-pending" });
      }
    }
    function fail() {
      st.ok = false;
      sseFailed = true;
      es.close();
      window.clearInterval(tick);
      if (timer) { window.clearTimeout(timer); }
      if (caret.parentNode) { caret.parentNode.removeChild(caret); }
    }

    es.addEventListener("think", function (e) {
      var li = document.createElement("li");
      li.textContent = JSON.parse(e.data).text;
      if (steps) { steps.appendChild(li); }
    });
    es.addEventListener("delta", function (e) { push(JSON.parse(e.data).text); });
    es.addEventListener("reset", function () {
      queue = [];
      out.textContent = "";
      block = null;
    });
    es.addEventListener("done", function () {
      ended = true;
      es.close();
      if (!timer && !queue.length) { finish(); }
    });
    es.onerror = function () { if (!ended) { fail(); } };
  }

  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var el = e.detail && e.detail.elt;
    if (el && el.id === "chat-pending" && stream && stream.ok && !stream.finishing) {
      e.preventDefault();   /* the stream is working: no polling */
    }
  });

  /* new messages slide in (only the ones the user just sent, never the final swap) */
  document.body.addEventListener("htmx:afterSwap", function (e) {
    var cfg = e.detail && e.detail.requestConfig;
    if (!cfg || cfg.elt !== form || reduced) { return; }
    var msgs = log.querySelectorAll(".chat-msg");
    for (var i = Math.max(0, msgs.length - 2); i < msgs.length; i++) {
      msgs[i].classList.add("chat-enter");
      msgs[i].addEventListener("animationend", function (ev) { ev.currentTarget.classList.remove("chat-enter"); }, { once: true });
    }
  });

  function refresh() {
    form.classList.toggle("is-busy", busy());
    codeButtons();
    var pend = log.querySelector("#chat-pending");
    if (pend && !sseFailed && !pend.getAttribute("data-live")) { startStream(pend); }
    if (stick) { toBottom(); }
  }

  new MutationObserver(refresh).observe(log, { childList: true, subtree: true });
  window.addEventListener("scroll", function () {
    stick = nearBottom();
    if (jump) { jump.hidden = stick; }
  }, { passive: true });
  if (jump) { jump.addEventListener("click", function () { stick = true; toBottom(); }); }

  input.addEventListener("input", grow);
  input.addEventListener("keydown", function (e) {
    if (e.key !== "Enter" || e.shiftKey || e.isComposing) { return; }
    e.preventDefault();
    if (!busy() && input.value.trim()) { form.requestSubmit(); }
  });

  form.addEventListener("htmx:beforeRequest", function (e) {
    if (busy()) { e.preventDefault(); return; }
    ["chat-empty", "chat-chips"].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) { el.remove(); }
    });
    var view = document.getElementById("chat-view");
    if (view) { view.classList.remove("is-empty"); }
    stick = true;
  });
  form.addEventListener("htmx:afterRequest", function (e) {
    if (e.detail.successful && e.target === form) { input.value = ""; grow(); }
  });

  document.addEventListener("click", function (e) {
    var chip = e.target.closest("[data-suggest]");
    if (!chip || busy()) { return; }
    input.value = chip.getAttribute("data-suggest");
    form.requestSubmit();
  });
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) { e.preventDefault(); }
  });

  /* ---------- history: drawer on small screens, search, row menus ---------- */

  var shell = document.getElementById("chat-shell");
  var histBtn = document.getElementById("chat-histbtn");
  var scrim = document.getElementById("chat-scrim");
  var closeBtn = document.getElementById("chat-side-close");
  var search = document.getElementById("chat-search");

  function drawer(open) {
    if (!shell) { return; }
    shell.classList.toggle("is-drawer", open);
    if (histBtn) { histBtn.setAttribute("aria-expanded", open ? "true" : "false"); }
    if (open && closeBtn) { closeBtn.focus({ preventScroll: true }); }
    if (!open && histBtn && document.activeElement && shell.querySelector("#chat-side").contains(document.activeElement)) {
      histBtn.focus({ preventScroll: true });
    }
  }
  if (histBtn) { histBtn.addEventListener("click", function () { drawer(!shell.classList.contains("is-drawer")); }); }
  if (scrim) { scrim.addEventListener("click", function () { drawer(false); }); }
  if (closeBtn) { closeBtn.addEventListener("click", function () { drawer(false); }); }

  function closeMenus(except) {
    document.querySelectorAll(".chat-hmenu[open]").forEach(function (d) { if (d !== except) { d.removeAttribute("open"); } });
  }
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") { return; }
    closeMenus(null);
    if (shell && shell.classList.contains("is-drawer")) { drawer(false); }
  });
  document.addEventListener("click", function (e) {
    var menu = e.target.closest && e.target.closest(".chat-hmenu");
    closeMenus(menu);
  });

  function filter() {
    var list = document.getElementById("chat-hist");
    if (!list || !search) { return; }
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    list.querySelectorAll(".chat-hgroup").forEach(function (g) {
      var any = 0;
      g.querySelectorAll(".chat-hitem").forEach(function (li) {
        var hit = !q || (li.getAttribute("data-title") || "").indexOf(q) !== -1;
        li.hidden = !hit;
        if (hit) { any++; }
      });
      g.hidden = !any;
      shown += any;
    });
    var none = list.querySelector(".chat-hnone");
    if (none) { none.hidden = !(q && !shown); }
  }
  if (search) { search.addEventListener("input", filter); }
  document.body.addEventListener("htmx:afterSwap", filter);

  grow();
  refresh();
  toBottom();
})();
