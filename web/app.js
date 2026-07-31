(() => {
  const params = new URLSearchParams(location.search);
  // Auth uses an HttpOnly cookie set by the server on first visit. The
  // legacy token (query / meta) is kept only as a fallback for old servers.
  const token = params.get('token') || document.querySelector('meta[name="lerobot-token"]')?.content || '';
  if (params.get('token')) {
    // Scrub the token from the address bar / browser history.
    params.delete('token');
    const query = params.toString();
    history.replaceState(null, '', `${location.pathname}${query ? `?${query}` : ''}`);
  }
  const state = {
    browseRoot: '', currentFolder: '', parentFolder: null,
    dataset: null, episodeByIndex: new Map(), states: {}, filtered: [], currentEpisode: null,
    playing: false, animationFrame: null, progressPath: '',
    timelineDragging: false, resumeAfterSeek: false, seekSequence: 0,
    previewSeekTimer: null, pendingSeekSeconds: 0,
    autoFilterPollTimer: null, autoFilterJobId: null,
    episodeReady: false, requiredBufferSeconds: 2,
    pendingAutoPlay: false,
    trajectory: null, trajectoryEpisode: null,
    convertContext: null, // { path, format } for standalone convert
    convertJobs: [],
    augmentContext: null,
    augmentJobs: [],
    augmentOptions: null,
    augmentPreviewOk: false,
    lastAugmentPreviewJobId: null,
    breakpoints: [], // sorted seconds; consecutive pairs form draft intervals
    draftIntervalMeta: {}, // `${start}_${end}` -> { tags, note }
    trajVisible: null, // boolean[] aligned to action dims
    trajScaleMode: 'per', // shared | per
    workspaceMode: 'browse',
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  // Shared DOM-free helpers live in utils.js (window.EmbodyUtils).
  const { escapeHtml, escapeAttr, formatTime, downsampleSeries } = window.EmbodyUtils;

  const t = (key, vars) => (window.EmbodyI18n ? window.EmbodyI18n.t(key, vars) : key);


  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { 'Content-Type': 'application/json', 'X-LeRobot-Token': token, ...(options.headers || {}) }
    });
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try { message = (await response.json()).detail || message; } catch {}
      throw new Error(message);
    }
    return response.json();
  }

  // Custom tooltip for [data-tip] elements: instant, styled, multiline,
  // follows the pointer and stays inside the viewport (unlike native title).
  function initTooltips() {
    const tip = document.createElement('div');
    tip.className = 'app-tooltip hidden';
    document.body.appendChild(tip);
    let anchor = null;
    const hide = () => {
      anchor = null;
      tip.classList.add('hidden');
    };
    const position = (x, y) => {
      const pad = 14;
      const rect = tip.getBoundingClientRect();
      let left = x + pad;
      let top = y + pad;
      if (left + rect.width > window.innerWidth - 8) left = x - rect.width - pad;
      if (top + rect.height > window.innerHeight - 8) top = y - rect.height - pad;
      tip.style.left = `${Math.max(8, left)}px`;
      tip.style.top = `${Math.max(8, top)}px`;
    };
    document.addEventListener('mouseover', (event) => {
      const target = event.target.closest?.('[data-tip]');
      if (!target || target === anchor) return;
      const text = target.dataset.tip;
      if (!text) return;
      anchor = target;
      tip.textContent = text;
      tip.classList.remove('hidden');
      position(event.clientX, event.clientY);
    });
    document.addEventListener('mousemove', (event) => {
      if (anchor) position(event.clientX, event.clientY);
    });
    document.addEventListener('mouseout', (event) => {
      if (anchor && !anchor.contains(event.relatedTarget)) hide();
    });
    document.addEventListener('scroll', hide, true);
    document.addEventListener('click', hide, true);
  }

  async function initialize() {
    bindChooser();
    bindReview();
    bindLanguage();
    initTooltips();
    window.EmbodyI18n?.applyStaticI18n();
    try {
      const health = await api('/api/health');
      state.browseRoot = health.browseRoot;
      $('#pathInput').value = health.browseRoot;
      await browse(health.browseRoot);
    } catch (error) {
      showToast(error.message, true);
      $('#folderList').innerHTML = `<div class="empty error">${escapeHtml(t('cannotConnect', { msg: error.message }))}</div>`;
    }
    try {
      await refreshConvertJobs();
    } catch {}
    try {
      await refreshAugmentJobs();
    } catch {}
  }

  function bindLanguage() {
    const select = $('#langSelect');
    if (!select || select.dataset.bound === '1') return;
    select.dataset.bound = '1';
    if (!window.EmbodyI18n) {
      console.error('[Embodit] i18n failed to load; language switch will not work');
    }
    select.value = window.EmbodyI18n?.getLang?.() || 'zh';
    select.addEventListener('change', () => {
      if (!window.EmbodyI18n) {
        showToast('Language pack failed to load. Please hard-refresh the page.', true);
        return;
      }
      window.EmbodyI18n.setLang(select.value);
      refreshLocalizedUi();
    });
    window.addEventListener('embody:langchange', () => refreshLocalizedUi());
  }

  function refreshLocalizedUi() {
    window.EmbodyI18n?.applyStaticI18n();
    if (state.dataset) {
      renderSummary();
      renderEpisodeList();
      if (Number.isInteger(state.currentEpisode)) {
        renderEpisodeHeader();
        renderLabelPanel();
        drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
        const prompt = episodePrompt(currentEpisode()) || '—';
        const promptEl = $('#episodePrompt');
        if (promptEl) promptEl.textContent = prompt;
      }
      const toggle = $('#headerToggle');
      if (toggle) {
        const collapsed = document.body.classList.contains('header-collapsed');
        toggle.title = collapsed ? t('expandHeader') : t('collapseHeader');
        toggle.setAttribute('aria-label', toggle.title);
      }
    } else if (state.currentFolder) {
      browse(state.currentFolder);
    }
    try { renderConvertJobDock(); } catch {}
    try { renderAugmentJobDock(); } catch {}
    try { renderConvertFidelity(); } catch {}
    try { fillAugmentColorPresets(); syncAugmentFormVisibility(); } catch {}
  }

  function bindChooser() {
    $('#browsePath').addEventListener('click', () => browse($('#pathInput').value));
    $('#openPath').addEventListener('click', () => openDataset($('#pathInput').value));
    $('#goParent').addEventListener('click', () => state.parentFolder && browse(state.parentFolder));
    $('#pathInput').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') browse(event.target.value);
    });
  }

  function bindReview() {
    $('#changeDataset').addEventListener('click', () => {
      pauseAll();
      $('#review').classList.add('hidden');
      $('#chooser').classList.remove('hidden');
      onLeaveReview();
    });
    $('#search').addEventListener('input', applyFilters);
    $('#stateFilter').addEventListener('change', applyFilters);
    $('#interventionOnly').addEventListener('change', applyFilters);
    $('#episodeList').addEventListener('click', (event) => {
      const button = event.target.closest('[data-episode]');
      if (button) selectEpisode(Number(button.dataset.episode));
    });
    $('#playPause').addEventListener('click', togglePlay);
    $('#timeline').addEventListener('pointerdown', beginTimelineSeek);
    $('#timeline').addEventListener('input', onTimelineInput);
    $('#timeline').addEventListener('change', endTimelineSeek);
    $('#timeline').addEventListener('pointerup', endTimelineSeek);
    $('#timeline').addEventListener('pointercancel', endTimelineSeek);
    $('#previousEpisode').addEventListener('click', () => moveEpisode(-1));
    $('#nextEpisode').addEventListener('click', () => moveEpisode(1));
    $('#saveProgress').addEventListener('click', saveProgress);
    $('#loadProgress').addEventListener('click', loadProgress);
    $('#createDataset').addEventListener('click', createDataset);
    $('#closeAutoFilter')?.addEventListener('click', closeAutoFilterDialog);
    $('#closeAutoFilter2')?.addEventListener('click', closeAutoFilterDialog);
    $('#closeConvert')?.addEventListener('click', closeConvertDialog);
    $('#startConvert')?.addEventListener('click', startConvert);
    $('#convertTarget')?.addEventListener('change', renderConvertFidelity);
    $('#refreshConvertJobs')?.addEventListener('click', () => refreshConvertJobs().catch(() => {}));
    $('#toggleConvertJobs')?.addEventListener('click', () => {
      $('#convertJobDock')?.classList.toggle('collapsed');
    });
    $('#convertJobList')?.addEventListener('click', async (event) => {
      const cancelButton = event.target.closest('[data-cancel-job]');
      if (cancelButton) {
        const jobId = cancelButton.dataset.cancelJob;
        if (!jobId) return;
        try {
          await api(`/api/convert/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: '{}' });
          await refreshConvertJobs();
        } catch (error) {
          showToast(error.message, true);
        }
        return;
      }
      const button = event.target.closest('[data-dismiss-job]');
      if (!button) return;
      const jobId = button.dataset.dismissJob;
      if (!jobId) return;
      try {
        await api(`/api/convert/jobs/${encodeURIComponent(jobId)}/dismiss`, { method: 'POST', body: '{}' });
        await refreshConvertJobs();
      } catch (error) {
        showToast(error.message, true);
      }
    });
    $('#closeAugment')?.addEventListener('click', closeAugmentDialog);
    $('#runAugmentPreview')?.addEventListener('click', runAugmentPreview);
    $('#startAugmentBatch')?.addEventListener('click', startAugmentBatch);
    $('#augmentType')?.addEventListener('change', () => { syncAugmentFormVisibility(); invalidateAugmentPreview(); });
    $('#augmentBrightnessMode')?.addEventListener('change', () => { syncAugmentFormVisibility(); invalidateAugmentPreview(); });
    $('#augmentBatchScope')?.addEventListener('change', syncAugmentFormVisibility);
    $('#augmentColorMode')?.addEventListener('change', () => { syncAugmentFormVisibility(); invalidateAugmentPreview(); });
    $('#augmentApplyMode')?.addEventListener('change', () => { fillAugmentColorPresets(); invalidateAugmentPreview(); });
    ['augmentBrightnessGain', 'augmentBrightnessGamma', 'augmentSamPrompts', 'augmentColorPreset',
      'augmentColorCustom', 'augmentGpuId'].forEach((id) => {
      $(`#${id}`)?.addEventListener('input', invalidateAugmentPreview);
      $(`#${id}`)?.addEventListener('change', invalidateAugmentPreview);
    });
    $('#refreshAugmentJobs')?.addEventListener('click', () => refreshAugmentJobs().catch(() => {}));
    $('#toggleAugmentJobs')?.addEventListener('click', () => {
      $('#augmentJobDock')?.classList.toggle('collapsed');
    });
    $('#augmentJobList')?.addEventListener('click', async (event) => {
      const cancelButton = event.target.closest('[data-cancel-augment-job]');
      if (cancelButton) {
        const jobId = cancelButton.dataset.cancelAugmentJob;
        if (!jobId) return;
        try {
          await api(`/api/augment/jobs/${encodeURIComponent(jobId)}/cancel`, { method: 'POST', body: '{}' });
          await refreshAugmentJobs();
        } catch (error) {
          showToast(error.message, true);
        }
        return;
      }
      const button = event.target.closest('[data-dismiss-augment-job]');
      if (!button) return;
      const jobId = button.dataset.dismissAugmentJob;
      if (!jobId) return;
      try {
        await api(`/api/augment/jobs/${encodeURIComponent(jobId)}/dismiss`, { method: 'POST', body: '{}' });
        await refreshAugmentJobs();
      } catch (error) {
        showToast(error.message, true);
      }
    });
    $('#saveEpisodeLabel')?.addEventListener('click', saveEpisodeLabel);
    $('#intervalBreakpoint')?.addEventListener('click', addIntervalBreakpoint);
    $('#undoBreakpoint')?.addEventListener('click', undoIntervalBreakpoint);
    $('#clearBreakpoints')?.addEventListener('click', clearIntervalBreakpoints);
    $('#saveTaggedIntervals')?.addEventListener('click', saveTaggedIntervals);
    $('#draftIntervalList')?.addEventListener('input', (event) => {
      const row = event.target.closest('[data-draft-start]');
      if (!row) return;
      captureDraftMetaFromDom();
    });
    $('#breakpointChips')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-remove-bp]');
      if (!button) return;
      removeIntervalBreakpoint(Number(button.dataset.removeBp));
    });
    $('#intervalList')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-delete-interval]');
      if (!button) return;
      const start = Number(button.dataset.start);
      const end = Number(button.dataset.end);
      deleteIntervalLabel(start, end);
    });
    $('#trajSelectAll')?.addEventListener('click', () => setAllTrajDims(true));
    $('#trajSelectNone')?.addEventListener('click', () => setAllTrajDims(false));
    $('#trajSelectFirst')?.addEventListener('click', () => setFirstTrajDims(6));
    $$('input[name="trajScale"]').forEach((input) => {
      input.addEventListener('change', () => {
        if (!input.checked) return;
        state.trajScaleMode = input.value === 'per' ? 'per' : 'shared';
        drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
      });
    });
    $('#trajDimToggles')?.addEventListener('change', (event) => {
      const input = event.target.closest('input[data-traj-dim]');
      if (!input || !Array.isArray(state.trajVisible)) return;
      const index = Number(input.dataset.trajDim);
      if (!Number.isInteger(index)) return;
      state.trajVisible[index] = input.checked;
      input.closest('.traj-dim-toggle')?.classList.toggle('off', !input.checked);
      drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
    });
    $$('#workspaceTabs [data-mode]').forEach((button) => {
      button.addEventListener('click', () => setWorkspaceMode(button.dataset.mode));
    });
    setWorkspaceMode(state.workspaceMode || 'browse');
    $$('[data-state]').forEach((button) => button.addEventListener('click', () => setDecision(button.dataset.state)));
    $$('[data-bulk]').forEach((button) => button.addEventListener('click', () => bulkSet(button.dataset.bulk)));
    document.addEventListener('keydown', onKeyDown);
  }

  function setWorkspaceMode(mode) {
    const allowed = new Set(['browse', 'annotate', 'filter', 'convert', 'augment']);
    const next = allowed.has(mode) ? mode : 'browse';
    // Convert / augment are actions: open dialog and stay on prior work mode.
    if (next === 'convert') {
      if (state.dataset) {
        openConvertDialog(state.dataset.path, state.dataset.format || null);
      } else {
        showToast(t('needConvertDataset'), true);
      }
      const fallback = state.workspaceMode && !['convert', 'augment'].includes(state.workspaceMode)
        ? state.workspaceMode
        : 'browse';
      applyWorkspaceMode(fallback);
      return;
    }
    if (next === 'augment') {
      if (state.dataset) {
        openAugmentDialog(state.dataset.path, state.dataset.format || null);
      } else {
        showToast(t('needAugmentDataset'), true);
      }
      const fallback = state.workspaceMode && !['convert', 'augment'].includes(state.workspaceMode)
        ? state.workspaceMode
        : 'browse';
      applyWorkspaceMode(fallback);
      return;
    }
    applyWorkspaceMode(next);
  }

  function applyWorkspaceMode(next) {
    state.workspaceMode = next;
    document.body.classList.remove('mode-browse', 'mode-annotate', 'mode-filter', 'mode-convert');
    document.body.classList.add(`mode-${next}`);
    $$('#workspaceTabs [data-mode]').forEach((button) => {
      button.classList.toggle('active', button.dataset.mode === next);
    });
    if (next === 'annotate') {
      try { renderLabelPanel(); } catch {}
    }
    if (next === 'browse' || next === 'annotate' || next === 'filter') {
      // Canvas may have been 0-sized while hidden; redraw after mode switch.
      requestAnimationFrame(() => {
        drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
      });
    }
    try { localStorage.setItem('embody-workspace-mode', next); } catch {}
  }

  function formatDatasetBrief(brief) {
    if (!brief) return '';
    const parts = [];
    if (brief.totalEpisodes != null) parts.push(t('briefEpisodes', { n: brief.totalEpisodes }));
    if (brief.totalFrames != null) parts.push(t('briefFrames', { n: Number(brief.totalFrames).toLocaleString() }));
    if (brief.fps != null) parts.push(`${brief.fps} FPS`);
    if (brief.robotType) parts.push(String(brief.robotType));
    if (brief.labels) parts.push(t('briefLabels', { n: brief.labels.labelCount, m: brief.labels.labeledEpisodes }));
    return parts.join(' · ');
  }

  async function browse(path) {
    setBusy(true, t('readingRemote'), path);
    try {
      const result = await api(`/api/list?path=${encodeURIComponent(path)}`);
      state.currentFolder = result.path;
      state.parentFolder = result.parent;
      $('#pathInput').value = result.path;
      $('#goParent').disabled = !result.parent;
      const fmtLabel = result.formatLabel || '';
      const rootBrief = formatDatasetBrief(result.brief);
      $('#folderInfo').innerHTML = result.isDataset
        ? `<span class="dataset-badge">${escapeHtml(t('currentIsDataset'))} · ${escapeHtml(fmtLabel || t('recognized'))}${rootBrief ? ` · ${escapeHtml(rootBrief)}` : ''}</span>`
        : `<span>${escapeHtml(t('entriesCount', { n: result.entries.length }))}</span>`;
      $('#folderList').innerHTML = [
        result.isDataset ? `<div class="folder dataset current">
            <button class="folder-main" data-open="${escapeAttr(result.path)}">
              <span class="folder-icon">DS</span>
              <span><strong>${escapeHtml(t('openCurrentDataset'))}</strong><small>${escapeHtml(fmtLabel)} · ${escapeHtml(result.path)}</small></span>
            </button>
            <div class="folder-actions">
              <button class="folder-convert" data-convert="${escapeAttr(result.path)}" data-format="${escapeAttr(result.format || '')}">${escapeHtml(t('convertFormat'))}</button>
              <button class="folder-augment" data-augment="${escapeAttr(result.path)}" data-format="${escapeAttr(result.format || '')}">${escapeHtml(t('augmentFormat'))}</button>
            </div>
          </div>` : '',
        ...result.entries.map((entry) => {
          const entryBrief = entry.isDataset ? formatDatasetBrief(entry.brief) : '';
          const entryKind = entry.isDataset
            ? `${escapeHtml(entry.formatLabel || t('dataset'))}${entryBrief ? ` · ${escapeHtml(entryBrief)}` : ''}`
            : (entry.isDir === false ? escapeHtml(t('file')) : escapeHtml(t('directory')));
          return `
          <div class="folder ${entry.isDataset ? 'dataset' : ''}">
            <button class="folder-main" data-${entry.isDataset || entry.isDir === false ? 'open' : 'browse'}="${escapeAttr(entry.path)}">
              <span class="folder-icon">${entry.isDataset ? 'DS' : (entry.isDir === false ? 'FIL' : '▰')}</span>
              <span><strong>${escapeHtml(entry.name)}</strong><small>${entryKind}</small></span>
            </button>
            ${entry.isDataset ? `<div class="folder-actions">
              <button class="folder-convert" data-convert="${escapeAttr(entry.path)}" data-format="${escapeAttr(entry.format || '')}">${escapeHtml(t('convertFormat'))}</button>
              <button class="folder-augment" data-augment="${escapeAttr(entry.path)}" data-format="${escapeAttr(entry.format || '')}">${escapeHtml(t('augmentFormat'))}</button>
            </div>` : ''}
          </div>`;
        })
      ].join('') || `<div class="empty">${escapeHtml(t('emptyFolder'))}</div>`;
      $$('[data-browse]').forEach((button) => button.addEventListener('click', () => browse(button.dataset.browse)));
      $$('[data-open]').forEach((button) => button.addEventListener('click', () => openDataset(button.dataset.open)));
      $$('[data-convert]').forEach((button) => button.addEventListener('click', (event) => {
        event.stopPropagation();
        openConvertDialog(button.dataset.convert, button.dataset.format || null);
      }));
      $$('[data-augment]').forEach((button) => button.addEventListener('click', (event) => {
        event.stopPropagation();
        openAugmentDialog(button.dataset.augment, button.dataset.format || null);
      }));
    } catch (error) {
      showToast(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function openDataset(path) {
    setBusy(true, t('readingMeta'), path);
    try {
      state.dataset = await api('/api/inspect', { method: 'POST', body: JSON.stringify({ dataset: path }) });
      state.episodeByIndex = new Map(state.dataset.episodes.map((episode) => [episode.episodeIndex, episode]));
      state.states = normalizeStates(loadLocalStates(state.dataset.path));
      state.filtered = state.dataset.episodes;
      state.labels = [];
      state.labelPresets = [];
      try {
        const labelDoc = await api('/api/labels/load', { method: 'POST', body: JSON.stringify({ dataset: state.dataset.path }) });
        state.labels = labelDoc.labels || [];
        state.labelPresets = labelDoc.presets || [];
        state.labelsPath = labelDoc.path;
      } catch {}
      $('#datasetName').textContent = `${state.dataset.name} · ${state.dataset.formatLabel || state.dataset.format || ''}`;
      $('#datasetPath').textContent = state.dataset.path;
      $('#chooser').classList.add('hidden');
      $('#review').classList.remove('hidden');
      onEnterReview();
      try {
        const savedMode = localStorage.getItem('embody-workspace-mode');
        applyWorkspaceMode(savedMode && savedMode !== 'convert' ? savedMode : 'browse');
      } catch {
        applyWorkspaceMode('browse');
      }
      renderSummary();
      renderEpisodeList();
      selectEpisode(state.dataset.episodes[0]?.episodeIndex);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  function labelStats() {
    const labels = state.labels || [];
    const episodes = new Set();
    const tagCounts = new Map();
    labels.forEach((item) => {
      const idx = Number(item.episode_index);
      if (Number.isInteger(idx)) episodes.add(idx);
      (item.tags || []).forEach((tag) => tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1));
    });
    const topTags = [...tagCounts.entries()]
      .sort((a, b) => b[1] - a[1] || String(a[0]).localeCompare(String(b[0])))
      .slice(0, 5);
    return { total: labels.length, episodes: episodes.size, topTags };
  }

  function formatTotalDuration(frames, fps) {
    if (!frames || !fps) return null;
    const seconds = frames / fps;
    if (seconds < 90) return `${Math.round(seconds)} s`;
    if (seconds < 5400) return `${(seconds / 60).toFixed(1)} min`;
    return `${(seconds / 3600).toFixed(1)} h`;
  }

  function renderSummary() {
    const counts = { pass: 0, quarantine: 0, review: 0 };
    state.dataset.episodes.forEach((episode) => counts[getState(episode.episodeIndex)]++);
    const reviewed = counts.pass + counts.quarantine;
    const percent = state.dataset.totalEpisodes ? Math.round(reviewed / state.dataset.totalEpisodes * 100) : 0;
    const cameras = state.dataset.videoKeys || [];
    const stats = labelStats();
    const features = state.dataset.features || {};

    // Distinct task strings with episode counts.
    const taskCounts = new Map();
    state.dataset.episodes.forEach((episode) => (episode.tasks || []).forEach((task) => {
      const text = String(task || '').trim();
      if (text) taskCounts.set(text, (taskCounts.get(text) || 0) + 1);
    }));
    const sortedTasks = [...taskCounts.entries()].sort((a, b) => b[1] - a[1]);
    const shownTasks = sortedTasks.slice(0, 3);
    const tasksCell = shownTasks.length
      ? `<div class="summary-tags" data-tip="${escapeAttr(sortedTasks.map(([task, n]) => `${task} × ${n}`).join('\n'))}"><span>${escapeHtml(t('topTasksLabel'))}</span><div class="summary-tag-list">${shownTasks.map(([task, n]) => `<em class="summary-tag summary-task">${escapeHtml(task)}${taskCounts.size > 1 ? ` × ${n}` : ''}</em>`).join('')}${sortedTasks.length > shownTasks.length ? `<em class="summary-tag muted">${escapeHtml(t('moreTasks', { n: sortedTasks.length }))}</em>` : ''}</div></div>`
      : '';

    // action / state dimensionality with joint names on hover.
    const dimOf = (key) => (Array.isArray(features[key]?.shape)
      ? features[key].shape.reduce((a, b) => a * b, 1)
      : null);
    const actionDim = dimOf('action');
    const stateDim = dimOf('observation.state');
    const dimNames = [
      ...flattenFeatureNames(features.action?.names).map((name) => `action.${name}`),
      ...flattenFeatureNames(features['observation.state']?.names).map((name) => `state.${name}`),
    ];
    const dimsCell = (actionDim != null || stateDim != null)
      ? `<div${dimNames.length ? ` data-tip="${escapeAttr(dimNames.join('\n'))}"` : ''}><strong>${actionDim ?? '-'} / ${stateDim ?? '-'}</strong><span>${escapeHtml(t('dimsLabel'))}</span></div>`
      : '';

    // First camera resolution from feature shape [H, W, C].
    const camShape = cameras.map((key) => features[key]?.shape).find((shape) => Array.isArray(shape) && shape.length === 3);
    const resolution = camShape ? ` · ${camShape[1]}×${camShape[0]}` : '';

    const totalDuration = formatTotalDuration(state.dataset.totalFrames, state.dataset.fps);
    const fpsAssumed = state.dataset.extras?.fpsAssumed
      ? ` <b class="warn">${escapeHtml(t('fpsAssumedBadge'))}</b>`
      : '';
    const labelCell = stats.total
      ? `<div><strong>${stats.total}</strong><span>${escapeHtml(t('labelsCellSuffix', { m: stats.episodes }))}</span></div>`
      : `<div><strong>0</strong><span>${escapeHtml(t('labelsTotalLabel'))}</span></div>`;
    const tagsCell = stats.topTags.length
      ? `<div class="summary-tags"><span>${escapeHtml(t('topTagsLabel'))}</span><div class="summary-tag-list">${stats.topTags.map(([tag, n]) => `<em class="summary-tag">${escapeHtml(tag)} × ${n}</em>`).join('')}</div></div>`
      : '';
    $('#summary').innerHTML = `
      <div><strong>${state.dataset.totalEpisodes}</strong><span>${escapeHtml(t('episodes'))}</span></div>
      <div><strong>${Number(state.dataset.totalFrames || 0).toLocaleString()}</strong><span>${escapeHtml(t('frames'))}${totalDuration ? ` · ≈ ${escapeHtml(totalDuration)}` : ''}</span></div>
      <div><strong>${state.dataset.fps || '-'}</strong><span>FPS · ${escapeHtml(state.dataset.formatLabel || '')}${fpsAssumed}</span></div>
      <div${state.dataset.robotType ? ` data-tip="${escapeAttr(String(state.dataset.robotType))}"` : ''}><strong>${escapeHtml(String(state.dataset.robotType || '-'))}</strong><span>${escapeHtml(t('robotTypeLabel'))}</span></div>
      <div${cameras.length ? ` data-tip="${escapeAttr(cameras.map((key) => `${key}${Array.isArray(features[key]?.shape) && features[key].shape.length === 3 ? ` (${features[key].shape[1]}×${features[key].shape[0]})` : ''}`).join('\n'))}"` : ''}><strong>${cameras.length}</strong><span>${escapeHtml(t('camerasLabel'))}${resolution}${cameras.length ? ` · ${escapeHtml(cameras.slice(0, 2).join(', '))}${cameras.length > 2 ? '…' : ''}` : ''}</span></div>
      ${dimsCell}
      <div class="good"><strong>${counts.pass}</strong><span>${escapeHtml(t('pass'))}</span></div>
      <div class="bad"><strong>${counts.quarantine}</strong><span>${escapeHtml(t('quarantine'))}</span></div>
      <div><strong>${counts.review}</strong><span>${escapeHtml(t('review'))}</span></div>
      <div class="review-progress"><span>${escapeHtml(t('reviewProgress'))} ${percent}%</span><div><i style="width:${percent}%"></i></div></div>
      ${labelCell}
      ${tagsCell}
      ${tasksCell}`;
  }

  function episodeLabelSummary(episodeIndex, labelsByEpisode) {
    const labels = labelsByEpisode.get(episodeIndex);
    if (!labels?.length) return null;
    const episodeLevel = labels.find((item) => item.target === 'episode');
    const intervals = labels.filter((item) => item.target === 'interval').length;
    const tags = [...new Set(labels.flatMap((item) => item.tags || []))];
    const parts = [t('episodeLabeledBadge')];
    if (episodeLevel?.quality_score != null) parts.push(`${t('qualityShort')} ${episodeLevel.quality_score}`);
    if (episodeLevel?.success === true) parts.push(t('successYes'));
    if (episodeLevel?.success === false) parts.push(t('successNo'));
    if (intervals) parts.push(t('intervalsBadge', { n: intervals }));
    return { text: parts.join(' · '), tags, success: episodeLevel?.success };
  }

  function groupLabelsByEpisode() {
    const map = new Map();
    (state.labels || []).forEach((item) => {
      const idx = Number(item.episode_index);
      if (!Number.isInteger(idx)) return;
      if (!map.has(idx)) map.set(idx, []);
      map.get(idx).push(item);
    });
    return map;
  }

  function renderEpisodeList() {
    const labelsByEpisode = groupLabelsByEpisode();
    $('#episodeList').innerHTML = state.filtered.map((episode) => {
      const decision = getState(episode.episodeIndex);
      const cssDecision = decision === 'pass' ? 'keep' : decision === 'quarantine' ? 'exclude' : 'pending';
      const selected = state.currentEpisode === episode.episodeIndex ? ' selected' : '';
      const task = episode.tasks?.join('\n') || t('noTask');
      const labelInfo = episodeLabelSummary(episode.episodeIndex, labelsByEpisode);
      const labelLine = labelInfo
        ? `<span class="episode-labels ${labelInfo.success === false ? 'bad' : ''}" data-tip="${escapeAttr([labelInfo.text, ...labelInfo.tags].join('\n'))}">${escapeHtml(labelInfo.text)}${labelInfo.tags.length ? ` · ${escapeHtml(labelInfo.tags.slice(0, 3).join(', '))}${labelInfo.tags.length > 3 ? '…' : ''}` : ''}</span>`
        : '';
      return `<button class="episode ${cssDecision}${selected}" data-episode="${episode.episodeIndex}">
        <span class="episode-row"><strong>Episode ${episode.episodeIndex}</strong><i class="state-dot"></i></span>
        <span class="episode-task" data-tip="${escapeAttr(task)}">${escapeHtml(task.replace(/\n/g, ', '))}</span>
        <span class="episode-meta">${episode.length} ${escapeHtml(t('framesUnit'))} · ${formatTime(episode.duration)}${episode.hasIntervention ? ` · <b>${escapeHtml(t('humanIntervention'))}</b>` : ''}</span>
        ${labelLine}
      </button>`;
    }).join('') || `<div class="empty">${escapeHtml(t('noMatchingEpisode'))}</div>`;
  }

  function selectEpisode(index) {
    if (!Number.isInteger(index)) return;
    pauseAll();
    setEpisodeReady(false, t('bufferingEpisode'));
    const previousIndex = state.currentEpisode;
    state.currentEpisode = index;
    state.pendingAutoPlay = true;
    state.breakpoints = [];
    state.draftIntervalMeta = {};
    state.trajVisible = null;
    const episode = currentEpisode();
    updateEpisodeSelection(previousIndex, index);
    renderEpisodeHeader();
    renderVideosOptimized(episode);
    loadEpisodeSignals(episode);
    try { renderLabelPanel(); } catch (error) { console.error(error); }
  }

  function updateEpisodeSelection(previousIndex, nextIndex) {
    if (Number.isInteger(previousIndex)) {
      $('#episodeList').querySelector(`[data-episode="${previousIndex}"]`)?.classList.remove('selected');
    }
    const nextButton = $('#episodeList').querySelector(`[data-episode="${nextIndex}"]`);
    nextButton?.classList.add('selected');
    if (nextButton && !isElementVisibleInList(nextButton, $('#episodeList'))) {
      nextButton.scrollIntoView({ block: 'nearest' });
    }
  }

  function isElementVisibleInList(element, container) {
    const item = element.getBoundingClientRect();
    const list = container.getBoundingClientRect();
    return item.top >= list.top && item.bottom <= list.bottom;
  }

  function episodePrompt(episode) {
    if (!episode) return '';
    return episode.extras?.prompt || episode.tasks?.filter(Boolean).join(' · ') || state.dataset?.extras?.prompt || '';
  }

  function renderEpisodeHeader() {
    const episode = currentEpisode();
    if (!episode) return;
    const decision = getState(episode.episodeIndex);
    const prompt = episodePrompt(episode) || t('noTask');
    const labelInfo = episodeLabelSummary(episode.episodeIndex, groupLabelsByEpisode());
    const labelFacts = labelInfo
      ? `<span class="label-fact ${labelInfo.success === false ? 'warning' : ''}" data-tip="${escapeAttr([labelInfo.text, ...labelInfo.tags].join('\n'))}">${escapeHtml(labelInfo.text)}</span>${labelInfo.tags.slice(0, 4).map((tag) => `<span class="label-fact-tag">${escapeHtml(tag)}</span>`).join('')}${labelInfo.tags.length > 4 ? `<span class="label-fact-tag" data-tip="${escapeAttr(labelInfo.tags.slice(4).join('\n'))}">+${labelInfo.tags.length - 4}</span>` : ''}`
      : '';
    $('#episodeHeader').innerHTML = `
      <div>
        <h3>Episode ${episode.episodeIndex}</h3>
        <p class="episode-prompt-line" data-tip="${escapeAttr(prompt)}"><span class="prompt-kicker">${escapeHtml(t('promptLabel'))}</span> ${escapeHtml(prompt)}</p>
      </div>
      <div class="episode-facts">
        <span>${episode.length} ${escapeHtml(t('framesUnit'))}</span><span>${formatTime(episode.duration)}</span>
        <span>${escapeHtml(t('platformId'))} ${escapeHtml(episode.platformEpisodeId || episode.extras?.mcapName || '-')}</span>
        ${episode.hasIntervention ? `<span class="warning">${escapeHtml(t('humanIntervention'))}</span>` : ''}
        ${labelFacts}
        <span class="decision-tag ${decision === 'pass' ? 'keep' : decision === 'quarantine' ? 'exclude' : 'pending'}">${stateLabel(decision)}</span>
      </div>`;
    const promptEl = $('#episodePrompt');
    if (promptEl) promptEl.textContent = prompt || '—';
    syncDecisionBar(decision);
  }

  async function loadEpisodeSignals(episode) {
    if (!episode || !state.dataset) return;
    const episodeIndex = episode.episodeIndex;
    const prompt = episodePrompt(episode) || '—';
    const promptEl = $('#episodePrompt');
    if (promptEl) promptEl.textContent = prompt;
    $('#trajMeta').textContent = t('trajLoading');
    $('#trajDimToggles') && ($('#trajDimToggles').innerHTML = '');
    $('#trajReadout') && ($('#trajReadout').textContent = '');
    drawTrajectory(null, 0);
    try {
      const result = await api(`/api/timeseries?dataset=${encodeURIComponent(state.dataset.path)}&episode=${episodeIndex}&maxPoints=1200`);
      if (state.currentEpisode !== episodeIndex) return;
      const series = result.series || {};
      const eefKeys = Object.keys(series).filter((key) => key.startsWith('eef.'));
      let action = series.action || null;
      let sourceKey = action ? 'action' : null;
      if (!action) {
        const candidates = ['observation.state', ...Object.keys(series).filter((key) => !key.startsWith('eef.'))];
        for (const key of candidates) {
          const rows = series[key];
          if (Array.isArray(rows) && rows.length && Array.isArray(rows[0]) && rows[0].length >= 2) {
            action = rows;
            sourceKey = key;
            break;
          }
        }
      }
      const eefSeries = eefKeys.map((key) => ({ key, rows: series[key] })).filter((item) => item.rows?.length);
      state.trajectory = {
        action,
        sourceKey,
        eefKeys: eefSeries.map((item) => item.rows),
        eefNames: eefSeries.map((item) => item.key),
        raw: series,
      };
      state.trajectoryEpisode = episodeIndex;
      const primary = pickActionMatrix(state.trajectory);
      const dims = Array.isArray(primary?.[0]) ? primary[0].length : (primary ? 1 : 0);
      const steps = primary?.length || 0;
      state.trajectory.names = resolveTrajectoryDimNames(dims, state.trajectory);
      ensureTrajVisibility(dims);
      renderTrajDimToggles();
      $('#trajMeta').textContent = steps
        ? t('trajMeta', { steps, dims, visible: state.trajVisible.filter(Boolean).length })
        : t('trajEmpty');
      drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
    } catch (error) {
      if (state.currentEpisode !== episodeIndex) return;
      state.trajectory = null;
      state.trajVisible = null;
      state.trajectoryEpisode = episodeIndex;
      $('#trajMeta').textContent = `${t('trajEmpty')} (${error.message || 'error'})`;
      $('#trajDimToggles') && ($('#trajDimToggles').innerHTML = '');
      drawTrajectory(null, 0);
    }
  }

  const TRAJ_DIM_COLORS = [
    '#2563eb', '#ea580c', '#16a34a', '#db2777', '#7c3aed', '#0891b2',
    '#ca8a04', '#dc2626', '#4f46e5', '#059669', '#c026d3', '#0d9488',
    '#9333ea', '#b45309', '#0284c7', '#be123c',
  ];

  function flattenFeatureNames(raw) {
    if (raw == null) return [];
    if (typeof raw === 'string') {
      return raw.split(/[,|]/).map((part) => part.trim()).filter(Boolean);
    }
    if (Array.isArray(raw)) {
      if (raw.length === 1 && typeof raw[0] === 'string' && /[,|]/.test(raw[0])) {
        return flattenFeatureNames(raw[0]);
      }
      if (raw.length === 1 && Array.isArray(raw[0])) {
        return flattenFeatureNames(raw[0]);
      }
      if (raw.every((item) => typeof item === 'string' || typeof item === 'number')) {
        return raw.map((item) => String(item));
      }
      return raw.flat(Infinity).map((item) => String(item));
    }
    if (typeof raw === 'object') {
      const preferred = raw.motors || raw.names || raw.joints || raw.action || raw.state;
      if (preferred) return flattenFeatureNames(preferred);
      const firstArray = Object.values(raw).find((value) => Array.isArray(value) || typeof value === 'string');
      if (firstArray) return flattenFeatureNames(firstArray);
    }
    return [];
  }

  function resolveTrajectoryDimNames(dims, trajectory) {
    const features = state.dataset?.features || {};
    const sourceKey = trajectory?.sourceKey;
    let names = [];
    if (sourceKey && features[sourceKey]?.names != null) {
      names = flattenFeatureNames(features[sourceKey].names);
    } else if (features.action?.names != null) {
      names = flattenFeatureNames(features.action.names);
    } else if (features['observation.state']?.names != null) {
      names = flattenFeatureNames(features['observation.state'].names);
    } else if (trajectory?.eefNames?.length && !trajectory?.action?.length) {
      // Build names from eef streams × pose dims.
      const sample = trajectory.eefKeys?.[0]?.[0];
      const per = Array.isArray(sample) ? sample.length : 1;
      const pose = ['x', 'y', 'z', 'qx', 'qy', 'qz', 'qw', 'grip'];
      trajectory.eefNames.forEach((key) => {
        const short = String(key).replace(/^eef\./, '');
        for (let i = 0; i < per; i++) names.push(`${short}.${pose[i] || i}`);
      });
    }
    while (names.length < dims) names.push(`d${names.length}`);
    return names.slice(0, dims);
  }

  function pickActionMatrix(trajectory) {
    if (!trajectory) return null;
    if (trajectory.action?.length) return trajectory.action;
    if (trajectory.eefKeys?.length) {
      const streams = trajectory.eefKeys.filter((rows) => rows?.length);
      if (!streams.length) return null;
      if (streams.length === 1) return streams[0];
      const len = Math.min(...streams.map((rows) => rows.length));
      const out = [];
      for (let i = 0; i < len; i++) {
        const row = [];
        streams.forEach((rows) => {
          const sample = rows[i];
          if (Array.isArray(sample)) row.push(...sample.map((v) => Number(v) || 0));
          else row.push(Number(sample) || 0);
        });
        out.push(row);
      }
      return out;
    }
    return null;
  }

  function ensureTrajVisibility(dims) {
    if (!Number.isInteger(dims) || dims <= 0) {
      state.trajVisible = [];
      return;
    }
    if (!Array.isArray(state.trajVisible) || state.trajVisible.length !== dims) {
      const keep = Math.min(dims, 6);
      state.trajVisible = Array.from({ length: dims }, (_, index) => index < keep);
    }
  }

  function setAllTrajDims(visible) {
    if (!Array.isArray(state.trajVisible)) return;
    state.trajVisible = state.trajVisible.map(() => Boolean(visible));
    renderTrajDimToggles();
    drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
  }

  function setFirstTrajDims(count) {
    if (!Array.isArray(state.trajVisible)) return;
    state.trajVisible = state.trajVisible.map((_, index) => index < count);
    renderTrajDimToggles();
    drawTrajectory(state.trajectory, Number($('#timeline').value) || 0);
  }

  function renderTrajDimToggles() {
    const host = $('#trajDimToggles');
    if (!host) return;
    const names = state.trajectory?.names || [];
    const dims = names.length || (Array.isArray(state.trajVisible) ? state.trajVisible.length : 0);
    if (!dims) {
      host.innerHTML = '';
      return;
    }
    ensureTrajVisibility(dims);
    host.innerHTML = Array.from({ length: dims }, (_, index) => {
      const checked = state.trajVisible[index] ? 'checked' : '';
      const off = state.trajVisible[index] ? '' : ' off';
      const color = TRAJ_DIM_COLORS[index % TRAJ_DIM_COLORS.length];
      const name = names[index] || `d${index}`;
      return `<label class="traj-dim-toggle${off}" title="${escapeAttr(name)}">
        <input type="checkbox" data-traj-dim="${index}" ${checked}>
        <i class="swatch" style="background:${color}"></i>
        <span class="dim-name">${escapeHtml(name)}</span>
      </label>`;
    }).join('');
    const visible = state.trajVisible.filter(Boolean).length;
    const steps = pickActionMatrix(state.trajectory)?.length || 0;
    if (steps) $('#trajMeta').textContent = t('trajMeta', { steps, dims, visible });
  }

  function sampleMatrixValue(rows, index, dim) {
    const sample = rows[index];
    if (Array.isArray(sample)) return Number(sample[dim]) || 0;
    return Number(sample) || 0;
  }

  // Layered trajectory rendering: the static layer (background, grid, curves,
  // axes) is cached on an offscreen canvas and only the playhead + markers are
  // redrawn per animation frame.
  const trajLayer = { key: '', canvas: null, meta: null };

  function trajLayerKey(trajectory, width, height, duration) {
    const visibleBits = (state.trajVisible || []).map((v) => (v ? 1 : 0)).join('');
    return [
      state.trajectoryEpisode,
      trajectory?.sourceKey || '',
      pickActionMatrix(trajectory)?.length || 0,
      visibleBits,
      state.trajScaleMode,
      width,
      height,
      duration.toFixed(3),
    ].join('|');
  }

  function buildTrajLayer(trajectory, width, height, duration) {
    const offscreen = document.createElement('canvas');
    offscreen.width = width;
    offscreen.height = height;
    const ctx = offscreen.getContext('2d');
    ctx.fillStyle = '#f8fafc';
    ctx.fillRect(0, 0, width, height);

    const matrix = pickActionMatrix(trajectory);
    const rows = downsampleSeries(matrix, 600);
    const dims = Array.isArray(rows[0]) ? rows[0].length : 1;
    const names = (trajectory?.names?.length === dims)
      ? trajectory.names
      : resolveTrajectoryDimNames(dims, trajectory);
    ensureTrajVisibility(dims);
    const visible = [];
    for (let d = 0; d < dims; d++) if (state.trajVisible[d]) visible.push(d);
    const n = rows.length;
    const padL = 48;
    const padR = 14;
    const padT = 14;
    const padB = 28;
    const plotW = Math.max(1, width - padL - padR);
    const plotH = Math.max(1, height - padT - padB);

    // Grid
    ctx.strokeStyle = '#e2e8f0';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = padT + (plotH / 4) * i;
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(width - padR, y);
      ctx.stroke();
    }
    for (let i = 0; i <= 4; i++) {
      const x = padL + (plotW / 4) * i;
      ctx.beginPath();
      ctx.moveTo(x, padT);
      ctx.lineTo(x, height - padB);
      ctx.stroke();
    }

    const meta = { rows, n, dims, names, visible, padL, padR, padT, padB, plotW, plotH, width, height };
    if (!visible.length) {
      ctx.fillStyle = '#94a3b8';
      ctx.font = '12px sans-serif';
      ctx.fillText(t('trajNoneSelected'), padL + 8, padT + 20);
      return { canvas: offscreen, meta };
    }

    const scaleMode = state.trajScaleMode === 'per' ? 'per' : 'shared';
    const stats = Array.from({ length: dims }, () => ({ min: Infinity, max: -Infinity }));
    visible.forEach((d) => {
      for (let i = 0; i < n; i++) {
        const value = sampleMatrixValue(rows, i, d);
        if (value < stats[d].min) stats[d].min = value;
        if (value > stats[d].max) stats[d].max = value;
      }
      if (!Number.isFinite(stats[d].min)) { stats[d].min = 0; stats[d].max = 1; }
      if (stats[d].max - stats[d].min < 1e-9) stats[d].max = stats[d].min + 1;
    });

    let sharedMin = Infinity;
    let sharedMax = -Infinity;
    if (scaleMode === 'shared') {
      visible.forEach((d) => {
        sharedMin = Math.min(sharedMin, stats[d].min);
        sharedMax = Math.max(sharedMax, stats[d].max);
      });
      if (!Number.isFinite(sharedMin)) { sharedMin = 0; sharedMax = 1; }
      if (sharedMax - sharedMin < 1e-9) sharedMax = sharedMin + 1;
    }
    meta.scaleMode = scaleMode;
    meta.stats = stats;
    meta.sharedMin = sharedMin;
    meta.sharedMax = sharedMax;

    const mapX = (t01) => padL + t01 * plotW;
    const mapYFor = (d, value) => {
      const min = scaleMode === 'shared' ? sharedMin : stats[d].min;
      const max = scaleMode === 'shared' ? sharedMax : stats[d].max;
      return padT + (1 - ((value - min) / (max - min))) * plotH;
    };

    // Curves
    visible.forEach((d) => {
      ctx.strokeStyle = TRAJ_DIM_COLORS[d % TRAJ_DIM_COLORS.length];
      ctx.lineWidth = visible.length <= 4 ? 2.4 : 1.8;
      ctx.lineJoin = 'round';
      ctx.lineCap = 'round';
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        const value = sampleMatrixValue(rows, i, d);
        const t01 = n <= 1 ? 0 : i / (n - 1);
        const x = mapX(t01);
        const y = mapYFor(d, value);
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    });

    // Axes
    ctx.fillStyle = '#64748b';
    ctx.font = '11px ui-sans-serif, system-ui, sans-serif';
    ctx.fillText('0s', padL, height - 8);
    ctx.fillText(`${duration.toFixed(1)}s`, width - padR - 34, height - 8);
    if (scaleMode === 'shared') {
      ctx.fillText(sharedMax.toFixed(2), 4, padT + 10);
      ctx.fillText(sharedMin.toFixed(2), 4, height - padB);
    } else {
      ctx.fillText(t('trajAxisNorm'), 4, padT + 10);
    }
    return { canvas: offscreen, meta };
  }

  function drawTrajectory(trajectory, relativeSeconds = 0) {
    const canvas = $('#trajCanvas');
    const readout = $('#trajReadout');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const width = canvas.clientWidth || canvas.width;
    const height = canvas.clientHeight || canvas.height;
    if (canvas.width !== width) canvas.width = width;
    if (canvas.height !== height) canvas.height = height;
    ctx.clearRect(0, 0, width, height);

    const matrix = pickActionMatrix(trajectory);
    if (!matrix?.length) {
      ctx.fillStyle = '#f8fafc';
      ctx.fillRect(0, 0, width, height);
      ctx.fillStyle = '#94a3b8';
      ctx.font = '12px sans-serif';
      ctx.fillText(t('trajEmpty'), 12, height / 2);
      if (readout) readout.textContent = '';
      return;
    }

    const episode = currentEpisode();
    const duration = Math.max(1e-6, episode?.duration || 1);
    const key = trajLayerKey(trajectory, width, height, duration);
    if (trajLayer.key !== key || !trajLayer.canvas) {
      const built = buildTrajLayer(trajectory, width, height, duration);
      trajLayer.key = key;
      trajLayer.canvas = built.canvas;
      trajLayer.meta = built.meta;
    }
    ctx.drawImage(trajLayer.canvas, 0, 0);

    const meta = trajLayer.meta;
    if (!meta.visible.length) {
      if (readout) readout.textContent = t('trajNoneSelected');
      return;
    }

    const progress = Math.max(0, Math.min(1, relativeSeconds / duration));
    const { rows, n, names, padL, padT, padB, plotW, plotH } = meta;
    const mapX = (t01) => padL + t01 * plotW;
    const mapYFor = (d, value) => {
      const min = meta.scaleMode === 'shared' ? meta.sharedMin : meta.stats[d].min;
      const max = meta.scaleMode === 'shared' ? meta.sharedMax : meta.stats[d].max;
      return padT + (1 - ((value - min) / (max - min))) * plotH;
    };

    // Dynamic layer: playhead + value markers only.
    const playX = mapX(progress);
    const cursorIndex = Math.min(n - 1, Math.max(0, Math.round(progress * (n - 1))));
    ctx.strokeStyle = '#0f172a';
    ctx.lineWidth = 1.25;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(playX, padT);
    ctx.lineTo(playX, height - padB);
    ctx.stroke();
    ctx.setLineDash([]);

    const readoutParts = [];
    meta.visible.forEach((d) => {
      const value = sampleMatrixValue(rows, cursorIndex, d);
      const y = mapYFor(d, value);
      const color = TRAJ_DIM_COLORS[d % TRAJ_DIM_COLORS.length];
      ctx.fillStyle = '#fff';
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(playX, y, 3.5, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      const label = names[d] || `d${d}`;
      readoutParts.push(`<span style="color:${color}"><b>${escapeHtml(label)}</b>=${value.toFixed(3)}</span>`);
    });
    if (readout) {
      readout.innerHTML = `${escapeHtml(t('trajAtTime', { time: relativeSeconds.toFixed(2) }))} · ${readoutParts.join(' · ')}`;
    }
  }

  function syncDecisionBar(decision) {
    $$('.decision-bar .decision').forEach((button) => {
      button.classList.toggle('active', button.dataset.state === decision);
    });
  }

  function renderVideosOptimized(episode) {
    cancelPreviewSeek();
    const entries = organizeCameras(episode.videos || {});
    const strip = $('#videoStrip');
    const existingCards = Array.from(strip.querySelectorAll('.video-card'));
    const layoutMatches = existingCards.length === entries.length && existingCards.every((card, index) => (
      card.querySelector('video')?.dataset.cameraKey === entries[index].key
    ));
    let reused = 0;
    let replaced = 0;

    if (!layoutMatches) {
      strip.replaceChildren(...entries.map((entry, index) => createVideoCardOptimized(entry, index, 'auto')));
      replaced = entries.length;
    } else {
      entries.forEach((entry, index) => {
        const currentCard = existingCards[index];
        const currentVideo = currentCard.querySelector('video');
        const key = mediaKey(entry.video, entry.key);
        const sameFile = entry.video?.kind !== 'topic' && entry.video?.kind !== 'frames' && currentVideo.dataset.videoPath === key;
        if (sameFile) {
          currentVideo.dataset.start = String(entry.video.fromTimestamp ?? 0);
          currentVideo.dataset.end = String(entry.video.toTimestamp ?? '');
          reused++;
        } else {
          currentCard.replaceWith(createVideoCardOptimized(entry, index, 'auto'));
          replaced++;
        }
      });
    }

    applyStripLayout(entries);
    resetTimeline(episode);
    seekAll(0);
    if (reused === entries.length) {
      setStatus(t('reusedShards'));
    } else if (reused > 0) {
      setStatus(t('reusedPartial', { reused, replaced }));
    } else {
      setStatus(t('loadingTriple'));
    }
  }

  function mediaKey(video, cameraKey = '') {
    if (video?.kind === 'topic' && video.topic) return `topic:${video.topic}`;
    if (video?.kind === 'frames') return `frames:${cameraKey || video.topic || ''}`;
    return `path:${video?.path || ''}`;
  }

  function videoSource(video, episodeIndex = state.currentEpisode, cameraKey = '') {
    if (video?.kind === 'topic' && video.topic) {
      return authedAssetUrl(`/api/mcap/video?dataset=${encodeURIComponent(state.dataset.path)}&episode=${episodeIndex}&topic=${encodeURIComponent(video.topic)}`);
    }
    if (video?.kind === 'frames') {
      const camera = cameraKey || video.topic || '';
      return authedAssetUrl(`/api/hdf5/video?dataset=${encodeURIComponent(state.dataset.path)}&episode=${episodeIndex}&camera=${encodeURIComponent(camera)}`);
    }
    return authedAssetUrl(`/api/video?dataset=${encodeURIComponent(state.dataset.path)}&relative=${encodeURIComponent(video.path || '')}`);
  }

  function createVideoCardOptimized({key, video, role, label}, index, preloadMode = 'metadata') {
    const source = videoSource(video, state.currentEpisode, key);
    const start = video.fromTimestamp ?? 0;
    const end = video.toTimestamp ?? '';
    const card = document.createElement('figure');
    card.className = `video-card ${role}`;
    card.innerHTML = `<div class="video-label">${label}</div>
      <div class="video-state">${t('loading')}</div>
      <video id="video-${index}" preload="${preloadMode}" playsinline muted data-camera-key="${escapeAttr(key)}" data-video-path="${escapeAttr(mediaKey(video, key))}" data-media-kind="${escapeAttr(video.kind || 'video')}" data-topic="${escapeAttr(video.topic || '')}" data-start="${start}" data-end="${end}" src="${source}"></video>`;
    bindVideoEventsOptimized(card.querySelector('video'));
    return card;
  }

  function bindVideoEventsOptimized(video) {
    if (video.dataset.eventsBound === '1') return;
    video.dataset.eventsBound = '1';
    const card = video.closest('.video-card');
    const stateLabel = card.querySelector('.video-state');
    video.addEventListener('loadeddata', () => {
      card.classList.add('ready');
      stateLabel.textContent = '';
      updateEpisodeReadiness();
    });
    video.addEventListener('seeking', () => {
      setEpisodeReady(false, t('seekingBuffers'));
      card.classList.add('seeking');
      stateLabel.textContent = t('seeking');
    });
    video.addEventListener('seeked', () => {
      card.classList.remove('seeking');
      if (video.readyState >= 2) stateLabel.textContent = '';
      updateEpisodeReadiness();
    });
    video.addEventListener('waiting', () => {
      if (!video.seeking) {
        if (state.playing) pauseAll();
        setEpisodeReady(false, t('pauseWaitingBuffer'));
        card.classList.add('buffering');
        stateLabel.textContent = t('buffering');
      }
    });
    video.addEventListener('playing', () => {
      card.classList.remove('buffering');
      stateLabel.textContent = '';
    });
    video.addEventListener('progress', updateEpisodeReadiness);
    video.addEventListener('canplay', updateEpisodeReadiness);
    video.addEventListener('canplaythrough', updateEpisodeReadiness);
    video.addEventListener('click', togglePlay);
    video.addEventListener('error', () => {
      setEpisodeReady(false, t('videoLoadFailed'));
      card.classList.add('video-error');
      stateLabel.textContent = t('loadFailed');
      if (video.dataset.disposed !== '1') {
        setStatus(t('videoDecodeHint', { name: card.querySelector('.video-label').textContent }), true);
      }
    });
  }

  function resetTimeline(episode) {
    $('#timeline').max = String(episode.duration || 0);
    $('#timeline').value = '0';
    updateTimeLabel(0);
  }

  function applyFilters() {
    const query = $('#search').value.trim().toLowerCase();
    const decision = $('#stateFilter').value;
    const interventionOnly = $('#interventionOnly').checked;
    state.filtered = state.dataset.episodes.filter((episode) => {
      const text = `${episode.episodeIndex} ${episode.platformEpisodeId || ''} ${(episode.tasks || []).join(' ')}`.toLowerCase();
      return (!query || text.includes(query))
        && (decision === 'all' || getState(episode.episodeIndex) === decision)
        && (!interventionOnly || episode.hasIntervention);
    });
    renderEpisodeList();
  }

  function setDecision(decision) {
    if (state.workspaceMode !== 'filter') return;
    if (!Number.isInteger(state.currentEpisode)) return;
    const normalized = normalizeDecision(decision);
    state.states[state.currentEpisode] = normalized;
    saveLocalStates();
    renderSummary();
    renderEpisodeHeader();
    applyFilters();
    flashDecision(normalized);
  }

  let stampTimer = null;
  function flashDecision(decision) {
    const stamp = $('#decisionStamp');
    if (!stamp) return;
    const normalized = normalizeDecision(decision);
    const stampClass = normalized === 'pass' ? 'keep' : normalized === 'quarantine' ? 'exclude' : 'pending';
    const glyph = normalized === 'pass' ? '✓' : normalized === 'quarantine' ? '×' : '?';
    stamp.innerHTML = `<b>${glyph}</b><span>${escapeHtml(stateLabel(normalized))}</span>`;
    stamp.className = `decision-stamp ${stampClass}`;
    // restart the CSS animation even on repeated identical decisions
    void stamp.offsetWidth;
    stamp.classList.add('show');
    const button = document.querySelector(`.decision-bar .decision[data-state="${normalized}"]`);
    if (button) {
      button.classList.remove('flash');
      void button.offsetWidth;
      button.classList.add('flash');
    }
    clearTimeout(stampTimer);
    stampTimer = setTimeout(() => stamp.classList.remove('show'), 720);
  }

  function bulkSet(decision) {
    if (state.workspaceMode !== 'filter') return;
    const normalized = normalizeDecision(decision);
    state.filtered.forEach((episode) => { state.states[episode.episodeIndex] = normalized; });
    saveLocalStates();
    renderSummary();
    renderEpisodeHeader();
    applyFilters();
    showToast(t('bulkMarked', { n: state.filtered.length, label: stateLabel(normalized) }));
  }

  async function saveProgress() {
    const suggested = state.progressPath || `${state.dataset.path}.review.json`;
    const target = prompt(t('saveProgressPrompt'), suggested);
    if (!target) return;
    try {
      const result = await api('/api/progress/save', {
        method: 'POST', body: JSON.stringify({ path: target, dataset: state.dataset.path, states: state.states })
      });
      state.progressPath = result.path;
      showToast(t('progressSaved', { path: result.path }));
    } catch (error) { showToast(error.message, true); }
  }

  async function loadProgress() {
    const target = prompt(t('loadProgressPrompt'), state.progressPath || `${state.dataset.path}.review.json`);
    if (!target) return;
    try {
      const result = await api('/api/progress/load', { method: 'POST', body: JSON.stringify({ path: target }) });
      if (result.dataset && result.dataset !== state.dataset.path && !confirm(t('progressOtherDataset'))) return;
      state.states = normalizeStates(result.states || {});
      state.progressPath = target;
      saveLocalStates();
      renderSummary();
      applyFilters();
      renderEpisodeHeader();
      showToast(t('progressLoaded', { user: result.updatedBy || t('otherUser') }));
    } catch (error) { showToast(error.message, true); }
  }

  function openAutoFilterDialog() {
    $('#autoFilterDialog').classList.remove('hidden');
  }

  function closeAutoFilterDialog() {
    $('#autoFilterDialog').classList.add('hidden');
  }

  function openConvertDialog(pathOrEvent, formatHint) {
    // Allow: openConvertDialog(), openConvertDialog(path), openConvertDialog(path, format)
    let path = null;
    let format = null;
    if (typeof pathOrEvent === 'string') {
      path = pathOrEvent;
      format = formatHint || null;
    } else if (state.dataset) {
      path = state.dataset.path;
      format = state.dataset.format || null;
    } else if (state.currentFolder) {
      path = $('#pathInput')?.value || state.currentFolder;
    }
    if (!path) return showToast(t('needConvertDataset'), true);

    const dialog = $('#convertDialog');
    const sourceEl = $('#convertSource');
    const outputEl = $('#convertOutput');
    const targetEl = $('#convertTarget');
    if (!dialog || !sourceEl || !outputEl || !targetEl) {
      return showToast(t('convertUiBroken'), true);
    }

    state.convertContext = { path, format: format || state.convertContext?.format || null };
    const fmt = state.convertContext.format || 'lerobot_v3';
    const suggested = (fmt === 'hdf5' || fmt === 'mcap')
      ? `${path}.converted`
      : `${path}_converted`;
    sourceEl.textContent = path;
    outputEl.value = suggested;
    const preferred = fmt === 'lerobot_v3' ? 'lerobot_v21' : 'lerobot_v3';
    targetEl.value = preferred;
    // Prefer not converting to the same format when possible.
    if (targetEl.value === fmt) {
      const options = Array.from(targetEl.options).map((item) => item.value);
      const alt = options.find((item) => item !== fmt);
      if (alt) targetEl.value = alt;
    }
    $('#convertProgress')?.classList.add('hidden');
    dialog.classList.remove('hidden');
    loadConvertCapabilities(fmt).catch(() => {});
  }

  let convertCapabilities = new Map();

  async function loadConvertCapabilities(sourceFormat) {
    convertCapabilities = new Map();
    if (!sourceFormat) return renderConvertFidelity();
    try {
      const result = await api(`/api/convert/targets?sourceFormat=${encodeURIComponent(sourceFormat)}`);
      (result.formats || []).forEach((item) => convertCapabilities.set(item.id, item));
    } catch {}
    renderConvertFidelity();
  }

  function renderConvertFidelity() {
    const hint = $('#convertFidelity');
    if (!hint) return;
    const info = convertCapabilities.get($('#convertTarget')?.value);
    if (!info || !info.fidelity) {
      hint.classList.add('hidden');
      hint.textContent = '';
      return;
    }
    const fidelityText = info.fidelity === 'full'
      ? t('fidelityFull')
      : info.fidelity === 'high' ? t('fidelityHigh') : t('fidelityPartial');
    // Notes arrive as stable keys; fall back to the raw string for old servers.
    const notes = (info.notes || []).map((note) => {
      const translated = t(`convertNote_${note}`);
      return translated === `convertNote_${note}` ? note : translated;
    }).join('；');
    hint.textContent = notes ? `${fidelityText} · ${notes}` : fidelityText;
    hint.classList.remove('hidden');
  }

  function closeConvertDialog() {
    $('#convertDialog').classList.add('hidden');
  }

  async function startConvert() {
    const ctx = state.convertContext || (state.dataset ? { path: state.dataset.path, format: state.dataset.format } : null);
    if (!ctx?.path) return showToast(t('needConvertDataset'), true);
    const targetFormat = $('#convertTarget').value;
    const output = $('#convertOutput').value.trim();
    if (!output) return showToast(t('needOutputPath'), true);
    let mapping = {};
    const raw = $('#convertMapping').value.trim();
    if (raw) {
      try { mapping = JSON.parse(raw); }
      catch { return showToast(t('invalidMapping'), true); }
    }
    const progress = $('#convertProgress');
    progress.classList.remove('hidden');
    progress.querySelector('i').style.width = '8%';
    progress.querySelector('span').textContent = t('convertStarted');
    $('#startConvert').disabled = true;
    try {
      const job = await api('/api/convert/start', {
        method: 'POST',
        body: JSON.stringify({
          dataset: ctx.path,
          output,
          targetFormat,
          mapping,
        }),
      });
      showToast(t('convertBgStarted', { id: String(job.jobId || '').slice(0, 8) }));
      progress.querySelector('i').style.width = `${Math.round((Number(job.progress) || 0.05) * 100)}%`;
      progress.querySelector('span').textContent = job.message || t('convertBgStartedShort');
      closeConvertDialog();
      await refreshConvertJobs();
      trackConvertJob(job.jobId);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      $('#startConvert').disabled = false;
    }
  }

  // Shared job poller: re-entrancy guard + exponential backoff on errors.
  function createJobPoller({ refresh, isActive, interval = 1500, maxInterval = 10000 }) {
    let timer = null;
    let running = false;
    let delay = interval;
    const tracked = new Set();
    async function tick() {
      timer = null;
      if (running) return schedule();
      running = true;
      try {
        await refresh();
        delay = interval;
      } catch {
        delay = Math.min(maxInterval, delay * 2);
      } finally {
        running = false;
      }
      if (isActive() || tracked.size) schedule();
    }
    function schedule() {
      if (timer) return;
      timer = setTimeout(tick, delay);
    }
    return {
      tracked,
      track(jobId) {
        if (!jobId) return;
        tracked.add(jobId);
        schedule();
      },
      ensure: schedule,
    };
  }

  const convertJobStatusSeen = new Map();
  const convertPoller = createJobPoller({
    refresh: () => refreshConvertJobs(),
    isActive: () => (state.convertJobs || []).some((job) => job.status === 'queued' || job.status === 'running'),
  });

  function trackConvertJob(jobId) {
    convertPoller.track(jobId);
  }

  async function refreshConvertJobs() {
    const result = await api('/api/convert/jobs?limit=20');
    const jobs = result.jobs || [];
    state.convertJobs = jobs;
    convertPoller.tracked.clear();
    jobs.forEach((job) => {
      const prev = convertJobStatusSeen.get(job.jobId);
      convertJobStatusSeen.set(job.jobId, job.status);
      if (job.status === 'queued' || job.status === 'running') convertPoller.tracked.add(job.jobId);
      if (prev && prev !== job.status) {
        if (job.status === 'completed') {
          const reportPath = job.result?.reportPath || '';
          showToast(t('convertDone', {
            output: job.result?.output || job.output || '',
            report: reportPath ? t('convertReport', { path: reportPath }) : '',
          }));
        } else if (job.status === 'failed') {
          showToast(job.message || t('convertFailed'), true);
        }
      }
    });
    renderConvertJobDock();
    if (convertPoller.tracked.size) convertPoller.ensure();
  }

  function renderConvertJobDock() {
    const dock = $('#convertJobDock');
    const list = $('#convertJobList');
    if (!dock || !list) return;
    const jobs = state.convertJobs || [];
    if (!jobs.length) {
      dock.classList.add('hidden');
      list.innerHTML = '';
      return;
    }
    dock.classList.remove('hidden');
    list.innerHTML = jobs.map((job) => {
      const pct = Math.max(0, Math.min(100, Math.round((Number(job.progress) || 0) * 100)));
      const status = job.status || 'unknown';
      const shortId = String(job.jobId || '').slice(0, 8);
      const src = String(job.dataset || '').split('/').pop() || job.dataset || '';
      const detail = job.total
        ? `${job.current || 0}/${job.total}`
        : '';
      const resultPath = job.result?.output || job.output || '';
      const report = job.result?.reportPath ? ` · ${escapeHtml(t('convertReport', { path: job.result.reportPath }))}` : '';
      const canDismiss = status === 'completed' || status === 'failed' || status === 'cancelled';
      const dismissBtn = canDismiss
        ? `<button type="button" class="convert-job-dismiss" data-dismiss-job="${escapeAttr(job.jobId)}">${escapeHtml(t('dismiss'))}</button>`
        : `<button type="button" class="convert-job-dismiss" data-cancel-job="${escapeAttr(job.jobId)}">${escapeHtml(t('cancel'))}</button>`;
      return `<div class="convert-job-item status-${escapeAttr(status)}">
        <div class="convert-job-top">
          <strong>${escapeHtml(shortId)}</strong>
          <span class="convert-job-status">${escapeHtml(statusLabel(status))}</span>
          ${dismissBtn}
        </div>
        <div class="convert-job-meta">${escapeHtml(src)} → ${escapeHtml(job.targetFormat || '')}${detail ? ` · ${escapeHtml(detail)}` : ''}</div>
        <div class="convert-job-bar"><i style="width:${pct}%"></i></div>
        <div class="convert-job-msg">${escapeHtml(job.message || '')}${status === 'completed' && resultPath ? `<br><span class="muted">${escapeHtml(resultPath)}${report}</span>` : ''}</div>
      </div>`;
    }).join('');
  }

  function statusLabel(status) {
    if (status === 'queued') return t('convertStatusQueued');
    if (status === 'running') return t('convertStatusRunning');
    if (status === 'completed') return t('convertStatusDone');
    if (status === 'failed') return t('convertStatusFailed');
    if (status === 'cancelled') return t('jobStatusCancelled');
    return status;
  }

  function authedAssetUrl(url) {
    if (!url) return '';
    // Cookie-based auth: same-origin asset requests carry the HttpOnly
    // cookie automatically. Only fall back to a token query when the page
    // was served by an old server that still embeds one.
    if (!token) return url;
    const join = url.includes('?') ? '&' : '?';
    return `${url}${join}token=${encodeURIComponent(token)}`;
  }

  async function ensureAugmentOptions() {
    state.augmentOptions = await api('/api/augment/options');
    return state.augmentOptions;
  }

  function syncAugmentFormVisibility() {
    const augType = $('#augmentType')?.value || 'brightness';
    const isColor = augType === 'color';
    const isBrightness = augType === 'brightness';
    $('#augmentColorFields')?.classList.toggle('hidden', !isColor);
    $('#augmentBrightnessFields')?.classList.toggle('hidden', !isBrightness);
    const fixed = $('#augmentColorMode')?.value === 'fixed';
    $('#augmentFixedColorRow')?.classList.toggle('hidden', !fixed);
    const manual = $('#augmentBrightnessMode')?.value === 'manual';
    $('#augmentBrightnessManualRow')?.classList.toggle('hidden', !manual);
    const random = $('#augmentBatchScope')?.value === 'random';
    $('#augmentRandomCountRow')?.classList.toggle('hidden', !random);
  }

  function fillAugmentColorPresets() {
    const select = $('#augmentColorPreset');
    const options = state.augmentOptions;
    if (!select || !options) return;
    const applyMode = $('#augmentApplyMode')?.value || 'object_recolor';
    const palette = applyMode === 'background_replace' ? (options.bgColors || {}) : (options.clothColors || {});
    const previous = select.value;
    select.innerHTML = Object.entries(palette).map(([name, rgb]) => (
      `<option value="${escapeAttr(name)}" data-rgb="${escapeAttr(rgb.join(','))}">${escapeHtml(name)}</option>`
    )).join('');
    if (previous && palette[previous]) select.value = previous;
  }

  async function openAugmentDialog(pathOrEvent, formatHint) {
    let path = null;
    let format = null;
    if (typeof pathOrEvent === 'string') {
      path = pathOrEvent;
      format = formatHint || null;
    } else if (state.dataset) {
      path = state.dataset.path;
      format = state.dataset.format || null;
    }
    if (!path) return showToast(t('needAugmentDataset'), true);

    try {
      await ensureAugmentOptions();
    } catch (error) {
      return showToast(error.message, true);
    }

    state.augmentContext = { path, format: format || state.augmentContext?.format || null };
    const colorCapability = state.augmentOptions?.capabilities?.color;
    const colorOption = $('#augmentType')?.querySelector('option[value="color"]');
    if (colorOption) colorOption.disabled = !colorCapability?.available;
    const availability = $('#augmentAvailability');
    if (availability) {
      availability.textContent = colorCapability?.available
        ? t('augmentColorReady')
        : t('augmentColorUnavailable', { reason: colorCapability?.reason || t('augmentDependencyMissing') });
    }
    state.augmentPreviewOk = false;
    state.lastAugmentPreviewJobId = null;
    $('#augmentSource').textContent = path;
    $('#augmentOutput').value = `${path}_augmented`;
    $('#augmentType').value = 'brightness';
    $('#augmentBrightnessMode').value = 'auto';
    $('#augmentBrightnessGain').value = '1.00';
    $('#augmentBrightnessGamma').value = '1.00';
    $('#augmentColorMode').value = 'random';
    $('#augmentApplyMode').value = 'object_recolor';
    $('#augmentSamPrompts').value = '';
    $('#augmentGpuId').value = '0';
    const ep = Number.isInteger(state.currentEpisode) ? state.currentEpisode : 0;
    $('#augmentPreviewEpisode').value = String(ep);
    $('#augmentBatchScope').value = 'all';
    const totalEps = Array.isArray(state.dataset?.episodes) ? state.dataset.episodes.length : 0;
    $('#augmentRandomCount').value = String(Math.min(10, Math.max(1, totalEps || 10)));
    $('#startAugmentBatch').disabled = true;
    $('#augmentPreviewGrid').innerHTML = '';
    $('#augmentPreviewMeta')?.classList.add('hidden');
    $('#augmentProgress')?.classList.add('hidden');
    fillAugmentColorPresets();
    syncAugmentFormVisibility();
    $('#augmentDialog')?.classList.remove('hidden');
  }

  function closeAugmentDialog() {
    $('#augmentDialog')?.classList.add('hidden');
  }

  function invalidateAugmentPreview() {
    if (!state.augmentPreviewOk) return;
    state.augmentPreviewOk = false;
    state.lastAugmentPreviewJobId = null;
    const button = $('#startAugmentBatch');
    if (button) button.disabled = true;
    showToast(t('augmentPreviewInvalidated'));
  }

  function collectAugmentPayload(mode) {
    const ctx = state.augmentContext || (state.dataset ? { path: state.dataset.path, format: state.dataset.format } : null);
    if (!ctx?.path) throw new Error(t('needAugmentDataset'));
    const augType = $('#augmentType').value;
    const promptsRaw = $('#augmentSamPrompts').value || '';
    if (augType === 'color' && !promptsRaw.trim()) {
      throw new Error(t('needSamPrompts'));
    }
    const colorMode = $('#augmentColorMode').value || 'random';
    let colorName = null;
    let colorRgb = null;
    if (augType === 'color' && colorMode === 'fixed') {
      const custom = ($('#augmentColorCustom').value || '').trim();
      if (custom) {
        const parts = custom.split(/[,\s]+/).map(Number).filter((n) => Number.isFinite(n));
        if (parts.length >= 3) {
          colorRgb = parts.slice(0, 3).map((n) => Math.max(0, Math.min(255, Math.round(n))));
          colorName = 'custom';
        }
      }
      if (!colorRgb) {
        const preset = $('#augmentColorPreset');
        colorName = preset?.value || null;
        const rgbAttr = preset?.selectedOptions?.[0]?.dataset?.rgb;
        if (rgbAttr) colorRgb = rgbAttr.split(',').map(Number);
      }
    }
    const brightnessMode = $('#augmentBrightnessMode')?.value || 'auto';
    let brightnessGain = Number($('#augmentBrightnessGain')?.value);
    let brightnessGamma = Number($('#augmentBrightnessGamma')?.value);
    if (!Number.isFinite(brightnessGain)) brightnessGain = 1.0;
    if (!Number.isFinite(brightnessGamma)) brightnessGamma = 1.0;
    const previewEpisode = Number($('#augmentPreviewEpisode').value);
    const payload = {
      dataset: ctx.path,
      mode,
      augType,
      applyMode: $('#augmentApplyMode').value || 'object_recolor',
      samPrompts: promptsRaw,
      colorMode,
      colorName,
      colorRgb,
      brightnessMode,
      brightnessGain: brightnessMode === 'manual' ? brightnessGain : null,
      brightnessGamma: brightnessMode === 'manual' ? brightnessGamma : null,
      gpuId: Number($('#augmentGpuId').value) || 0,
      previewEpisode: Number.isFinite(previewEpisode) ? previewEpisode : undefined,
      targetFormat: ctx.format === 'lerobot_v21' ? 'lerobot_v21' : 'lerobot_v3',
    };
    if (mode === 'batch') {
      const output = $('#augmentOutput').value.trim();
      if (!output) throw new Error(t('needOutputPath'));
      payload.output = output;
      const scope = resolveAugmentEpisodeScope();
      payload.episodes = scope.episodes;
      payload.sampleCount = scope.sampleCount;
      payload.previewJobId = state.lastAugmentPreviewJobId;
    }
    return payload;
  }

  function resolveAugmentEpisodeScope() {
    const scope = $('#augmentBatchScope')?.value || 'all';
    if (scope === 'current') {
      if (!Number.isInteger(state.currentEpisode)) return { episodes: null, sampleCount: null };
      return { episodes: [state.currentEpisode], sampleCount: null };
    }
    if (scope === 'filtered') {
      return {
        episodes: (state.filtered || []).map((ep) => ep.episodeIndex),
        sampleCount: null,
      };
    }
    if (scope === 'pass') {
      return {
        episodes: Object.entries(state.states || {})
          .filter(([, value]) => value === 'pass')
          .map(([key]) => Number(key))
          .filter((n) => Number.isInteger(n)),
        sampleCount: null,
      };
    }
    if (scope === 'random') {
      const count = Math.floor(Number($('#augmentRandomCount')?.value));
      if (!Number.isFinite(count) || count < 1) {
        throw new Error(t('needRandomCount'));
      }
      const pool = (state.dataset?.episodes || state.filtered || []).map((ep) => ep.episodeIndex);
      if (pool.length) {
        const shuffled = pool.slice();
        for (let i = shuffled.length - 1; i > 0; i -= 1) {
          const j = Math.floor(Math.random() * (i + 1));
          [shuffled[i], shuffled[j]] = [shuffled[j], shuffled[i]];
        }
        return { episodes: shuffled.slice(0, Math.min(count, shuffled.length)), sampleCount: null };
      }
      // Dataset not loaded in UI — let backend sample.
      return { episodes: null, sampleCount: count };
    }
    return { episodes: null, sampleCount: null };
  }

  async function runAugmentPreview() {
    let payload;
    try {
      payload = collectAugmentPayload('preview');
    } catch (error) {
      return showToast(error.message, true);
    }
    const progress = $('#augmentProgress');
    progress?.classList.remove('hidden');
    if (progress) {
      progress.querySelector('i').style.width = '10%';
      progress.querySelector('span').textContent = t('augmentPreparing');
    }
    $('#runAugmentPreview').disabled = true;
    $('#startAugmentBatch').disabled = true;
    state.augmentPreviewOk = false;
    try {
      const job = await api('/api/augment/preview', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      state.lastAugmentPreviewJobId = job.jobId;
      showToast(t('augmentPreviewStarted', { id: String(job.jobId || '').slice(0, 8) }));
      trackAugmentJob(job.jobId);
      await waitAugmentJob(job.jobId, (live) => {
        if (!progress) return;
        progress.querySelector('i').style.width = `${Math.round((Number(live.progress) || 0.1) * 100)}%`;
        progress.querySelector('span').textContent = live.message || t('augmentPreparing');
      });
      const finalJob = await api(`/api/augment/status/${encodeURIComponent(job.jobId)}`);
      if (finalJob.status !== 'completed') {
        throw new Error(finalJob.message || t('augmentPreviewFailed'));
      }
      renderAugmentPreview(finalJob.result || {});
      state.augmentPreviewOk = true;
      $('#startAugmentBatch').disabled = false;
      showToast(t('augmentPreviewReady'));
    } catch (error) {
      showToast(error.message || t('augmentPreviewFailed'), true);
      state.augmentPreviewOk = false;
      $('#startAugmentBatch').disabled = true;
    } finally {
      $('#runAugmentPreview').disabled = false;
    }
  }

  function renderAugmentPreview(result) {
    const grid = $('#augmentPreviewGrid');
    const meta = $('#augmentPreviewMeta');
    if (!grid) return;
    const cameras = result.cameras || [];
    const notes = [];
    if (result.colorName || result.colorRgb) {
      notes.push(t('augmentColorUsed', {
        name: result.colorName || '—',
        rgb: (result.colorRgb || []).join(','),
      }));
    }
    if (result.augType === 'brightness' || result.brightnessGain != null || result.brightnessGamma != null) {
      notes.push(t('augmentBrightnessUsed', {
        mode: result.brightnessMode === 'manual' ? t('augmentBrightnessManual') : t('augmentBrightnessAuto'),
        gain: result.brightnessGain != null ? Number(result.brightnessGain).toFixed(3) : '—',
        gamma: result.brightnessGamma != null ? Number(result.brightnessGamma).toFixed(3) : '—',
      }));
    }
    const brightnessQa = result.meta?.brightness?.cameras?._qa;
    if (brightnessQa?.status === 'warning' && brightnessQa.camera_p50_spread != null) {
      notes.push(t('augmentBrightnessQaWarning', {
        spread: Number(brightnessQa.camera_p50_spread).toFixed(1),
      }));
    }
    if (notes.length) {
      meta?.classList.remove('hidden');
      if (meta) meta.textContent = notes.join(' · ');
    } else {
      meta?.classList.add('hidden');
    }
    if (!cameras.length) {
      grid.innerHTML = `<div class="empty">${escapeHtml(t('augmentPreviewFailed'))}</div>`;
      return;
    }

    const shortName = (name) => {
      const raw = String(name || '');
      const parts = raw.split('.');
      return parts[parts.length - 1] || raw;
    };

    const tabs = cameras.length > 1
      ? `<div class="augment-preview-tabs" role="tablist">
          ${cameras.map((cam, index) => `
            <button type="button" class="augment-preview-tab${index === 0 ? ' active' : ''}" data-preview-tab="${index}" role="tab" aria-selected="${index === 0 ? 'true' : 'false'}">
              ${escapeHtml(shortName(cam.camera))}
            </button>`).join('')}
        </div>`
      : '';

    const panels = cameras.map((cam, camIndex) => {
      const group = `aug-preview-${camIndex}`;
      const hasVideo = Boolean(cam.originalVideoUrl || cam.resultVideoUrl);
      const maskBlock = (cam.maskVideoUrl || cam.maskUrl)
        ? `<div class="augment-preview-mask">
            <div class="augment-preview-label">${escapeHtml(t(cam.maskVideoUrl ? 'augmentMaskVideo' : 'augmentMask'))}</div>
            ${cam.maskVideoUrl
              ? `<video class="augment-preview-video" data-sync-group="${escapeAttr(group)}" controls playsinline muted preload="metadata" poster="${escapeAttr(authedAssetUrl(cam.maskUrl || ''))}" src="${escapeAttr(authedAssetUrl(cam.maskVideoUrl))}"></video>`
              : `<img class="augment-preview-still" src="${escapeAttr(authedAssetUrl(cam.maskUrl))}" alt="mask">`}
          </div>`
        : '';

      const pair = hasVideo
        ? `<div class="augment-preview-pair">
            <figure>
              <div class="augment-preview-label">${escapeHtml(t('augmentOriginalVideo'))}</div>
              <video class="augment-preview-video" data-sync-group="${escapeAttr(group)}" controls playsinline muted preload="metadata" poster="${escapeAttr(authedAssetUrl(cam.originalUrl || ''))}" src="${escapeAttr(authedAssetUrl(cam.originalVideoUrl))}"></video>
            </figure>
            <figure>
              <div class="augment-preview-label">${escapeHtml(t('augmentResultVideo'))}</div>
              <video class="augment-preview-video" data-sync-group="${escapeAttr(group)}" controls playsinline muted preload="metadata" poster="${escapeAttr(authedAssetUrl(cam.resultUrl || ''))}" src="${escapeAttr(authedAssetUrl(cam.resultVideoUrl))}"></video>
            </figure>
          </div>`
        : `<div class="augment-preview-pair">
            <figure>
              <div class="augment-preview-label">${escapeHtml(t('augmentOriginal'))}</div>
              <img class="augment-preview-still" src="${escapeAttr(authedAssetUrl(cam.originalUrl))}" alt="original">
            </figure>
            <figure>
              <div class="augment-preview-label">${escapeHtml(t('augmentResult'))}</div>
              <img class="augment-preview-still" src="${escapeAttr(authedAssetUrl(cam.resultUrl))}" alt="result">
            </figure>
          </div>`;

      return `<section class="augment-preview-panel${camIndex === 0 ? ' active' : ''}" data-preview-panel="${camIndex}">
        <div class="augment-preview-cam-title" title="${escapeAttr(cam.camera || '')}">${escapeHtml(cam.camera || '')}</div>
        ${pair}
        ${maskBlock}
      </section>`;
    }).join('');

    grid.innerHTML = `<div class="augment-preview-shell">${tabs}<div class="augment-preview-panels">${panels}</div></div>`;
    grid.querySelectorAll('[data-preview-tab]').forEach((button) => {
      button.addEventListener('click', () => {
        const index = button.dataset.previewTab;
        grid.querySelectorAll('video').forEach((video) => {
          try { video.pause(); } catch {}
        });
        grid.querySelectorAll('[data-preview-tab]').forEach((item) => {
          const on = item.dataset.previewTab === index;
          item.classList.toggle('active', on);
          item.setAttribute('aria-selected', on ? 'true' : 'false');
        });
        grid.querySelectorAll('[data-preview-panel]').forEach((panel) => {
          panel.classList.toggle('active', panel.dataset.previewPanel === index);
        });
      });
    });
    bindAugmentPreviewVideoSync(grid);
  }

  function bindAugmentPreviewVideoSync(root) {
    const videos = Array.from(root.querySelectorAll('video[data-sync-group]'));
    if (!videos.length) return;
    const groups = new Map();
    videos.forEach((video) => {
      const key = video.dataset.syncGroup;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(video);
    });
    groups.forEach((peers) => {
      peers.forEach((video) => {
        video.addEventListener('play', () => {
          peers.forEach((other) => {
            if (other !== video && other.paused) other.play().catch(() => {});
          });
        });
        video.addEventListener('pause', () => {
          peers.forEach((other) => {
            if (other !== video && !other.paused) other.pause();
          });
        });
        video.addEventListener('seeked', () => {
          peers.forEach((other) => {
            if (other !== video && Math.abs((other.currentTime || 0) - (video.currentTime || 0)) > 0.12) {
              try { other.currentTime = video.currentTime; } catch {}
            }
          });
        });
      });
    });
  }

  async function startAugmentBatch() {
    if (!state.augmentPreviewOk) return showToast(t('needAugmentPreview'), true);
    let payload;
    try {
      payload = collectAugmentPayload('batch');
    } catch (error) {
      return showToast(error.message, true);
    }
    $('#startAugmentBatch').disabled = true;
    try {
      const job = await api('/api/augment/start', {
        method: 'POST',
        body: JSON.stringify(payload),
      });
      showToast(t('augmentBatchStarted', { id: String(job.jobId || '').slice(0, 8) }));
      closeAugmentDialog();
      await refreshAugmentJobs();
      trackAugmentJob(job.jobId);
    } catch (error) {
      showToast(error.message, true);
      $('#startAugmentBatch').disabled = false;
    }
  }

  const augmentJobStatusSeen = new Map();
  const augmentPoller = createJobPoller({
    refresh: () => refreshAugmentJobs(),
    isActive: () => (state.augmentJobs || []).some((job) => job.status === 'queued' || job.status === 'running'),
  });

  function trackAugmentJob(jobId) {
    augmentPoller.track(jobId);
  }

  async function waitAugmentJob(jobId, onUpdate) {
    for (;;) {
      const job = await api(`/api/augment/status/${encodeURIComponent(jobId)}`);
      onUpdate?.(job);
      if (job.status === 'completed' || job.status === 'failed') return job;
      await new Promise((resolve) => setTimeout(resolve, 1200));
    }
  }

  async function refreshAugmentJobs() {
    const result = await api('/api/augment/jobs?limit=20');
    const jobs = result.jobs || [];
    state.augmentJobs = jobs;
    augmentPoller.tracked.clear();
    jobs.forEach((job) => {
      const prev = augmentJobStatusSeen.get(job.jobId);
      augmentJobStatusSeen.set(job.jobId, job.status);
      if (job.status === 'queued' || job.status === 'running') augmentPoller.tracked.add(job.jobId);
      if (prev && prev !== job.status) {
        if (job.status === 'completed' && job.mode !== 'preview') {
          showToast(t('augmentDone', { output: job.result?.output || job.output || '' }));
        } else if (job.status === 'failed' && job.mode !== 'preview') {
          showToast(job.message || t('augmentFailed'), true);
        } else if (job.status === 'cancelled' && job.mode !== 'preview') {
          showToast(job.message || t('augmentCancelled'));
        }
      }
    });
    renderAugmentJobDock();
    if (augmentPoller.tracked.size) augmentPoller.ensure();
  }

  function renderAugmentJobDock() {
    const dock = $('#augmentJobDock');
    const list = $('#augmentJobList');
    if (!dock || !list) return;
    const jobs = (state.augmentJobs || []).filter((job) => job.mode !== 'preview' || job.status === 'running' || job.status === 'queued');
    if (!jobs.length) {
      dock.classList.add('hidden');
      list.innerHTML = '';
      return;
    }
    dock.classList.remove('hidden');
    list.innerHTML = jobs.map((job) => {
      const pct = Math.max(0, Math.min(100, Math.round((Number(job.progress) || 0) * 100)));
      const status = job.status || 'unknown';
      const shortId = String(job.jobId || '').slice(0, 8);
      const src = String(job.dataset || '').split('/').pop() || job.dataset || '';
      const detail = job.total ? `${job.current || 0}/${job.total}` : '';
      const resultPath = job.result?.output || job.output || '';
      const canDismiss = status === 'completed' || status === 'failed' || status === 'cancelled';
      const dismissBtn = canDismiss
        ? `<button type="button" class="convert-job-dismiss" data-dismiss-augment-job="${escapeAttr(job.jobId)}">${escapeHtml(t('dismiss'))}</button>`
        : `<button type="button" class="convert-job-dismiss" data-cancel-augment-job="${escapeAttr(job.jobId)}">${escapeHtml(t('cancel'))}</button>`;
      const kind = job.mode === 'preview' ? 'preview' : (job.augType || 'augment');
      return `<div class="convert-job-item status-${escapeAttr(status)}">
        <div class="convert-job-top">
          <strong>${escapeHtml(shortId)}</strong>
          <span class="convert-job-status">${escapeHtml(statusLabel(status))}</span>
          ${dismissBtn}
        </div>
        <div class="convert-job-meta">${escapeHtml(src)} · ${escapeHtml(kind)}${detail ? ` · ${escapeHtml(detail)}` : ''}</div>
        <div class="convert-job-bar"><i style="width:${pct}%"></i></div>
        <div class="convert-job-msg">${escapeHtml(job.message || '')}${status === 'completed' && resultPath ? `<br><span class="muted">${escapeHtml(resultPath)}</span>` : ''}</div>
      </div>`;
    }).join('');
  }

  function renderLabelPanel() {
    if (!state.dataset || !Number.isInteger(state.currentEpisode)) return;
    if (!$('#labelPanel')) return;
    const episodeLabels = (state.labels || []).filter((item) => Number(item.episode_index) === state.currentEpisode);
    const episodeLevel = episodeLabels.find((item) => item.target === 'episode') || {};
    const intervals = episodeLabels
      .filter((item) => item.target === 'interval')
      .slice()
      .sort((a, b) => Number(a.start_s) - Number(b.start_s) || Number(a.end_s) - Number(b.end_s));
    $('#qualityScore').value = episodeLevel.quality_score != null ? String(episodeLevel.quality_score) : '';
    $('#successFlag').value = episodeLevel.success === true ? 'true' : episodeLevel.success === false ? 'false' : '';
    $('#labelTags').value = (episodeLevel.tags || []).join(', ');
    $('#labelNote').value = episodeLevel.note || '';
    renderBreakpointUi();
    const intervalList = $('#intervalList');
    if (intervalList) {
      intervalList.innerHTML = intervals.map((item) => {
        const start = Number(item.start_s);
        const end = Number(item.end_s);
        const tags = (item.tags || []).map(escapeHtml).join(', ') || '—';
        const note = item.note ? ` · ${escapeHtml(item.note)}` : '';
        return `<div class="label-item interval-item">
          <div class="interval-item-main">
            <strong>${escapeHtml(start.toFixed(2))}–${escapeHtml(end.toFixed(2))}s</strong>
            <span>${tags}${note}</span>
          </div>
          <button type="button" class="interval-delete" data-delete-interval
            data-start="${escapeHtml(String(start))}" data-end="${escapeHtml(String(end))}"
            title="${escapeHtml(t('deleteInterval'))}">${escapeHtml(t('delete'))}</button>
        </div>`;
      }).join('') || `<div class="empty">${escapeHtml(t('noIntervals'))}</div>`;
    }
    const others = episodeLabels.filter((item) => item.target !== 'interval');
    $('#labelList').innerHTML = others.map((item) => {
      const span = item.target === 'frame'
        ? `frame ${item.frame_index ?? '-'}`
        : 'episode';
      return `<div class="label-item"><strong>${escapeHtml(item.target)}</strong> ${escapeHtml(String(span))} · ${(item.tags || []).map(escapeHtml).join(', ')} ${item.quality_score != null ? '· ' + escapeHtml(t('qualityScore')) + ' ' + item.quality_score : ''}</div>`;
    }).join('') || `<div class="empty">${escapeHtml(t('noLabels'))}</div>`;
  }

  function refreshLabelViews() {
    // Label edits also surface in the summary bar, episode list and header.
    try {
      renderSummary();
      renderEpisodeList();
      renderEpisodeHeader();
    } catch (error) {
      console.error(error);
    }
  }

  function currentTimelineSeconds() {
    return Number($('#timeline')?.value) || 0;
  }

  function roundTime(seconds) {
    return Math.round(Number(seconds) * 100) / 100;
  }

  function draftKey(start, end) {
    return `${roundTime(start).toFixed(2)}_${roundTime(end).toFixed(2)}`;
  }

  function episodeDurationSeconds() {
    return Math.max(0, roundTime(currentEpisode()?.duration || 0));
  }

  function isBoundaryBreakpoint(point, duration = episodeDurationSeconds()) {
    const value = roundTime(point);
    return Math.abs(value) < 0.005 || Math.abs(value - duration) < 0.005;
  }

  function allBreakpoints() {
    const duration = episodeDurationSeconds();
    const start = 0;
    const end = Math.max(start, duration);
    const mid = (state.breakpoints || [])
      .map((point) => roundTime(point))
      .filter((point) => point > start + 0.004 && point < end - 0.004);
    const unique = [];
    [start, ...mid.sort((a, b) => a - b), end].forEach((point) => {
      if (!unique.some((existing) => Math.abs(existing - point) < 0.005)) unique.push(point);
    });
    // If duration is 0, still keep a single point; intervals need end > start.
    if (unique.length === 1 && end <= start) unique.push(roundTime(start + 0.01));
    return unique;
  }

  function draftIntervalsFromBreakpoints() {
    const points = allBreakpoints();
    const intervals = [];
    for (let i = 0; i < points.length - 1; i++) {
      if (points[i + 1] > points[i]) intervals.push({ start: points[i], end: points[i + 1] });
    }
    return intervals;
  }

  function captureDraftMetaFromDom() {
    $$('#draftIntervalList [data-draft-start]').forEach((row) => {
      const start = Number(row.dataset.draftStart);
      const end = Number(row.dataset.draftEnd);
      const tags = row.querySelector('[data-draft-tags]')?.value || '';
      const note = row.querySelector('[data-draft-note]')?.value || '';
      state.draftIntervalMeta[draftKey(start, end)] = { tags, note };
    });
  }

  function renderBreakpointUi() {
    captureDraftMetaFromDom();
    const chips = $('#breakpointChips');
    const draftList = $('#draftIntervalList');
    const hint = $('#intervalMarkHint');
    const duration = episodeDurationSeconds();
    // Keep only user mid-points in state; boundaries are implicit.
    state.breakpoints = (state.breakpoints || [])
      .map((point) => roundTime(point))
      .filter((point) => !isBoundaryBreakpoint(point, duration))
      .filter((point) => point > 0.004 && point < duration - 0.004)
      .sort((a, b) => a - b);

    const points = allBreakpoints();
    if (chips) {
      chips.innerHTML = points.map((point, index) => {
        const boundary = isBoundaryBreakpoint(point, duration);
        const label = boundary
          ? (Math.abs(point) < 0.005 ? t('breakpointStart') : t('breakpointEnd'))
          : `#${index}`;
        const remove = boundary
          ? ''
          : `<button type="button" data-remove-bp="${escapeAttr(String(point))}" title="${escapeHtml(t('removeBreakpoint'))}">×</button>`;
        return `<span class="breakpoint-chip${boundary ? ' boundary' : ''}">
          ${escapeHtml(label)} ${escapeHtml(point.toFixed(2))}s
          ${remove}
        </span>`;
      }).join('');
    }

    const drafts = draftIntervalsFromBreakpoints();
    if (draftList) {
      if (!drafts.length) {
        draftList.innerHTML = `<div class="draft-interval-empty">${escapeHtml(t('intervalMarkIdle'))}</div>`;
      } else {
        draftList.innerHTML = drafts.map((item, index) => {
          const meta = state.draftIntervalMeta[draftKey(item.start, item.end)] || { tags: '', note: '' };
          return `<div class="draft-interval-item" data-draft-start="${escapeAttr(String(item.start))}" data-draft-end="${escapeAttr(String(item.end))}">
            <strong>${escapeHtml(t('draftIntervalTitle', { n: index + 1, start: item.start.toFixed(2), end: item.end.toFixed(2) }))}</strong>
            <label>${escapeHtml(t('intervalTagsLabel'))}
              <input data-draft-tags type="text" value="${escapeAttr(meta.tags)}" placeholder="adaptation_frame, collision">
            </label>
            <label>${escapeHtml(t('intervalNoteLabel'))}
              <input data-draft-note type="text" value="${escapeAttr(meta.note)}" placeholder="${escapeAttr(t('notePlaceholder'))}">
            </label>
          </div>`;
        }).join('');
      }
    }

    if (hint) {
      hint.textContent = t('intervalMarkReady', {
        n: drafts.length,
        points: points.length,
        mid: state.breakpoints.length,
      });
    }
  }

  function addIntervalBreakpoint() {
    if (state.workspaceMode !== 'annotate') return;
    if (!state.dataset || !Number.isInteger(state.currentEpisode)) return;
    const duration = episodeDurationSeconds();
    const now = roundTime(currentTimelineSeconds());
    if (isBoundaryBreakpoint(now, duration)) {
      showToast(t('breakpointBoundaryFixed', { time: now.toFixed(2) }), true);
      return;
    }
    if (now <= 0.004 || now >= duration - 0.004) {
      showToast(t('breakpointBoundaryFixed', { time: now.toFixed(2) }), true);
      return;
    }
    const all = allBreakpoints();
    if (all.some((point) => Math.abs(point - now) < 0.005)) {
      showToast(t('breakpointExists', { time: now.toFixed(2) }), true);
      return;
    }
    state.breakpoints.push(now);
    state.breakpoints.sort((a, b) => a - b);
    renderBreakpointUi();
    const total = allBreakpoints().length;
    setStatus(t('breakpointAdded', { time: now.toFixed(2), n: total }));
    showToast(t('breakpointAdded', { time: now.toFixed(2), n: total }));
  }

  function removeIntervalBreakpoint(value) {
    const target = roundTime(value);
    if (isBoundaryBreakpoint(target)) return;
    state.breakpoints = (state.breakpoints || []).filter((point) => Math.abs(point - target) >= 0.005);
    renderBreakpointUi();
  }

  function undoIntervalBreakpoint() {
    if (!state.breakpoints.length) return;
    state.breakpoints.pop();
    renderBreakpointUi();
  }

  function clearIntervalBreakpoints() {
    state.breakpoints = [];
    state.draftIntervalMeta = {};
    renderBreakpointUi();
  }

  async function saveEpisodeLabel() {
    if (!state.dataset || !Number.isInteger(state.currentEpisode)) return;
    const tags = $('#labelTags').value.split(',').map((item) => item.trim()).filter(Boolean);
    const quality = $('#qualityScore').value;
    const successRaw = $('#successFlag').value;
    const label = {
      target: 'episode',
      episode_index: state.currentEpisode,
      tags,
      quality_score: quality ? Number(quality) : null,
      success: successRaw === '' ? null : successRaw === 'true',
      note: $('#labelNote').value.trim(),
    };
    try {
      const result = await api('/api/labels/upsert', {
        method: 'POST',
        body: JSON.stringify({ dataset: state.dataset.path, path: state.labelsPath || null, label }),
      });
      state.labels = result.labels || [];
      state.labelsPath = result.path;
      renderLabelPanel();
      refreshLabelViews();
      showToast(t('episodeLabelSaved'));
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function saveTaggedIntervals() {
    if (!state.dataset || !Number.isInteger(state.currentEpisode)) return;
    captureDraftMetaFromDom();
    const drafts = draftIntervalsFromBreakpoints();
    const tagged = drafts.map((item) => {
      const meta = state.draftIntervalMeta[draftKey(item.start, item.end)] || { tags: '', note: '' };
      const tags = String(meta.tags || '').split(',').map((part) => part.trim()).filter(Boolean);
      return { ...item, tags, note: String(meta.note || '').trim() };
    }).filter((item) => item.tags.length);
    if (!tagged.length) return showToast(t('needTaggedIntervals'), true);

    try {
      let labels = state.labels || [];
      let labelsPath = state.labelsPath || null;
      for (const item of tagged) {
        const result = await api('/api/labels/upsert', {
          method: 'POST',
          body: JSON.stringify({
            dataset: state.dataset.path,
            path: labelsPath,
            label: {
              target: 'interval',
              episode_index: state.currentEpisode,
              start_s: item.start,
              end_s: item.end,
              tags: item.tags,
              note: item.note,
            },
          }),
        });
        labels = result.labels || labels;
        labelsPath = result.path || labelsPath;
      }
      state.labels = labels;
      state.labelsPath = labelsPath;
      // Keep untagged drafts; clear only saved ones from draft meta / collapse breakpoints optionally.
      tagged.forEach((item) => {
        delete state.draftIntervalMeta[draftKey(item.start, item.end)];
      });
      // After save, clear breakpoints so next round starts clean.
      state.breakpoints = [];
      state.draftIntervalMeta = {};
      renderLabelPanel();
      refreshLabelViews();
      showToast(t('taggedIntervalsSaved', { n: tagged.length }));
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function deleteIntervalLabel(start, end) {
    if (!state.dataset || !Number.isInteger(state.currentEpisode)) return;
    if (!Number.isFinite(start) || !Number.isFinite(end)) return;
    const label = {
      target: 'interval',
      episode_index: state.currentEpisode,
      start_s: start,
      end_s: end,
      tags: [],
    };
    try {
      const result = await api('/api/labels/delete', {
        method: 'POST',
        body: JSON.stringify({ dataset: state.dataset.path, path: state.labelsPath || null, label }),
      });
      state.labels = result.labels || [];
      state.labelsPath = result.path;
      renderLabelPanel();
      refreshLabelViews();
      showToast(t('intervalDeleted'));
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function createDataset() {
    const episodes = Object.entries(state.states)
      .filter(([, value]) => normalizeDecision(value) === 'pass')
      .map(([key]) => Number(key))
      .sort((a, b) => a - b);
    if (!episodes.length) return showToast(t('needPassEpisode'), true);
    const output = prompt(t('exportPathPrompt'), `${state.dataset.path}_filtered`);
    if (!output) return;
    const doConvert = confirm(t('exportConvertConfirm'));
    let targetFormat = null;
    if (doConvert) {
      targetFormat = prompt(t('targetFormatPrompt'), state.dataset.format === 'lerobot_v3' ? 'lerobot_v21' : 'lerobot_v3');
      if (!targetFormat) return;
    }
    const copy = confirm(t('mediaModeConfirm'));
    if (!confirm(t('exportConfirm', { n: episodes.length }))) return;
    setBusy(true, t('exporting'), `${episodes.length} episodes`);
    try {
      const job = await api('/api/create', {
        method: 'POST',
        body: JSON.stringify({
          dataset: state.dataset.path,
          output,
          episodes,
          mediaMode: copy ? 'copy' : 'hardlink',
          targetFormat,
          copyLabels: true,
        }),
      });
      // Export now runs as a detached background job; progress shows in the
      // convert job dock instead of blocking this request.
      showToast(t('convertBgStarted', { id: String(job.jobId || '').slice(0, 8) }));
      await refreshConvertJobs();
      trackConvertJob(job.jobId);
    } catch (error) { showToast(error.message, true); }
    finally { setBusy(false); }
  }

  function setEpisodeReady(ready, message = '') {
    state.episodeReady = ready;
    const button = $('#playPause');
    if (button) {
      button.disabled = !ready;
      if (!state.playing) button.textContent = ready ? '▶' : '…';
    }
    if (message) setStatus(message, false);
  }

  function updateEpisodeReadiness() {
    const videos = getVideos();
    if (!videos.length) {
      // Trajectory-only / no camera: still allow transport & autoplay for timeline scrubbing.
      if (!state.episodeReady) {
        setEpisodeReady(true, t('readyToPlay'));
        if (state.pendingAutoPlay) {
          state.pendingAutoPlay = false;
        }
      }
      return;
    }
    const ready = videos.every((video) => {
      if (video.error || video.seeking || video.readyState < 3) return false;
      const remaining = Math.max(0, Number(video.dataset.end || video.duration || 0) - video.currentTime);
      const required = Math.min(state.requiredBufferSeconds, remaining);
      return bufferedSecondsAhead(video) + 0.05 >= required;
    });
    if (ready && !state.episodeReady) {
      videos.forEach((video) => video.closest('.video-card')?.classList.remove('buffering'));
      setEpisodeReady(true, t('readyToPlay'));
      if (state.pendingAutoPlay) {
        state.pendingAutoPlay = false;
        playAll();
      }
    } else if (!ready && state.episodeReady) {
      setEpisodeReady(false, t('waitingBuffer'));
    }
  }

  function bufferedSecondsAhead(video) {
    const time = video.currentTime;
    for (let index = 0; index < video.buffered.length; index++) {
      if (video.buffered.start(index) <= time + 0.05 && video.buffered.end(index) >= time) {
        return Math.max(0, video.buffered.end(index) - time);
      }
    }
    return 0;
  }

  function togglePlay() { state.playing ? pauseAll() : playAll(); }

  async function playAll() {
    const videos = getVideos();
    if (!videos.length) return;
    if (!state.episodeReady) {
      setStatus(t('notReadyPlay'), true);
      return;
    }
    const episode = currentEpisode();
    if (Number($('#timeline').value) >= episode.duration - 0.02) seekAll(0);
    state.playing = true;
    $('#playPause').textContent = 'Ⅱ';
    videos.forEach((video) => { video.playbackRate = 1; });
    const results = await Promise.allSettled(videos.map((video) => video.play()));
    if (results.some((result) => result.status === 'rejected')) {
      setStatus(t('partialPlayFail'), true);
    }
    syncLoop();
  }

  function pauseAll() {
    state.playing = false;
    if (state.animationFrame) cancelAnimationFrame(state.animationFrame);
    getVideos().forEach((video) => {
      video.pause();
      video.playbackRate = 1;
    });
    const button = $('#playPause');
    if (button) button.textContent = '▶';
  }

  function beginTimelineSeek() {
    if (state.timelineDragging) return;
    state.timelineDragging = true;
    state.resumeAfterSeek = state.playing;
    state.pendingSeekSeconds = Number($('#timeline').value);
    pauseAll();
  }

  function endTimelineSeek() {
    if (!state.timelineDragging) return;
    cancelPreviewSeek();
    state.timelineDragging = false;
    seekAll(Number($('#timeline').value));
    if (state.resumeAfterSeek) {
      state.resumeAfterSeek = false;
      playAll();
    }
  }

  function onTimelineInput(event) {
    const episode = currentEpisode();
    if (!episode) return;
    const value = Math.max(0, Math.min(Number(event.target.value) || 0, episode.duration));
    $('#timeline').value = String(value);
    updateTimeLabel(value);
    if (!state.timelineDragging) {
      seekAll(value);
      return;
    }
    state.pendingSeekSeconds = value;
    if (state.previewSeekTimer !== null) return;
    state.previewSeekTimer = setTimeout(() => {
      state.previewSeekTimer = null;
      seekAll(state.pendingSeekSeconds, true);
    }, 150);
  }

  function cancelPreviewSeek() {
    if (state.previewSeekTimer !== null) {
      clearTimeout(state.previewSeekTimer);
      state.previewSeekTimer = null;
    }
  }

  function seekAll(relativeSeconds, fast = false) {
    const episode = currentEpisode();
    if (!episode) return;
    setEpisodeReady(false, fast ? '' : t('seekingSync'));
    const safeRelative = Math.max(0, Math.min(Number(relativeSeconds) || 0, episode.duration));
    const sequence = ++state.seekSequence;
    getVideos().forEach((video) => seekVideo(video, safeRelative, sequence, fast));
    $('#timeline').value = String(safeRelative);
    updateTimeLabel(safeRelative);
    drawTrajectory(state.trajectory, safeRelative);
    setTimeout(updateEpisodeReadiness, 0);
  }

  function seekVideo(video, relativeSeconds, sequence, fast = false) {
    const apply = () => {
      if (sequence !== state.seekSequence) return;
      const start = Number(video.dataset.start || 0);
      const declaredEnd = Number(video.dataset.end || start);
      const mediaEnd = Number.isFinite(video.duration) ? video.duration : declaredEnd;
      const target = Math.max(0, Math.min(start + relativeSeconds, declaredEnd, mediaEnd));
      if (Math.abs(video.currentTime - target) > 0.002) {
        if (fast && typeof video.fastSeek === 'function') video.fastSeek(target);
        else video.currentTime = target;
      }
      video.playbackRate = 1;
    };
    if (video.readyState === 0) {
      video.addEventListener('loadedmetadata', apply, { once: true });
    } else {
      apply();
    }
  }

  function syncLoop() {
    if (!state.playing) return;
    const videos = getVideos();
    const master = videos.find((video) => video.readyState >= 2) || videos[0];
    const episode = currentEpisode();
    if (!master || !episode) return;
    const relative = Math.max(0, master.currentTime - Number(master.dataset.start || 0));
    if (relative >= episode.duration) { seekAll(episode.duration); pauseAll(); return; }
    videos.forEach((video) => {
      if (video === master) return;
      const target = Number(video.dataset.start || 0) + relative;
      if (video.readyState < 2 || video.seeking) return;
      const drift = target - video.currentTime;
      if (Math.abs(drift) > 0.75) {
        video.currentTime = target;
        video.playbackRate = 1;
      } else if (Math.abs(drift) > 0.12) {
        video.playbackRate = drift > 0 ? 1.04 : 0.96;
      } else {
        video.playbackRate = 1;
      }
    });
    $('#timeline').value = String(relative);
    updateTimeLabel(relative);
    drawTrajectory(state.trajectory, relative);
    state.animationFrame = requestAnimationFrame(syncLoop);
  }

  function moveEpisode(delta) {
    const pool = state.filtered.length ? state.filtered : state.dataset.episodes;
    const position = pool.findIndex((episode) => episode.episodeIndex === state.currentEpisode);
    const next = pool[Math.max(0, Math.min(pool.length - 1, position + delta))];
    if (next) selectEpisode(next.episodeIndex);
  }

  function onKeyDown(event) {
    if ($('#review').classList.contains('hidden')) return;
    if (['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) return;
    const key = event.key.toLowerCase();
    const mode = state.workspaceMode;
    if (mode === 'filter' && key === 'k') setDecision('pass');
    else if (mode === 'filter' && key === 'x') setDecision('quarantine');
    else if (mode === 'filter' && key === 'u') setDecision('review');
    else if (key === 'arrowleft') moveEpisode(-1);
    else if (key === 'arrowright') moveEpisode(1);
    else if (key === ' ') { event.preventDefault(); togglePlay(); }
    else if (mode === 'annotate' && key === 'b') { event.preventDefault(); addIntervalBreakpoint(); }
  }

  function currentEpisode() { return state.episodeByIndex.get(state.currentEpisode); }
  function getVideos() { return $$('#videoStrip video'); }
  function normalizeDecision(decision) {
    const value = String(decision || '').toLowerCase();
    if (value === 'keep') return 'pass';
    if (value === 'exclude') return 'quarantine';
    if (value === 'pending') return 'review';
    if (value === 'pass' || value === 'review' || value === 'quarantine') return value;
    return 'review';
  }

  function normalizeStates(states) {
    const out = {};
    Object.entries(states || {}).forEach(([key, value]) => { out[key] = normalizeDecision(value); });
    return out;
  }

  function getState(index) { return normalizeDecision(state.states[index] || 'review'); }
  function saveLocalStates() { localStorage.setItem(`embody-review:${state.dataset.path}`, JSON.stringify(state.states)); }
  function loadLocalStates(path) { try { return JSON.parse(localStorage.getItem(`embody-review:${path}`) || '{}'); } catch { return {}; } }
  function updateTimeLabel(value) { $('#timeLabel').textContent = `${formatTime(value)} / ${formatTime(currentEpisode()?.duration || 0)}`; }
  function setStatus(message, error = false) { $('#status').textContent = message; $('#status').classList.toggle('error', error); }

  function setBusy(visible, title = '', text = '') {
    $('#busy').classList.toggle('hidden', !visible);
    if (visible) { $('#busyTitle').textContent = title; $('#busyText').textContent = text; }
  }

  let toastTimer;
  function showToast(message, error = false) {
    clearTimeout(toastTimer);
    const toast = $('#toast');
    if (!toast) return;
    toast.textContent = message || '';
    toast.classList.remove('hidden', 'error', 'success');
    toast.classList.add('toast', error ? 'error' : 'success');
    toastTimer = setTimeout(() => toast.classList.add('hidden'), 3200);
  }

  function organizeCameras(videos) {
    const remaining = Object.entries(videos || {}).map(([key, video]) => ({key, video}));
    const take = (predicate) => {
      const index = remaining.findIndex(({key}) => predicate(key.toLowerCase()));
      return index >= 0 ? remaining.splice(index, 1)[0] : undefined;
    };
    const left = take((key) => /(^|[._-])(left|robot0)([._-]|$)/.test(key));
    const right = take((key) => /(^|[._-])(right|robot1)([._-]|$)/.test(key));
    let main = take((key) => /(^|[._-])(head|base|main|front)([._-]|$)/.test(key));
    // Dual-arm only: keep left/right side-by-side, do not invent a head camera.
    if (!main && !(left && right)) main = remaining.shift();
    const leftoverLeft = left || remaining.shift();
    const leftoverRight = right || remaining.shift();
    // If exactly two unnamed leftovers somehow remain as head+one, remap to dual-arm.
    if (main && leftoverLeft && !leftoverRight && remaining.length === 0 && left && right) {
      // already dual
    }
    if (!main && leftoverLeft && leftoverRight) {
      return [
        {...leftoverLeft, role: 'camera-left', label: t('camLeft')},
        {...leftoverRight, role: 'camera-right', label: t('camRight')},
      ];
    }
    return [
      main && {...main, role: 'camera-head', label: t('camHead')},
      leftoverLeft && {...leftoverLeft, role: 'camera-left', label: t('camLeft')},
      leftoverRight && {...leftoverRight, role: 'camera-right', label: t('camRight')}
    ].filter(Boolean);
  }

  function applyStripLayout(entries) {
    const strip = $('#videoStrip');
    if (!strip) return;
    const roles = new Set(entries.map((entry) => entry.role));
    const hasHead = roles.has('camera-head');
    const dualArm = entries.length === 2 && !hasHead && roles.has('camera-left') && roles.has('camera-right');
    const single = entries.length === 1;
    strip.classList.toggle('dual-arm', dualArm);
    strip.classList.toggle('single-cam', single);
    const handle = $('#splitColumns');
    if (handle) handle.style.display = dualArm || single || !hasHead ? 'none' : '';
  }
  function stateLabel(value) { const n = normalizeDecision(value); return n === 'pass' ? t('pass') : n === 'quarantine' ? t('quarantine') : t('review'); }
  // ---------------------------------------------------------------------------
  // Layout: collapsible header + draggable splitters (sizes persisted locally)
  // ---------------------------------------------------------------------------
  const layout = (() => {
    try { return JSON.parse(localStorage.getItem('lerobot-layout') || '{}'); } catch { return {}; }
  })();
  let layoutSaveTimer = null;
  function saveLayout() {
    clearTimeout(layoutSaveTimer);
    layoutSaveTimer = setTimeout(() => {
      try { localStorage.setItem('lerobot-layout', JSON.stringify(layout)); } catch {}
    }, 200);
  }

  let headerCollapseTimer = null;
  function setHeaderCollapsed(collapsed) {
    document.body.classList.toggle('header-collapsed', collapsed);
    const toggle = $('#headerToggle');
    if (toggle) { toggle.title = collapsed ? t('expandHeader') : t('collapseHeader'); toggle.setAttribute('aria-label', toggle.title); }
  }
  function onEnterReview() {
    if (layout.headerMode === 'open') return;
    clearTimeout(headerCollapseTimer);
    headerCollapseTimer = setTimeout(() => {
      if (!$('#review').classList.contains('hidden')) setHeaderCollapsed(true);
    }, 1600);
  }
  function onLeaveReview() {
    clearTimeout(headerCollapseTimer);
    if (layout.headerMode !== 'collapsed') setHeaderCollapsed(false);
  }

  function clamp(value, low, high) { return Math.min(high, Math.max(low, value)); }

  function bindSplitter(element, options) {
    if (!element) return;
    const horizontal = options.axis === 'x';
    let startPointer = 0;
    let startValue = 0;
    element.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      startPointer = horizontal ? event.clientX : event.clientY;
      startValue = options.get();
      element.classList.add('dragging');
      document.body.classList.add('resizing', horizontal ? 'resizing-col' : 'resizing-row');
      element.setPointerCapture(event.pointerId);
    });
    element.addEventListener('pointermove', (event) => {
      if (!element.classList.contains('dragging')) return;
      const delta = (horizontal ? event.clientX : event.clientY) - startPointer;
      options.set(clamp(startValue + delta, options.min(), options.max()));
    });
    const stop = (event) => {
      if (!element.classList.contains('dragging')) return;
      element.classList.remove('dragging');
      document.body.classList.remove('resizing', 'resizing-col', 'resizing-row');
      if (event.pointerId !== undefined) {
        try { element.releasePointerCapture(event.pointerId); } catch {}
      }
      options.done?.();
    };
    element.addEventListener('pointerup', stop);
    element.addEventListener('pointercancel', stop);
    element.addEventListener('dblclick', () => { options.reset(); options.done?.(); });
    element.addEventListener('keydown', (event) => {
      const step = event.shiftKey ? 48 : 16;
      const keys = horizontal ? ['arrowleft', 'arrowright'] : ['arrowup', 'arrowdown'];
      const key = event.key.toLowerCase();
      if (!keys.includes(key)) return;
      event.preventDefault();
      const direction = key === keys[0] ? -1 : 1;
      options.set(clamp(options.get() + direction * step, options.min(), options.max()));
      options.done?.();
    });
  }

  function initLayout() {
    const workspace = document.querySelector('.review-workspace');
    const strip = $('#videoStrip');
    const stripWrap = document.querySelector('.strip-wrap');
    const columnHandle = $('#splitColumns');
    const viewer = document.querySelector('.viewer');

    // Header toggle -------------------------------------------------------
    $('#headerToggle')?.addEventListener('click', () => {
      clearTimeout(headerCollapseTimer);
      const collapsed = !document.body.classList.contains('header-collapsed');
      setHeaderCollapsed(collapsed);
      layout.headerMode = collapsed ? 'collapsed' : 'open';
      saveLayout();
    });
    if (layout.headerMode === 'collapsed') setHeaderCollapsed(true);

    // Sidebar width -------------------------------------------------------
    const sidebarWidth = () => document.querySelector('.episode-panel').getBoundingClientRect().width;
    if (layout.sidebarW) workspace.style.setProperty('--sidebar-w', `${layout.sidebarW}px`);
    bindSplitter($('#splitSidebar'), {
      axis: 'x',
      get: sidebarWidth,
      min: () => 180,
      max: () => Math.max(220, workspace.getBoundingClientRect().width - 480),
      set: (value) => workspace.style.setProperty('--sidebar-w', `${Math.round(value)}px`),
      reset: () => { workspace.style.removeProperty('--sidebar-w'); layout.sidebarW = null; },
      done: () => {
        const inline = workspace.style.getPropertyValue('--sidebar-w');
        layout.sidebarW = inline ? Math.round(sidebarWidth()) : null;
        saveLayout();
      }
    });

    // Video strip height --------------------------------------------------
    const stripHeight = () => strip.getBoundingClientRect().height;
    if (layout.stripH) strip.style.setProperty('--strip-h', `${layout.stripH}px`);
    bindSplitter($('#splitStrip'), {
      axis: 'y',
      get: stripHeight,
      min: () => 220,
      max: () => Math.max(260, viewer.getBoundingClientRect().height - 150),
      set: (value) => strip.style.setProperty('--strip-h', `${Math.round(value)}px`),
      reset: () => { strip.style.removeProperty('--strip-h'); layout.stripH = null; },
      done: () => {
        layout.stripH = strip.style.getPropertyValue('--strip-h') ? Math.round(stripHeight()) : null;
        saveLayout();
        syncColumnHandle();
      }
    });

    // Head camera column width (overlay handle; survives strip re-renders) -
    const headCard = () => strip.querySelector('.video-card.camera-head');
    const headWidth = () => headCard()?.getBoundingClientRect().width ?? 0;
    function syncColumnHandle() {
      const card = headCard();
      const dual = strip.classList.contains('dual-arm') || strip.classList.contains('single-cam');
      if (!card || dual) { columnHandle.style.display = 'none'; return; }
      columnHandle.style.display = '';
      const stripBox = strip.getBoundingClientRect();
      const cardBox = card.getBoundingClientRect();
      columnHandle.style.left = `${cardBox.right - stripBox.left + 2 - strip.scrollLeft}px`;
    }
    if (layout.headW) strip.style.setProperty('--head-w', `${layout.headW}px`);
    bindSplitter(columnHandle, {
      axis: 'x',
      get: headWidth,
      min: () => 340,
      max: () => Math.max(360, strip.getBoundingClientRect().width - 260),
      set: (value) => {
        strip.style.setProperty('--head-w', `${Math.round(value)}px`);
        syncColumnHandle();
      },
      reset: () => { strip.style.removeProperty('--head-w'); layout.headW = null; syncColumnHandle(); },
      done: () => {
        layout.headW = strip.style.getPropertyValue('--head-w') ? Math.round(headWidth()) : null;
        saveLayout();
        syncColumnHandle();
      }
    });
    new MutationObserver(() => syncColumnHandle()).observe(strip, { childList: true });
    if (typeof ResizeObserver !== 'undefined') {
      new ResizeObserver(() => syncColumnHandle()).observe(strip);
      new ResizeObserver(() => syncColumnHandle()).observe(stripWrap);
    }
    window.addEventListener('resize', syncColumnHandle);
    syncColumnHandle();
  }

  initLayout();
  initialize();
})();
