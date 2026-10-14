// Probe a Chromium executable's GPU path through Remotion's own openBrowser().
// usage: node browser_gpu_probe.cjs <executable> <gl|-> [extraFlags...]
const renderer = '/root/projects/openmontage/remotion-composer/node_modules/@remotion/renderer/dist';
const { openBrowser } = require(renderer + '/open-browser.js');
(async () => {
  const [exe, glArg] = process.argv.slice(2);
  const gl = glArg && glArg !== '-' ? glArg : null;
  let browser;
  try {
    browser = await openBrowser('chrome', {
      browserExecutable: exe,
      chromiumOptions: { gl, enableMultiProcessOnLinux: true },
      logLevel: process.env.RLOG || 'error',
    });
    const info = await browser.connection.send('SystemInfo.getInfo');
    const g = info.gpu || {};
    console.log('GPU_DEVICES', JSON.stringify((g.devices || []).map((d) => ({ vendor: d.vendorString, device: d.deviceString, driver: d.driverVendor + ' ' + d.driverVersion }))));
    console.log('FEATURE_STATUS', JSON.stringify(g.featureStatus));
    const aux = g.auxAttributes || {};
    console.log('AUX', JSON.stringify({ gl_renderer: aux.glRenderer, gl_vendor: aux.glVendor, gl_version: aux.glVersion, angle: aux.displayType, skia: aux.skiaBackendType, vulkan: aux.vulkanVersion }));
    const page = await browser.newPage({ context: null, logLevel: 'error', indent: false, pageIndex: 0, onBrowserLog: null, onLog: () => {} });
    const res = await page.evaluate(() => {
      const c = document.createElement('canvas'); c.width = 16; c.height = 16;
      const g = c.getContext('webgl2') || c.getContext('webgl');
      if (!g) return { available: false };
      const dbg = g.getExtension('WEBGL_debug_renderer_info');
      g.clearColor(0.25, 0.5, 0.75, 1); g.clear(g.COLOR_BUFFER_BIT);
      const px = new Uint8Array(4); g.readPixels(0, 0, 1, 1, g.RGBA, g.UNSIGNED_BYTE, px);
      return { available: true, vendor: dbg && g.getParameter(dbg.UNMASKED_VENDOR_WEBGL), renderer: dbg && g.getParameter(dbg.UNMASKED_RENDERER_WEBGL), pixel: Array.from(px), err: g.getError() };
    });
    console.log('WEBGL', JSON.stringify(res));
  } finally {
    if (browser) await browser.close({ silent: true });
  }
})().catch((e) => { console.error('PROBE_ERROR', e.message.split('\n').slice(0, 6).join(' | ')); process.exitCode = 1; });
