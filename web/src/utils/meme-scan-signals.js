import { directoryAssetKey, directoryRelationState } from './meme-directory-presentation.js';
import { relationMatchesAsset } from './relations.js';
import { normalizeTicker } from './stock-theme-model.js';

// Same names never identify an asset. Keep the representative first, then
// preserve the loaded member order while deduplicating by chain and contract.
export function memeGroupMembers(representative, members = []) {
  const seen = new Set();
  return [representative, ...members].filter(asset => {
    if (!asset) return false;
    const key = directoryAssetKey(asset);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function memeStockRelations(asset = {}, relations = [], now = Date.now()) {
  const matches = relations.filter(relation => relationMatchesAsset(relation, asset));
  if (asset.match?.level === 'B' && asset.match.ticker) matches.push({ ...asset.match, level: 'B' });
  const rank = relation => {
    const state = directoryRelationState(relation, now);
    return state === 'current-pair' ? 0 : state === 'community' ? 2 : state === 'name-match' ? 3 : 1;
  };
  const seen = new Set();
  return matches.sort((a, b) => rank(a) - rank(b))
    .map(relation => ({ ...relation, ticker: normalizeTicker(relation.ticker) }))
    .filter(relation => {
      if (!relation.ticker || seen.has(relation.ticker)) return false;
      seen.add(relation.ticker);
      return true;
    });
}
