/* Notice (toast): any [data-notice] hides after 4s; errors stay until clicked. */
(function () {
  document.querySelectorAll("[data-notice]").forEach(function (n) {
    if (n.classList.contains("error")) { n.addEventListener("click", function () { n.remove(); }); return; }
    setTimeout(function () { n.remove(); }, 4000);
  });
})();
