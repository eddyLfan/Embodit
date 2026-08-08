// Embodit shared DOM-free utilities (loaded before app.js).
(() => {
  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>'"]/g, (char) => ({
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
    if (!rows || !rows.length) return [];
    const requested = Math.floor(Number(maxPoints));
    // A sampled trajectory is only useful when both endpoints survive. Treat
    // invalid/smaller limits as two points rather than silently dropping one.
    const limit = Number.isFinite(requested) ? Math.max(2, requested) : 400;
    if (rows.length <= limit) return rows;
    const step = (rows.length - 1) / (limit - 1);
    const out = [];
    for (let i = 0; i < limit; i += 1) out.push(rows[Math.round(i * step)]);
    out[0] = rows[0];
    out[out.length - 1] = rows[rows.length - 1];
    return out;
  }

  function mediaIdentity(datasetPath, video, cameraKey = '') {
    let source;
    if (video && video.kind === 'topic' && video.topic) source = `topic:${video.topic}`;
    else if (video && video.kind === 'frames') source = `frames:${cameraKey || video.topic || ''}`;
    else source = `path:${video && video.path || ''}`;
    return JSON.stringify([String(datasetPath || ''), source]);
  }

  window.EmbodyUtils = { escapeHtml, escapeAttr, formatTime, downsampleSeries, mediaIdentity };
})();
