(() => {
  'use strict';

  // Library and Performers expose their tabs as links so each screen keeps a
  // address of its own. Keyboard activation follows the ARIA tab pattern by
  // moving focus and clicking the link, which the router then renders.
  const tablists = Array.from(document.querySelectorAll('nav.section-tabs[role="tablist"]'));
  if (!tablists.length) return;

  function tabsOf(tablist) { return Array.from(tablist.querySelectorAll('[role="tab"]')); }

  function updateTabStops(tablist) {
    const tabs = tabsOf(tablist);
    if (!tabs.length) return;
    const selected = tabs.find(tab => tab.getAttribute('aria-selected') === 'true') || tabs[0];
    tabs.forEach(tab => tab.setAttribute('tabindex', tab === selected ? '0' : '-1'));
  }

  tablists.forEach(tablist => {
    const tabs = tabsOf(tablist);
    // The router updates aria-selected and panel visibility; run after its
    // listener so pointer and keyboard activation share one roving tab stop.
    tabs.forEach(tab => tab.addEventListener('click', () => updateTabStops(tablist)));
    tablist.addEventListener('keydown', event => {
      const current = tabs.indexOf(document.activeElement);
      if (current < 0) return;
      let next;
      if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
      else if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = tabs.length - 1;
      else return;

      event.preventDefault();
      tabs[next].focus();
      tabs[next].click();
    });
    updateTabStops(tablist);
  });
  document.addEventListener('adminv2:navigate', () => tablists.forEach(updateTabStops));
})();
