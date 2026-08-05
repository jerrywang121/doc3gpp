/* Dropdown-checkbox column picker for the tdoc list filter form.
 *
 * The form lives outside the #results fragment, so it is never
 * HTMX-swapped; this script runs once on full page load. No-JS users
 * still get the form, with the panel collapsed (hidden attribute).
 */
(function () {
  'use strict';
  document.addEventListener('DOMContentLoaded', function () {
    var trigger = document.querySelector('.columns-trigger');
    if (!trigger) return;
    var root = trigger.closest('.columns-dropdown');
    var panel = root.querySelector('.columns-panel');
    var count = root.querySelector('.columns-count');

    function updateCount() {
      var n = panel.querySelectorAll('input[type="checkbox"]:checked').length;
      if (count) count.textContent = n;
    }

    trigger.addEventListener('click', function (e) {
      e.stopPropagation();
      var open = panel.hidden;
      panel.hidden = !open;
      trigger.setAttribute('aria-expanded', String(open));
    });
    panel.addEventListener('change', updateCount);
    document.addEventListener('click', function (e) {
      if (!e.target.closest('.columns-dropdown')) {
        panel.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
      }
    });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && !panel.hidden) {
        panel.hidden = true;
        trigger.setAttribute('aria-expanded', 'false');
      }
    });
  });
})();
