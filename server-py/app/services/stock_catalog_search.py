"""Search the stock directory by declared identity, never address fragments.

Company aliases mirror the display names in stock-theme-model.js. They make
those labels searchable; they do not verify an issuer or infer a stock from a
token's name or suffix.
"""
import re


# These labels are attached only to an existing stock code in the catalog.
COMPANY_ALIASES = {
    'AAPL': ('苹果', 'Apple'),
    'AMD': ('超威半导体', 'Advanced Micro Devices'),
    'AMZN': ('亚马逊', 'Amazon'),
    'BABA': ('阿里巴巴', 'Alibaba'),
    'COIN': ('Coinbase',),
    'GME': ('游戏驿站', 'GameStop'),
    'GOOGL': ('谷歌', 'Google', 'Alphabet'),
    'HOOD': ('Robinhood',),
    'INTC': ('英特尔', 'Intel'),
    'META': ('Meta',),
    'MSFT': ('微软', 'Microsoft'),
    'NVDA': ('英伟达', 'NVIDIA'),
    'NFLX': ('奈飞', 'Netflix'),
    'QQQ': ('纳斯达克100指数ETF', 'Nasdaq 100'),
    'SPY': ('标普500指数ETF', 'S&P 500'),
    'TSLA': ('特斯拉', 'Tesla'),
    'TSM': ('台积电', 'Taiwan Semiconductor'),
    'MU': ('美光科技', 'Micron'),
    'PLTR': ('帕兰提尔', 'Palantir'),
    '1': ('长江和记实业', 'CK Hutchison'),
    '700': ('腾讯控股', 'Tencent'),
    '1024': ('快手', 'Kuaishou'),
    '1038': ('长江基建', 'CK Infrastructure'),
    '1088': ('中国神华', 'China Shenhua'),
    '1093': ('石药集团', 'CSPC Pharmaceutical'),
    '1810': ('小米集团', 'Xiaomi'),
    '9992': ('泡泡玛特', 'Pop Mart'),
}
_ADDRESS = re.compile(r'0x[0-9a-f]{40}', re.IGNORECASE)
_NUMERIC = re.compile(r'[0-9]+')


def normalize_stock_ticker(value):
    """Keep missing and zero distinct and normalize HK display padding."""
    code = str(value if value is not None else '').strip().upper()
    return code.lstrip('0') or '0' if _NUMERIC.fullmatch(code) else code


def _declared_codes(row, versions):
    values = [row.get('ticker')]
    for token in versions:
        identity = token.get('stockIdentity') or {}
        values.extend((identity.get('code'), token.get('stockCode'), token.get('ticker')))
    return {code for value in values if (code := normalize_stock_ticker(value))}


def stock_catalog_matches(row, versions, query, known_tickers=()):
    """Match one theme and its deployments without changing catalog scope.

    ``known_tickers`` is the normalized set of codes in the scoped directory.
    When the query is a known code, its exact identity takes priority over a
    substring in another company's name. Company-name searches otherwise
    remain case-insensitive substring searches.
    """
    term = str(query if query is not None else '').strip().casefold()
    if not term:
        return True

    # All supported catalog chains use EVM addresses. A partial/malformed 0x
    # query must not accidentally match a name or some other contract.
    if term.startswith('0x'):
        if not _ADDRESS.fullmatch(term):
            return False
        addresses = [row.get('stockToken')]
        addresses.extend(token.get('tokenContractAddress') or token.get('token') for token in versions)
        return any(_ADDRESS.fullmatch(str(value or '')) and str(value).casefold() == term
                   for value in addresses)

    code = normalize_stock_ticker(term)
    codes = _declared_codes(row, versions)
    if _NUMERIC.fullmatch(term) or code in known_tickers or code in codes:
        return code in codes

    names = [row.get('name'), row.get('nameZh'), row.get('nameEn')]
    identity = row.get('stockIdentity') or {}
    names.extend(identity.get(field) for field in ('nameZh', 'nameEn'))
    for token in versions:
        names.extend(token.get(field) for field in ('tokenSymbol', 'tokenName', 'name', 'nameZh', 'nameEn'))
        names.extend((token.get('stockIdentity') or {}).get(field) for field in ('nameZh', 'nameEn'))
    for declared_code in codes:
        names.extend(COMPANY_ALIASES.get(declared_code, ()))
    return any(term in str(value or '').casefold() for value in names)
