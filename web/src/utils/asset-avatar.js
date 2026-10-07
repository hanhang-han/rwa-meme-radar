const PALETTE = [
  ['#e9eefb', '#435da4'], ['#e7f2ee', '#2f7160'],
  ['#f7ece5', '#975b36'], ['#eeeaf7', '#6b5298'],
  ['#e6f0f6', '#356b87'], ['#f5e9ee', '#985a72'],
];

export function avatarUrl(asset) {
  const value = asset?.logoUrl ?? asset?.tokenLogoUrl;
  if (typeof value !== 'string' || value.length > 2048) return '';
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
  } catch { return ''; }
}

export function avatarLabel(asset, { name = '', symbol = '' } = {}) {
  const value = String(symbol || asset?.symbol || asset?.tokenSymbol || name || asset?.name || asset?.tokenName || '?').trim();
  if (/^0x[0-9a-f]{8,}$/i.test(value)) return '?';
  const parts = [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(value)].map(part => part.segment);
  return parts.slice(0, 2).join('').toUpperCase() || '?';
}

export function avatarColors(asset, identity = '') {
  const stock = asset?.stockIdentity?.code || asset?.stockCode;
  const key = String(identity || (stock ? `stock:${stock}` : `${asset?.chainId || ''}:${asset?.token || asset?.tokenContractAddress || asset?.symbol || asset?.name || '?'}`)).toLowerCase();
  let hash = 0;
  for (const char of key) hash = (Math.imul(hash, 31) + char.codePointAt(0)) >>> 0;
  const [background, color] = PALETTE[hash % PALETTE.length];
  return { background, color };
}
