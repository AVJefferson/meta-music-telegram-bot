(function () {
  var path = location.pathname.replace(/\/$/, "") || "/";
  document.querySelectorAll(".top nav a").forEach(function (a) {
    var href = a.getAttribute("href") || "";
    if (href.indexOf("://") !== -1) return;
    if (href === path || (path === "/" && href === "/")) {
      a.setAttribute("aria-current", "page");
    }
  });
  document.querySelectorAll("a[href]").forEach(function (a) {
    var href = a.getAttribute("href") || "";
    if (!/^https?:\/\//i.test(href)) return;
    try {
      if (new URL(href, location.href).origin === location.origin) return;
    } catch (e) {
      return;
    }
    a.setAttribute("target", "_blank");
    a.setAttribute("rel", "noopener noreferrer");
  });
  var y = document.getElementById("y");
  if (y) y.textContent = String(new Date().getUTCFullYear());
})();
