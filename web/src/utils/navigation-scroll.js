import { isNavigationReturn } from './navigation-context.js';

// A route's quote data stays live. Only its viewport and visible row identity
// are remembered, so delayed directory/group loading can restore the same row.
export function createNavigationScroll({ getWindow = () => typeof window === 'undefined' ? null : window,
  getDocument = () => typeof document === 'undefined' ? null : document, delay = ms => new Promise(resolve => setTimeout(resolve, ms)),
  now = Date.now, timeout = 6000 } = {}) {
  const positions = new Map();
  let navigation = 0;
  function capture(to, from) {
    navigation++;
    const win = getWindow(), doc = getDocument();
    if (!win || !doc || !from.fullPath || !from.matched?.length) return;
    let anchor;
    for (const node of doc.querySelectorAll('[data-navigation-anchor]')) {
      const rect = node.getBoundingClientRect();
      if (rect.height && rect.bottom > 0 && rect.top < win.innerHeight) {
        anchor = { id: node.getAttribute('data-navigation-anchor'), offset: rect.top }; break;
      }
    }
    positions.delete(from.fullPath);
    positions.set(from.fullPath, { left: win.scrollX, top: win.scrollY, anchor, at: now() });
    while (positions.size > 60) positions.delete(positions.keys().next().value);
  }
  async function scrollBehavior(to, from, saved) {
    const win = getWindow(), doc = getDocument();
    if (!win || !doc) return saved ?? { top: 0 };
    const previous = positions.get(to.fullPath);
    const remembered = previous && now() - previous.at < 30 * 60 * 1000 ? previous : null;
    const destination = saved ?? (isNavigationReturn(to, from) ? remembered : null);
    if (!destination) return to.path === from.path ? false : { top: 0 };
    if (!destination.top && !destination.anchor) return { left: destination.left ?? 0, top: 0 };
    const ticket = navigation, started = now();
    let cancelled = false;
    const cancel = () => { cancelled = true; };
    const events = ['wheel', 'touchstart', 'pointerdown', 'keydown'];
    events.forEach(event => win.addEventListener(event, cancel, { passive: true, once: true }));
    let stable = 0, lastHeight = -1, lastTop = -1, target = destination.top;
    try {
      while (now() - started < timeout) {
        if (cancelled || ticket !== navigation) return false;
        const height = Math.max(doc.documentElement.scrollHeight, doc.body?.scrollHeight ?? 0);
        // Browser history's saved coordinates take precedence over row anchoring.
        const anchor = !saved && destination.anchor ? [...doc.querySelectorAll('[data-navigation-anchor]')]
          .find(node => node.getAttribute('data-navigation-anchor') === destination.anchor.id) : null;
        if (anchor) target = Math.max(0, win.scrollY + anchor.getBoundingClientRect().top - destination.anchor.offset);
        const ready = height - win.innerHeight >= target && (!destination.anchor || saved || anchor);
        stable = ready && Math.abs(lastHeight - height) < 2 && Math.abs(lastTop - target) < 2 ? stable + 1 : 0;
        if (stable >= 3) return { left: destination.left ?? 0, top: target };
        lastHeight = height; lastTop = target;
        await delay(60);
      }
      if (cancelled || ticket !== navigation) return false;
      return { left: destination.left ?? 0, top: Math.min(target, Math.max(0, doc.documentElement.scrollHeight - win.innerHeight)) };
    } finally {
      events.forEach(event => win.removeEventListener(event, cancel));
    }
  }
  return { capture, scrollBehavior };
}
