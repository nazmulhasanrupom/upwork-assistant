/* Copy buttons: <button type="button" data-copy="#element-id">. Plain JS, no library. */
document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-copy]");
  if (!button) return;
  var source = document.querySelector(button.getAttribute("data-copy"));
  if (!source) return;
  var text = source.textContent.trim();
  var label = button.textContent;
  function done(ok) {
    button.textContent = ok ? "Copied" : "Press Ctrl+C";
    setTimeout(function () { button.textContent = label; }, 1500);
  }
  function fallback() {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(area);
    done(ok);
  }
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function () { done(true); }, fallback);
  } else {
    fallback();
  }
});
