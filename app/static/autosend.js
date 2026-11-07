/* Auto-send countdown clocks (round 3). The server deadline in data-deadline is the truth;
   this only redraws mm:ss every second. At zero the page reloads once so the card goes away. */
(function () {
  function fmt(s) {
    s = Math.max(0, s);
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    var ss = (r < 10 ? "0" : "") + r;
    return h ? h + ":" + (m < 10 ? "0" : "") + m + ":" + ss : m + ":" + ss;
  }
  function tick() {
    var now = Date.now(), ended = false;
    document.querySelectorAll(".auto-timer[data-deadline]").forEach(function (el) {
      var t = Date.parse(el.getAttribute("data-deadline"));
      if (isNaN(t)) return;
      var left = Math.round((t - now) / 1000);
      el.textContent = fmt(left);
      if (left <= 0) ended = true;
    });
    if (ended && !window.__autoSendReloaded) {
      window.__autoSendReloaded = true;
      setTimeout(function () { window.location.reload(); }, 90000);
    }
  }
  if (document.querySelector(".auto-timer")) { tick(); setInterval(tick, 1000); }
})();
