// Marks the page stale when the last check is too old, and shows local times.
// Without JavaScript the page still shows the UTC time of the last check.
(function () {
  var body = document.body;
  var generated = Date.parse(body.getAttribute('data-generated') || '');
  var staleHours = Number(body.getAttribute('data-stale-hours')) || 7;
  var fmt;
  try {
    fmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  } catch (e) { fmt = null; }
  Array.prototype.forEach.call(document.querySelectorAll('time[datetime]'), function (el) {
    var t = Date.parse(el.getAttribute('datetime'));
    if (!isNaN(t) && fmt) { el.textContent = fmt.format(new Date(t)); el.title = el.getAttribute('datetime'); }
  });
  var age = (Date.now() - generated) / 3600000;
  if (isNaN(generated) || age > staleHours) {
    body.classList.add('is-stale');
    var banner = document.getElementById('stale');
    if (banner) {
      if (!isNaN(generated)) {
        banner.querySelector('p').textContent =
          'Monitoring data is out of date (last check ' + Math.floor(age) + ' hours ago). Current status is unknown.';
      }
      banner.hidden = false;
    }
    var headline = document.querySelector('.overall .headline');
    if (headline) headline.textContent = 'Status unknown';
    Array.prototype.forEach.call(document.querySelectorAll('.component .state-label'), function (el) {
      el.textContent = 'Unknown';
    });
  }
})();
