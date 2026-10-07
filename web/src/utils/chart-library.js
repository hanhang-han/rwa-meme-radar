// The vendor is needed only by mounted candle charts. Share one load across
// simultaneous charts, and clear failed attempts so a later retry can recover.
export function createChartLibraryLoader({
  document: documentRef,
  global: globalRef,
  baseUrl = '/',
  timeoutMs = 15_000,
  setTimer = globalThis.setTimeout,
  clearTimer = globalThis.clearTimeout,
}) {
  let pending = null;
  return function load() {
    if (globalRef?.LightweightCharts) return Promise.resolve(globalRef.LightweightCharts);
    if (pending) return pending;
    const attempt = new Promise((resolve, reject) => {
      let script;
      let timer;
      const cleanup = failed => {
        if (timer != null) clearTimer(timer);
        if (script) {
          script.onload = script.onerror = null;
          if (failed) script.remove();
        }
      };
      const fail = () => {
        cleanup(true);
        reject(new Error('chart-library-unavailable'));
      };
      try {
        script = documentRef.createElement('script');
        script.src = `${baseUrl.endsWith('/') ? baseUrl : `${baseUrl}/`}lightweight-charts.js`;
        script.async = true;
        script.onload = () => {
          if (!globalRef?.LightweightCharts) return fail();
          cleanup(false);
          resolve(globalRef.LightweightCharts);
        };
        script.onerror = fail;
        timer = setTimer(fail, timeoutMs);
        documentRef.head.appendChild(script);
      } catch {
        fail();
      }
    });
    pending = attempt;
    attempt.then(() => { pending = null; }, () => { pending = null; });
    return attempt;
  };
}

let browserLoader;
export function loadChartLibrary() {
  browserLoader ??= createChartLibraryLoader({
    document: globalThis.document,
    global: globalThis.window,
    baseUrl: import.meta.env?.BASE_URL ?? '/',
  });
  return browserLoader();
}
