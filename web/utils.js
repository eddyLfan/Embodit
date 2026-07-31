// Embodit shared DOM-free utilities (loaded before app.js).
(() => {
  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>'"]/g, (char) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    })[char]);
  }

  function escapeAttr(value) {
    return escapeHtml(value);
  }

  function formatTime(seconds) {
    const safe = Math.max(0, Number(seconds) || 0);
    const m = Math.floor(safe / 60);
    const s = Math.floor(safe % 60);
    const ms = Math.floor((safe % 1) * 1000);
    return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}.${String(ms).padStart(3, '0')}`;
  }

  function downsampleSeries(rows, maxPoints = 400) {
    if (!rows?.length) return [];
    if (rows.length <= maxPoints) return rows;
    const step = rows.length / maxPoints;
    const out = [];
    for (let i = 0; i < maxPoints; i++) out.push(rows[Math.min(rows.length - 1, Math.floor(i * step))]);
    return out;
  }

  window.EmbodyUtils = { escapeHtml, escapeAttr, formatTime, downsampleSeries };
})();
