'use strict';

const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');

const webRoot = __dirname;
const appPath = path.join(webRoot, 'app.js');
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
  let resolved = String(value || '').trim().toLowerCase();
  const seen = new Set();

  while (resolved.startsWith('var(')) {
    const variableMatch = resolved.match(/^var\((--[\w-]+)(?:,\s*([^)]+))?\)$/);
    assert.ok(variableMatch, `unsupported CSS variable color: ${resolved}`);
    const variableName = variableMatch[1];
    assert.equal(seen.has(variableName), false, `cyclic CSS variable: ${variableName}`);
    seen.add(variableName);
    resolved = String(variables[variableName] || variableMatch[2] || '').trim().toLowerCase();
  }

  const hexMatch = resolved.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i);
  assert.ok(hexMatch, `expected a solid hex color, received: ${resolved}`);
  const hex = hexMatch[1].length === 3
    ? [...hexMatch[1]].map((digit) => digit + digit).join('')
    : hexMatch[1];
  return [0, 2, 4].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16));
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
assert.equal(appSource.includes('setInterval(refreshDeploymentSession'), false);
assert.ok(appSource.includes('state.deploymentTimer = window.setTimeout(poll, 500)'));
assert.ok(appSource.includes('generation !== state.deploymentPollGeneration'));
assert.ok(appSource.includes('state.deploymentOfflineDatasetMeta = { path, metadata }'));
assert.ok(appSource.includes("addEventListener('click', () => loadDeploymentOfflineDataset())"));
assert.ok(appSource.includes('generation !== state.deploymentOfflineLoadGeneration'));
assert.ok(appSource.includes('state.deploymentOfflineRunning'));
assert.ok(appSource.includes('deploymentOfflineDatasetPath() === path'));
assert.ok(appSource.includes('cancelDeploymentOfflineChartDraw();'));
assert.ok(appSource.includes('$(selector)?.replaceChildren();'));
assert.ok(appSource.includes('renderKey !== state.deploymentModelIoRenderKey'));
assert.ok(appSource.includes('state.deploymentModelIoRenderKey = renderKey'));
assert.ok(appSource.includes('return mediaIdentity(state.dataset?.path, video, cameraKey);'));

const utilsSource = fs.readFileSync(utilsPath, 'utf8');
assert.ok(utilsSource.includes("JSON.stringify([String(datasetPath || ''), source])"));

const stylesSource = fs.readFileSync(stylesPath, 'utf8');
const rootTheme = readEffectiveDeclarations(stylesSource, ':root');
assert.equal(rootTheme['color-scheme'], 'light');

for (const variable of ['--bg', '--surface', '--surface-2', '--surface-3']) {
  assert.ok(
    relativeLuminance(resolveCssColor(rootTheme[variable], rootTheme)) >= 0.75,
    `${variable} must remain part of the light application palette`,
  );
}
assert.ok(
  relativeLuminance(resolveCssColor(rootTheme['--text'], rootTheme)) <= 0.25,
  '--text must remain readable on the light application palette',
);

const radius = Number.parseFloat(rootTheme['--radius']);
assert.ok(Number.isFinite(radius) && radius <= 2, 'pixel corners must stay square (2px or less)');
for (const variable of ['--shadow', '--shadow-soft']) {
  assert.match(
    rootTheme[variable],
    /^-?\d+(?:\.\d+)?px\s+-?\d+(?:\.\d+)?px\s+0\s+var\(--shadow-color\)$/,
    `${variable} must remain a hard, blur-free offset shadow`,
  );
}

function assertLightSurface(selector) {
  const declarations = readEffectiveDeclarations(stylesSource, selector);
  assert.ok(declarations.background, `${selector} must define its application-surface background`);
  assert.ok(
    relativeLuminance(resolveCssColor(declarations.background, rootTheme)) >= 0.70,
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
  const borderRadius = Number.parseFloat(readEffectiveDeclarations(stylesSource, selector)['border-radius']);
  assert.ok(
    Number.isFinite(borderRadius) && borderRadius <= 2,
    `${selector} must retain pixel-style square corners`,
  );
}

// Only logs, video media, and their camera frames remain dark islands in the light app shell.
for (const selector of [
  '.video-card',
  '.deployment-log-panel',
  '.deployment-camera-card',
]) {
  const background = readEffectiveDeclarations(stylesSource, selector).background;
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
  '#trajCanvas',
]) {
  assert.equal(
    readEffectiveDeclarations(stylesSource, selector).background,
    'var(--chart-bg)',
    `${selector} must use the dedicated canvas palette`,
  );
}

console.log('web frontend regressions: ok');
