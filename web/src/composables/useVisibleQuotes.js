import { onMounted, onUnmounted, reactive, ref, unref, watch } from 'vue';
import { API_BASE } from '../api/client.js';
import { visibleQuoteAt, visibleQuoteTokens } from '../utils/visible-quotes.js';
import { createVisibleQuoteStream } from '../utils/visible-quotes-stream.js';

export function useVisibleQuotes({ rows, onQuote, enabled = true, getCursor, onReset }) {
  const status = reactive({ connected: false, state: 'connecting', lastMessageAt: 0 });
  const lastQuoteAt = ref(0);
  let connection, stopWatch, mounted = false;
  function reconnect() {
    connection?.stop(); connection = null;
    if (!mounted || !unref(enabled) || document.hidden) { status.state = 'paused'; return; }
    const tokens = visibleQuoteTokens(unref(rows));
    if (!tokens.length) { status.state = 'idle'; return; }
    connection = createVisibleQuoteStream({ apiBase: API_BASE, tokens, getCursor, onReset,
      onStatus: patch => Object.assign(status, patch), onQuote: packet => {
        if (onQuote?.(packet) === true) lastQuoteAt.value = Math.max(lastQuoteAt.value, visibleQuoteAt(packet));
      } });
    connection.start();
  }
  onMounted(() => {
    mounted = true;
    stopWatch = watch(() => `${!!unref(enabled)}:${visibleQuoteTokens(unref(rows)).join(',')}`, () => {
      lastQuoteAt.value = 0; reconnect();
    }, { immediate: true });
    document.addEventListener('visibilitychange', reconnect);
  });
  onUnmounted(() => {
    mounted = false; stopWatch?.(); connection?.stop();
    document.removeEventListener('visibilitychange', reconnect);
  });
  return { status, lastQuoteAt };
}
