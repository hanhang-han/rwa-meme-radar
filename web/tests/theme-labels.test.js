import test from 'node:test';
import assert from 'node:assert/strict';
import { themeLabel } from '../src/utils/theme-labels.js';

test('basket display names switch to English without changing source labels', () => {
  const sectors=['芯片','交易所','支付','托管','加密国库'];
  assert.deepEqual(sectors.map(name=>themeLabel(name,'en')),
    ['Semiconductors','Exchanges','Payments','Custody','Crypto treasuries']);
  assert.deepEqual(sectors.map(name=>themeLabel(name,'zh')),sectors);
  assert.equal(themeLabel('新主题','en'),'新主题');
  assert.deepEqual(sectors,['芯片','交易所','支付','托管','加密国库']);
});
