/* Fold / unfold all search-result matching-fields blocks.
 *
 * The toggle lives inside the #results fragment, so it is recreated on
 * every HTMX swap. The chosen state is persisted in localStorage and
 * re-applied after each swap so the preference survives re-queries.
 */
(function () {
  'use strict';
  var KEY = 'doc3gpp-search-expand';

  function applyState(container) {
    var toggle = container.querySelector('#fold-toggle');
    if (!toggle) return;
    var want = localStorage.getItem(KEY) === '1';
    toggle.checked = want;
    container.querySelectorAll('details.hit-details').forEach(function (d) {
      d.open = want;
    });
  }

  document.addEventListener('change', function (e) {
    if (e.target && e.target.id === 'fold-toggle') {
      localStorage.setItem(KEY, e.target.checked ? '1' : '0');
      document.querySelectorAll('details.hit-details').forEach(function (d) {
        d.open = e.target.checked;
      });
    }
  });

  document.body.addEventListener('htmx:afterSwap', function (e) {
    var target = e.detail && e.detail.target;
    if (target && target.querySelector) applyState(target);
  });

  document.addEventListener('DOMContentLoaded', function () {
    var results = document.getElementById('results');
    if (results) applyState(results);
  });
})();
