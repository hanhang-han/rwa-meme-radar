import { STOCK_LOGOS } from './stock-logo-catalogue.js';
import { TOKEN_LOGOS } from './token-logo-catalogue.js';
import { assetLogoIdentity } from '../api/asset-logos.js';
const base = import.meta.env?.BASE_URL || '/dashboard/';
const local = file => `${base}assets/asset-logos/${file}`;

export function stockThemeLogo(identity) {
  const match = /^stock:([a-z0-9.\-]{1,24})$/i.exec(String(identity || ''));
  const code = match?.[1].toUpperCase();
  return code && Object.hasOwn(STOCK_LOGOS, code) ? local(STOCK_LOGOS[code]) : '';
}

export function localTokenLogo(asset) {
  const key = assetLogoIdentity(asset);
  return key && Object.hasOwn(TOKEN_LOGOS, key) ? local(TOKEN_LOGOS[key]) : '';
}
