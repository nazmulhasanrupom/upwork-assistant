/* Notice (toast): any [data-notice] hides after 4s; errors stay until clicked.
   A notice with data-toast-id (a finished background action) shows once per browser tab.
   The top bar part #actions-bar is swapped by HTMX; its open list is kept open. */
(function () {
  var wasOpen = false;
  function seen(id) {
    try {
      var list = JSON.parse(sessionStorage.getItem("toasts") || "[]");
      if (list.indexOf(id) >= 0) { return true; }
      list.push(id);
      sessionStorage.setItem("toasts", JSON.stringify(list.slice(-50)));
    } catch (e) { /* storage blocked: show it */ }
    return false;
  }
  function start(root) {
    root.querySelectorAll("[data-notice]").forEach(function (n) {
      if (n.dataset.noticeOn) { return; }
      n.dataset.noticeOn = "1";
      var id = n.getAttribute("data-toast-id");
      if (id && seen(id)) { n.remove(); return; }
      if (n.classList.contains("error")) { n.addEventListener("click", function () { n.remove(); }); return; }
      setTimeout(function () { n.remove(); }, 4000);
    });
  }
  start(document);
  document.addEventListener("htmx:beforeSwap", function (e) {
    if (e.detail.target && e.detail.target.id === "actions-bar") {
      var d = e.detail.target.querySelector("details");
      wasOpen = !!(d && d.open);
    }
  });
  document.addEventListener("htmx:afterSwap", function (e) {
    var t = e.detail.target;
    if (t && t.id === "actions-bar") {
      var d = t.querySelector("details");
      if (d && wasOpen) { d.open = true; }
      start(t);
    }
  });
})();
