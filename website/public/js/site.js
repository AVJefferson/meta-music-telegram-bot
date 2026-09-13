(function () {
  var path = location.pathname.replace(/\/$/, "") || "/";
  document.querySelectorAll("nav a").forEach(function (a) {
    var href = a.getAttribute("href") || "";
    if (href === path || (path === "/" && href === "/")) {
      a.setAttribute("aria-current", "page");
    }
  });
  var y = document.getElementById("y");
  if (y) y.textContent = String(new Date().getUTCFullYear());
})();
