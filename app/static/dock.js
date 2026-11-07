/* Claude jobs dock: HTMX swaps the whole dock every few seconds. Keep what the person opened
   (the dock and each job) open, and show the newest log lines. */
(function () {
  var open = null;
  document.addEventListener("htmx:beforeSwap", function (e) {
    var t = e.detail.target;
    if (!t || t.id !== "claude-dock") { return; }
    open = {};
    t.querySelectorAll("details[data-keep]").forEach(function (d) {
      if (d.open) { open[d.getAttribute("data-keep")] = true; }
    });
  });
  function restore() {
    var dock = document.getElementById("claude-dock");
    if (!dock) { return; }
    dock.querySelectorAll("details[data-keep]").forEach(function (d) {
      if (open && open[d.getAttribute("data-keep")]) { d.open = true; }
    });
    dock.querySelectorAll(".dock-log").forEach(function (pre) { pre.scrollTop = pre.scrollHeight; });
    open = null;
  }
  document.addEventListener("htmx:afterSwap", function (e) {
    if (open !== null) { restore(); }
  });
  // Nothing runs: the dock is hidden and has no HTMX poll. Look again now and then.
  function idleLook() {
    var dock = document.getElementById("claude-dock");
    var secs = dock ? parseInt(dock.getAttribute("data-idle-poll"), 10) : 0;
    if (secs > 0 && !document.hidden && window.htmx) {
      window.htmx.ajax("GET", "/claude/dock", { target: "#claude-dock", swap: "outerHTML" });
    }
    setTimeout(idleLook, (secs > 0 ? secs : 10) * 1000);
  }
  setTimeout(idleLook, 10000);
})();
