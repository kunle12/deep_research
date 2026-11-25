/* Apply the saved theme before first paint (classic, CSP-safe script).
   app.js::initTheme() keeps the toggle in sync afterwards. */
(function () {
  try {
    var saved = localStorage.getItem("dr-theme");
    document.documentElement.dataset.theme = saved === "dark" ? "dark" : "light";
  } catch (e) {
    document.documentElement.dataset.theme = "light";
  }
})();
