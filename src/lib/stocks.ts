import { readFileSync } from 'node:fs';

// B-grade name leads are deliberately curated. Catalogue tickers are not
// automatically keywords: generic words such as BTC created false relations.
type KeywordRule={ticker:string;terms:string[]};
type KeywordManifest={version:string;rules:KeywordRule[]};
const keywords:KeywordManifest=JSON.parse(readFileSync(new URL('../catalogues/stock-keywords.v1.json',import.meta.url),'utf8'));
const forbidden=new Set(['BTC','BITCOIN','比特币','ETH','ETHEREUM','AI','CRYPTO']);
const seen=new Set<string>();
for(const rule of keywords.rules){
  if(seen.has(rule.ticker)||!rule.terms.length||rule.terms.some(term=>forbidden.has(term.toUpperCase())))throw new Error('Invalid stock name keyword rule');
  seen.add(rule.ticker);
}

export interface StockMatch {
  ticker: string;
  matchType: 'exact'|'company';
  keyword: string;
  ruleVersion: string;
  level: 'B';
  evidenceStatus: 'name-only';
}

const escape=(word:string)=>word.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
function exactTerm(haystack:string,term:string):boolean {
  if(/[A-Za-z0-9]/.test(term))return new RegExp(`(?<![A-Za-z0-9])${escape(term)}(?![A-Za-z0-9])`,'i').test(haystack);
  // Written Chinese has no spaces between words. Match an exact curated
  // phrase, without fuzzy spelling or automatic prefix expansion.
  return haystack.includes(term);
}

export function matchStock(symbol:string,name:string,_knownTickers:Set<string>):StockMatch|null {
  for(const [haystack,matchType] of [[String(symbol??''),'exact'],[String(name??''),'company']] as const)
    for(const row of keywords.rules)for(const term of row.terms)
      if(exactTerm(haystack,term))return {ticker:row.ticker,matchType,keyword:term,
        ruleVersion:keywords.version,level:'B',evidenceStatus:'name-only'};
  return null;
}

export function buildTickerSet(underlyingSymbols:string[]):Set<string>{
  return new Set([...keywords.rules.map(row=>row.ticker),...underlyingSymbols.map(symbol=>String(symbol).trim().toUpperCase())]);
}
