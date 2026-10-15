(() => {
  'use strict';
  const endpoint = '/admin/api/v2/organize/quarantine';
  const button = document.querySelector('#quarantineExport');
  const feedback = document.querySelector('#quarantineFeedback');

  async function exportReport() {
    if (button.disabled) return;
    button.disabled = true;
    window.AdminV2.setFeedback(feedback, 'Preparing a complete quarantine report…');
    try {
      const items = [];
      const seen = new Set();
      let total = null;
      let offset = 0;
      for (;;) {
        const page = await window.AdminV2.getJson(`${endpoint}?limit=100&offset=${offset}`);
        if (page.available === false || !Array.isArray(page.items) || !Number.isInteger(page.total)
            || page.total < 0 || typeof page.truncated !== 'boolean' || page.offset !== offset) {
          throw new Error('The quarantine report is incomplete. No file was exported.');
        }
        if (total === null) total = page.total;
        if (page.total !== total) throw new Error('Quarantine changed during export. Please retry.');
        for (const item of page.items) {
          const key = JSON.stringify([item.id, item.operation_id]);
          if (seen.has(key)) throw new Error('Quarantine changed during export. Please retry.');
          seen.add(key);
          items.push(item);
        }
        if (items.length > total || (page.truncated && (!page.items.length || items.length >= total))) {
          throw new Error('The quarantine report is incomplete. No file was exported.');
        }
        if (!page.truncated) {
          if (items.length !== total) throw new Error('The quarantine report is incomplete. No file was exported.');
          break;
        }
        offset += page.items.length;
      }
      const report = { items, total: items.length, truncated: false, offset: 0, available: true };
      window.AdminV2.downloadBlob('quarantine-report.json', new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }));
      window.AdminV2.setFeedback(feedback, `Quarantine report exported (${items.length} items).`);
    } catch (error) {
      window.AdminV2.setFeedback(feedback, error.message || 'Could not export the quarantine report.', 'error');
    } finally { button.disabled = false; }
  }

  button.addEventListener('click', exportReport);
})();
