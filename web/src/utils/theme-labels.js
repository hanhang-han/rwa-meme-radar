const THEME_NAMES = {
  '芯片': 'Semiconductors',
  '交易所': 'Exchanges',
  '支付': 'Payments',
  '托管': 'Custody',
  '加密国库': 'Crypto treasuries',
};

export function themeLabel(name, language) {
  return language === 'en' ? THEME_NAMES[name] ?? name : name;
}
