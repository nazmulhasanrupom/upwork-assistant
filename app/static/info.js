/* "i" help icons: tap to open, tap elsewhere or Esc to close. Hover and focus are pure CSS. */
(function () {
  function closeAll(except) {
    document.querySelectorAll(".info-wrap.open").forEach(function (w) {
      if (w !== except) w.classList.remove("open");
    });
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest("button.info") : null;
    if (!btn) { closeAll(null); return; }
    var wrap = btn.closest(".info-wrap");
    if (!wrap) return;
    e.preventDefault();
    closeAll(wrap);
    wrap.classList.toggle("open");
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") closeAll(null);
  });
})();
