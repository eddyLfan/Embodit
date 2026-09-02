'use strict';

const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');

const webRoot = __dirname;
const appPath = path.join(webRoot, 'app.js');
const indexPath = path.join(webRoot, 'index.html');
const stylesPath = path.join(webRoot, 'styles.css');
const utilsPath = path.join(webRoot, 'utils.js');

function readEffectiveDeclarations(cssSource, targetSelector) {
  const declarations = {};
  const sourceWithoutComments = cssSource.replace(/\/\*[\s\S]*?\*\//g, '');
  const rulePattern = /([^{}]+)\{([^{}]*)\}/g;
  let ruleMatch;

  while ((ruleMatch = rulePattern.exec(sourceWithoutComments)) !== null) {
    const selectors = ruleMatch[1].split(',').map((selector) => selector.trim());
    if (!selectors.includes(targetSelector)) continue;

    const declarationPattern = /([\w-]+)\s*:\s*([^;]+)\s*;?/g;
    let declarationMatch;
    while ((declarationMatch = declarationPattern.exec(ruleMatch[2])) !== null) {
      declarations[declarationMatch[1]] = declarationMatch[2].trim();
    }
  }

  return declarations;
}

function resolveCssColor(value, variables) {
  let resolved = String(value || '').trim();
  const seen = new Set();

  while (resolved.startsWith('var(')) {
    const variableMatch = resolved.match(/^var\((--[\w-]+)(?:,\s*([^)]+))?\)$/);
    assert.ok(variableMatch, `unsupported CSS variable color: ${resolved}`);
    const variableName = variableMatch[1];
    assert.equal(seen.has(variableName), false, `cyclic CSS variable: ${variableName}`);
    seen.add(variableName);
    resolved = String(variables[variableName] || variableMatch[2] || '').trim();
  }

  const normalized = resolved.toLowerCase();
  if (normalized === 'white') return [255, 255, 255];
  if (normalized === 'black') return [0, 0, 0];

  const hexMatch = normalized.match(/^#([0-9a-f]{3}|[0-9a-f]{6})(?:[0-9a-f]{2})?$/i);
  if (hexMatch) {
    const hex = hexMatch[1].length === 3
      ? [...hexMatch[1]].map((digit) => digit + digit).join('')
      : hexMatch[1];
    return [0, 2, 4].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
  }

  const rgbMatch = normalized.match(/^rgba?\(([^)]+)\)$/);
  assert.ok(rgbMatch, `expected an RGB-compatible color, received: ${resolved}`);
  const channels = rgbMatch[1].split('/')[0].replace(/,/g, ' ').trim().split(/\s+/).slice(0, 3);
  assert.equal(channels.length, 3, `expected three RGB channels, received: ${resolved}`);
  return channels.map((channel) => {
    const numeric = Number.parseFloat(channel);
    assert.ok(Number.isFinite(numeric), `invalid RGB channel in: ${resolved}`);
    return channel.endsWith('%') ? Math.round(numeric * 2.55) : numeric;
  });
}

function resolveCssLength(value, variables) {
  let resolved = String(value || '').trim();
  const seen = new Set();

  while (resolved.startsWith('var(')) {
    const variableMatch = resolved.match(/^var\((--[\w-]+)(?:,\s*([^)]+))?\)$/);
    assert.ok(variableMatch, `unsupported CSS variable length: ${resolved}`);
    const variableName = variableMatch[1];
    assert.equal(seen.has(variableName), false, `cyclic CSS variable: ${variableName}`);
    seen.add(variableName);
    resolved = String(variables[variableName] || variableMatch[2] || '').trim();
  }

  const lengthMatch = resolved.match(/^(-?\d+(?:\.\d+)?)px$/);
  assert.ok(lengthMatch, `expected a pixel length, received: ${resolved}`);
  return Number.parseFloat(lengthMatch[1]);
}

function isBlue(rgb) {
  const [red, green, blue] = rgb;
  return blue >= 140 && blue >= red + 35 && blue >= green + 20;
}

function cssValueContainsBlue(value, variables) {
  const candidates = [String(value || '').trim()];
  for (const match of String(value || '').matchAll(/var\((--[\w-]+)/g)) {
    if (variables[match[1]]) candidates.push(variables[match[1]]);
  }
  for (const match of String(value || '').matchAll(/#[0-9a-f]{3,8}\b|rgba?\([^)]+\)/gi)) {
    candidates.push(match[0]);
  }

  return candidates.some((candidate) => {
    try {
      return isBlue(resolveCssColor(candidate, variables));
    } catch {
      return false;
    }
  });
}

function shadowHasBlur(value, variables = {}) {
  let resolved = String(value || '').trim();
  const seen = new Set();
  while (resolved.startsWith('var(')) {
    const variableMatch = resolved.match(/^var\((--[\w-]+)(?:,\s*([^)]+))?\)$/);
    if (!variableMatch || seen.has(variableMatch[1])) return false;
    seen.add(variableMatch[1]);
    resolved = String(variables[variableMatch[1]] || variableMatch[2] || '').trim();
  }

  const layerPattern = /(?:^|,)\s*(?:inset\s+)?(-?\d+(?:\.\d+)?)(?:px)?\s+(-?\d+(?:\.\d+)?)(?:px)?\s+(\d+(?:\.\d+)?)(?:px)?(?:\s+\d+(?:\.\d+)?px)?\s+/g;
  return [...resolved.matchAll(layerPattern)]
    .some((match) => Number.parseFloat(match[3]) > 0);
}

function relativeLuminance(rgb) {
  const linear = rgb.map((channel) => {
    const normalized = channel / 255;
    return normalized <= 0.04045
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return (0.2126 * linear[0]) + (0.7152 * linear[1]) + (0.0722 * linear[2]);
}

global.window = {};
require(utilsPath);

const { downsampleSeries, mediaIdentity } = global.window.EmbodyUtils;
const rows = Array.from({ length: 1001 }, (_, index) => index);
const sampled = downsampleSeries(rows, 400);
assert.equal(sampled.length, 400);
assert.equal(sampled[0], 0);
assert.equal(sampled[sampled.length - 1], 1000);
assert.deepEqual(downsampleSeries(rows, 2), [0, 1000]);
assert.deepEqual(downsampleSeries(rows, 1), [0, 1000]);

const relativeVideo = { kind: 'video', path: 'videos/chunk-000.mp4' };
assert.notEqual(
  mediaIdentity('/datasets/a', relativeVideo, 'wrist'),
  mediaIdentity('/datasets/b', relativeVideo, 'wrist'),
);
assert.equal(
  mediaIdentity('/datasets/a', relativeVideo, 'wrist'),
  mediaIdentity('/datasets/a', relativeVideo, 'wrist'),
);
assert.notEqual(
  mediaIdentity('/datasets/a', { kind: 'frames' }, 'left'),
  mediaIdentity('/datasets/a', { kind: 'frames' }, 'right'),
);

const appSource = fs.readFileSync(appPath, 'utf8');
const htmlSource = fs.readFileSync(indexPath, 'utf8');
assert.equal(appSource.includes('setInterval(refreshDeploymentSession'), false);
assert.ok(appSource.includes('state.deploymentTimer = window.setTimeout(poll, 500)'));
assert.ok(appSource.includes('nextPreviewAt += 125'));
assert.ok(appSource.includes('Math.max(0, nextPreviewAt - performance.now())'));
assert.ok(appSource.includes('/live-preview'));
assert.ok(appSource.includes('?includePreview=false'));
assert.ok(appSource.includes('includeModelImages=false'));
assert.ok(appSource.includes('trajectoryPoints=360'));
assert.ok(appSource.includes('deploymentConfigsReady'));
assert.ok(appSource.includes('const configsReady = state.deploymentConfigsReady'));
assert.ok(appSource.includes('generation !== state.deploymentPollGeneration'));
assert.ok(appSource.includes('state.deploymentOfflineDatasetMeta = { path, metadata }'));
assert.ok(appSource.includes("addEventListener('click', () => loadDeploymentOfflineDataset())"));
assert.ok(appSource.includes('generation !== state.deploymentOfflineLoadGeneration'));
assert.ok(appSource.includes('state.deploymentOfflineRunning'));
assert.ok(appSource.includes('deploymentOfflineDatasetPath() === path'));
assert.ok(appSource.includes('cancelDeploymentOfflineChartDraw();'));
assert.ok(appSource.includes('$(selector)?.replaceChildren();'));
assert.ok(appSource.includes('async function startDeploymentOfflineReplay()'));
assert.ok(appSource.includes('async function startDeploymentHardwareReplay()'));
assert.ok(appSource.includes('/hardware-replay'));
assert.ok(appSource.includes('/hardware-replay/stop'));
assert.ok(htmlSource.includes('id="deploymentOfflineReplayRobot"'));
assert.ok(htmlSource.includes('id="deploymentOfflineReplayRobotStop"'));
assert.ok(appSource.includes('/api/timeseries?dataset='));
assert.ok(appSource.includes('deploymentOfflineReplayVideoSource'));
assert.ok(appSource.includes('/api/hdf5/video?dataset='));
assert.ok(appSource.includes('/api/mcap/video?dataset='));
assert.ok(appSource.includes('window.requestAnimationFrame(syncDeploymentOfflineReplay)'));
assert.ok(appSource.includes('video.playbackRate = speed'));
assert.ok(appSource.includes('The episode clock is authoritative'));
assert.ok(appSource.includes('Do not count browser/media startup latency as replay time'));
const offlineReplaySyncFunction = appSource.slice(
  appSource.indexOf('function syncDeploymentOfflineReplay'),
  appSource.indexOf('function closeDeploymentOfflineReplay'),
);
assert.equal(offlineReplaySyncFunction.includes('video === master'), false);
assert.ok(appSource.includes('seekDeploymentOfflineReplay(Number(event.target.value), true)'));
assert.ok(appSource.includes('state.deploymentOfflineReplaySource'));
assert.ok(appSource.includes('function deploymentOfflineReplayGroups(source, width)'));
assert.ok(appSource.includes('for (let start = 0; start < width; start += 8)'));
assert.ok(appSource.includes('renderKey !== state.deploymentModelIoRenderKey'));
assert.ok(appSource.includes('state.deploymentModelIoRenderKey = renderKey'));
assert.ok(appSource.includes('renderDeploymentLivePreview(snapshot.livePreview'));
assert.equal(htmlSource.includes('id="deploymentStateGrid"'), false);
assert.equal(htmlSource.includes('id="deploymentActionGrid"'), false);
assert.equal(htmlSource.includes('id="deploymentTrajectorySource"'), false);
assert.equal(htmlSource.includes('id="deploymentTrajectoryWindow"'), false);
assert.ok(htmlSource.includes('class="deployment-trajectory-line-keys"'));
assert.ok(appSource.includes('historyPoints.concat(state.deploymentLiveStateHistory)'));
assert.ok(appSource.includes('const xMinimum = -10;'));
assert.ok(appSource.includes('context.setLineDash(dashed ? [7, 5] : [])'));
assert.ok(appSource.includes('action?.skippedPrefixSteps'));
assert.ok(htmlSource.includes('<option value="shared" selected>真实物理量程</option>'));
assert.ok(appSource.includes('context.bezierCurveTo('));
assert.ok(offlineReplaySyncFunction.includes('const frameChanged = nextFrame !== replay.frame;'));
assert.ok(appSource.includes('const robotState = snapshot?.robotState;'));
assert.ok(appSource.includes('snapshot?.components?.client?.active'));
assert.ok(
  appSource.indexOf('const robotStartingSteps = new Set(')
    < appSource.indexOf("['hardware_replay_prepare', 'hardware_replay_client_stop'"),
);
assert.ok(appSource.includes("robotConfigId: $('#deploymentRobotSelect')?.value || null"));
assert.ok(appSource.includes("modelConfigId: $('#deploymentModelSelect')?.value || null"));
assert.ok(appSource.includes('/switch-model'));
assert.ok(appSource.includes('startFrame: 0'));
assert.ok(appSource.includes("t(modelChanged ? 'deploySwitchModel'"));
assert.ok(appSource.includes("['model_ready', 'robot_ready', 'stopped', 'fault'].includes(snapshot?.state)"));
assert.ok(appSource.includes("model_switch_precheck: t('deployCheckingEnvironment')"));
const poseButtonSyncFunction = appSource.slice(
  appSource.indexOf('function syncDeploymentPoseButtons'),
  appSource.indexOf('function orchestrationLogRows'),
);
assert.equal(poseButtonSyncFunction.includes('modelIo'), false);
assert.equal(htmlSource.includes('id="refreshDeploymentLog"'), false);
assert.equal(htmlSource.includes('id="deploymentLogAutoscroll"'), false);
assert.equal(appSource.includes('hint.textContent = snapshot.lastError ||'), false);
assert.ok(appSource.includes("pose_move_failed: '回位失败'"));
assert.ok(appSource.includes("pose_move_failed: 'Move to pose failed'"));
assert.ok(appSource.includes("event.event !== 'step_started'"));
assert.ok(appSource.includes('const nearBottom = output.scrollHeight - output.scrollTop - output.clientHeight < 28'));
assert.ok(appSource.includes("recordVideo: Boolean($('#deploymentRecordLive')?.checked)"));
assert.ok(appSource.includes('renderDeploymentRecording(snapshot.recording, snapshot.state)'));
assert.ok(appSource.includes("setBusy(true, t('deployPausingEvaluation'), t('deployPausingEvaluationHint'))"));
assert.equal(appSource.includes('MediaRecorder'), false);
assert.equal(appSource.includes('captureStream'), false);
assert.ok(appSource.includes("const changing = ['starting', 'replaying', 'stopping'].includes(snapshot?.state)"));
assert.ok(appSource.includes('startDeploymentPolling();\n      setDeploymentResult(snapshot, t(\'deployDisconnectingRobot\'))'));
const robotConnectionFunction = appSource.slice(
  appSource.indexOf('async function checkDeploymentRobotConnection'),
  appSource.indexOf('async function prepareDeploymentModel'),
);
assert.equal(robotConnectionFunction.includes('/start-dry-run'), false);
assert.equal(robotConnectionFunction.includes('/api/deploy/robot-connection'), false);
assert.ok(robotConnectionFunction.includes('/api/deploy/orchestrations/connect-robot'));
assert.ok(robotConnectionFunction.includes('/connect-robot'));
assert.ok(robotConnectionFunction.includes('composeDeploymentRecipe()'));
assert.ok(robotConnectionFunction.includes('error.status !== 404'));
assert.ok(robotConnectionFunction.includes('result = await createConnection()'));
assert.ok(robotConnectionFunction.includes("await api('/api/deploy/orchestrations')"));
assert.ok(robotConnectionFunction.includes('Recovered robot connection after a lost connect response'));
assert.ok(appSource.includes('error.status = response.status'));
assert.ok(appSource.includes('if (state.deploymentSessionId) {'));
assert.ok(appSource.includes('Never overwrite that'));
const modelPreparationFunction = appSource.slice(
  appSource.indexOf('async function prepareDeploymentModel'),
  appSource.indexOf('async function disconnectDeploymentRobot'),
);
assert.ok(modelPreparationFunction.includes('error.status !== 404'));
assert.ok(modelPreparationFunction.includes('snapshot = await createPreparation()'));
assert.ok(appSource.includes("['model_ready', 'robot_ready'].includes(snapshot?.state)"));
assert.ok(appSource.includes("['robot_ready', 'dry_run', 'running'].includes(stateName)"));
assert.ok(appSource.includes('return mediaIdentity(state.dataset?.path, video, cameraKey);'));

const utilsSource = fs.readFileSync(utilsPath, 'utf8');
assert.ok(utilsSource.includes("JSON.stringify([String(datasetPath || ''), source])"));

const stylesSource = fs.readFileSync(stylesPath, 'utf8');
const rootTheme = readEffectiveDeclarations(stylesSource, ':root');
assert.equal(
  readEffectiveDeclarations(stylesSource, '.deployment-observation-stage')['overflow-anchor'],
  'none',
);
assert.equal(rootTheme['color-scheme'], 'light');

for (const variable of ['--bg', '--surface', '--surface-2', '--surface-3']) {
  assert.ok(
    relativeLuminance(resolveCssColor(rootTheme[variable], rootTheme)) >= 0.80,
    `${variable} must remain part of the white and light-gray application palette`,
  );
}
assert.ok(
  relativeLuminance(resolveCssColor(rootTheme['--text'], rootTheme)) <= 0.25,
  '--text must remain readable on the light application palette',
);

const bodyTypography = readEffectiveDeclarations(stylesSource, 'body');
const fontStack = bodyTypography['font-family'] || bodyTypography.font || '';
assert.match(
  fontStack,
  /(?:-apple-system|blinkmacsystemfont|sf pro)/i,
  'the application must use an Apple-compatible system UI font stack',
);
assert.match(
  fontStack,
  /(?:pingfang sc|noto sans cjk sc|microsoft yahei)/i,
  'the system UI font stack must retain a Chinese fallback',
);

const themeBlueVariables = Object.entries(rootTheme)
  .filter(([name]) => /(?:accent|blue|primary)/i.test(name))
  .filter(([, value]) => cssValueContainsBlue(value, rootTheme));
assert.ok(themeBlueVariables.length > 0, 'the theme must define a blue interaction accent');

function assertBlueInteraction(selectors, properties) {
  const selectorList = Array.isArray(selectors) ? selectors : [selectors];
  assert.ok(
    selectorList.some((selector) => {
      const declarations = readEffectiveDeclarations(stylesSource, selector);
      return properties.some((property) => cssValueContainsBlue(declarations[property], rootTheme));
    }),
    `${selectorList.join(' or ')} must expose the blue interaction accent`,
  );
}

assertBlueInteraction('button.primary', ['color', 'background', 'background-color', 'border-color', 'box-shadow']);
assertBlueInteraction(
  ['.layer-tab.active', '.layer-tab.active .layer-tab-icon'],
  ['color', 'background', 'background-color', 'border-color', 'box-shadow'],
);
assertBlueInteraction('input:focus', ['border-color', 'outline', 'box-shadow']);

const radius = resolveCssLength(rootTheme['--radius'], rootTheme);
assert.ok(radius >= 8 && radius <= 20, 'the shared corner radius must stay moderately rounded');
for (const variable of ['--shadow', '--shadow-soft']) {
  assert.ok(
    shadowHasBlur(rootTheme[variable], rootTheme),
    `${variable} must use a blurred, soft elevation instead of a hard pixel offset`,
  );
}

for (const selector of [
  '.app-header',
  '.chooser-card',
  '.modal-card',
  '.layer-tab.active',
  'button.primary',
  'button:hover:not(:disabled)',
  'input',
]) {
  const boxShadow = readEffectiveDeclarations(stylesSource, selector)['box-shadow'];
  assert.ok(
    !boxShadow || boxShadow === 'none' || shadowHasBlur(boxShadow, rootTheme),
    `${selector} must not reintroduce a hard pixel-offset shadow`,
  );
}

for (const selector of ['button:hover:not(:disabled)', 'button:active:not(:disabled)']) {
  const transform = readEffectiveDeclarations(stylesSource, selector).transform;
  assert.ok(!transform || transform === 'none', `${selector} must not use pixel-style jump motion`);
}

function assertLightSurface(selector) {
  const declarations = readEffectiveDeclarations(stylesSource, selector);
  const background = declarations['background-color'] || declarations.background;
  assert.ok(background, `${selector} must define its application-surface background`);
  assert.ok(
    relativeLuminance(resolveCssColor(background, rootTheme)) >= 0.75,
    `${selector} must remain a light application surface`,
  );
}

for (const selector of [
  'html',
  'body',
  '.app-header',
  '.chooser-card',
  '.episode-panel',
  '.decision-bar',
  'button',
  'input',
  'select',
  'textarea',
  '.deployment-config-editors textarea',
  '#deploymentRecipe',
  '#deploymentResult',
  '.modal-card',
  '.modal-title',
  '.modal-actions',
]) {
  assertLightSurface(selector);
}

for (const selector of ['button', 'input', 'select', 'textarea', '.chooser-card', '.modal-card']) {
  const borderRadius = resolveCssLength(
    readEffectiveDeclarations(stylesSource, selector)['border-radius'],
    rootTheme,
  );
  assert.ok(
    borderRadius >= 8 && borderRadius <= 24,
    `${selector} must retain a moderate Apple-style corner radius`,
  );
}

const collapsedHeader = readEffectiveDeclarations(stylesSource, 'body.header-collapsed');
assert.equal(collapsedHeader['--header-h'], '36px');

const collapsedSwitch = readEffectiveDeclarations(stylesSource, 'body.header-collapsed .layer-switch');
assert.ok(
  Number.parseFloat(collapsedSwitch.padding) <= 2,
  'the collapsed layer switch must fit inside the compact header',
);

const collapsedTab = readEffectiveDeclarations(stylesSource, 'body.header-collapsed .layer-tab');
assert.equal(collapsedTab.height, '28px');

for (const selector of [
  'body.header-collapsed .lang-switch select',
  'body.header-collapsed .header-toggle',
]) {
  assert.equal(
    readEffectiveDeclarations(stylesSource, selector).height,
    '28px',
    `${selector} must fit inside the collapsed header`,
  );
}

for (const dimension of ['width', 'height']) {
  assert.equal(
    readEffectiveDeclarations(stylesSource, 'body.header-collapsed .brand-logo')[dimension],
    '24px',
    `collapsed brand logo ${dimension} must stay compact`,
  );
  assert.equal(
    readEffectiveDeclarations(stylesSource, 'body.header-collapsed .layer-tab-icon')[dimension],
    '18px',
    `collapsed layer icon ${dimension} must stay compact`,
  );
}

// Only logs, video media, and their camera frames remain dark islands in the light app shell.
for (const selector of [
  '.video-card',
  '.deployment-log-panel',
  '.deployment-camera-card',
]) {
  const declarations = readEffectiveDeclarations(stylesSource, selector);
  const background = declarations['background-color'] || declarations.background;
  assert.ok(background, `${selector} must keep an explicit specialized background`);
  assert.ok(
    relativeLuminance(resolveCssColor(background, rootTheme)) <= 0.20,
    `${selector} must remain a dark media or log surface`,
  );
}
assert.doesNotThrow(
  () => resolveCssColor(rootTheme['--chart-bg'], rootTheme),
  '--chart-bg must remain an independent canvas palette',
);
for (const selector of [
  '.deployment-offline-dimension canvas',
  '.deployment-trajectory-chart',
  '#deploymentOfflineReplayChart',
  '#trajCanvas',
]) {
  assert.equal(
    readEffectiveDeclarations(stylesSource, selector).background,
    'var(--chart-bg)',
    `${selector} must use the dedicated canvas palette`,
  );
}

console.log('web frontend regressions: ok');
