const STORAGE_KEY = 'cliperx-product-preferences';
export const DEFAULT_PRODUCT_PREFERENCES = Object.freeze({ theme:'system', gainColor:'green' });

export function normalizeProductPreferences(value = {}) {
  return { theme:['system','light','dark'].includes(value.theme) ? value.theme : 'system',
    gainColor:value.gainColor === 'red' ? 'red' : 'green' };
}

export function readProductPreferences(storage = globalThis.localStorage) {
  try { return normalizeProductPreferences(JSON.parse(storage?.getItem(STORAGE_KEY) || '{}')); }
  catch { return { ...DEFAULT_PRODUCT_PREFERENCES }; }
}

export function applyProductPreferences(preferences = readProductPreferences(), root = globalThis.document?.documentElement) {
  const value = normalizeProductPreferences(preferences);
  if (root) { root.dataset.theme = value.theme; root.dataset.gainColor = value.gainColor; }
  return value;
}

export function setProductPreference(key, value, storage = globalThis.localStorage) {
  const next = normalizeProductPreferences({ ...readProductPreferences(storage), [key]:value });
  try { storage?.setItem(STORAGE_KEY, JSON.stringify(next)); } catch {}
  applyProductPreferences(next);
  if (typeof window !== 'undefined') window.dispatchEvent(new CustomEvent('product-preferences-change', { detail:next }));
  return next;
}

export function watchProductPreferences() {
  if (typeof window === 'undefined') return () => {};
  const update = () => applyProductPreferences();
  update();
  window.addEventListener('storage', update);
  window.addEventListener('product-preferences-change', update);
  return () => { window.removeEventListener('storage', update); window.removeEventListener('product-preferences-change', update); };
}
