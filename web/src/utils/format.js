// Shared formatters, ported from radar-v2.js so view markup stays identical
// in output. All take null-safe inputs and honor the current language via tr.
import { tr } from '../i18n';

export const usd = (v) => {
  if (v == null || !Number.isFinite(Number(v))) return tr('待采集', 'Pending');
  const n = Number(v);
  return '$' + n.toLocaleString('en-US', { maximumFractionDigits: n > 0 && n < 0.01 ? 8 : 2 });
};

export const num = (v) =>
  v == null ? tr('待采集', 'Pending') : Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 });

export const pct = (v) => {
  if (v == null || !Number.isFinite(Number(v))) return tr('待采集', 'Pending');
  const n = Number(v);
  return `${n >= 0 ? '+' : ''}${n.toFixed(2)}%`;
};

export const date = (v) => {
  if (!v) return tr('尚未采集', 'Not collected');
  const locale = typeof window !== 'undefined' && window.__LANG === 'en' ? 'en-US' : 'zh-CN';
  return new Date(v).toLocaleString(locale, { hour12: false });
};

export const age = (v) => {
  if (!v) return tr('待更新', 'Pending');
  const ms = Date.now() - v;
  if (ms < 60000) return tr('刚刚更新', 'Just updated');
  return tr(`${Math.floor(ms / 60000)} 分钟前`, `${Math.floor(ms / 60000)}m ago`);
};

export const short = (a) => (a ? a.slice(0, 7) + '…' + a.slice(-5) : '—');

export const fresh = (at, ageMs = 900000) => !!at && Date.now() - at < ageMs;

export const CHAIN_NAMES = { 196: 'X Layer', 56: 'BNB Smart Chain', 4663: 'Robinhood Chain' };
export const chainName = (a) => CHAIN_NAMES[a?.chainId ?? a?.chain ?? '196'] ?? '—';

export const chain = (a) => String(a?.chainId ?? a?.chain ?? '196');

export const explorer = (a, type = 'address', cid = '196') =>
  cid === '4663'
    ? `https://robinhoodchain.blockscout.com/${type === 'tx' ? 'tx' : 'address'}/${encodeURIComponent(a)}`
    : cid === '56'
      ? `https://www.oklink.com/bsc/${type}/${encodeURIComponent(a)}`
      : `https://www.oklink.com/xlayer/${type}/${encodeURIComponent(a)}`;

// Router paths (usable with RouterLink :to and router.push). For plain
// <a href> anchors use hashLink below.
export const detailLink = (a) =>
  `/detail/${encodeURIComponent(chain(a))}/${encodeURIComponent(a.token)}`;

export const pairLink = (r) =>
  `/pair/${encodeURIComponent(chain(r))}/${encodeURIComponent(r.stock)}?pool=${encodeURIComponent(r.pool)}`;

export const hashLink = (path) => `#${path}`;
