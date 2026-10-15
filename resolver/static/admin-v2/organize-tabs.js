(() => {
  'use strict';

  const tablist = document.querySelector('#organizeTabList');
  if (!tablist) return;
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));
  if (!tabs.length) return;

  function updateTabStops() {
    const selected = tabs.find(tab => tab.getAttribute('aria-selected') === 'true') || tabs[0];
    tabs.forEach(tab => tab.setAttribute('tabindex', tab === selected ? '0' : '-1'));
  }

  // The main Organize controller updates aria-selected and panel visibility.
  // Run after that button listener so pointer and keyboard activation share
  // the same roving tab stop.
  tabs.forEach(tab => tab.addEventListener('click', updateTabStops));
  tablist.addEventListener('keydown', event => {
    const current = tabs.indexOf(document.activeElement);
    if (current < 0) return;
    let next = current;
    if (event.key === 'ArrowRight') next = (current + 1) % tabs.length;
    else if (event.key === 'ArrowLeft') next = (current - 1 + tabs.length) % tabs.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = tabs.length - 1;
    else return;

    event.preventDefault();
    tabs[next].focus();
    tabs[next].click();
  });
  updateTabStops();
})();
