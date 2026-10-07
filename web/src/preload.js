import { getStockDirectory } from './api/product.js';
import { stockDirectoryQuery } from './utils/stock-directory-query.js';

// This entry has no Vue dependency, so data can start loading while the app's
// larger entry downloads and initializes. Detail pages own separate requests.
export function preloadStockDirectory(hash = globalThis.location?.hash ?? '') {
  const [path, search = ''] = hash.replace(/^#/, '').split('?');
  if (!/^\/stocks?\/?$/.test(path)) return null;
  const { chain, options } = stockDirectoryQuery(Object.fromEntries(new URLSearchParams(search)));
  return getStockDirectory(chain, options).catch(() => null);
}

preloadStockDirectory();
