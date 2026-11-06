/* Whole table row is a link: <tr data-href="/path">. The title link inside keeps keyboard use. */
document.addEventListener("click", function (event) {
  var row = event.target.closest ? event.target.closest("tr[data-href]") : null;
  if (!row || event.target.closest("a, button, input, select, textarea, summary")) return;
  if (window.getSelection && String(window.getSelection())) return;
  window.location.href = row.getAttribute("data-href");
});
