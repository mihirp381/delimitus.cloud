// Applies the chosen theme before the first paint. Kept in step with src/theme.ts by a test.
(function () {
  try {
    var kept = window.localStorage.getItem('ssc-console-theme');
    if (kept === 'light' || kept === 'dark') document.documentElement.setAttribute('data-theme', kept);
  } catch (e) {
    // No storage: the system setting decides.
  }
})();
