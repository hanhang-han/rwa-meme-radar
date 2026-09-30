// Display formatting never changes the stored precision or the quote currency.
import { tr, useI18n } from '../i18n/index.js';

const missing = () => tr('—', '—');
const finite = (v) => v != null && v !== '' && Number.isFinite(Number(v));
const fixed = (v, digits) => Number(v).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });

export const usd = (v) => {
  if (!finite(v)) return missing();
  const n = Number(v);
  const abs = Math.abs(n);
  if (abs >= 1e9) return `$${fixed(n / 1e9, 2)}B`;
  if (abs >= 1e6) return `$${fixed(n / 1e6, 2)}M`;
  if (abs >= 1e3) return `$${fixed(n / 1e3, 1)}K`;
  return `$${fixed(n, 2)}`;
};

export const fullUsd = (v) => finite(v) ? `$${fixed(v, 2)}` : missing();

const SUBSCRIPT = '₀₁₂₃₄₅₆₇₈₉';
export const price = (v, currency = 'USD') => {
  if (!finite(v)) return missing();
  const n = Number(v);
  if (n === 0) return currency === 'USD' ? '$0.000' : `0.000 ${currency || ''}`.trim();
  const abs = Math.abs(n);
  let value;
  if (abs >= 1000) value = Math.trunc(n).toLocaleString('en-US');
  else if (abs < .001) {
    const [mantissa, exponentText] = abs.toExponential(3).split('e');
    const exponent = Number(exponentText);
    const zeroes = Math.max(1, -exponent - 1);
    const significant = mantissa.replace('.', '');
    const marker = zeroes >= 2 ? [...String(zeroes)].map(d => SUBSCRIPT[Number(d)]).join('') : '';
    value = zeroes >= 2 ? `0.0${marker}${significant}` : abs.toPrecision(4);
    if (n < 0) value = `-${value}`;
  } else value = n.toPrecision(4);
  return currency === 'USD' ? `$${value}` : `${value} ${currency || tr('币种未知', 'Currency unknown')}`;
};

export const num = (v) =>
  finite(v) ? Number(v).toLocaleString('en-US', { maximumFractionDigits: 0 }) : missing();

export const money = (v, currency) => {
  if (!finite(v)) return missing();
  if (currency === 'USD') return usd(v);
  return `${Number(v).toLocaleString('en-US', { maximumSignificantDigits: 8 })} ${currency || tr('币种未知', 'Currency unknown')}`;
};

export const pct = (v) => {
  if (!finite(v)) return missing();
  const n = Number(v);
  if (Math.abs(n) > 10000) return `${n < 0 ? '<' : '>'}10,000%`;
  return `${n >= 0 ? '+' : ''}${n.toFixed(1)}%`;
};

export const date = (v) => {
  if (!v || !Number.isFinite(new Date(v).getTime())) return missing();
  const { lang } = useI18n();
  const locale = lang.lang === 'en' ? 'en-US' : 'zh-CN';
  return new Date(v).toLocaleString(locale, { hour12: false });
};

export const age = (v) => {
  if (!v || !Number.isFinite(new Date(v).getTime())) return missing();
  const ms = Date.now() - new Date(v).getTime();
  if (ms < 60000) return tr('刚刚更新', 'Just updated');
  if (ms < 3600000) return tr(`${Math.floor(ms / 60000)} 分钟前`, `${Math.floor(ms / 60000)}m ago`);
  const { lang } = useI18n();
  return new Date(v).toLocaleString(lang.lang === 'en' ? 'en-US' : 'zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
};

export const short = (a) => (a ? String(a).length > 12 ? String(a).slice(0, 6) + '…' + String(a).slice(-4) : String(a) : '—');

export const fresh = (at, ageMs = 900000) => !!at && Date.now() - at < ageMs;

export const CHAIN_NAMES = { 196: 'X Layer', 56: 'BNB Chain', 4663: 'Robinhood Chain', 5042:'Arc' };
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
  `/asset/${encodeURIComponent(chain(a))}/${encodeURIComponent(a.token)}`;

export const pairLink = (r) =>
  `/pair/${encodeURIComponent(chain(r))}/${encodeURIComponent(r.stock)}?pool=${encodeURIComponent(r.pool)}`;

export const hashLink = (path) => `#${path}`;
