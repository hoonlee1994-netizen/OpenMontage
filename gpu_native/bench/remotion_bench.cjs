// Remotion render benchmark: measures wall time, frame render vs encode progress.
// usage: node remotion_bench.cjs <propsJson> <out.mp4> [binariesDir|-] [browser] [concurrency] [gl] [frames]
const path = require('path');
const fs = require('fs');
const composer = '/root/projects/openmontage/remotion-composer';
const { bundle } = require(composer + '/node_modules/@remotion/bundler');
const { selectComposition, renderMedia } = require(composer + '/node_modules/@remotion/renderer');

(async () => {
  const [propsPath, out, binDir, browser, conc, gl, frames] = process.argv.slice(2);
  const cacheFile = '/tmp/claude-0/-root-projects-openmontage/16281f80-c812-4b56-9176-f0120c37fc70/scratchpad/remotion-bundle.txt';
  let serveUrl = fs.existsSync(cacheFile) ? fs.readFileSync(cacheFile, 'utf8').trim() : '';
  if (!serveUrl || !fs.existsSync(serveUrl)) {
    const t = performance.now();
    serveUrl = await bundle({ entryPoint: path.join(composer, 'src/index.tsx'), publicDir: path.join(composer, 'public') });
    fs.writeFileSync(cacheFile, serveUrl);
    console.log('BUNDLE_S', ((performance.now() - t) / 1000).toFixed(2));
  }
  const inputProps = JSON.parse(fs.readFileSync(propsPath, 'utf8'));
  const base = {
    serveUrl, inputProps,
    browserExecutable: browser && browser !== '-' ? browser : '/usr/bin/chromium',
    timeoutInMilliseconds: 120000,
    chromiumOptions: gl && gl !== '-' ? { gl } : {},
    binariesDirectory: binDir && binDir !== '-' ? binDir : null,
    logLevel: process.env.RLOG || 'info',
  };
  const t0 = performance.now();
  const composition = await selectComposition({ ...base, id: 'Explainer' });
  const tSel = performance.now();
  let lastR = 0, lastE = 0, firstEncodeAt = null, renderDoneAt = null;
  const opts = {
    ...base, composition, codec: 'h264', outputLocation: out,
    concurrency: conc && conc !== '-' ? Number(conc) : null,
    onProgress: ({ renderedFrames, encodedFrames, stitchStage }) => {
      const now = performance.now();
      if (encodedFrames > 0 && firstEncodeAt === null) firstEncodeAt = now;
      if (renderedFrames === composition.durationInFrames && renderDoneAt === null) renderDoneAt = now;
      lastR = renderedFrames; lastE = encodedFrames;
    },
  };
  if (frames && frames !== '-') opts.frameRange = [0, Number(frames) - 1];
  await renderMedia(opts);
  const tEnd = performance.now();
  const n = opts.frameRange ? opts.frameRange[1] + 1 : composition.durationInFrames;
  console.log(JSON.stringify({
    frames: n, width: composition.width, height: composition.height, fps: composition.fps,
    select_s: +((tSel - t0) / 1000).toFixed(2),
    render_wall_s: +((tEnd - tSel) / 1000).toFixed(2),
    render_fps: +(n / ((tEnd - tSel) / 1000)).toFixed(2),
    all_frames_rendered_at_s: renderDoneAt ? +((renderDoneAt - tSel) / 1000).toFixed(2) : null,
  }));
})().catch((e) => { console.error(e.stack || e); process.exitCode = 1; });
