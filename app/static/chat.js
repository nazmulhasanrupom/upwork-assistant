/* Chat page: send without reload, Enter to send, auto-grow, jump pill, code copy. Plain JS. */
(function () {
  var log = document.getElementById("chat-log");
  var form = document.getElementById("chat-form");
  var input = document.getElementById("chat-input");
  var jump = document.getElementById("chat-jump");
  if (!log || !form || !input) { return; }
  var stick = true;
  var seq = 0;

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

  function refresh() {
    form.classList.toggle("is-busy", busy());
    codeButtons();
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

  grow();
  refresh();
  toBottom();
})();
