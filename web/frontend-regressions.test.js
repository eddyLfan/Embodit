'use strict';

const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');

const webRoot = __dirname;
const appPath = path.join(webRoot, 'app.js');
const utilsPath = path.join(webRoot, 'utils.js');

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

console.log('web frontend regressions: ok');
