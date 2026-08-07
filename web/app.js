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
    dataset: null, episodeByIndex: new Map(), states: {}, quarantineReasons: {}, quarantineReasonOptions: [], filtered: [], currentEpisode: null,
    playing: false, animationFrame: null, progressPath: '',
    timelineDragging: false, resumeAfterSeek: false, seekSequence: 0,
    previewSeekTimer: null, pendingSeekSeconds: 0,
    autoFilterPollTimer: null, autoFilterJobId: null,
    qcJobs: [],
    qcSummary: null,
    qcPage: null,
    qcOffset: 0,
    qcSelectedEpisode: null,
    qcOverviewByEpisode: new Map(),
    qcDetailByEpisode: new Map(),
    qcActiveFindingId: null,
    qcShowRejected: false,
    qcPlaybackEnd: null,
    qcPlaybackToken: 0,
    qcStartedJobId: null,
    qcCurrentJob: null,
    qcLoadedScanId: null,
    episodeReady: false, requiredBufferSeconds: 2,
    pendingAutoPlay: false,
    trajectory: null, trajectoryEpisode: null,
    convertContext: null, // { path, format } for standalone convert
    convertJobs: [],
    mergeContext: null, // { sources, preflight, browserPath, outputTouched }
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
    primaryLayer: 'data',
    exportContext: null,
    deploymentSessionId: null,
    deploymentRuntimeKind: null,
    deploymentTimer: null,
    deploymentSnapshot: null,
    deploymentRobotConnected: false,
    deploymentConfigCache: { robot: new Map(), model: new Map() },
    deploymentTrajectoryGroup: 'left',
    deploymentTrajectorySource: 'state',
    deploymentLogRefreshTick: 0,
    deploymentLogLoading: false,
    deploymentLogRows: [],
    deploymentLogClearMarkers: {},
    deploymentSchedulerDirty: false,
    deploymentSchedulerApplying: false,
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
    bindDeployment();
    bindLanguage();
    initTooltips();
    window.EmbodyI18n?.applyStaticI18n();
    try {
      const health = await api('/api/health');
      state.quarantineReasonOptions = normalizeQuarantineReasonOptions(health.review?.quarantineReasons);
      renderQuarantineReasonOptions();
      if (health.review?.configError) {
        showToast(t('reviewConfigError', { msg: health.review.configError }), true);
      }
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
    const preferredLayer = location.hash === '#deploy'
      ? 'deploy'
      : (localStorage.getItem('embodit-primary-layer') || 'data');
    if (preferredLayer === 'deploy') await openDeploymentWorkspace();
    else activatePrimaryLayer('data');
  }

  function bindDeployment() {
    $('#openDataWorkspace')?.addEventListener('click', openDataWorkspace);
    $('#openDeployment')?.addEventListener('click', openDeploymentWorkspace);
    $('#deploymentRobotSelect')?.addEventListener('change', () => loadSelectedDeploymentConfigs().catch((error) => setDeploymentResult({ error: error.message }, t('deployFailed'), true)));
    $('#deploymentModelSelect')?.addEventListener('change', () => loadSelectedDeploymentConfigs().catch((error) => setDeploymentResult({ error: error.message }, t('deployFailed'), true)));
    $('#deploymentTaskPrompt')?.addEventListener('input', onDeploymentPromptInput);
    $('#checkDeploymentRobot')?.addEventListener('click', checkDeploymentRobotConnection);
    $('#prepareDeploymentModel')?.addEventListener('click', prepareDeploymentModel);
    $('#disconnectDeploymentRobot')?.addEventListener('click', disconnectDeploymentRobot);
    $('#closeDeploymentModel')?.addEventListener('click', closeDeploymentModel);
    $('#applyDeploymentPrompt')?.addEventListener('click', applyDeploymentPrompt);
    $('#applyDeploymentScheduler')?.addEventListener('click', applyDeploymentScheduler);
    ['#deploymentInferenceMode', '#deploymentActionSteps', '#deploymentRequestAfterSteps'].forEach((selector) => {
      $(selector)?.addEventListener('input', onDeploymentSchedulerInput);
    });
    $('#stopDeployment')?.addEventListener('click', stopDeploymentSession);
    $('#startLiveDeployment')?.addEventListener('click', startLiveDeployment);
    $('#stopLiveDeployment')?.addEventListener('click', stopLiveDeployment);
    $('#recordDeploymentPose')?.addEventListener('click', recordDeploymentPose);
    $('#moveDeploymentPose')?.addEventListener('click', moveDeploymentPose);
    $('#deleteDeploymentPose')?.addEventListener('click', deleteDeploymentPose);
    $('#deploymentPoseSelect')?.addEventListener('change', syncDeploymentPoseButtons);
    $('#deploymentLogSource')?.addEventListener('change', () => refreshDeploymentExecutionLog(true));
    $('#refreshDeploymentLog')?.addEventListener('click', () => refreshDeploymentExecutionLog(true));
    $('#deploymentLogFilter')?.addEventListener('input', () => renderDeploymentLogRows(state.deploymentLogRows));
    $('#clearDeploymentLogView')?.addEventListener('click', () => {
      const source = $('#deploymentLogSource')?.value || 'orchestration';
      if (source === 'orchestration') {
        const events = state.deploymentSnapshot?.events || [];
        state.deploymentLogClearMarkers[source] = events.at(-1)?.timeNs || Date.now() * 1e6;
      } else {
        state.deploymentLogClearMarkers[source] = state.deploymentLogRows.at(-1)?.message || null;
      }
      state.deploymentLogRows = [];
      const output = $('#deploymentExecutionLog');
      if (output) output.innerHTML = `<div class="deployment-log-empty">${escapeHtml(t('deployLogsCleared'))}</div>`;
    });
    $('#deploymentTrajectoryGroups')?.addEventListener('click', onDeploymentTrajectoryGroup);
    $('#deploymentTrajectorySource')?.addEventListener('change', renderCurrentDeploymentTrajectory);
    $('#deploymentTrajectoryWindow')?.addEventListener('change', renderCurrentDeploymentTrajectory);
    $('#deploymentTrajectoryScale')?.addEventListener('change', renderCurrentDeploymentTrajectory);
    window.addEventListener('resize', () => {
      if (state.primaryLayer !== 'deploy') return;
      const action = state.deploymentSnapshot?.modelIo?.output?.action || {};
      const chunk = Array.isArray(action.chunk) ? action.chunk.filter(Array.isArray) : [];
      renderDeploymentActionTrajectory(action, chunk, state.deploymentSnapshot?.trajectoryHistory);
    });
  }

  function activatePrimaryLayer(layer) {
    const next = layer === 'deploy' ? 'deploy' : 'data';
    state.primaryLayer = next;
    $('#dataWorkspace')?.classList.toggle('hidden', next !== 'data');
    $('#deploymentWorkspace')?.classList.toggle('hidden', next !== 'deploy');
    $$('.layer-tab[data-layer]').forEach((button) => {
      const active = button.dataset.layer === next;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    document.body.classList.toggle('layer-data', next === 'data');
    document.body.classList.toggle('layer-deploy', next === 'deploy');
    try { localStorage.setItem('embodit-primary-layer', next); } catch {}
    const hash = next === 'deploy' ? '#deploy' : '';
    if (location.hash !== hash) history.replaceState(null, '', `${location.pathname}${location.search}${hash}`);
  }

  function openDataWorkspace() {
    stopDeploymentPolling();
    activatePrimaryLayer('data');
    requestAnimationFrame(() => {
      try { drawTrajectory(state.trajectory, Number($('#timeline')?.value) || 0); } catch {}
    });
  }

  async function openDeploymentWorkspace() {
    try { pauseAll(); } catch {}
    activatePrimaryLayer('deploy');
    try { await refreshDeploymentOptions(); } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployLoadConfigsFailed'), true);
    }
    try {
      const orchestrations = await api('/api/deploy/orchestrations');
      const activeRecipe = (orchestrations.orchestrations || []).find((item) => !['stopped', 'fault'].includes(item.state));
      if (activeRecipe) {
        state.deploymentSessionId = activeRecipe.orchestrationId;
        state.deploymentRuntimeKind = 'recipe';
        renderDeploymentSnapshot(activeRecipe);
        startDeploymentPolling();
        return;
      }
    } catch {}
  }

  function deploymentConfig(kind) {
    const selector = kind === 'robot' ? '#deploymentRobotConfig' : '#deploymentModelConfig';
    let config;
    try {
      config = JSON.parse($(selector).value);
    } catch (error) {
      throw new Error(t('deployConfigJsonInvalid', { kind: t(kind === 'robot' ? 'deployKindRobot' : 'deployKindModel'), msg: error.message }));
    }
    if (!config || typeof config !== 'object' || Array.isArray(config)) {
      throw new Error(t('deployProfileObject'));
    }
    if (config.kind !== kind || config.version !== 1) {
      throw new Error(t('deployConfigVersionInvalid', { kind: t(kind === 'robot' ? 'deployKindRobot' : 'deployKindModel'), value: kind }));
    }
    return config;
  }

  function writeDeploymentConfig(kind, config) {
    const selector = kind === 'robot' ? '#deploymentRobotConfig' : '#deploymentModelConfig';
    const text = JSON.stringify(config, null, 2);
    $(selector).value = text;
  }

  function syncDeploymentCompositionIdentity() {
    const robot = deploymentConfig('robot');
    const model = deploymentConfig('model');
    $('#deploymentId').value = `${robot.config_id}--${model.config_id}`;
    $('#deploymentName').value = `${robot.name} + ${model.name}`;
  }

  async function composeDeploymentRecipe() {
    const robot = JSON.parse(JSON.stringify(deploymentConfig('robot')));
    const taskPrompt = $('#deploymentTaskPrompt')?.value.trim() || '';
    if (taskPrompt && robot.robot?.client) {
      robot.robot.client.config = { ...(robot.robot.client.config || {}), task_prompt: taskPrompt };
    }
    applySchedulerFormToRobot(robot);
    const result = await api('/api/deploy/compose', {
      method: 'POST',
      body: JSON.stringify({
        robot,
        model: deploymentConfig('model'),
        deployment_id: $('#deploymentId').value.trim() || null,
        name: $('#deploymentName').value.trim() || null,
      }),
    });
    $('#deploymentRecipe').value = JSON.stringify(result.recipe, null, 2);
    renderDeploymentHosts(result.recipe);
    return result.recipe;
  }

  async function refreshDeploymentComposition(showResult = false) {
    try {
      const recipe = await composeDeploymentRecipe();
      if (showResult) setDeploymentResult(recipe, t('deployRecipeComposed'));
      return recipe;
    } catch (error) {
      $('#deploymentRecipe').value = '';
      if (showResult) setDeploymentResult({ error: error.message }, t('deployFailed'), true);
      throw error;
    }
  }

  async function refreshDeploymentOptions() {
    const [robotResult, modelResult] = await Promise.all([
      api('/api/deploy/configs/robot'),
      api('/api/deploy/configs/model'),
    ]);
    const metadata = {
      robot: (robotResult.configs || []).filter((item) => item.valid),
      model: (modelResult.configs || []).filter((item) => item.valid),
    };
    if (!metadata.robot.length || !metadata.model.length) {
      const examples = await api('/api/deploy/examples/component-configs');
      for (const kind of ['robot', 'model']) {
        if (metadata[kind].length) continue;
        const config = examples[kind];
        const id = `__example_${kind}__`;
        state.deploymentConfigCache[kind].set(id, config);
        metadata[kind] = [{ configId: id, name: `${config.name} (${t('deployExample')})`, valid: true }];
      }
    }
    for (const kind of ['robot', 'model']) {
      const select = $(kind === 'robot' ? '#deploymentRobotSelect' : '#deploymentModelSelect');
      const previous = select.value;
      select.innerHTML = metadata[kind].map((item) =>
        `<option value="${escapeAttr(item.configId)}">${escapeHtml(item.name)} · ${escapeHtml(item.configId.replace(/^__example_|__$/g, ''))}${item.source === 'project' ? ` · ${escapeHtml(t('deployProjectConfig'))}` : ''}</option>`
      ).join('');
      select.value = metadata[kind].some((item) => item.configId === previous) ? previous : metadata[kind][0].configId;
    }
    await loadSelectedDeploymentConfigs();
  }

  async function selectedDeploymentConfig(kind) {
    const select = $(kind === 'robot' ? '#deploymentRobotSelect' : '#deploymentModelSelect');
    const id = select?.value;
    if (!id) throw new Error(t('deployNoConfig', { kind: t(kind === 'robot' ? 'deployKindRobot' : 'deployKindModel') }));
    if (state.deploymentConfigCache[kind].has(id)) return state.deploymentConfigCache[kind].get(id);
    const result = await api(`/api/deploy/configs/${kind}/${encodeURIComponent(id)}`);
    state.deploymentConfigCache[kind].set(id, result.config);
    return result.config;
  }

  async function loadSelectedDeploymentConfigs() {
    if (state.deploymentSessionId) return;
    const [robot, model] = await Promise.all([
      selectedDeploymentConfig('robot'),
      selectedDeploymentConfig('model'),
    ]);
    writeDeploymentConfig('robot', robot);
    writeDeploymentConfig('model', model);
    configureDeploymentPrompts(robot);
    configureDeploymentScheduler(robot, model);
    syncDeploymentCompositionIdentity();
    await refreshDeploymentComposition(false);
    setDeploymentComponentStatus('#deploymentRobotStatus', 'idle', t('deployConnectionUnchecked'));
    setDeploymentComponentStatus('#deploymentModelStatus', 'idle', t('deployNotStarted'));
    state.deploymentRobotConnected = false;
    syncDeploymentButtons(false);
  }

  function configureDeploymentPrompts(robot) {
    const config = robot?.robot?.client?.config || {};
    const prompts = Array.from(new Set([
      ...(Array.isArray(config.task_prompts) ? config.task_prompts : []),
      config.default_prompt,
    ].filter((value) => typeof value === 'string' && value.trim()).map((value) => value.trim())));
    const list = $('#deploymentTaskPromptOptions');
    if (list) list.innerHTML = prompts.map((prompt) => `<option value="${escapeAttr(prompt)}"></option>`).join('');
    const input = $('#deploymentTaskPrompt');
    if (input && (!input.value.trim() || !state.deploymentSessionId)) input.value = config.default_prompt || prompts[0] || '';
    const hint = $('#deploymentPromptHint');
    if (hint) hint.textContent = prompts.length
      ? t('deployPromptCount', { n: prompts.length })
      : t('deployPromptCustom');
  }

  function configureDeploymentScheduler(robot, model = null) {
    const config = robot?.robot?.client?.config || {};
    const control = config.control || {};
    const robotHorizon = Number(config.action?.horizon) || 1;
    const horizon = Number(model?.model?.action_horizon) || robotHorizon;
    const mode = control.inference_mode || 'synchronous';
    const requestAfter = control.asynchronous?.request_after_steps ?? 'auto';
    const configuredActionSteps = Number(control.action_steps) || robotHorizon;
    const actionSteps = horizon !== robotHorizon && configuredActionSteps === robotHorizon
      ? horizon
      : Math.min(horizon, configuredActionSteps);
    $('#deploymentInferenceMode').value = mode;
    $('#deploymentActionSteps').value = String(actionSteps);
    $('#deploymentActionSteps').max = String(horizon);
    $('#deploymentRequestAfterSteps').value = String(requestAfter);
    state.deploymentSchedulerDirty = false;
    state.deploymentSchedulerApplying = false;
    syncDeploymentSchedulerControls();
    renderDeploymentSchedulerHint(t('deploySchedulerBase', { horizon, rate: Number(control.rate_hz || 10) }));
  }

  function syncDeploymentSchedulerControls() {
    const asynchronous = $('#deploymentInferenceMode')?.value === 'asynchronous';
    $('#deploymentPrefetchField')?.classList.toggle('hidden', !asynchronous);
    if ($('#deploymentRequestAfterSteps')) $('#deploymentRequestAfterSteps').disabled = !asynchronous;
  }

  function deploymentSchedulerForm() {
    const horizon = Number($('#deploymentActionSteps')?.max) || 1;
    const actionSteps = Number($('#deploymentActionSteps')?.value);
    const mode = $('#deploymentInferenceMode')?.value || 'synchronous';
    const raw = ($('#deploymentRequestAfterSteps')?.value || 'auto').trim().toLowerCase();
    const requestAfterSteps = raw === 'auto' ? 'auto' : Number(raw);
    let error = '';
    if (!Number.isInteger(actionSteps) || actionSteps < 1 || actionSteps > horizon) {
      error = t('deployActionStepsRange', { horizon });
    } else if (mode === 'asynchronous' && requestAfterSteps !== 'auto'
      && (!Number.isInteger(requestAfterSteps) || requestAfterSteps < 1 || requestAfterSteps >= actionSteps)) {
      error = t('deployPrefetchInvalid');
    }
    return { mode, horizon, actionSteps, requestAfterSteps, error };
  }

  function renderDeploymentSchedulerHint(prefix = '') {
    const form = deploymentSchedulerForm();
    const mode = t(form.mode === 'asynchronous' ? 'deployModeAsync' : 'deployModeSync');
    const prefetch = form.mode === 'asynchronous'
      ? ` · ${form.requestAfterSteps === 'auto' ? t('deployPrefetchAuto') : t('deployPrefetchAt', { step: form.requestAfterSteps })}`
      : '';
    const status = state.deploymentSchedulerDirty ? ` · ${t('deployPendingApply')}` : '';
    $('#deploymentSchedulerHint').textContent = form.error
      || `${prefix ? `${prefix} · ` : ''}${mode} · ${t('deployUseSteps', { used: form.actionSteps, total: form.horizon })}${prefetch}${status}`;
  }

  function onDeploymentSchedulerInput() {
    state.deploymentSchedulerDirty = true;
    syncDeploymentSchedulerControls();
    renderDeploymentSchedulerHint();
    syncDeploymentButtons(Boolean(state.deploymentSessionId), state.deploymentSnapshot);
  }

  function applySchedulerFormToRobot(robot) {
    const client = robot?.robot?.client;
    if (!client) return;
    const config = client.config = { ...(client.config || {}) };
    const horizon = Number($('#deploymentActionSteps')?.max) || Number(config.action?.horizon) || 1;
    const actionSteps = Math.max(1, Math.min(horizon, Number($('#deploymentActionSteps')?.value) || horizon));
    const mode = $('#deploymentInferenceMode')?.value || 'synchronous';
    const raw = ($('#deploymentRequestAfterSteps')?.value || 'auto').trim().toLowerCase();
    const requestAfterSteps = raw === 'auto' ? 'auto' : Number(raw);
    config.action = { ...(config.action || {}), horizon };
    config.control = {
      ...(config.control || {}),
      inference_mode: mode,
      action_steps: actionSteps,
      asynchronous: {
        ...(config.control?.asynchronous || {}),
        request_after_steps: requestAfterSteps,
        latency_margin_ms: Number(config.control?.asynchronous?.latency_margin_ms ?? 30),
      },
    };
  }

  function onDeploymentPromptInput() {
    const button = $('#applyDeploymentPrompt');
    if (button) button.disabled = !state.deploymentSessionId || !$('#deploymentTaskPrompt')?.value.trim();
    if (!state.deploymentSessionId) refreshDeploymentComposition(false).catch(() => {});
  }

  function setDeploymentResult(payload, hint = '', error = false) {
    const output = $('#deploymentResult');
    if (output) output.textContent = typeof payload === 'string' ? payload : JSON.stringify(payload, null, 2);
    const hintElement = $('#deploymentResultHint');
    if (hintElement) {
      hintElement.textContent = hint || (error ? t('deployFailed') : t('deployReady'));
      hintElement.classList.toggle('error', error);
    }
  }

  function setDeploymentState(value) {
    const element = $('#deploymentState');
    if (!element) return;
    element.textContent = value || 'disconnected';
    element.className = `deployment-state ${value || 'disconnected'}`;
  }

  function setDeploymentComponentStatus(selector, status, text) {
    const element = $(selector);
    if (!element) return;
    element.className = `deployment-component-status ${status}`;
    element.innerHTML = `<i></i>${escapeHtml(text)}`;
  }

  function syncDeploymentButtons(active, snapshot = null) {
    const modelReady = snapshot?.state === 'model_ready' && snapshot?.components?.model?.active;
    const paused = snapshot?.state === 'dry_run' && snapshot?.components?.model?.active;
    const running = snapshot?.state === 'running';
    const changing = snapshot?.state === 'starting';
    const robotLinked = Boolean(snapshot?.components?.tunnel?.active || snapshot?.components?.ros?.active || snapshot?.components?.client?.active);
    const modelActive = Boolean(snapshot?.components?.model?.active);
    $('#prepareDeploymentModel').disabled = active;
    $('#checkDeploymentRobot').disabled = changing || robotLinked || Boolean(state.deploymentSessionId && !modelReady);
    $('#disconnectDeploymentRobot').disabled = changing || !robotLinked;
    $('#closeDeploymentModel').disabled = changing || !modelActive;
    $('#stopDeployment').disabled = !active;
    $('#stopDeployment').hidden = !active;
    $('#startLiveDeployment').disabled = !active || (!modelReady && !paused) || changing;
    $('#startLiveDeployment').textContent = t(paused ? 'deployResumeEvaluation' : 'deployStartEvaluation');
    $('#stopLiveDeployment').disabled = !active || !running;
    $('#deploymentRobotSelect').disabled = active;
    $('#deploymentModelSelect').disabled = active;
    $('#deploymentTaskPrompt').disabled = changing;
    $('#applyDeploymentPrompt').disabled = !active || changing || !$('#deploymentTaskPrompt')?.value.trim();
    const scheduler = deploymentSchedulerForm();
    const schedulerContextReady = !state.deploymentSessionId || (active && modelActive);
    $('#applyDeploymentScheduler').disabled = changing || state.deploymentSchedulerApplying
      || !schedulerContextReady || !state.deploymentSchedulerDirty || Boolean(scheduler.error);
    $('#applyDeploymentScheduler').textContent = t(state.deploymentSchedulerApplying ? 'deployApplying' : 'deployApply');
    const canRecord = active && ['dry_run', 'running'].includes(snapshot?.state)
      && Array.isArray(snapshot?.modelIo?.input?.state?.values);
    $('#recordDeploymentPose').disabled = !canRecord;
    syncDeploymentPoseButtons();
  }

  async function validateDeploymentRecipe() {
    try {
      const robot = deploymentConfig('robot');
      const model = deploymentConfig('model');
      await Promise.all([robot, model].map((config) => api('/api/deploy/configs/validate', {
        method: 'POST', body: JSON.stringify({ config })
      })));
      const recipe = await composeDeploymentRecipe();
      const result = await api('/api/deploy/recipes/validate', {
        method: 'POST', body: JSON.stringify({ recipe })
      });
      setDeploymentResult(result, t('deployValid'));
      renderDeploymentHosts(result.recipe);
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployFailed'), true);
    }
  }

  async function doctorDeployment() {
    setBusy(true, t('deployDoctorRunning'), t('deployDoctorWait'));
    try {
      const recipe = await composeDeploymentRecipe();
      const result = await api('/api/deploy/doctor', {
        method: 'POST', body: JSON.stringify({ recipe })
      });
      setDeploymentResult(result, result.ok ? t('deployDoctorPassed') : t('deployDoctorFailed'), !result.ok);
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployFailed'), true);
    } finally {
      setBusy(false);
    }
  }

  async function checkDeploymentRobotConnection() {
    const button = $('#checkDeploymentRobot');
    button.disabled = true;
    setDeploymentComponentStatus('#deploymentRobotStatus', 'pending', t('deployConnecting'));
    try {
      const snapshot = state.deploymentSnapshot;
      if (state.deploymentSessionId && snapshot?.state === 'model_ready' && snapshot?.components?.model?.active) {
        const prompt = $('#deploymentTaskPrompt')?.value.trim();
        if (!prompt) throw new Error(t('deployPromptRequired'));
        const connected = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/start-dry-run`, {
          method: 'POST', body: JSON.stringify({ taskPrompt: prompt })
        });
        renderDeploymentSnapshot(connected);
        setDeploymentResult(connected, t('deployOpeningReadOnlyLink'));
        startDeploymentPolling();
        return true;
      }
      const result = await api('/api/deploy/robot-connection', {
        method: 'POST', body: JSON.stringify({ config: deploymentConfig('robot') })
      });
      state.deploymentRobotConnected = Boolean(result.connected);
      setDeploymentComponentStatus('#deploymentRobotStatus', 'ready', result.hostname ? `${t('deployConnected')} · ${result.hostname}` : t('deployConnected'));
      return true;
    } catch (error) {
      state.deploymentRobotConnected = false;
      setDeploymentComponentStatus('#deploymentRobotStatus', 'error', t('deployConnectionFailed'));
      setDeploymentResult({ error: error.message }, t('deployRobotConnectionFailed'), true);
      return false;
    } finally {
      syncDeploymentButtons(Boolean(state.deploymentSessionId), state.deploymentSnapshot);
    }
  }

  async function prepareDeploymentModel() {
    setBusy(true, t('deployPreparingModel'), t('deployPreparingModelHint'));
    try {
      if (!state.deploymentRobotConnected && !await checkDeploymentRobotConnection()) return;
      const recipe = await composeDeploymentRecipe();
      const snapshot = await api('/api/deploy/orchestrations/prepare-model', {
        method: 'POST', body: JSON.stringify({ recipe, mode: 'dry_run' })
      });
      state.deploymentSessionId = snapshot.orchestrationId;
      state.deploymentRuntimeKind = 'recipe';
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deployCheckingRobotStartingModel'));
      startDeploymentPolling();
    } catch (error) {
      state.deploymentSessionId = null;
      state.deploymentRuntimeKind = null;
      state.deploymentSnapshot = null;
      state.deploymentRobotConnected = false;
      syncDeploymentButtons(false);
      setDeploymentComponentStatus('#deploymentRobotStatus', 'error', t('deployConnectionFailed'));
      setDeploymentComponentStatus('#deploymentModelStatus', 'error', t('deployStartFailed'));
      setDeploymentResult({ error: error.message }, t('deployFailed'), true);
    } finally {
      setBusy(false);
    }
  }

  async function disconnectDeploymentRobot() {
    if (!state.deploymentSessionId) return;
    setBusy(true, t('deployDisconnectingRobot'), t('deployDisconnectingRobotHint'));
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/disconnect-robot`, { method: 'POST' });
      state.deploymentRobotConnected = false;
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deployRobotDisconnected'));
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployDisconnectFailed'), true);
    } finally { setBusy(false); }
  }

  async function closeDeploymentModel() {
    if (!state.deploymentSessionId) return;
    setBusy(true, t('deployClosingModel'), t('deployClosingModelHint'));
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/close-model`, { method: 'POST' });
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deployModelClosed'));
      stopDeploymentPolling();
      state.deploymentSessionId = null;
      state.deploymentRuntimeKind = null;
      state.deploymentRobotConnected = false;
      syncDeploymentButtons(false);
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployCloseModelFailed'), true);
    } finally { setBusy(false); }
  }

  function startDeploymentPolling() {
    stopDeploymentPolling();
    state.deploymentTimer = window.setInterval(refreshDeploymentSession, 500);
  }

  function stopDeploymentPolling() {
    if (state.deploymentTimer) window.clearInterval(state.deploymentTimer);
    state.deploymentTimer = null;
  }

  async function refreshDeploymentSession() {
    if (!state.deploymentSessionId) return;
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}`);
      renderDeploymentSnapshot(snapshot);
      const anyManagedActive = Object.values(snapshot.components || {}).some((component) => component?.active);
      if (['stopped', 'disconnected'].includes(snapshot.state) || (snapshot.state === 'fault' && !anyManagedActive)) {
        stopDeploymentPolling();
        setDeploymentResult(snapshot, snapshot.lastError || t('deployStateMessage', { state: snapshot.state }), snapshot.state === 'fault');
        state.deploymentSessionId = null;
        state.deploymentRuntimeKind = null;
        syncDeploymentButtons(false);
      } else if (snapshot.state === 'fault') {
        stopDeploymentPolling();
        setDeploymentResult(snapshot, snapshot.lastError || t('deployFaultHint'), true);
      }
    } catch (error) {
      stopDeploymentPolling();
      setDeploymentResult({ error: error.message }, t('deployFailed'), true);
    }
  }

  async function stopDeploymentSession() {
    stopDeploymentPolling();
    if (!state.deploymentSessionId) return;
    try {
      const result = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/stop`, { method: 'POST' });
      setDeploymentState(result.state);
      setDeploymentResult(result, t('deployStopped'));
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployFailed'), true);
    } finally {
      state.deploymentSessionId = null;
      state.deploymentRuntimeKind = null;
      state.deploymentSnapshot = null;
      state.deploymentRobotConnected = false;
      syncDeploymentButtons(false);
      setDeploymentComponentStatus('#deploymentRobotStatus', 'idle', t('deployConnectionUnchecked'));
      setDeploymentComponentStatus('#deploymentModelStatus', 'idle', t('deployNotStarted'));
    }
  }

  async function startLiveDeployment() {
    if (!state.deploymentSessionId) return;
    const taskPrompt = $('#deploymentTaskPrompt')?.value.trim() || '';
    if (!taskPrompt) {
      setDeploymentResult({ error: t('deployPromptChoose') }, t('deployPromptMissing'), true);
      $('#deploymentTaskPrompt')?.focus();
      return;
    }
    try {
      const base = '/api/deploy/orchestrations';
      const snapshot = await api(`${base}/${encodeURIComponent(state.deploymentSessionId)}/start-evaluation`, {
        method: 'POST', body: JSON.stringify({ taskPrompt })
      });
      renderDeploymentSnapshot(snapshot);
      startDeploymentPolling();
      setDeploymentResult(snapshot, t(snapshot.state === 'starting' ? 'deployStartingEvaluation' : 'deployEvaluationRunning'));
    } catch (error) { setDeploymentResult({ error: error.message }, t('deployFailed'), true); }
  }

  async function stopLiveDeployment() {
    if (!state.deploymentSessionId || state.deploymentSnapshot?.state !== 'running') return;
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/stop-evaluation`, {
        method: 'POST'
      });
      renderDeploymentSnapshot(snapshot);
      startDeploymentPolling();
      setDeploymentResult(snapshot, t('deployEvaluationPaused'));
    } catch (error) { setDeploymentResult({ error: error.message }, t('deployFailed'), true); }
  }

  async function applyDeploymentPrompt() {
    if (!state.deploymentSessionId) return;
    const taskPrompt = $('#deploymentTaskPrompt')?.value.trim() || '';
    if (!taskPrompt) return;
    const button = $('#applyDeploymentPrompt');
    button.disabled = true;
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/prompt`, {
        method: 'POST', body: JSON.stringify({ taskPrompt })
      });
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deployPromptChanged', { prompt: taskPrompt }));
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deployPromptChangeFailed'), true);
    } finally {
      syncDeploymentButtons(true, state.deploymentSnapshot);
    }
  }

  async function applyDeploymentScheduler() {
    const form = deploymentSchedulerForm();
    if (form.error) {
      setDeploymentResult({ error: form.error }, t('deploySchedulerInvalid'), true);
      return;
    }
    state.deploymentSchedulerApplying = true;
    syncDeploymentButtons(Boolean(state.deploymentSessionId), state.deploymentSnapshot);
    try {
      if (!state.deploymentSessionId) {
        await refreshDeploymentComposition(false);
        state.deploymentSchedulerDirty = false;
        renderDeploymentSchedulerHint();
        setDeploymentResult({ scheduler: form }, t('deploySchedulerDeferred'));
        return;
      }
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/scheduler`, {
        method: 'POST',
        body: JSON.stringify({
          mode: form.mode,
          actionSteps: form.actionSteps,
          requestAfterSteps: form.requestAfterSteps,
          latencyMarginMs: 30,
        }),
      });
      state.deploymentSchedulerDirty = false;
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deploySchedulerChanged', { mode: t(form.mode === 'asynchronous' ? 'deployModeAsync' : 'deployModeSync') }));
    } catch (error) {
      setDeploymentResult({ error: error.message }, t('deploySchedulerChangeFailed'), true);
    } finally {
      state.deploymentSchedulerApplying = false;
      syncDeploymentButtons(Boolean(state.deploymentSessionId), state.deploymentSnapshot);
    }
  }

  async function recordDeploymentPose() {
    if (!state.deploymentSessionId) return;
    const suggested = `${t('deployPose')} ${new Date().toLocaleTimeString(window.EmbodyI18n?.getLang?.() === 'en' ? 'en-US' : 'zh-CN', { hour12: false })}`;
    const name = $('#deploymentPoseName')?.value.trim() || suggested;
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/poses`, {
        method: 'POST', body: JSON.stringify({ name })
      });
      renderDeploymentSnapshot(snapshot);
      const poses = snapshot.recordedPoses || [];
      if (poses.length) $('#deploymentPoseSelect').value = poses[poses.length - 1].poseId;
      if ($('#deploymentPoseName')) $('#deploymentPoseName').value = '';
      syncDeploymentPoseButtons();
      setDeploymentResult(snapshot, t('deployPoseRecorded'));
    } catch (error) { setDeploymentResult({ error: error.message }, t('deployPoseRecordFailed'), true); }
  }

  async function moveDeploymentPose() {
    const poseId = $('#deploymentPoseSelect')?.value;
    if (!state.deploymentSessionId || !poseId) return;
    setBusy(true, t('deployMovingPose'), t('deployMovingPoseHint'));
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/poses/${encodeURIComponent(poseId)}/move`, {
        method: 'POST', body: JSON.stringify({ durationS: 3 })
      });
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deployPoseReached'));
    } catch (error) { setDeploymentResult({ error: error.message }, t('deployPoseMoveFailed'), true); }
    finally { setBusy(false); }
  }

  async function deleteDeploymentPose() {
    const poseId = $('#deploymentPoseSelect')?.value;
    if (!state.deploymentSessionId || !poseId) return;
    try {
      const snapshot = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/poses/${encodeURIComponent(poseId)}`, {
        method: 'DELETE'
      });
      renderDeploymentSnapshot(snapshot);
      setDeploymentResult(snapshot, t('deployPoseDeleted'));
    } catch (error) { setDeploymentResult({ error: error.message }, t('deployPoseDeleteFailed'), true); }
  }

  function downloadDeploymentRecord() {
    if (!state.deploymentSessionId) return;
    const path = `/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/manifest`;
    window.open(path, '_blank', 'noopener');
  }

  function renderDeploymentSnapshot(snapshot) {
    if (!snapshot) return;
    state.deploymentSnapshot = snapshot;
    setDeploymentState(snapshot.state);
    state.deploymentRuntimeKind = 'recipe';
    const observedPrompt = snapshot.modelIo?.input?.prompt;
    const promptInput = $('#deploymentTaskPrompt');
    if (observedPrompt && promptInput && document.activeElement !== promptInput) promptInput.value = observedPrompt;
    syncDeploymentButtons(!['disconnected', 'stopped', 'fault'].includes(snapshot.state), snapshot);
    const precheck = (snapshot.steps || []).find((item) => item.name === 'precheck');
    const robotLinked = Boolean(snapshot.components?.tunnel?.active || snapshot.components?.ros?.active || snapshot.components?.client?.active);
    if (robotLinked) {
      state.deploymentRobotConnected = true;
      const mode = t(snapshot.clientRuntime?.mode === 'live' ? 'deployActionExecution' : 'deployReadOnlyObservation');
      setDeploymentComponentStatus('#deploymentRobotStatus', 'ready', `${t('deployConnected')} · ${mode}`);
    } else if (snapshot.state === 'model_ready') {
      state.deploymentRobotConnected = false;
      setDeploymentComponentStatus('#deploymentRobotStatus', 'idle', t('deployDisconnected'));
    } else if (precheck?.status === 'failed' || snapshot.state === 'fault') {
      state.deploymentRobotConnected = false;
      setDeploymentComponentStatus('#deploymentRobotStatus', 'error', t('deployConnectionFailed'));
    }
    else if (snapshot.currentStep === 'precheck') setDeploymentComponentStatus('#deploymentRobotStatus', 'pending', t('deployConnecting'));
    else setDeploymentComponentStatus('#deploymentRobotStatus', 'idle', t('deployConnectionUnchecked'));
    const clientStatus = snapshot.components?.client?.status;
    const robotDetail = $('#deploymentRobotDetail');
    if (robotDetail) robotDetail.textContent = robotLinked
      ? `Client ${clientStatus?.activeState || 'active'}/${clientStatus?.subState || 'running'} · ROS ${snapshot.components?.ros?.active ? 'active' : 'off'} · Tunnel ${snapshot.components?.tunnel?.active ? 'active' : 'off'}${snapshot.clientRuntime?.actionShape ? ` · ${t('deployAction')} ${snapshot.clientRuntime.actionShape.join('×')}` : ''}`
      : t('deployObservationLinkMissing');
    if (snapshot.components?.model?.active) setDeploymentComponentStatus('#deploymentModelStatus', 'ready', t('deployModelStarted'));
    else if (snapshot.currentStep === 'model' || snapshot.currentStep === 'model_health') setDeploymentComponentStatus('#deploymentModelStatus', 'pending', t('deployStartingShort'));
    else if (snapshot.state === 'fault' && snapshot.modelIo) setDeploymentComponentStatus('#deploymentModelStatus', 'idle', t('deployModelStopped'));
    else if (snapshot.state === 'fault') setDeploymentComponentStatus('#deploymentModelStatus', 'error', t('deployStartFailed'));
    else setDeploymentComponentStatus('#deploymentModelStatus', 'idle', t('deployNotStarted'));
    const modelManaged = snapshot.components?.model?.status;
    const modelDetail = $('#deploymentModelDetail');
    if (modelDetail) modelDetail.textContent = snapshot.components?.model?.active
      ? `${modelManaged?.activeState || 'active'}/${modelManaged?.subState || 'running'}${modelManaged?.pid ? ` · PID ${modelManaged.pid}` : ''}${Number.isFinite(Number(snapshot.clientRuntime?.inferenceLatencyMs)) ? ` · ${t('deployInference')} ${Number(snapshot.clientRuntime.inferenceLatencyMs).toFixed(0)} ms` : ''}`
      : `${t('deployModelServiceStopped')}${modelManaged?.result ? ` · ${modelManaged.result}` : ''}`;
    if (snapshot.scheduler) {
      const scheduler = snapshot.scheduler;
      const fields = [$('#deploymentInferenceMode'), $('#deploymentActionSteps'), $('#deploymentRequestAfterSteps')];
      const editing = state.deploymentSchedulerDirty || fields.includes(document.activeElement);
      if (!editing) {
        $('#deploymentInferenceMode').value = scheduler.mode || 'synchronous';
        $('#deploymentActionSteps').value = String(scheduler.actionSteps || scheduler.outputSteps || 1);
        $('#deploymentActionSteps').max = String(scheduler.outputSteps || scheduler.actionSteps || 1);
        $('#deploymentRequestAfterSteps').value = String(scheduler.prefetchPolicy === 'auto' ? 'auto' : (scheduler.requestAfterSteps ?? 'auto'));
      }
      syncDeploymentSchedulerControls();
      if (editing) renderDeploymentSchedulerHint();
      else {
        const prefetch = scheduler.mode === 'asynchronous'
          ? ` · ${scheduler.prefetchPolicy === 'auto'
            ? (Number.isFinite(Number(scheduler.requestAfterSteps)) ? t('deployAutoPrefetchCurrent', { step: scheduler.requestAfterSteps }) : t('deployAutoPrefetchRuntime'))
            : t('deployPrefetchAt', { step: scheduler.requestAfterSteps })}`
          : '';
        $('#deploymentSchedulerHint').textContent = t('deploySchedulerEffective', { output: scheduler.outputSteps, used: scheduler.actionSteps, prefetch });
      }
      syncDeploymentButtons(!['disconnected', 'stopped', 'fault'].includes(snapshot.state), snapshot);
    }
    renderDeploymentPoses(snapshot.recordedPoses || []);
    renderDeploymentModelIo(snapshot.modelIo, snapshot.dryRunSafety, snapshot.trajectoryHistory, snapshot.runtimeTiming);
    refreshDeploymentExecutionLog(false);
    const output = $('#deploymentResult');
    if (output) output.textContent = JSON.stringify({
      state: snapshot.state,
      mode: snapshot.mode,
      currentStep: snapshot.currentStep,
      components: snapshot.components,
      steps: snapshot.steps,
      dryRunSafety: snapshot.dryRunSafety,
      lastError: snapshot.lastError,
      recentEvents: (snapshot.events || []).slice(-30),
    }, null, 2);
    const hint = $('#deploymentResultHint');
    if (hint) hint.textContent = snapshot.lastError || `${snapshot.name || snapshot.deploymentId} · ${snapshot.state}`;
  }

  function renderDeploymentPoses(poses) {
    const select = $('#deploymentPoseSelect');
    if (!select) return;
    const previous = select.value;
    select.innerHTML = poses.length
      ? poses.map((pose) => `<option value="${escapeAttr(pose.poseId)}">${escapeHtml(pose.name)}</option>`).join('')
      : `<option value="">${escapeHtml(t('deployNoRecordedPose'))}</option>`;
    if (poses.some((pose) => pose.poseId === previous)) select.value = previous;
    syncDeploymentPoseButtons();
  }

  function syncDeploymentPoseButtons() {
    const hasPose = Boolean($('#deploymentPoseSelect')?.value);
    const stateName = state.deploymentSnapshot?.state;
    const canMove = hasPose && ['dry_run', 'running'].includes(stateName);
    if ($('#moveDeploymentPose')) $('#moveDeploymentPose').disabled = !canMove;
    if ($('#deleteDeploymentPose')) $('#deleteDeploymentPose').disabled = !hasPose;
    const hint = $('#deploymentPoseHint');
    const hasState = ['dry_run', 'running'].includes(stateName)
      && Array.isArray(state.deploymentSnapshot?.modelIo?.input?.state?.values);
    if (hint) hint.textContent = hasState
      ? t('deployPoseReady', { n: state.deploymentSnapshot.modelIo.input.state.values.length })
      : t('deployPoseNotReady');
  }

  function orchestrationLogRows(snapshot) {
    const marker = Number(state.deploymentLogClearMarkers.orchestration || 0);
    const rows = (snapshot?.events || []).filter((event) => Number(event.timeNs || 0) > marker).slice(-160).map((event) => {
      const time = event.timeNs ? new Date(event.timeNs / 1e6).toLocaleTimeString('zh-CN', { hour12: false }) : '--:--:--';
      const detail = Object.entries(event)
        .filter(([key]) => !['timeNs', 'event', 'state'].includes(key))
        .map(([key, value]) => `${key}=${typeof value === 'object' ? JSON.stringify(value) : value}`)
        .join(' ');
      const name = String(event.event || 'event');
      const level = /fault|failed|error|unhealthy/.test(name) ? 'error' : (/requested|starting/.test(name) ? 'pending' : 'info');
      return { time, level, tag: name, message: detail || event.state || '' };
    });
    if (snapshot?.lastError) rows.push({ time: '--:--:--', level: 'error', tag: 'ERROR', message: snapshot.lastError });
    return rows;
  }

  function componentLogRows(lines, source) {
    let rows = (lines || []).map((line) => {
      const text = String(line);
      const timestamp = text.match(/^\S+\s+(\d\d:\d\d:\d\d)/)?.[1] || '--:--:--';
      const level = /error|exception|traceback|failed|fault/i.test(text) ? 'error' : (/warn/i.test(text) ? 'pending' : 'info');
      return { time: timestamp, level, tag: source, message: text };
    });
    const marker = state.deploymentLogClearMarkers[source];
    if (marker) {
      let markerIndex = -1;
      for (let index = rows.length - 1; index >= 0; index -= 1) {
        if (rows[index].message === marker) { markerIndex = index; break; }
      }
      rows = markerIndex >= 0 ? rows.slice(markerIndex + 1) : rows;
    }
    return rows;
  }

  function renderDeploymentLogRows(rows) {
    const output = $('#deploymentExecutionLog');
    if (!output) return;
    state.deploymentLogRows = rows;
    const filter = ($('#deploymentLogFilter')?.value || '').trim().toLowerCase();
    const visible = filter ? rows.filter((row) => `${row.tag} ${row.message}`.toLowerCase().includes(filter)) : rows;
    output.innerHTML = visible.length ? visible.map((row) => `
      <div class="deployment-log-row ${escapeAttr(row.level)}">
        <time>${escapeHtml(row.time)}</time>
        <span>${escapeHtml(row.tag)}</span>
        <p>${escapeHtml(row.message)}</p>
      </div>`).join('') : `<div class="deployment-log-empty">${escapeHtml(t('deployNoMatchingLogs'))}</div>`;
    if ($('#deploymentLogAutoscroll')?.checked) output.scrollTop = output.scrollHeight;
  }

  async function refreshDeploymentExecutionLog(force = false) {
    const output = $('#deploymentExecutionLog');
    if (!output) return;
    const source = $('#deploymentLogSource')?.value || 'orchestration';
    if (source === 'orchestration') {
      renderDeploymentLogRows(orchestrationLogRows(state.deploymentSnapshot));
      return;
    }
    state.deploymentLogRefreshTick += 1;
    if (!force && state.deploymentLogRefreshTick % 4 !== 0) return;
    if (!state.deploymentSessionId || state.deploymentLogLoading) return;
    state.deploymentLogLoading = true;
    try {
      const result = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/logs`, {
        method: 'POST', body: JSON.stringify({ component: source, lines: 160 })
      });
      renderDeploymentLogRows(componentLogRows(result.lines, source));
    } catch (error) {
      renderDeploymentLogRows([{ time: '--:--:--', level: 'error', tag: source, message: t('deployLogReadFailed', { msg: error.message }) }]);
    } finally {
      state.deploymentLogLoading = false;
    }
  }

  function formatModelIoValue(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return '—';
    const absolute = Math.abs(number);
    if (absolute >= 1000 || (absolute > 0 && absolute < 0.001)) return number.toExponential(3);
    return number.toFixed(4).replace(/\.?0+$/, '');
  }

  function safeModelImageUrl(value) {
    const url = typeof value === 'string' ? value : '';
    return /^data:image\/(?:jpeg|png|webp|bmp);base64,[A-Za-z0-9+/=]+$/.test(url) ? url : '';
  }

  function renderDeploymentModelIo(modelIo, dryRunSafety = null, trajectoryHistory = null, runtimeTiming = null) {
    const placeholder = $('#deploymentModelIoPlaceholder');
    const container = $('#deploymentModelIo');
    if (!placeholder || !container) return;
    if (!modelIo?.input || !modelIo?.output) {
      placeholder.classList.remove('hidden');
      container.classList.add('hidden');
      return;
    }
    placeholder.classList.add('hidden');
    container.classList.remove('hidden');

    const input = modelIo.input;
    const cameras = Array.isArray(input.cameras) ? input.cameras : [];
    $('#deploymentCameraGrid').innerHTML = cameras.length
      ? cameras.map((camera) => {
        const url = safeModelImageUrl(camera.dataUrl);
        if (!url) return '';
        const size = camera.width && camera.height ? `${camera.width} × ${camera.height}` : 'JPEG';
        return `<figure class="deployment-camera-card"><img src="${escapeAttr(url)}" alt="${escapeAttr(camera.label || camera.key || t('deployModelImageInput'))}"><figcaption><strong>${escapeHtml(camera.label || camera.key || t('deployImageInput'))}</strong><small>${escapeHtml(size)}</small></figcaption></figure>`;
      }).join('')
      : `<div class="deployment-io-empty">${escapeHtml(t('deployNoInputImages'))}</div>`;
    $('#deploymentInputMeta').textContent = input.prompt ? `Prompt · ${input.prompt}` : t('deployActualModelInput');

    const inputState = input.state;
    $('#deploymentStateLabel').textContent = inputState?.label || t('deployStateInput');
    $('#deploymentStateKey').textContent = inputState?.key || '—';
    const stateValues = Array.isArray(inputState?.values) ? inputState.values : [];
    $('#deploymentStateGrid').innerHTML = stateValues.length
      ? stateValues.map((value, index) => {
        const name = inputState.names?.[index] || `state_${index + 1}`;
        return `<div class="deployment-vector-item"><strong title="${escapeAttr(name)}">${escapeHtml(name)}</strong><span>${escapeHtml(formatModelIoValue(value))}</span><small>${escapeHtml(inputState.units?.[index] || '')}</small></div>`;
      }).join('')
      : `<div class="deployment-io-empty">${escapeHtml(t('deployNoStateVector'))}</div>`;

    const output = modelIo.output;
    const action = output.action || {};
    const chunk = Array.isArray(action.chunk) ? action.chunk.filter(Array.isArray) : [];
    const width = chunk[0]?.length || 0;
    $('#deploymentActionShape').textContent = chunk.length && width ? `${chunk.length} × ${width}` : '—';
    const latencyText = Number.isFinite(Number(output.inferenceLatencyMs))
      ? `${t('deployInference')} ${Number(output.inferenceLatencyMs).toFixed(1)} ms`
      : t('deployModelActionOutput');
    const pipeline = output.pipeline || {};
    const timingParts = [
      Number.isFinite(Number(pipeline.observationMs)) ? `${t('deployObservation')} ${Number(pipeline.observationMs).toFixed(1)} ms` : '',
      Number.isFinite(Number(pipeline.requestSerializationMs)) ? `${t('deployEncoding')} ${Number(pipeline.requestSerializationMs).toFixed(1)} ms` : '',
      Number.isFinite(Number(runtimeTiming?.scheduleLagMeanMs)) ? `${t('deployScheduleJitter')} ${Number(runtimeTiming.scheduleLagMeanMs).toFixed(1)} ms` : '',
    ].filter(Boolean);
    const detailedLatency = `${latencyText}${timingParts.length ? ` · ${timingParts.join(' · ')}` : ''}`;
    $('#deploymentOutputMeta').textContent = dryRunSafety?.passed === false
      ? `${detailedLatency} · ${t('deployActionValidationFailed')}`
      : detailedLatency;
    renderDeploymentActionTrajectory(action, chunk, trajectoryHistory);
    $('#deploymentActionGrid').innerHTML = width
      ? Array.from({ length: width }, (_, index) => {
        const series = chunk.map((row) => Number(row[index])).filter(Number.isFinite);
        const first = series[0];
        const minimum = Math.min(...series);
        const maximum = Math.max(...series);
        const unit = action.units?.[index] || '';
        const name = action.names?.[index] || `action_${index + 1}`;
        return `<div class="deployment-vector-item output"><strong title="${escapeAttr(name)}">${escapeHtml(name)}</strong><span>${escapeHtml(formatModelIoValue(first))}</span><small>${escapeHtml(`${formatModelIoValue(minimum)} ～ ${formatModelIoValue(maximum)}${unit ? ` ${unit}` : ''}`)}</small></div>`;
      }).join('')
      : `<div class="deployment-io-empty">${escapeHtml(t('deployNoActionOutput'))}</div>`;
  }

  const deploymentTrajectoryColors = [
    '#2563eb', '#e11d48', '#059669', '#d97706', '#7c3aed', '#0891b2', '#db2777', '#4f46e5',
  ];

  function deploymentTrajectoryGroups(action, width) {
    const names = Array.from({ length: width }, (_, index) => action.names?.[index] || `action_${index + 1}`);
    const indexes = (predicate) => names.map((name, index) => predicate(name.toLowerCase(), index) ? index : -1).filter((index) => index >= 0);
    const groups = [
      { id: 'left', label: t('deployLeftArm'), indexes: indexes((name) => name.includes('left') && !name.includes('gripper')) },
      { id: 'right', label: t('deployRightArm'), indexes: indexes((name) => name.includes('right') && !name.includes('gripper')) },
      { id: 'gripper', label: t('deployGripper'), indexes: indexes((name) => name.includes('gripper') || name.includes('effector')) },
      { id: 'all', label: t('filterAll'), indexes: Array.from({ length: width }, (_, index) => index) },
    ].filter((group) => group.indexes.length);
    return { names, groups };
  }

  function deploymentTrajectoryData(action, chunk, history) {
    const requested = $('#deploymentTrajectorySource')?.value || state.deploymentTrajectorySource || 'state';
    const windowSeconds = Number($('#deploymentTrajectoryWindow')?.value) || 20;
    const historyKey = ['state', 'planned', 'executed'].includes(requested) ? requested : null;
    let points = historyKey && Array.isArray(history?.[historyKey]) ? history[historyKey] : [];
    let source = requested;
    if (source === 'chunk') {
      return {
        source: 'chunk',
        rows: chunk,
        xValues: chunk.map((_, index) => index + 1),
        xLabel: t('deployActionStep'),
        names: action.names || [],
        units: action.units || [],
      };
    }
    if (!points.length) {
      return { source, rows: [], xValues: [], xLabel: t('deployRecentSeconds', { n: windowSeconds }), names: history?.names || action.names || [], units: history?.units || action.units || [] };
    }
    const latest = Math.max(...points.map((point) => Number(point.tNs)).filter(Number.isFinite));
    const cutoff = latest - windowSeconds * 1e9;
    points = points.filter((point) => Number(point.tNs) >= cutoff && Array.isArray(point.values));
    const stateSource = source === 'state';
    return {
      source,
      rows: points.map((point) => point.values),
      xValues: points.map((point) => (Number(point.tNs) - latest) / 1e9),
      xLabel: t('deployRecentSeconds', { n: windowSeconds }),
      names: stateSource ? (history.stateNames || action.names || []) : (history.names || action.names || []),
      units: stateSource ? (history.stateUnits || action.units || []) : (history.units || action.units || []),
    };
  }

  function renderCurrentDeploymentTrajectory() {
    state.deploymentTrajectorySource = $('#deploymentTrajectorySource')?.value || 'executed';
    const action = state.deploymentSnapshot?.modelIo?.output?.action || {};
    const chunk = Array.isArray(action.chunk) ? action.chunk.filter(Array.isArray) : [];
    renderDeploymentActionTrajectory(action, chunk, state.deploymentSnapshot?.trajectoryHistory);
  }

  function renderDeploymentActionTrajectory(action, chunk, history = null) {
    const canvas = $('#deploymentActionTrajectory');
    const legend = $('#deploymentTrajectoryLegend');
    const groupContainer = $('#deploymentTrajectoryGroups');
    if (!canvas || !legend || !groupContainer) return;
    const data = deploymentTrajectoryData(action, chunk, history);
    const rows = data.rows || [];
    const width = rows[0]?.length || 0;
    if (!width || !rows.length) {
      legend.innerHTML = `<span>${escapeHtml(t('deployWaitingActionOutput'))}</span>`;
      return;
    }
    const sourceAction = { names: data.names, units: data.units };
    const { names, groups } = deploymentTrajectoryGroups(sourceAction, width);
    if (!groups.some((group) => group.id === state.deploymentTrajectoryGroup)) {
      state.deploymentTrajectoryGroup = groups[0]?.id || 'all';
    }
    groupContainer.innerHTML = groups.map((group) =>
      `<button type="button" data-trajectory-group="${escapeAttr(group.id)}" class="${group.id === state.deploymentTrajectoryGroup ? 'active' : ''}">${escapeHtml(group.label)}</button>`
    ).join('');
    const selected = groups.find((group) => group.id === state.deploymentTrajectoryGroup) || groups[0];
    const indexes = selected.indexes;
    legend.innerHTML = indexes.map((index, colorIndex) =>
      `<span><i style="background:${deploymentTrajectoryColors[colorIndex % deploymentTrajectoryColors.length]}"></i>${escapeHtml(names[index])}</span>`
    ).join('');

    const rect = canvas.getBoundingClientRect();
    const cssWidth = Math.max(320, Math.floor(rect.width || 800));
    const cssHeight = Math.max(180, Math.floor(rect.height || 220));
    const ratio = Math.min(2, window.devicePixelRatio || 1);
    if (canvas.width !== Math.floor(cssWidth * ratio) || canvas.height !== Math.floor(cssHeight * ratio)) {
      canvas.width = Math.floor(cssWidth * ratio);
      canvas.height = Math.floor(cssHeight * ratio);
    }
    const context = canvas.getContext('2d');
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, cssWidth, cssHeight);
    const margin = { left: 48, right: 18, top: 16, bottom: 30 };
    const plotWidth = cssWidth - margin.left - margin.right;
    const plotHeight = cssHeight - margin.top - margin.bottom;
    const perDimension = ($('#deploymentTrajectoryScale')?.value || 'per') === 'per';
    const seriesStats = new Map(indexes.map((index) => {
      const values = rows.map((row) => Number(row[index])).filter(Number.isFinite);
      return [index, { minimum: Math.min(...values), maximum: Math.max(...values) }];
    }));
    const values = indexes.flatMap((index) => rows.map((row) => Number(row[index])).filter(Number.isFinite));
    let minimum = perDimension ? 0 : Math.min(...values);
    let maximum = perDimension ? 1 : Math.max(...values);
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) return;
    if (minimum === maximum) {
      const delta = Math.max(0.1, Math.abs(minimum) * 0.1);
      minimum -= delta;
      maximum += delta;
    } else {
      const padding = (maximum - minimum) * 0.12;
      minimum -= padding;
      maximum += padding;
    }
    const xValues = data.xValues || rows.map((_, index) => index);
    const xMinimum = Math.min(...xValues);
    const xMaximum = Math.max(...xValues);
    const x = (step) => margin.left + (xMaximum === xMinimum ? 0 : (xValues[step] - xMinimum) / (xMaximum - xMinimum)) * plotWidth;
    const y = (value) => margin.top + (maximum - value) / (maximum - minimum) * plotHeight;
    context.font = '9px ui-monospace, SFMono-Regular, Menlo, monospace';
    context.lineWidth = 1;
    for (let tick = 0; tick <= 4; tick += 1) {
      const yy = margin.top + tick / 4 * plotHeight;
      const value = maximum - tick / 4 * (maximum - minimum);
      context.strokeStyle = '#e2e8f0';
      context.beginPath(); context.moveTo(margin.left, yy); context.lineTo(cssWidth - margin.right, yy); context.stroke();
      context.fillStyle = '#64748b';
      context.textAlign = 'right';
      context.fillText(perDimension ? `${Math.round(value * 100)}%` : formatModelIoValue(value), margin.left - 7, yy + 3);
    }
    if (!perDimension && minimum < 0 && maximum > 0) {
      context.strokeStyle = '#94a3b8';
      context.setLineDash([4, 4]);
      context.beginPath(); context.moveTo(margin.left, y(0)); context.lineTo(cssWidth - margin.right, y(0)); context.stroke();
      context.setLineDash([]);
    }
    for (let tick = 0; tick <= 4; tick += 1) {
      const step = Math.round(tick / 4 * Math.max(0, rows.length - 1));
      const xx = x(step);
      context.fillStyle = '#94a3b8';
      context.textAlign = 'center';
      const label = data.source === 'chunk' ? String(step + 1) : `${xValues[step].toFixed(1)}s`;
      context.fillText(label, xx, cssHeight - 10);
    }
    indexes.forEach((index, colorIndex) => {
      const series = rows.map((row) => Number(row[index]));
      const stats = seriesStats.get(index);
      const plotValue = (value) => {
        if (!perDimension || !stats || stats.minimum === stats.maximum) return perDimension ? 0.5 : value;
        return (value - stats.minimum) / (stats.maximum - stats.minimum);
      };
      context.strokeStyle = deploymentTrajectoryColors[colorIndex % deploymentTrajectoryColors.length];
      context.lineWidth = 2;
      context.lineJoin = 'round';
      context.lineCap = 'round';
      context.beginPath();
      series.forEach((value, step) => {
        if (!Number.isFinite(value)) return;
        if (step === 0) context.moveTo(x(step), y(plotValue(value)));
        else context.lineTo(x(step), y(plotValue(value)));
      });
      context.stroke();
      if (series.length <= 60) series.forEach((value, step) => {
          if (!Number.isFinite(value)) return;
          context.fillStyle = deploymentTrajectoryColors[colorIndex % deploymentTrajectoryColors.length];
          context.beginPath(); context.arc(x(step), y(plotValue(value)), 1.8, 0, Math.PI * 2); context.fill();
        });
    });
    context.fillStyle = '#64748b';
    context.textAlign = 'center';
    context.fillText(`${data.xLabel}${perDimension ? ` · ${t('deployPerDimensionScale')}` : ''}`, margin.left + plotWidth / 2, cssHeight - 2);
  }

  function onDeploymentTrajectoryGroup(event) {
    const button = event.target.closest('[data-trajectory-group]');
    if (!button) return;
    state.deploymentTrajectoryGroup = button.dataset.trajectoryGroup;
    const modelIo = state.deploymentSnapshot?.modelIo;
    const action = modelIo?.output?.action || {};
    const chunk = Array.isArray(action.chunk) ? action.chunk.filter(Array.isArray) : [];
    renderDeploymentActionTrajectory(action, chunk, state.deploymentSnapshot?.trajectoryHistory);
  }

  function renderDeploymentComponents(components) {
    const container = $('#deploymentComponents');
    if (!container) return;
    container.classList.remove('hidden');
    container.innerHTML = Object.entries(components)
      .filter(([name]) => ['model', 'tunnel', 'ros', 'client'].includes(name))
      .map(([name, value]) => `<div class="deployment-component ${value.active ? 'active' : ''}">
        <span><i></i><strong>${escapeHtml(name)}</strong><small>${escapeHtml(value.active ? 'active' : 'inactive')}</small></span>
      </div>`).join('');
  }

  async function onDeploymentComponentAction(event) {
    const button = event.target.closest('[data-component-action]');
    if (!button || !state.deploymentSessionId || state.deploymentRuntimeKind !== 'recipe') return;
    const component = button.dataset.component;
    const action = button.dataset.componentAction;
    try {
      let result;
      if (action === 'logs') {
        result = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/logs`, {
          method: 'POST', body: JSON.stringify({ component, lines: 200 })
        });
        setDeploymentResult(result, t('deployRecentComponentLogs', { component }));
      } else {
        if (!window.confirm(t('deployRestartConfirm', { component }))) return;
        setBusy(true, t('deployRestarting', { component }), t('deployRestartingHint'));
        result = await api(`/api/deploy/orchestrations/${encodeURIComponent(state.deploymentSessionId)}/components/${encodeURIComponent(component)}/restart`, { method: 'POST' });
        renderDeploymentSnapshot(result);
        setDeploymentResult(result, t('deployRestarted', { component }));
      }
    } catch (error) {
      setDeploymentResult({ component, error: error.message }, t('deployFailed'), true);
    } finally {
      setBusy(false);
    }
  }

  function renderDeploymentHosts(recipe) {
    const container = $('#deploymentHosts');
    if (!container) return;
    const hosts = recipe?.hosts && typeof recipe.hosts === 'object' ? Object.entries(recipe.hosts) : [];
    if (!hosts.length) {
      container.innerHTML = `<div class="muted">${escapeHtml(t('deployNoHosts'))}</div>`;
      return;
    }
    container.innerHTML = hosts.map(([name, host]) => `<article class="deployment-host-card">
      <div class="deployment-host-head"><strong>${escapeHtml(name)}</strong><code>${escapeHtml(`${host.user}@${host.address}:${host.port || 22}`)}</code></div>
      <div class="muted">${escapeHtml(t('deployConnectionLabel'))}：${escapeHtml(host.connection === 'local' ? t('deployLocalHost') : 'SSH')} · ${escapeHtml(t('deployAuthLabel'))}：${escapeHtml(host.connection === 'local' ? t('deployNoManagedAuth') : (host.auth?.type || t('deployNotConfigured')))} · systemd：${escapeHtml(host.service_manager || 'system')} · ${escapeHtml(host.connection === 'local' ? t('deployLocalExecutionHint') : t('deploySshExecutionHint'))}</div>
    </article>`).join('');
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
    renderQuarantineReasonOptions();
    if (state.dataset) {
      renderSummary();
      renderEpisodeList();
      if (Number.isInteger(state.currentEpisode)) {
        renderEpisodeHeader();
        renderLabelPanel();
        renderQcInlineEvidence();
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
    try { if (!$('#exportDialog')?.classList.contains('hidden')) renderExportDialog(); } catch {}
    try { if (!$('#mergeDialog')?.classList.contains('hidden')) renderMergeDialog(); } catch {}
    try {
      if (state.qcSummary) {
        renderQcSummary();
        renderQcEpisodeTable();
        renderQcPagination();
      }
    } catch {}
    try { fillAugmentColorPresets(); syncAugmentFormVisibility(); } catch {}
    try {
      if ($('#deploymentRobotConfig')?.value.trim() && $('#deploymentModelConfig')?.value.trim()) {
        refreshDeploymentComposition(false).catch(() => {});
      }
    } catch {}
    try {
      if (state.deploymentSnapshot) renderDeploymentSnapshot(state.deploymentSnapshot);
      else {
        setDeploymentComponentStatus('#deploymentRobotStatus', 'idle', t('deployConnectionUnchecked'));
        setDeploymentComponentStatus('#deploymentModelStatus', 'idle', t('deployNotStarted'));
      }
    } catch {}
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
    $('#quarantineReason')?.addEventListener('change', updateCurrentQuarantineReason);
    $('#saveProgress').addEventListener('click', saveProgress);
    $('#loadProgress').addEventListener('click', loadProgress);
    $('#createDataset').addEventListener('click', createDataset);
    $('#closeExport')?.addEventListener('click', closeExportDialog);
    $('#cancelExport')?.addEventListener('click', closeExportDialog);
    $('#startExport')?.addEventListener('click', startExport);
    $('#exportTarget')?.addEventListener('change', () => {
      if (state.exportContext && !state.exportContext.outputTouched) {
        $('#exportOutput').value = suggestedExportPath($('#exportTarget').value);
      }
      renderExportDialog();
    });
    $('#exportMediaMode')?.addEventListener('change', renderExportDialog);
    $('#exportCopyLabels')?.addEventListener('change', renderExportDialog);
    $('#exportIncludeReview')?.addEventListener('change', renderExportDialog);
    $('#exportOutput')?.addEventListener('input', () => {
      if (state.exportContext) {
        state.exportContext.outputTouched = true;
        state.exportContext.chosenDirectory = null;
      }
      renderExportDialog();
    });
    $('#browseExportOutput')?.addEventListener('click', openExportPathBrowser);
    $('#exportBrowserParent')?.addEventListener('click', () => {
      const parent = state.exportContext?.browserParent;
      if (parent) browseExportDirectory(parent);
    });
    $('#useExportDirectory')?.addEventListener('click', useCurrentExportDirectory);
    $('#exportBrowserList')?.addEventListener('click', (event) => {
      const button = event.target.closest('[data-export-directory]');
      if (button) browseExportDirectory(button.dataset.exportDirectory);
    });
    $('#closeAutoFilter')?.addEventListener('click', closeAutoFilterDialog);
    $('#closeAutoFilter2')?.addEventListener('click', closeAutoFilterDialog);
    $('#openAutoFilter')?.addEventListener('click', openAutoFilterDialog);
    $('#startQcScan')?.addEventListener('click', startQcScan);
    $('#refreshQcJobs')?.addEventListener('click', refreshQcJobs);
    $('#pauseQcScan')?.addEventListener('click', () => controlQcScan('pause'));
    $('#resumeQcScan')?.addEventListener('click', () => controlQcScan('resume'));
    $('#cancelQcScan')?.addEventListener('click', () => controlQcScan('cancel'));
    $('#qcHistory')?.addEventListener('change', () => {
      const id = $('#qcHistory').value;
      if (id) selectQcJob(id);
    });
    $('#applyQcFilters')?.addEventListener('click', () => queryQcEpisodes(0));
    $('#qcSearch')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') queryQcEpisodes(0);
    });
    $('#qcIssueFilters')?.addEventListener('change', () => queryQcEpisodes(0));
    $('#qcEpisodeTable')?.addEventListener('click', onQcEpisodeTableClick);
    $('#qcEpisodeDetail')?.addEventListener('click', onQcDetailClick);
    $('#qcEvidencePanel')?.addEventListener('click', onQcInlineClick);
    $('#qcTimelineOverlay')?.addEventListener('click', onQcInlineClick);
    $('#qcPagination')?.addEventListener('click', onQcPaginationClick);
    $('#exportQcSelection')?.addEventListener('click', exportQcSelection);
    $('#downloadQcEpisodes')?.addEventListener('click', () => downloadQcReport('episodes'));
    $('#downloadQcFindings')?.addEventListener('click', () => downloadQcReport('findings'));
    $('#closeConvert')?.addEventListener('click', closeConvertDialog);
    $('#startConvert')?.addEventListener('click', startConvert);
    $('#convertTarget')?.addEventListener('change', renderConvertFidelity);
    $('#closeMerge')?.addEventListener('click', closeMergeDialog);
    $('#addMergeSource')?.addEventListener('click', addMergeSourceFromInput);
    $('#mergeSourceInput')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') addMergeSourceFromInput();
    });
    $('#browseMergeSource')?.addEventListener('click', openMergePathBrowser);
    $('#mergeBrowserParent')?.addEventListener('click', () => {
      const parent = state.mergeContext?.browserParent;
      if (parent) browseMergeDirectory(parent);
    });
    $('#mergeBrowserList')?.addEventListener('click', (event) => {
      const add = event.target.closest('[data-merge-add-path]');
      if (add) return addMergeSource(add.dataset.mergeAddPath);
      const browse = event.target.closest('[data-merge-browse-path]');
      if (browse) browseMergeDirectory(browse.dataset.mergeBrowsePath);
    });
    $('#mergeSourceList')?.addEventListener('click', onMergeSourceAction);
    $('#mergeOutput')?.addEventListener('input', () => {
      if (state.mergeContext) state.mergeContext.outputTouched = true;
      renderMergeDialog();
    });
    $('#startMerge')?.addEventListener('click', startMerge);
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
    const allowed = new Set(['browse', 'annotate', 'filter', 'convert', 'merge', 'augment']);
    const next = allowed.has(mode) ? mode : 'browse';
    // Convert / merge / augment are actions: open a dialog and stay on the prior work mode.
    if (next === 'convert') {
      if (state.dataset) {
        openConvertDialog(state.dataset.path, state.dataset.format || null);
      } else {
        showToast(t('needConvertDataset'), true);
      }
      const fallback = state.workspaceMode && !['convert', 'merge', 'augment'].includes(state.workspaceMode)
        ? state.workspaceMode
        : 'browse';
      applyWorkspaceMode(fallback);
      return;
    }
    if (next === 'merge') {
      if (state.dataset) {
        openMergeDialog(state.dataset.path);
      } else {
        showToast(t('needMergeDataset'), true);
      }
      const fallback = state.workspaceMode && !['convert', 'merge', 'augment'].includes(state.workspaceMode)
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
      const fallback = state.workspaceMode && !['convert', 'merge', 'augment'].includes(state.workspaceMode)
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
    if (next === 'filter') renderQcInlineEvidence();
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
      resetQcVisualization();
      state.dataset = await api('/api/inspect', { method: 'POST', body: JSON.stringify({ dataset: path }) });
      state.episodeByIndex = new Map(state.dataset.episodes.map((episode) => [episode.episodeIndex, episode]));
      state.states = normalizeStates(loadLocalStates(state.dataset.path));
      state.quarantineReasons = normalizeQuarantineReasons(loadLocalQuarantineReasons(state.dataset.path));
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
      loadLatestQcVisualization().catch(() => {});
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
      const qc = state.qcOverviewByEpisode.get(episode.episodeIndex);
      const qcLine = qc
        ? `<span class="episode-qc ${escapeAttr(qc.integrityStatus || '')}">${escapeHtml(t('qcInlineTitle'))} · ${Number(qc.usableRatio || 0).toFixed(0)}% · ${Number(qc.findingCount || 0)} ${escapeHtml(t('qcFindings'))}</span>`
        : '';
      const quarantineReason = decision === 'quarantine' ? getQuarantineReason(episode.episodeIndex) : '';
      const quarantineReasonLine = quarantineReason
        ? `<span class="episode-quarantine-reason">${escapeHtml(t('quarantineReasonPrefix', { reason: quarantineReasonLabel(quarantineReason) }))}</span>`
        : '';
      return `<button class="episode ${cssDecision}${selected}" data-episode="${episode.episodeIndex}">
        <span class="episode-row"><strong>Episode ${episode.episodeIndex}</strong><i class="state-dot"></i></span>
        <span class="episode-task" data-tip="${escapeAttr(task)}">${escapeHtml(task.replace(/\n/g, ', '))}</span>
        <span class="episode-meta">${episode.length} ${escapeHtml(t('framesUnit'))} · ${formatTime(episode.duration)}</span>
        ${labelLine}
        ${qcLine}
        ${quarantineReasonLine}
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
    state.qcActiveFindingId = null;
    state.qcPlaybackEnd = null;
    state.qcPlaybackToken += 1;
    const episode = currentEpisode();
    updateEpisodeSelection(previousIndex, index);
    renderEpisodeHeader();
    renderVideosOptimized(episode);
    loadEpisodeSignals(episode);
    loadQcInlineEvidence(index).catch(() => {});
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
    const qc = state.qcOverviewByEpisode.get(episode.episodeIndex);
    const qcFact = qc
      ? `<span class="label-fact ${qc.integrityStatus === 'invalid' ? 'warning' : ''}">${escapeHtml(t('qcInlineTitle'))} ${Number(qc.usableRatio || 0).toFixed(0)}% · ${Number(qc.findingCount || 0)} ${escapeHtml(t('qcFindings'))}</span>`
      : '';
    const quarantineReason = decision === 'quarantine' ? getQuarantineReason(episode.episodeIndex) : '';
    const quarantineReasonFact = quarantineReason
      ? `<span class="label-fact warning">${escapeHtml(t('quarantineReasonPrefix', { reason: quarantineReasonLabel(quarantineReason) }))}</span>`
      : '';
    $('#episodeHeader').innerHTML = `
      <div>
        <h3>Episode ${episode.episodeIndex}</h3>
        <p class="episode-prompt-line" data-tip="${escapeAttr(prompt)}"><span class="prompt-kicker">${escapeHtml(t('promptLabel'))}</span> ${escapeHtml(prompt)}</p>
      </div>
      <div class="episode-facts">
        <span>${episode.length} ${escapeHtml(t('framesUnit'))}</span><span>${formatTime(episode.duration)}</span>
        <span>${escapeHtml(t('platformId'))} ${escapeHtml(episode.platformEpisodeId || episode.extras?.mcapName || '-')}</span>
        ${labelFacts}
        ${qcFact}
        ${quarantineReasonFact}
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
    const reasonSelect = $('#quarantineReason');
    if (reasonSelect) {
      const reason = decision === 'quarantine' ? getQuarantineReason(state.currentEpisode) : '';
      reasonSelect.value = Array.from(reasonSelect.options).some((option) => option.value === reason) ? reason : '';
      reasonSelect.closest('.quarantine-reason-field')?.classList.toggle('active', decision === 'quarantine');
    }
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
    state.filtered = state.dataset.episodes.filter((episode) => {
      const text = `${episode.episodeIndex} ${episode.platformEpisodeId || ''} ${(episode.tasks || []).join(' ')}`.toLowerCase();
      return (!query || text.includes(query))
        && (decision === 'all' || getState(episode.episodeIndex) === decision);
    });
    renderEpisodeList();
  }

  function setDecision(decision) {
    if (state.workspaceMode !== 'filter') return;
    if (!Number.isInteger(state.currentEpisode)) return;
    const normalized = normalizeDecision(decision);
    state.states[state.currentEpisode] = normalized;
    if (normalized === 'quarantine') {
      const reason = normalizeQuarantineReason($('#quarantineReason')?.value);
      if (reason) state.quarantineReasons[state.currentEpisode] = reason;
      else delete state.quarantineReasons[state.currentEpisode];
    } else {
      delete state.quarantineReasons[state.currentEpisode];
    }
    saveLocalStates();
    renderSummary();
    renderEpisodeHeader();
    applyFilters();
    flashDecision(normalized);
  }

  function updateCurrentQuarantineReason() {
    if (state.workspaceMode !== 'filter' || !Number.isInteger(state.currentEpisode)) return;
    if (getState(state.currentEpisode) !== 'quarantine') return;
    const reason = normalizeQuarantineReason($('#quarantineReason')?.value);
    if (reason) state.quarantineReasons[state.currentEpisode] = reason;
    else delete state.quarantineReasons[state.currentEpisode];
    saveLocalStates();
    renderEpisodeHeader();
    applyFilters();
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
    state.filtered.forEach((episode) => {
      state.states[episode.episodeIndex] = normalized;
      if (normalized !== 'quarantine') delete state.quarantineReasons[episode.episodeIndex];
    });
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
        method: 'POST', body: JSON.stringify({
          path: target,
          dataset: state.dataset.path,
          states: state.states,
          quarantineReasons: state.quarantineReasons,
        })
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
      state.quarantineReasons = normalizeQuarantineReasons(result.quarantineReasons || {});
      state.progressPath = target;
      saveLocalStates();
      renderSummary();
      applyFilters();
      renderEpisodeHeader();
      showToast(t('progressLoaded', { user: result.updatedBy || t('otherUser') }));
    } catch (error) { showToast(error.message, true); }
  }

  function resetQcVisualization() {
    if (state.autoFilterPollTimer) clearTimeout(state.autoFilterPollTimer);
    state.autoFilterPollTimer = null;
    state.autoFilterJobId = null;
    state.qcJobs = [];
    state.qcSummary = null;
    state.qcPage = null;
    state.qcSelectedEpisode = null;
    state.qcOverviewByEpisode = new Map();
    state.qcDetailByEpisode = new Map();
    state.qcActiveFindingId = null;
    state.qcShowRejected = false;
    state.qcPlaybackEnd = null;
    state.qcPlaybackToken += 1;
    state.qcCurrentJob = null;
    state.qcLoadedScanId = null;
    $('#qcTimelineOverlay') && ($('#qcTimelineOverlay').innerHTML = '');
    renderQcInlineEvidence();
  }

  async function loadLatestQcVisualization() {
    if (!state.dataset) return;
    const datasetPath = state.dataset.path;
    const result = await api(`/api/qc/scans?dataset=${encodeURIComponent(datasetPath)}`);
    if (state.dataset?.path !== datasetPath) return;
    state.qcJobs = result.jobs || [];
    const latest = state.qcJobs.find((job) => job.status === 'completed');
    if (!latest) {
      renderQcInlineEvidence();
      return;
    }
    await loadQcReport(latest.jobId);
  }

  async function loadQcEpisodeOverview(scanId) {
    const overview = new Map();
    let offset = 0;
    let total = 0;
    do {
      const page = await api(`/api/qc/scans/${encodeURIComponent(scanId)}/episodes/query`, {
        method: 'POST', body: JSON.stringify({ filters: { limit: 500, offset } }),
      });
      if (state.autoFilterJobId !== scanId) return;
      (page.episodes || []).forEach((episode) => overview.set(Number(episode.episodeIndex), episode));
      offset += (page.episodes || []).length;
      total = Number(page.total || 0);
      if (!(page.episodes || []).length) break;
    } while (offset < total);
    state.qcOverviewByEpisode = overview;
    renderEpisodeList();
    renderEpisodeHeader();
  }

  async function loadQcInlineEvidence(episodeIndex = state.currentEpisode, { force = false } = {}) {
    if (!state.autoFilterJobId || !state.qcSummary || !Number.isInteger(episodeIndex)) {
      renderQcInlineEvidence();
      return;
    }
    if (!force && state.qcDetailByEpisode.has(episodeIndex)) {
      if (state.currentEpisode === episodeIndex) renderQcInlineEvidence();
      return;
    }
    const scanId = state.autoFilterJobId;
    if (state.currentEpisode === episodeIndex) renderQcInlineEvidence(null, true);
    try {
      const detail = await api(`/api/qc/scans/${encodeURIComponent(scanId)}/episodes/${episodeIndex}`);
      if (state.autoFilterJobId !== scanId) return;
      state.qcDetailByEpisode.set(episodeIndex, detail);
      if (state.currentEpisode === episodeIndex) renderQcInlineEvidence(detail);
    } catch (error) {
      if (state.currentEpisode === episodeIndex) renderQcInlineEvidence({ error: error.message });
    }
  }

  function qcFindingBounds(finding) {
    const start = finding?.adjustedStartS ?? finding?.startS;
    const end = finding?.adjustedEndS ?? finding?.endS ?? start;
    return {
      start: start == null ? null : Number(start),
      end: end == null ? null : Number(end),
    };
  }

  function qcFindingCode(finding) {
    return finding?.adjustedIssueCode || finding?.issueCode || 'QC';
  }

  function qcFindingSeverity(finding) {
    return finding?.adjustedSeverity || finding?.severity || 'warning';
  }

  function qcFindingReviewLabel(status) {
    if (status === 'confirmed') return t('qcReviewConfirmed');
    if (status === 'rejected') return t('qcReviewRejected');
    if (status === 'modified') return t('qcReviewModified');
    return t('qcUnreviewed');
  }

  function visibleQcFindings(findings = []) {
    return state.qcShowRejected
      ? findings
      : findings.filter((finding) => finding.reviewStatus !== 'rejected');
  }

  function qcRejectedToggle(findings = []) {
    const count = findings.filter((finding) => finding.reviewStatus === 'rejected').length;
    if (!count) return '';
    const label = state.qcShowRejected
      ? t('qcHideRejected')
      : t('qcShowRejected', { n: count });
    return `<button class="qc-show-rejected" data-qc-toggle-rejected aria-pressed="${state.qcShowRejected}">${escapeHtml(label)}</button>`;
  }

  function updateQcOverviewFromDetail(episodeIndex, detail) {
    const findings = (detail?.findings || []).filter(
      (finding) => finding.reviewStatus !== 'rejected',
    );
    const grouped = new Map();
    findings.forEach((finding) => {
      const issueCode = qcFindingCode(finding);
      const severity = qcFindingSeverity(finding);
      const key = `${issueCode}\u0000${severity}`;
      const current = grouped.get(key) || { issueCode, severity, count: 0 };
      current.count += 1;
      grouped.set(key, current);
    });
    const apply = (episode) => {
      if (!episode || Number(episode.episodeIndex) !== Number(episodeIndex)) return;
      episode.findingCount = findings.length;
      episode.issues = Array.from(grouped.values());
    };
    apply(state.qcOverviewByEpisode.get(Number(episodeIndex)));
    (state.qcPage?.episodes || []).forEach(apply);
    renderEpisodeList();
    renderEpisodeHeader();
    renderQcEpisodeTable();
  }

  async function saveQcFindingReview(findingId, reviewStatus) {
    if (!findingId || !['unreviewed', 'confirmed', 'rejected'].includes(reviewStatus)) return;
    const changed = [];
    state.qcDetailByEpisode.forEach((detail, episodeIndex) => {
      const finding = detail?.findings?.find((item) => item.findingId === findingId);
      if (!finding) return;
      changed.push({ episodeIndex, detail, finding, previous: finding.reviewStatus || 'unreviewed' });
      finding.reviewStatus = reviewStatus;
      updateQcOverviewFromDetail(episodeIndex, detail);
    });
    if (reviewStatus === 'rejected' && !state.qcShowRejected) state.qcActiveFindingId = null;
    const rerender = () => {
      if (changed.some((item) => item.episodeIndex === state.currentEpisode)) renderQcInlineEvidence();
      const selected = changed.find((item) => item.episodeIndex === state.qcSelectedEpisode);
      if (selected) renderQcEpisodeDetail(selected.detail);
    };
    rerender();
    try {
      await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/findings/${encodeURIComponent(findingId)}/review`, {
        method: 'POST', body: JSON.stringify({ reviewStatus }),
      });
      state.qcSummary = await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/summary`);
      renderQcSummary();
      await queryQcEpisodes(state.qcOffset);
      showToast(t('qcReviewSaved', { status: qcFindingReviewLabel(reviewStatus) }));
    } catch (error) {
      changed.forEach((item) => {
        item.finding.reviewStatus = item.previous;
        updateQcOverviewFromDetail(item.episodeIndex, item.detail);
      });
      rerender();
      showToast(error.message, true);
    }
  }

  function qcFindingWhere(finding) {
    return [finding?.cameraKey, finding?.signalKey].filter(Boolean).join(' · ');
  }

  function renderQcTimeline(findings = []) {
    const host = $('#qcTimelineOverlay');
    const episode = currentEpisode();
    if (!host || !episode || !state.qcSummary) {
      if (host) host.innerHTML = '';
      return;
    }
    const duration = Math.max(0.001, Number(episode.duration || 0));
    host.innerHTML = visibleQcFindings(findings).map((finding) => {
      const { start, end } = qcFindingBounds(finding);
      if (start == null) return '';
      const safeStart = Math.max(0, Math.min(duration, start));
      const safeEnd = Math.max(safeStart, Math.min(duration, end ?? start));
      const left = safeStart / duration * 100;
      const width = Math.max(0.35, (safeEnd - safeStart) / duration * 100);
      const severity = qcFindingSeverity(finding);
      const rejected = finding.reviewStatus === 'rejected' ? ' rejected' : '';
      const active = state.qcActiveFindingId === finding.findingId ? ' active' : '';
      const label = `${qcFindingCode(finding)} · ${safeStart.toFixed(2)}s–${safeEnd.toFixed(2)}s`;
      return `<button class="qc-timeline-segment ${escapeAttr(severity)}${rejected}${active}" style="left:${left.toFixed(4)}%;width:max(7px,${width.toFixed(4)}%)" data-qc-inline-play data-finding="${escapeAttr(finding.findingId)}" data-start="${safeStart}" data-end="${safeEnd}" aria-label="${escapeAttr(label)}" data-tip="${escapeAttr(label)}"></button>`;
    }).join('');
  }

  function renderQcInlineEvidence(explicitDetail = null, loading = false) {
    const panel = $('#qcEvidencePanel');
    if (!panel) return;
    if (!state.qcSummary) {
      renderQcTimeline([]);
      const running = ['queued', 'running', 'paused'].includes(state.qcCurrentJob?.status);
      const message = running
        ? `${statusLabel(state.qcCurrentJob.status)} · ${Math.round(Number(state.qcCurrentJob.progress || 0) * 100)}%`
        : t('qcInlineEmpty');
      panel.innerHTML = `<div class="qc-evidence-empty"><div><strong>${escapeHtml(t('qcInlineTitle'))}</strong><span>${escapeHtml(message)}</span></div><button data-open-qc>${escapeHtml(t('qcInlineConfigure'))}</button></div>`;
      return;
    }
    if (loading) {
      renderQcTimeline([]);
      panel.innerHTML = `<div class="qc-evidence-empty"><div><strong>${escapeHtml(t('qcInlineTitle'))}</strong><span>${escapeHtml(t('qcInlineLoading'))}</span></div></div>`;
      return;
    }
    const detail = explicitDetail || state.qcDetailByEpisode.get(state.currentEpisode);
    if (!detail || detail.error) {
      renderQcTimeline([]);
      panel.innerHTML = `<div class="qc-evidence-empty"><div><strong>${escapeHtml(t('qcInlineTitle'))}</strong><span>${escapeHtml(detail?.error || t('qcInlineLoading'))}</span></div></div>`;
      return;
    }
    const episode = detail.episode || {};
    const allFindings = detail.findings || [];
    const findings = visibleQcFindings(allFindings);
    const intervals = findings.filter((finding) => qcFindingBounds(finding).start != null);
    const whole = findings.length - intervals.length;
    if (state.qcActiveFindingId && !findings.some((finding) => finding.findingId === state.qcActiveFindingId)) {
      state.qcActiveFindingId = null;
    }
    renderQcTimeline(intervals);
    const cards = findings.map((finding) => {
      const { start, end } = qcFindingBounds(finding);
      const range = start == null ? t('qcWholeEpisode') : `${start.toFixed(2)}s – ${(end ?? start).toFixed(2)}s`;
      const where = qcFindingWhere(finding);
      const severity = qcFindingSeverity(finding);
      const active = state.qcActiveFindingId === finding.findingId ? ' active' : '';
      const reviewed = finding.reviewStatus === 'confirmed' || finding.reviewStatus === 'rejected'
        ? ` ${finding.reviewStatus}` : '';
      return `<button class="qc-evidence-card ${escapeAttr(severity)}${active}${reviewed}" data-qc-select-finding="${escapeAttr(finding.findingId)}">
        <i class="qc-evidence-dot"></i><span><strong>${escapeHtml(qcFindingCode(finding))}</strong><small>${escapeHtml(range)}${where ? ` · ${escapeHtml(where)}` : ''}</small></span>
      </button>`;
    }).join('');
    const selected = findings.find((finding) => finding.findingId === state.qcActiveFindingId);
    let selectedHtml = '';
    if (selected) {
      const { start, end } = qcFindingBounds(selected);
      const confidence = Math.round(Number(selected.confidence || 0) * 100);
      const where = qcFindingWhere(selected);
      const reviewStatus = selected.reviewStatus || 'unreviewed';
      const reviewActions = reviewStatus === 'rejected'
        ? `<button class="qc-review-choice" data-qc-inline-review="unreviewed" data-finding="${escapeAttr(selected.findingId)}">${escapeHtml(t('qcRestoreRejected'))}</button>`
        : `<button class="qc-review-choice confirmed${reviewStatus === 'confirmed' ? ' active' : ''}" data-qc-inline-review="confirmed" data-finding="${escapeAttr(selected.findingId)}" aria-pressed="${reviewStatus === 'confirmed'}">${escapeHtml(t('qcConfirmIssue'))}</button>
          <button class="qc-review-choice rejected" data-qc-inline-review="rejected" data-finding="${escapeAttr(selected.findingId)}" aria-pressed="false">${escapeHtml(t('qcRejectIssue'))}</button>`;
      selectedHtml = `<div class="qc-evidence-detail">
        <div><p>${escapeHtml(selected.explanation || qcFindingCode(selected))}</p><div class="qc-evidence-detail-meta">${escapeHtml(t('qcInlineConfidence', { n: confidence }))}${where ? ` · ${escapeHtml(where)}` : ''} · ${escapeHtml(qcFindingSeverity(selected))}</div></div>
        <div class="qc-evidence-detail-actions">
          ${start == null ? '' : `<button class="primary" data-qc-inline-play data-finding="${escapeAttr(selected.findingId)}" data-start="${start}" data-end="${end ?? start}">▶ ${escapeHtml(t('qcInlinePlay'))}</button>`}
          ${reviewActions}
          <span class="qc-review-state review-${escapeAttr(reviewStatus)}">${escapeHtml(qcFindingReviewLabel(reviewStatus))}</span>
        </div>
        <details class="qc-evidence-raw"><summary>${escapeHtml(t('qcMetrics'))}</summary><pre>${escapeHtml(JSON.stringify({ metrics: selected.metrics, threshold: selected.threshold }, null, 2))}</pre></details>
      </div>`;
    }
    const countText = intervals.length ? t('qcInlineSegments', { n: intervals.length }) : t('qcInlineClean');
    const wholeText = whole ? ` · ${t('qcInlineWholeIssues', { n: whole })}` : '';
    panel.innerHTML = `<div class="qc-evidence-head"><div><div class="qc-evidence-title"><strong>${escapeHtml(t('qcInlineTitle'))}</strong><span>${escapeHtml(countText + wholeText)}</span><span class="qc-status ${escapeAttr(episode.integrityStatus || '')}">${escapeHtml(episode.integrityStatus === 'invalid' ? t('qcInvalid') : t('qcValid'))}</span>${qcRejectedToggle(allFindings)}</div><div class="qc-evidence-hint">${escapeHtml(t('qcInlineHint'))}</div></div><div class="qc-evidence-score"><span>${escapeHtml(t('qualityScore'))} <b>${Number(episode.qualityScore || 0).toFixed(1)}</b></span><span>${escapeHtml(t('qcAverageUsable'))} <b>${Number(episode.usableRatio || 0).toFixed(1)}%</b></span></div></div>${cards ? `<div class="qc-evidence-cards">${cards}</div>` : ''}${selectedHtml}`;
  }

  async function onQcInlineClick(event) {
    const toggleRejected = event.target.closest('[data-qc-toggle-rejected]');
    if (toggleRejected) {
      state.qcShowRejected = !state.qcShowRejected;
      if (!state.qcShowRejected) state.qcActiveFindingId = null;
      renderQcInlineEvidence();
      const selectedDetail = state.qcDetailByEpisode.get(state.qcSelectedEpisode);
      if (selectedDetail) renderQcEpisodeDetail(selectedDetail);
      return;
    }
    const open = event.target.closest('[data-open-qc]');
    if (open) return openAutoFilterDialog();
    const select = event.target.closest('[data-qc-select-finding]');
    if (select) {
      const findingId = select.dataset.qcSelectFinding;
      state.qcActiveFindingId = findingId;
      const finding = state.qcDetailByEpisode.get(state.currentEpisode)?.findings?.find(
        (item) => item.findingId === findingId,
      );
      const { start, end } = qcFindingBounds(finding);
      if (start != null) {
        playQcInterval(state.currentEpisode, start, end, findingId);
        return;
      }
      renderQcInlineEvidence();
      return;
    }
    const play = event.target.closest('[data-qc-inline-play]');
    if (play) {
      state.qcActiveFindingId = play.dataset.finding || null;
      playQcInterval(state.currentEpisode, Number(play.dataset.start), Number(play.dataset.end));
      return;
    }
    const review = event.target.closest('[data-qc-inline-review]');
    if (review) {
      await saveQcFindingReview(review.dataset.finding, review.dataset.qcInlineReview);
    }
  }

  function playQcInterval(episodeIndex, start, end, findingId = state.qcActiveFindingId) {
    if (!Number.isFinite(start)) return;
    if (state.currentEpisode !== episodeIndex) {
      selectEpisode(episodeIndex);
      state.pendingAutoPlay = false;
    }
    state.qcActiveFindingId = findingId || null;
    pauseAll();
    const episode = currentEpisode();
    const safeStart = Math.max(0, Math.min(Number(episode?.duration || 0), start));
    const safeEnd = Math.max(safeStart + 0.03, Math.min(Number(episode?.duration || end), Number.isFinite(end) ? end : start + 1));
    const token = ++state.qcPlaybackToken;
    state.qcPlaybackEnd = safeEnd;
    renderQcInlineEvidence();
    seekAll(safeStart);
    const tryPlay = (attempt = 0) => {
      if (token !== state.qcPlaybackToken || state.currentEpisode !== episodeIndex) return;
      if (state.episodeReady) {
        playAll();
        return;
      }
      if (attempt < 30) setTimeout(() => tryPlay(attempt + 1), 100);
    };
    setTimeout(() => tryPlay(), 80);
  }

  async function openAutoFilterDialog() {
    if (!state.dataset) return showToast(t('needConvertDataset'), true);
    $('#qcDatasetPath').textContent = state.dataset.path;
    $('#autoFilterDialog').classList.remove('hidden');
    try { await refreshQcJobs(); } catch (error) { showToast(error.message, true); }
  }

  function closeAutoFilterDialog() {
    $('#autoFilterDialog').classList.add('hidden');
  }

  async function refreshQcJobs() {
    if (!state.dataset) return;
    const result = await api(`/api/qc/scans?dataset=${encodeURIComponent(state.dataset.path)}`);
    state.qcJobs = result.jobs || [];
    const history = $('#qcHistory');
    const previous = history.value || state.autoFilterJobId || '';
    history.innerHTML = state.qcJobs.length
      ? state.qcJobs.map((job) => {
        const stamp = job.createdAt ? new Date(job.createdAt).toLocaleString() : '';
        const label = `${String(job.jobId || '').slice(0, 8)} · ${statusLabel(job.status)}${stamp ? ` · ${stamp}` : ''}`;
        return `<option value="${escapeAttr(job.jobId)}">${escapeHtml(label)}</option>`;
      }).join('')
      : `<option value="">${escapeHtml(t('qcNoHistory'))}</option>`;
    const chosen = state.qcJobs.find((job) => job.jobId === previous)
      || state.qcJobs.find((job) => ['queued', 'running', 'paused'].includes(job.status))
      || state.qcJobs[0];
    if (chosen) {
      history.value = chosen.jobId;
      await selectQcJob(chosen.jobId);
    } else {
      state.autoFilterJobId = null;
      renderQcJob(null);
      $('#qcReport')?.classList.add('hidden');
    }
  }

  async function startQcScan() {
    if (!state.dataset) return showToast(t('needConvertDataset'), true);
    const button = $('#startQcScan');
    button.disabled = true;
    try {
      const config = {
        profile: $('#qcProfile').value,
        requirements: { state: $('#qcRequireState').checked },
      };
      const episodeWorkers = Number($('#qcEpisodeWorkers')?.value || 0);
      const cameraWorkers = Number($('#qcCameraWorkers')?.value || 0);
      if (episodeWorkers > 0 || cameraWorkers > 0) {
        config.runtime = {};
        if (episodeWorkers > 0) config.runtime.episodeWorkers = episodeWorkers;
        if (cameraWorkers > 0) config.runtime.cameraWorkers = cameraWorkers;
      }
      const job = await api('/api/qc/scans', {
        method: 'POST',
        body: JSON.stringify({
          dataset: state.dataset.path,
          config,
          useCache: $('#qcUseCache').checked,
        }),
      });
      state.autoFilterJobId = job.jobId;
      state.qcStartedJobId = job.jobId;
      state.qcCurrentJob = job;
      state.qcSummary = null;
      state.qcLoadedScanId = null;
      state.qcOverviewByEpisode = new Map();
      state.qcDetailByEpisode = new Map();
      state.qcActiveFindingId = null;
      renderEpisodeList();
      renderEpisodeHeader();
      renderQcInlineEvidence();
      state.qcJobs = [job, ...state.qcJobs.filter((item) => item.jobId !== job.jobId)];
      renderQcJob(job);
      showToast(t('qcScanStarted', { id: String(job.jobId).slice(0, 8) }));
      await refreshQcJobs();
    } catch (error) {
      showToast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function selectQcJob(jobId) {
    if (!jobId) return;
    state.autoFilterJobId = jobId;
    if ($('#qcHistory')) $('#qcHistory').value = jobId;
    if (state.autoFilterPollTimer) clearTimeout(state.autoFilterPollTimer);
    try {
      const job = await api(`/api/qc/scans/${encodeURIComponent(jobId)}/status`);
      renderQcJob(job);
      if (job.status === 'completed') {
        await loadQcReport(jobId);
        if (state.qcStartedJobId === jobId) {
          state.qcStartedJobId = null;
          closeAutoFilterDialog();
          applyWorkspaceMode('filter');
          showToast(t('qcInlineCompleted'));
        }
      } else if (['queued', 'running', 'paused'].includes(job.status)) {
        if (state.qcLoadedScanId !== jobId) {
          state.qcSummary = null;
          state.qcOverviewByEpisode = new Map();
          state.qcDetailByEpisode = new Map();
          renderEpisodeList();
          renderEpisodeHeader();
          renderQcInlineEvidence();
        }
        scheduleQcPoll(job.status === 'paused' ? 3000 : 1000);
      } else {
        $('#qcReport')?.classList.add('hidden');
      }
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function scheduleQcPoll(delay = 1200) {
    if (state.autoFilterPollTimer) clearTimeout(state.autoFilterPollTimer);
    state.autoFilterPollTimer = setTimeout(async () => {
      state.autoFilterPollTimer = null;
      if (!state.autoFilterJobId) return;
      await selectQcJob(state.autoFilterJobId);
    }, delay);
  }

  function renderQcJob(job) {
    const progress = $('#qcProgress');
    const actions = $('#qcJobActions');
    if (!job) {
      state.qcCurrentJob = null;
      progress?.classList.add('hidden');
      actions?.classList.add('hidden');
      renderQcInlineEvidence();
      return;
    }
    state.qcCurrentJob = job;
    const ratio = Math.max(0, Math.min(1, Number(job.progress) || 0));
    const pct = Math.round(ratio * 100);
    progress?.classList.remove('hidden');
    if (progress) {
      progress.querySelector('i').style.width = `${pct}%`;
      progress.querySelector('span').textContent = `${statusLabel(job.status)} · ${job.current || 0}/${job.total || 0} · ${pct}%${job.message ? ` · ${job.message}` : ''}`;
    }
    const active = ['queued', 'running', 'paused'].includes(job.status);
    actions?.classList.toggle('hidden', !active);
    $('#pauseQcScan').classList.toggle('hidden', job.status === 'paused');
    $('#resumeQcScan').classList.toggle('hidden', job.status !== 'paused');
    if (job.status === 'failed') showToast(job.error || job.message || t('qcScanFailed'), true);
    if (!state.qcSummary) renderQcInlineEvidence();
  }

  async function controlQcScan(action) {
    if (!state.autoFilterJobId) return;
    try {
      const job = await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/${action}`, {
        method: 'POST', body: '{}',
      });
      renderQcJob(job);
      if (action !== 'cancel') scheduleQcPoll(500);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  async function loadQcReport(scanId) {
    state.autoFilterJobId = scanId;
    state.qcShowRejected = false;
    const summary = await api(`/api/qc/scans/${encodeURIComponent(scanId)}/summary`);
    if (state.autoFilterJobId !== scanId) return;
    state.qcSummary = summary;
    state.qcLoadedScanId = scanId;
    state.qcDetailByEpisode = new Map();
    state.qcActiveFindingId = null;
    $('#qcReport').classList.remove('hidden');
    renderQcSummary();
    await Promise.all([
      queryQcEpisodes(0),
      loadQcEpisodeOverview(scanId),
    ]);
    await loadQcInlineEvidence(state.currentEpisode, { force: true });
  }

  function renderQcSummary() {
    const totals = state.qcSummary?.totals || {};
    const cards = [
      [t('qcEpisodes'), totals.episodes || 0, ''],
      [t('qcInvalid'), totals.invalid || 0, 'invalid'],
      [t('qcAverageQuality'), Number(totals.averageQuality || 0).toFixed(1), 'good'],
      [t('qcAverageUsable'), `${Number(totals.averageUsable || 0).toFixed(1)}%`, 'good'],
      [t('qcAverageCoverage'), `${Number(totals.averageCoverage || 0).toFixed(1)}%`, ''],
    ];
    $('#qcSummaryCards').innerHTML = cards.map(([label, value, css]) =>
      `<div class="qc-summary-card ${css}"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join('');
    const issues = state.qcSummary?.issues || [];
    const issueHtml = issues.length
      ? issues.map((item) => `<span class="qc-issue-chip ${escapeAttr(item.severity)}"><b>${escapeHtml(item.issueCode)}</b> ${item.count} · ${item.episodes} ep</span>`).join('')
      : `<span class="muted">${escapeHtml(t('qcNoFindings'))}</span>`;
    const detectorHtml = (state.qcSummary?.detectors || []).map((detector) => {
      const coverage = detector.coverage == null ? '—' : `${Number(detector.coverage).toFixed(0)}%`;
      const skipped = Number(detector.skippedCount) || 0;
      const failed = Number(detector.failedCount) || 0;
      const css = failed ? 'error' : skipped ? 'warning' : '';
      const details = `${coverage}${skipped ? ` · ${t('qcSkippedCount', { n: skipped })}` : ''}${failed ? ` · ${t('qcFailedCount', { n: failed })}` : ''}`;
      return `<span class="qc-issue-chip detector ${css}" data-tip="${escapeAttr(detector.skipReason || '')}"><b>${escapeHtml(detector.detectorId)}</b> ${escapeHtml(details)}</span>`;
    }).join('');
    $('#qcIssueSummary').innerHTML = `${issueHtml}${detectorHtml}`;
    const checked = new Set($$('#qcIssueFilters input:checked').map((input) => input.value));
    $('#qcIssueFilters').innerHTML = issues.map((item) => `<label>
      <input type="checkbox" value="${escapeAttr(item.issueCode)}"${checked.has(item.issueCode) ? ' checked' : ''}>
      <span>${escapeHtml(item.issueCode)} (${item.episodes})</span>
    </label>`).join('');
  }

  function qcFilters({ paginate = true } = {}) {
    const numberOrNull = (selector) => {
      const raw = $(selector)?.value;
      return raw === '' || raw == null ? null : Number(raw);
    };
    return {
      search: $('#qcSearch')?.value.trim() || '',
      integrityStatus: $('#qcIntegrityFilter')?.value || null,
      decision: $('#qcDecisionFilter')?.value || null,
      minimumQuality: numberOrNull('#qcMinQuality'),
      minimumUsable: numberOrNull('#qcMinUsable'),
      issueCodes: $$('#qcIssueFilters input:checked').map((input) => input.value),
      ...(paginate ? { limit: 100, offset: state.qcOffset } : {}),
    };
  }

  async function queryQcEpisodes(offset = 0) {
    if (!state.autoFilterJobId) return;
    state.qcOffset = Math.max(0, Number(offset) || 0);
    try {
      const page = await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/episodes/query`, {
        method: 'POST', body: JSON.stringify({ filters: qcFilters() }),
      });
      state.qcPage = page;
      renderQcEpisodeTable();
      renderQcPagination();
      $('#qcSelectionCount').textContent = t('qcSelectionCount', { n: page.total || 0 });
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function effectiveQcDecision(episode) {
    return episode.manualDecision || episode.autoDecision || 'review';
  }

  function renderQcEpisodeTable() {
    const rows = state.qcPage?.episodes || [];
    $('#qcEpisodeTable').innerHTML = rows.length ? rows.map((episode) => {
      const decision = effectiveQcDecision(episode);
      const issues = (episode.issues || []).map((item) => `<span class="severity-${escapeAttr(item.severity)}">${escapeHtml(item.issueCode)}${item.count > 1 ? ` ×${item.count}` : ''}</span>`).join('');
      return `<button class="qc-episode-row ${escapeAttr(episode.integrityStatus)}${state.qcSelectedEpisode === episode.episodeIndex ? ' selected' : ''}" data-qc-episode="${episode.episodeIndex}">
        <span><strong>Episode ${episode.episodeIndex}</strong><small>${escapeHtml(episode.taskText || t('noTask'))}</small></span>
        <span class="qc-score"><b>${Number(episode.qualityScore).toFixed(1)}</b><small>${escapeHtml(t('qualityScore'))}</small></span>
        <span class="qc-score"><b>${Number(episode.usableRatio).toFixed(1)}%</b><small>${escapeHtml(t('qcAverageUsable'))}</small></span>
        <span class="qc-status ${escapeAttr(episode.integrityStatus)}">${escapeHtml(episode.integrityStatus === 'invalid' ? t('qcInvalid') : t('qcValid'))}</span>
        <span class="qc-status ${escapeAttr(decision)}">${escapeHtml(stateLabel(decision))}</span>
        <span class="qc-row-issues">${issues || escapeHtml(t('qcNoFindings'))}</span>
      </button>`;
    }).join('') : `<div class="empty">${escapeHtml(t('noMatchingEpisode'))}</div>`;
  }

  function renderQcPagination() {
    const page = state.qcPage || { total: 0, limit: 100, offset: 0 };
    const current = Math.floor(page.offset / page.limit) + 1;
    const totalPages = Math.max(1, Math.ceil(page.total / page.limit));
    $('#qcPagination').innerHTML = `<button data-qc-page="${Math.max(0, page.offset - page.limit)}"${page.offset <= 0 ? ' disabled' : ''}>${escapeHtml(t('qcPrevious'))}</button>
      <span>${escapeHtml(t('qcPage', { current, total: totalPages }))}</span>
      <button data-qc-page="${page.offset + page.limit}"${page.offset + page.limit >= page.total ? ' disabled' : ''}>${escapeHtml(t('qcNext'))}</button>`;
  }

  function onQcEpisodeTableClick(event) {
    const row = event.target.closest('[data-qc-episode]');
    if (row) loadQcEpisodeDetail(Number(row.dataset.qcEpisode));
  }

  function onQcPaginationClick(event) {
    const button = event.target.closest('[data-qc-page]');
    if (button && !button.disabled) queryQcEpisodes(Number(button.dataset.qcPage));
  }

  async function loadQcEpisodeDetail(episodeIndex) {
    if (!state.autoFilterJobId || !Number.isInteger(episodeIndex)) return;
    state.qcSelectedEpisode = episodeIndex;
    renderQcEpisodeTable();
    const panel = $('#qcEpisodeDetail');
    panel.innerHTML = `<div class="empty">${escapeHtml(t('busyDefault'))}</div>`;
    try {
      const detail = await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/episodes/${episodeIndex}`);
      state.qcDetailByEpisode.set(episodeIndex, detail);
      if (state.qcSelectedEpisode === episodeIndex) renderQcEpisodeDetail(detail);
      if (state.currentEpisode === episodeIndex) renderQcInlineEvidence(detail);
    } catch (error) {
      panel.innerHTML = `<div class="empty error">${escapeHtml(error.message)}</div>`;
    }
  }

  function renderQcEpisodeDetail(detail) {
    const episode = detail.episode;
    const decision = effectiveQcDecision(episode);
    const allFindings = detail.findings || [];
    const findings = visibleQcFindings(allFindings);
    const detectorSkips = (detail.detectors || []).filter((item) => item.status === 'skipped' || item.status === 'failed');
    const findingHtml = findings.length ? findings.map((finding) => {
      const start = finding.adjustedStartS ?? finding.startS;
      const end = finding.adjustedEndS ?? finding.endS;
      const code = finding.adjustedIssueCode || finding.issueCode;
      const severity = finding.adjustedSeverity || finding.severity;
      const where = [finding.cameraKey, finding.signalKey].filter(Boolean).join(' · ');
      const interval = start == null ? t('qcWholeEpisode') : `${Number(start).toFixed(2)}s – ${Number(end ?? start).toFixed(2)}s`;
      const reviewStatus = finding.reviewStatus || 'unreviewed';
      const reviewActions = reviewStatus === 'rejected'
        ? `<button class="qc-review-choice" data-qc-review="unreviewed" data-finding="${escapeAttr(finding.findingId)}">${escapeHtml(t('qcRestoreRejected'))}</button>`
        : `<button class="qc-review-choice confirmed${reviewStatus === 'confirmed' ? ' active' : ''}" data-qc-review="confirmed" data-finding="${escapeAttr(finding.findingId)}" aria-pressed="${reviewStatus === 'confirmed'}">${escapeHtml(t('qcConfirmIssue'))}</button>
          <button class="qc-review-choice rejected" data-qc-review="rejected" data-finding="${escapeAttr(finding.findingId)}" aria-pressed="false">${escapeHtml(t('qcRejectIssue'))}</button>`;
      return `<article class="qc-finding ${escapeAttr(severity)}">
        <header class="qc-finding-head"><strong>${escapeHtml(code)}</strong><span>${escapeHtml(severity)} · ${Math.round(Number(finding.confidence || 0) * 100)}%</span></header>
        <div class="qc-finding-meta">${escapeHtml(interval)}${where ? ` · ${escapeHtml(where)}` : ''}</div>
        <p>${escapeHtml(finding.explanation || '')}</p>
        <details><summary>${escapeHtml(t('qcMetrics'))}</summary><pre>${escapeHtml(JSON.stringify({ metrics: finding.metrics, threshold: finding.threshold }, null, 2))}</pre></details>
        <footer class="qc-finding-actions">
          ${start == null ? '' : `<button data-qc-seek="${Number(start)}" data-qc-seek-episode="${episode.episodeIndex}">${escapeHtml(t('qcViewEvidence'))}</button>`}
          ${reviewActions}
          <span class="qc-review-state review-${escapeAttr(reviewStatus)}">${escapeHtml(qcFindingReviewLabel(reviewStatus))}</span>
        </footer>
      </article>`;
    }).join('') : `<div class="empty">${escapeHtml(t('qcNoFindings'))}</div>`;
    const skipHtml = detectorSkips.length
      ? `<details class="qc-skips"><summary>${escapeHtml(t('qcSkippedDetectors', { n: detectorSkips.length }))}</summary>${detectorSkips.map((item) => `<p><b>${escapeHtml(item.detectorId)}</b>: ${escapeHtml(item.skipReason || item.status)}</p>`).join('')}</details>`
      : '';
    $('#qcEpisodeDetail').innerHTML = `<div class="qc-detail-head">
      <div><h3>Episode ${episode.episodeIndex}</h3><p>${escapeHtml(episode.taskText || t('noTask'))}</p></div>
      <span class="qc-status ${escapeAttr(episode.integrityStatus)}">${escapeHtml(episode.integrityStatus === 'invalid' ? t('qcInvalid') : t('qcValid'))}</span>
    </div>
    <div class="qc-detail-metrics"><div><strong>${Number(episode.qualityScore).toFixed(1)}</strong><span>${escapeHtml(t('qualityScore'))}</span></div><div><strong>${Number(episode.usableRatio).toFixed(1)}%</strong><span>${escapeHtml(t('qcAverageUsable'))}</span></div><div><strong>${Number(episode.coverage).toFixed(1)}%</strong><span>${escapeHtml(t('qcAverageCoverage'))}</span></div></div>
    <div class="qc-manual-decision"><span>${escapeHtml(t('qcManualDecision'))}: <b>${escapeHtml(stateLabel(decision))}</b></span>
      <button data-qc-decision="pass">${escapeHtml(t('filterPass'))}</button><button data-qc-decision="review">${escapeHtml(t('filterReview'))}</button><button data-qc-decision="quarantine">${escapeHtml(t('filterQuarantine'))}</button>
    </div>${skipHtml}<div class="qc-findings"><h4>${escapeHtml(t('qcFindings'))} (${findings.length}) ${qcRejectedToggle(allFindings)}</h4>${findingHtml}</div>`;
  }

  async function onQcDetailClick(event) {
    const toggleRejected = event.target.closest('[data-qc-toggle-rejected]');
    if (toggleRejected) {
      state.qcShowRejected = !state.qcShowRejected;
      if (!state.qcShowRejected) state.qcActiveFindingId = null;
      const detail = state.qcDetailByEpisode.get(state.qcSelectedEpisode);
      if (detail) renderQcEpisodeDetail(detail);
      renderQcInlineEvidence();
      return;
    }
    const seek = event.target.closest('[data-qc-seek]');
    if (seek) {
      const episodeIndex = Number(seek.dataset.qcSeekEpisode);
      const seconds = Number(seek.dataset.qcSeek);
      if (!state.episodeByIndex.has(episodeIndex)) return showToast(t('qcEpisodeUnavailable'), true);
      closeAutoFilterDialog();
      applyWorkspaceMode('filter');
      const finding = state.qcDetailByEpisode.get(episodeIndex)?.findings?.find((item) => {
        const start = item.adjustedStartS ?? item.startS;
        return Math.abs(Number(start) - seconds) < 0.001;
      });
      const end = finding ? (finding.adjustedEndS ?? finding.endS ?? seconds + 1) : seconds + 1;
      state.qcActiveFindingId = finding?.findingId || null;
      playQcInterval(episodeIndex, seconds, Number(end));
      return;
    }
    const review = event.target.closest('[data-qc-review]');
    if (review) {
      await saveQcFindingReview(review.dataset.finding, review.dataset.qcReview);
      return;
    }
    const decision = event.target.closest('[data-qc-decision]');
    if (decision) {
      try {
        await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/episodes/${state.qcSelectedEpisode}/review`, {
          method: 'POST', body: JSON.stringify({ decision: decision.dataset.qcDecision, note: '' }),
        });
        state.qcDetailByEpisode.delete(state.qcSelectedEpisode);
        await loadQcEpisodeDetail(state.qcSelectedEpisode);
        await queryQcEpisodes(state.qcOffset);
      } catch (error) { showToast(error.message, true); }
    }
  }

  async function exportQcSelection() {
    if (!state.autoFilterJobId || !state.dataset) return;
    try {
      const selection = await api(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/selection/preview`, {
        method: 'POST', body: JSON.stringify({ filters: qcFilters({ paginate: false }) }),
      });
      if (selection.invalidEpisodes?.length) showToast(t('qcInvalidExcluded', { n: selection.invalidEpisodes.length }), true);
      if (!selection.episodes?.length) return showToast(t('qcNoExportable'), true);
      openExportDialog({
        fixedEpisodes: selection.episodes,
        invalidCount: selection.invalidEpisodes?.length || 0,
        suffix: '_qc_filtered',
        source: 'qc',
      });
    } catch (error) { showToast(error.message, true); }
  }

  function downloadQcReport(kind) {
    if (!state.autoFilterJobId) return;
    window.open(`/api/qc/scans/${encodeURIComponent(state.autoFilterJobId)}/export-report?kind=${encodeURIComponent(kind)}`, '_blank', 'noopener');
  }

  let exportCapabilities = new Map();

  function exportDecisionCounts() {
    const counts = { pass: 0, review: 0, quarantine: 0 };
    (state.dataset?.episodes || []).forEach((episode) => {
      counts[getState(episode.episodeIndex)] += 1;
    });
    return counts;
  }

  function exportEpisodes() {
    const ctx = state.exportContext;
    if (!ctx) return [];
    if (Array.isArray(ctx.fixedEpisodes)) return [...ctx.fixedEpisodes];
    const includeReview = Boolean($('#exportIncludeReview')?.checked);
    return (state.dataset?.episodes || [])
      .filter((episode) => {
        const decision = getState(episode.episodeIndex);
        return decision === 'pass' || (includeReview && decision === 'review');
      })
      .map((episode) => episode.episodeIndex)
      .sort((a, b) => a - b);
  }

  function replaceExportSuffix(path, suffixes, nextSuffix) {
    const lower = path.toLowerCase();
    const matched = suffixes.find((suffix) => lower.endsWith(suffix));
    if (matched) return `${path.slice(0, -matched.length)}${nextSuffix}`;
    const slash = path.lastIndexOf('/');
    const dot = path.lastIndexOf('.');
    return dot > slash ? `${path.slice(0, dot)}${nextSuffix}` : `${path}${nextSuffix}`;
  }

  function resolvedExportPath(path, targetFormat) {
    const value = String(path || '').trim();
    if (!value) return '';
    if (targetFormat === 'hdf5') return replaceExportSuffix(value, ['.hdf5', '.h5'], '.hdf5');
    if (targetFormat === 'mcap') return replaceExportSuffix(value, ['.mcap'], '.mcap');
    return value.replace(/\/$/, '');
  }

  function exportResultName(targetFormat) {
    const dataset = state.dataset;
    const suffix = state.exportContext?.suffix || '_filtered';
    let name = String(dataset?.name || 'dataset').replace(/\/$/, '');
    if (dataset?.format === 'hdf5') name = name.replace(/\.(hdf5|h5)$/i, '');
    if (dataset?.format === 'mcap') name = name.replace(/\.mcap$/i, '');
    return resolvedExportPath(`${name}${suffix}`, targetFormat);
  }

  function dirname(path) {
    const clean = String(path || '').replace(/\/+$/, '');
    const index = clean.lastIndexOf('/');
    if (index <= 0) return '/';
    return clean.slice(0, index);
  }

  function joinServerPath(directory, name) {
    return `${String(directory || '/').replace(/\/+$/, '') || '/'}${directory === '/' ? '' : '/'}${name}`;
  }

  function suggestedExportPath(targetFormat) {
    const ctx = state.exportContext;
    if (ctx?.chosenDirectory) return joinServerPath(ctx.chosenDirectory, exportResultName(targetFormat));
    const source = String(state.dataset?.path || '').replace(/\/+$/, '');
    const suffix = ctx?.suffix || '_filtered';
    let base = source;
    if (state.dataset?.format === 'hdf5') base = base.replace(/\.(hdf5|h5)$/i, '');
    if (state.dataset?.format === 'mcap') base = base.replace(/\.mcap$/i, '');
    return resolvedExportPath(`${base}${suffix}`, targetFormat);
  }

  function formatCapabilityText(info) {
    if (!info?.fidelity) return '';
    const fidelityText = info.fidelity === 'full'
      ? t('fidelityFull')
      : info.fidelity === 'high' ? t('fidelityHigh') : t('fidelityPartial');
    const notes = (info.notes || []).map((note) => {
      const translated = t(`convertNote_${note}`);
      return translated === `convertNote_${note}` ? note : translated;
    }).join('；');
    return notes ? `${fidelityText} · ${notes}` : fidelityText;
  }

  async function loadExportCapabilities(sourceFormat) {
    exportCapabilities = new Map();
    try {
      const result = await api(`/api/convert/targets?sourceFormat=${encodeURIComponent(sourceFormat || '')}`);
      (result.formats || []).forEach((item) => exportCapabilities.set(item.id, item));
    } catch {}
    if (!exportCapabilities.has(sourceFormat)) {
      exportCapabilities.set(sourceFormat, {
        id: sourceFormat,
        label: state.dataset?.formatLabel || sourceFormat,
        fidelity: 'full',
        notes: ['sameFormatLossless'],
      });
    }
    const target = $('#exportTarget');
    if (!target) return;
    target.innerHTML = [...exportCapabilities.values()]
      .map((item) => `<option value="${escapeAttr(item.id)}">${escapeHtml(item.label || item.id)}</option>`)
      .join('');
    target.value = sourceFormat;
    if (!target.value && target.options.length) target.selectedIndex = 0;
    if (state.exportContext && !state.exportContext.outputTouched) {
      $('#exportOutput').value = suggestedExportPath(target.value);
    }
    renderExportDialog();
  }

  function openExportDialog(options = {}) {
    if (!state.dataset) return showToast(t('needConvertDataset'), true);
    state.exportContext = {
      fixedEpisodes: Array.isArray(options.fixedEpisodes)
        ? [...new Set(options.fixedEpisodes.map(Number).filter(Number.isInteger))].sort((a, b) => a - b)
        : null,
      invalidCount: Number(options.invalidCount) || 0,
      suffix: options.suffix || '_filtered',
      source: options.source || 'review',
      outputTouched: false,
      chosenDirectory: null,
      browserPath: null,
      browserParent: null,
    };
    const fixed = Array.isArray(state.exportContext.fixedEpisodes);
    $('#exportSource').textContent = state.dataset.path;
    $('#exportSourceFormat').textContent = state.dataset.formatLabel || state.dataset.format || '—';
    $('#exportIncludeReviewLabel').classList.toggle('hidden', fixed);
    $('#exportFixedSelectionHint').classList.toggle('hidden', !fixed);
    if (fixed) $('#exportFixedSelectionHint').textContent = t('exportFixedSelectionHint');
    $('#exportIncludeReview').checked = false;
    $('#exportMediaMode').value = 'hardlink';
    $('#exportCopyLabels').checked = true;
    $('#exportPathBrowser').classList.add('hidden');
    $('#exportOutput').value = suggestedExportPath(state.dataset.format);
    $('#startExport').disabled = false;
    $('#exportDialog').classList.remove('hidden');
    renderExportDialog();
    loadExportCapabilities(state.dataset.format).catch(() => {});
  }

  function closeExportDialog() {
    $('#exportDialog')?.classList.add('hidden');
    state.exportContext = null;
  }

  function renderExportDialog() {
    if (!state.exportContext || !state.dataset) return;
    const counts = exportDecisionCounts();
    const selected = exportEpisodes();
    const fixed = Array.isArray(state.exportContext.fixedEpisodes);
    const stats = fixed
      ? [
        [selected.length, t('exportStatSelected'), 'good'],
        [state.dataset.totalEpisodes || state.dataset.episodes.length, t('exportStatTotal'), ''],
        [Math.max(0, (state.dataset.totalEpisodes || 0) - selected.length), t('exportStatFilteredOut'), 'warn'],
        [state.exportContext.invalidCount, t('exportStatInvalid'), 'bad'],
      ]
      : [
        [selected.length, t('exportStatSelected'), 'good'],
        [counts.pass, t('filterPass'), 'good'],
        [counts.review, t('filterReview'), 'warn'],
        [counts.quarantine, t('filterQuarantine'), 'bad'],
      ];
    $('#exportScopeStats').innerHTML = stats.map(([value, label, cls]) =>
      `<div class="export-scope-stat ${cls}"><strong>${value}</strong><span>${escapeHtml(label)}</span></div>`
    ).join('');

    const targetFormat = $('#exportTarget')?.value || state.dataset.format;
    const capability = exportCapabilities.get(targetFormat);
    const fidelity = $('#exportFidelity');
    fidelity.textContent = formatCapabilityText(capability) || t('exportCapabilityLoading');
    fidelity.classList.toggle('partial', capability?.fidelity === 'partial');
    const output = $('#exportOutput')?.value.trim() || '';
    const resolved = resolvedExportPath(output, targetFormat);
    const absoluteOutput = output.startsWith('/');
    const mediaMode = $('#exportMediaMode')?.value || 'hardlink';
    const copyLabels = Boolean($('#exportCopyLabels')?.checked);
    $('#exportReviewCount').textContent = String(selected.length);
    $('#exportReviewFormat').textContent = capability?.label || $('#exportTarget')?.selectedOptions?.[0]?.textContent || targetFormat;
    $('#exportReviewMedia').textContent = t(mediaMode === 'copy' ? 'exportMediaCopyShort' : 'exportMediaHardlinkShort');
    $('#exportReviewLabels').textContent = t(copyLabels ? 'yes' : 'no');
    $('#exportResolvedOutput').textContent = resolved || t('exportPathMissing');
    $('#exportReviewNotice').textContent = !absoluteOutput
      ? t('exportAbsolutePathRequired')
      : targetFormat === state.dataset.format ? t('exportSameFormatNotice') : t('exportConvertNotice');
    $('#exportReviewNotice').classList.toggle('error', Boolean(output && !absoluteOutput));
    $('#startExport').disabled = !selected.length || !output || !absoluteOutput;
  }

  function openExportPathBrowser() {
    if (!state.exportContext) return;
    const browser = $('#exportPathBrowser');
    if (!browser.classList.contains('hidden')) {
      browser.classList.add('hidden');
      return;
    }
    browser.classList.remove('hidden');
    const start = state.exportContext.browserPath || dirname($('#exportOutput').value) || state.browseRoot;
    browseExportDirectory(start);
  }

  async function browseExportDirectory(path) {
    if (!state.exportContext) return;
    const list = $('#exportBrowserList');
    list.innerHTML = `<div class="export-browser-empty">${escapeHtml(t('loading'))}</div>`;
    try {
      const result = await api(`/api/list?path=${encodeURIComponent(path)}`);
      if (!state.exportContext) return;
      state.exportContext.browserPath = result.path;
      state.exportContext.browserParent = result.parent;
      $('#exportBrowserPath').textContent = result.path;
      $('#exportBrowserParent').disabled = !result.parent;
      const directories = (result.entries || []).filter((entry) => entry.isDir !== false);
      list.innerHTML = directories.length
        ? directories.map((entry) => `<button type="button" class="export-browser-entry" data-export-directory="${escapeAttr(entry.path)}"><span>▰</span><span>${escapeHtml(entry.name)}</span></button>`).join('')
        : `<div class="export-browser-empty">${escapeHtml(t('exportNoSubdirectories'))}</div>`;
    } catch (error) {
      list.innerHTML = `<div class="export-browser-empty error">${escapeHtml(error.message)}</div>`;
    }
  }

  function useCurrentExportDirectory() {
    const directory = state.exportContext?.browserPath;
    if (!directory) return;
    state.exportContext.chosenDirectory = directory;
    state.exportContext.outputTouched = false;
    $('#exportOutput').value = suggestedExportPath($('#exportTarget').value);
    $('#exportPathBrowser').classList.add('hidden');
    renderExportDialog();
  }

  async function startExport() {
    if (!state.exportContext || !state.dataset) return;
    const episodes = exportEpisodes();
    const output = $('#exportOutput').value.trim();
    if (!episodes.length) return showToast(t('needPassEpisode'), true);
    if (!output) return showToast(t('needOutputPath'), true);
    if (!output.startsWith('/')) return showToast(t('exportAbsolutePathRequired'), true);
    const button = $('#startExport');
    button.disabled = true;
    try {
      const job = await api('/api/create', {
        method: 'POST',
        body: JSON.stringify({
          dataset: state.dataset.path,
          output,
          episodes,
          mediaMode: $('#exportMediaMode').value,
          targetFormat: $('#exportTarget').value || state.dataset.format,
          copyLabels: $('#exportCopyLabels').checked,
        }),
      });
      closeExportDialog();
      showToast(t('exportStarted', { n: episodes.length, id: String(job.jobId || '').slice(0, 8) }));
      await refreshConvertJobs();
      trackConvertJob(job.jobId);
    } catch (error) {
      showToast(error.message, true);
      button.disabled = false;
    }
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

  function suggestedMergeOutput(path) {
    const source = String(path || '').replace(/\/+$/, '');
    if (!source) return '';
    const folder = dirname(source);
    let name = source.split('/').pop() || 'dataset';
    name = name.replace(/\.(hdf5|h5|mcap)$/i, '');
    return joinServerPath(folder || '/', `${name}_merged`);
  }

  function openMergeDialog(initialPath) {
    const first = typeof initialPath === 'string' ? initialPath : state.dataset?.path;
    if (!first) return showToast(t('needMergeDataset'), true);
    state.mergeContext = {
      sources: [first],
      preflight: null,
      checking: false,
      requestSequence: 0,
      browserPath: dirname(first) || state.currentFolder || state.browseRoot || '/',
      browserParent: null,
      outputTouched: false,
    };
    $('#mergeSourceInput').value = '';
    $('#mergeOutput').value = suggestedMergeOutput(first);
    $('#mergeMediaMode').value = 'hardlink';
    $('#mergeCopyLabels').checked = true;
    $('#mergePathBrowser').classList.add('hidden');
    $('#mergeProgress').classList.add('hidden');
    $('#mergeDialog').classList.remove('hidden');
    renderMergeDialog();
  }

  function closeMergeDialog() {
    $('#mergeDialog')?.classList.add('hidden');
    state.mergeContext = null;
  }

  function addMergeSourceFromInput() {
    addMergeSource($('#mergeSourceInput')?.value || '');
  }

  function addMergeSource(rawPath) {
    const ctx = state.mergeContext;
    const path = String(rawPath || '').trim().replace(/\/+$/, '') || '/';
    if (!ctx || !path) return;
    if (!path.startsWith('/')) return showToast(t('mergeAbsoluteSource'), true);
    if (ctx.sources.includes(path)) return showToast(t('mergeDuplicateSource'), true);
    ctx.sources.push(path);
    ctx.preflight = null;
    if (!ctx.outputTouched && ctx.sources.length === 2) {
      $('#mergeOutput').value = suggestedMergeOutput(ctx.sources[0]);
    }
    $('#mergeSourceInput').value = '';
    renderMergeDialog();
    runMergePreflight();
  }

  function onMergeSourceAction(event) {
    const button = event.target.closest('[data-merge-action]');
    const ctx = state.mergeContext;
    if (!button || !ctx) return;
    const index = Number(button.dataset.mergeIndex);
    if (!Number.isInteger(index) || index < 0 || index >= ctx.sources.length) return;
    const action = button.dataset.mergeAction;
    ctx.requestSequence += 1;
    if (action === 'remove') ctx.sources.splice(index, 1);
    if (action === 'up' && index > 0) [ctx.sources[index - 1], ctx.sources[index]] = [ctx.sources[index], ctx.sources[index - 1]];
    if (action === 'down' && index < ctx.sources.length - 1) [ctx.sources[index + 1], ctx.sources[index]] = [ctx.sources[index], ctx.sources[index + 1]];
    if (!ctx.outputTouched && ctx.sources[0]) $('#mergeOutput').value = suggestedMergeOutput(ctx.sources[0]);
    ctx.preflight = null;
    renderMergeDialog();
    if (ctx.sources.length >= 2) runMergePreflight();
  }

  function renderMergeDialog() {
    const ctx = state.mergeContext;
    if (!ctx) return;
    const list = $('#mergeSourceList');
    list.innerHTML = ctx.sources.map((path, index) => `
      <div class="merge-source-row">
        <span class="merge-source-order">${index + 1}</span>
        <span class="merge-source-path" title="${escapeAttr(path)}">${escapeHtml(path)}</span>
        <span class="merge-source-actions">
          <button type="button" data-merge-action="up" data-merge-index="${index}" ${index === 0 ? 'disabled' : ''} aria-label="${escapeAttr(t('mergeMoveUp'))}">↑</button>
          <button type="button" data-merge-action="down" data-merge-index="${index}" ${index === ctx.sources.length - 1 ? 'disabled' : ''} aria-label="${escapeAttr(t('mergeMoveDown'))}">↓</button>
          <button type="button" data-merge-action="remove" data-merge-index="${index}" aria-label="${escapeAttr(t('delete'))}">×</button>
        </span>
      </div>`).join('');

    const checked = ctx.preflight;
    $('#mergeFormat').textContent = checked?.formatLabel || state.dataset?.formatLabel || '—';
    const preflight = $('#mergePreflight');
    preflight.classList.remove('ok', 'error');
    if (ctx.checking) {
      preflight.textContent = t('mergeChecking');
    } else if (ctx.sources.length < 2) {
      preflight.textContent = t('mergeNeedTwo');
    } else if (checked?.compatible) {
      preflight.textContent = t('mergeCompatible');
      preflight.classList.add('ok');
    } else if (checked) {
      const conflicts = checked.conflicts || [];
      preflight.innerHTML = `<strong>${escapeHtml(t('mergeIncompatible'))}</strong><ul>${conflicts.map((item) => `<li>${escapeHtml(item.message || String(item))}</li>`).join('')}</ul>`;
      preflight.classList.add('error');
    } else {
      preflight.textContent = t('mergeChecking');
    }

    const stats = $('#mergeStats');
    stats.innerHTML = checked ? [
      [checked.sources?.length || ctx.sources.length, t('mergeStatSources')],
      [checked.totalEpisodes || 0, t('mergeStatEpisodes')],
      [Number(checked.totalFrames || 0).toLocaleString(), t('mergeStatFrames')],
      [checked.totalLabels || 0, t('mergeStatLabels')],
    ].map(([value, label]) => `<div class="export-scope-stat"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`).join('') : '';
    const output = $('#mergeOutput')?.value.trim() || '';
    $('#startMerge').disabled = !checked?.compatible || !output.startsWith('/') || ctx.checking;
  }

  async function runMergePreflight() {
    const ctx = state.mergeContext;
    if (!ctx || ctx.sources.length < 2) return renderMergeDialog();
    const sequence = ++ctx.requestSequence;
    ctx.checking = true;
    renderMergeDialog();
    try {
      const result = await api('/api/merge/preflight', {
        method: 'POST',
        body: JSON.stringify({ sources: ctx.sources }),
      });
      if (!state.mergeContext || sequence !== state.mergeContext.requestSequence) return;
      ctx.preflight = result;
    } catch (error) {
      if (!state.mergeContext || sequence !== state.mergeContext.requestSequence) return;
      ctx.preflight = { compatible: false, conflicts: [{ message: error.message }], sources: [] };
    } finally {
      if (state.mergeContext && sequence === state.mergeContext.requestSequence) {
        ctx.checking = false;
        renderMergeDialog();
      }
    }
  }

  function openMergePathBrowser() {
    const ctx = state.mergeContext;
    if (!ctx) return;
    $('#mergePathBrowser').classList.toggle('hidden');
    if (!$('#mergePathBrowser').classList.contains('hidden')) {
      browseMergeDirectory(ctx.browserPath || state.currentFolder || state.browseRoot || '/');
    }
  }

  async function browseMergeDirectory(path) {
    const ctx = state.mergeContext;
    if (!ctx) return;
    const list = $('#mergeBrowserList');
    list.innerHTML = `<div class="export-browser-empty">${escapeHtml(t('loading'))}</div>`;
    try {
      const result = await api(`/api/list?path=${encodeURIComponent(path)}`);
      if (!state.mergeContext) return;
      ctx.browserPath = result.path;
      ctx.browserParent = result.parent;
      $('#mergeBrowserPath').textContent = result.path;
      $('#mergeBrowserParent').disabled = !result.parent;
      const rows = [];
      if (result.isDataset) {
        rows.push(`<button type="button" class="export-browser-entry" data-merge-add-path="${escapeAttr(result.path)}"><span>DS</span><span>${escapeHtml(t('mergeAddCurrentDataset'))}</span></button>`);
      }
      for (const entry of result.entries || []) {
        if (entry.isDataset) {
          rows.push(`<button type="button" class="export-browser-entry" data-merge-add-path="${escapeAttr(entry.path)}"><span>DS</span><span>${escapeHtml(entry.name)} · ${escapeHtml(entry.formatLabel || '')}</span></button>`);
        } else if (entry.isDir !== false) {
          rows.push(`<button type="button" class="export-browser-entry" data-merge-browse-path="${escapeAttr(entry.path)}"><span>▰</span><span>${escapeHtml(entry.name)}</span></button>`);
        }
      }
      list.innerHTML = rows.join('') || `<div class="export-browser-empty">${escapeHtml(t('exportNoSubdirectories'))}</div>`;
    } catch (error) {
      list.innerHTML = `<div class="export-browser-empty error">${escapeHtml(error.message)}</div>`;
    }
  }

  async function startMerge() {
    const ctx = state.mergeContext;
    const output = $('#mergeOutput')?.value.trim() || '';
    if (!ctx || !ctx.preflight?.compatible) return showToast(t('mergeMustPass'), true);
    if (!output.startsWith('/')) return showToast(t('exportAbsolutePathRequired'), true);
    const progress = $('#mergeProgress');
    progress.classList.remove('hidden');
    progress.querySelector('i').style.width = '5%';
    progress.querySelector('span').textContent = t('mergePreparing');
    $('#startMerge').disabled = true;
    try {
      const job = await api('/api/merge/start', {
        method: 'POST',
        body: JSON.stringify({
          sources: ctx.sources,
          output,
          mediaMode: $('#mergeMediaMode').value,
          copyLabels: $('#mergeCopyLabels').checked,
        }),
      });
      showToast(t('mergeStarted', { id: String(job.jobId || '').slice(0, 8) }));
      closeMergeDialog();
      await refreshConvertJobs();
      trackConvertJob(job.jobId);
    } catch (error) {
      showToast(error.message, true);
      progress.classList.add('hidden');
      renderMergeDialog();
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
          if (job.kind === 'merge') {
            showToast(t('mergeDone', { output: job.result?.output || job.output || '' }));
          } else {
            const reportPath = job.result?.reportPath || '';
            showToast(t('convertDone', {
              output: job.result?.output || job.output || '',
              report: reportPath ? t('convertReport', { path: reportPath }) : '',
            }));
          }
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
      const src = job.kind === 'merge'
        ? t('mergeJobSources', { n: job.sourceCount || job.sources?.length || 0 })
        : String(job.dataset || '').split('/').pop() || job.dataset || '';
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
        <div class="convert-job-meta">${escapeHtml(src)} → ${escapeHtml(job.kind === 'merge' ? t('mergeJobOutput') : (job.targetFormat || ''))}${detail ? ` · ${escapeHtml(detail)}` : ''}</div>
        <div class="convert-job-bar"><i style="width:${pct}%"></i></div>
        <div class="convert-job-msg">${escapeHtml(job.message || '')}${status === 'completed' && resultPath ? `<br><span class="muted">${escapeHtml(resultPath)}${report}</span>` : ''}</div>
      </div>`;
    }).join('');
  }

  function statusLabel(status) {
    if (status === 'queued') return t('convertStatusQueued');
    if (status === 'running') return t('convertStatusRunning');
    if (status === 'paused') return t('qcPaused');
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

  function createDataset() {
    const counts = exportDecisionCounts();
    if (!counts.pass) return showToast(t('needPassEpisode'), true);
    openExportDialog();
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

  function togglePlay() {
    if (state.playing) {
      pauseAll();
      return;
    }
    state.qcPlaybackEnd = null;
    state.qcPlaybackToken += 1;
    playAll();
  }

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
    state.qcPlaybackEnd = null;
    state.qcPlaybackToken += 1;
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
    if (Number.isFinite(state.qcPlaybackEnd) && relative >= state.qcPlaybackEnd - 0.015) {
      const stopAt = state.qcPlaybackEnd;
      state.qcPlaybackEnd = null;
      state.qcPlaybackToken += 1;
      seekAll(stopAt);
      pauseAll();
      return;
    }
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
    if (!$('#exportDialog')?.classList.contains('hidden')) {
      if (event.key === 'Escape') closeExportDialog();
      return;
    }
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

  function normalizeQuarantineReason(reason) {
    const value = String(reason || '').trim();
    return value.length <= 128 ? value : '';
  }

  function normalizeQuarantineReasonOptions(options) {
    const seen = new Set();
    return (Array.isArray(options) ? options : []).flatMap((option) => {
      const id = normalizeQuarantineReason(option?.id);
      const zh = String(option?.label?.zh || '').trim();
      const en = String(option?.label?.en || zh).trim();
      if (!id || !zh || seen.has(id)) return [];
      seen.add(id);
      return [{ id, label: { zh, en: en || zh }, enabled: option.enabled !== false }];
    });
  }

  function quarantineReasonOption(reason) {
    return state.quarantineReasonOptions.find((option) => option.id === reason);
  }

  function renderQuarantineReasonOptions() {
    const select = $('#quarantineReason');
    if (!select) return;
    const selected = select.value;
    const lang = window.EmbodyI18n?.getLang?.() || 'zh';
    select.replaceChildren();
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = t('quarantineReasonNone');
    select.appendChild(placeholder);
    state.quarantineReasonOptions.forEach((reason) => {
      const option = document.createElement('option');
      option.value = reason.id;
      option.textContent = reason.label[lang] || reason.label.zh || reason.id;
      option.disabled = !reason.enabled;
      if (!reason.enabled) option.textContent += t('quarantineReasonDisabled');
      select.appendChild(option);
    });
    if (Array.from(select.options).some((option) => option.value === selected)) select.value = selected;
  }

  function normalizeQuarantineReasons(reasons) {
    const out = {};
    Object.entries(reasons || {}).forEach(([key, value]) => {
      const normalized = normalizeQuarantineReason(value);
      if (normalized) out[key] = normalized;
    });
    return out;
  }

  function quarantineReasonLabel(reason) {
    const option = quarantineReasonOption(reason);
    const lang = window.EmbodyI18n?.getLang?.() || 'zh';
    return option?.label?.[lang] || option?.label?.zh || reason;
  }

  function getState(index) { return normalizeDecision(state.states[index] || 'review'); }
  function getQuarantineReason(index) { return normalizeQuarantineReason(state.quarantineReasons[index]); }
  function saveLocalStates() {
    localStorage.setItem(`embody-review:${state.dataset.path}`, JSON.stringify(state.states));
    localStorage.setItem(`embody-quarantine-reasons:${state.dataset.path}`, JSON.stringify(state.quarantineReasons));
  }
  function loadLocalStates(path) { try { return JSON.parse(localStorage.getItem(`embody-review:${path}`) || '{}'); } catch { return {}; } }
  function loadLocalQuarantineReasons(path) { try { return JSON.parse(localStorage.getItem(`embody-quarantine-reasons:${path}`) || '{}'); } catch { return {}; } }
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
    setHeaderCollapsed(true);
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
      const direction = options.invert ? -1 : 1;
      const delta = ((horizontal ? event.clientX : event.clientY) - startPointer) * direction;
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
    const deploymentWorkspace = document.querySelector('.deployment-layout');
    const deploymentSidebar = document.querySelector('.deployment-config-pane');
    const deploymentLog = document.querySelector('.deployment-log-panel');
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

    // Deployment operation sidebar width --------------------------------
    const deploymentSidebarWidth = () => deploymentSidebar.getBoundingClientRect().width;
    if (layout.deploymentSidebarW) {
      deploymentWorkspace.style.setProperty('--deployment-sidebar-w', `${layout.deploymentSidebarW}px`);
    }
    bindSplitter($('#splitDeploymentSidebar'), {
      axis: 'x',
      get: deploymentSidebarWidth,
      min: () => 320,
      max: () => Math.max(380, deploymentWorkspace.getBoundingClientRect().width - 520),
      set: (value) => deploymentWorkspace.style.setProperty('--deployment-sidebar-w', `${Math.round(value)}px`),
      reset: () => {
        deploymentWorkspace.style.removeProperty('--deployment-sidebar-w');
        layout.deploymentSidebarW = null;
      },
      done: () => {
        const inline = deploymentWorkspace.style.getPropertyValue('--deployment-sidebar-w');
        layout.deploymentSidebarW = inline ? Math.round(deploymentSidebarWidth()) : null;
        saveLayout();
      }
    });

    // Deployment log height ----------------------------------------------
    const deploymentLogHeight = () => deploymentLog.getBoundingClientRect().height;
    if (layout.deploymentLogH) {
      deploymentSidebar.style.setProperty('--deployment-log-h', `${layout.deploymentLogH}px`);
    }
    bindSplitter($('#splitDeploymentLog'), {
      axis: 'y',
      invert: true,
      get: deploymentLogHeight,
      min: () => 120,
      max: () => Math.max(180, deploymentSidebar.getBoundingClientRect().height - 180),
      set: (value) => deploymentSidebar.style.setProperty('--deployment-log-h', `${Math.round(value)}px`),
      reset: () => {
        deploymentSidebar.style.removeProperty('--deployment-log-h');
        layout.deploymentLogH = null;
      },
      done: () => {
        const inline = deploymentSidebar.style.getPropertyValue('--deployment-log-h');
        layout.deploymentLogH = inline ? Math.round(deploymentLogHeight()) : null;
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
