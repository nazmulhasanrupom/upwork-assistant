/* Inbox: pick a room on the left, its thread shows on the right. One pane at a time on a phone. */
(function () {
  var split = document.querySelector(".inbox-split");
  if (!split) return;
  split.classList.add("js");
  var links = split.querySelectorAll("[data-thread]");
  var threads = split.querySelectorAll(".thread");
  function open(id) {
    threads.forEach(function (t) { t.hidden = t.id !== id; });
    links.forEach(function (l) {
      if (l.getAttribute("data-thread") === id) l.setAttribute("aria-current", "true");
      else l.removeAttribute("aria-current");
    });
    split.classList.toggle("has-open", !!id);
  }
  links.forEach(function (l) {
    l.addEventListener("click", function (e) { e.preventDefault(); open(l.getAttribute("data-thread")); });
  });
  var back = split.querySelector("[data-thread-back]");
  if (back) back.addEventListener("click", function () { open(""); });
  open(window.matchMedia("(max-width: 719px)").matches ? "" : (links[0] ? links[0].getAttribute("data-thread") : ""));
})();
