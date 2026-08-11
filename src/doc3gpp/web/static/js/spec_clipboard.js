/* Click-to-copy for spec list / detail cells.
 *
 * Any element with a `data-copy` attribute copies that value to the
 * clipboard on click. Event delegation on `document` means it works for
 * both the initial page load and HTMX-swapped partials (the spec list
 * results fragment is re-rendered on filter changes, so a one-time
 * DOMContentLoaded binding would miss those cells).
 */
(function () {
  'use strict';

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    // Fallback for non-secure contexts / older browsers.
    return new Promise(function (resolve, reject) {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.position = 'absolute';
      ta.style.left = '-9999px';
      document.body.appendChild(ta);
      ta.select();
      try {
        document.execCommand('copy');
        resolve();
      } catch (err) {
        reject(err);
      } finally {
        document.body.removeChild(ta);
      }
    });
  }

  document.addEventListener('click', function (e) {
    var cell = e.target.closest('[data-copy]');
    if (!cell) return;
    var text = cell.getAttribute('data-copy');
    if (!text) return;
    copyText(text).then(function () {
      var original = cell.textContent;
      cell.textContent = 'copied';
      setTimeout(function () {
        cell.textContent = original;
      }, 1200);
    }).catch(function () {
      // Clipboard unavailable; leave the value in the title tooltip.
    });
  });
})();
