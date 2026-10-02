(function () {
  'use strict';

  var panel = document.querySelector('[data-status-url]');
  if (!panel) return;

  function poll() {
    fetch(panel.dataset.statusUrl, {credentials: 'same-origin'})
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.status === 'finished') {
          window.location.href = panel.dataset.doneUrl;
        } else if (data.status === 'in_progress' && !panel.classList.contains('handoff-status')) {
          window.location.reload();
        }
      })
      .catch(function () { /* The next poll retries; game state remains server-authoritative. */ });
  }

  setInterval(poll, 4000);
})();
