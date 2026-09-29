import { onMounted, onUnmounted, readonly, ref } from 'vue';

const now = ref(Date.now());
let subscribers = 0;
let timer = null;

function start() {
  if (timer != null || typeof window === 'undefined') return;
  now.value = Date.now();
  timer = window.setInterval(() => { now.value = Date.now(); }, 15_000);
}

function stop() {
  if (timer == null || typeof window === 'undefined') return;
  window.clearInterval(timer);
  timer = null;
}

export function useMinuteClock() {
  onMounted(() => {
    subscribers += 1;
    start();
  });
  onUnmounted(() => {
    subscribers = Math.max(0, subscribers - 1);
    if (!subscribers) stop();
  });
  return readonly(now);
}
