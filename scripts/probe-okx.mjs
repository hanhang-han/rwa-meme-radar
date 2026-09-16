// Run on the application server; credentials remain in its private .env.
import { createHmac } from 'node:crypto';
process.loadEnvFile('.env');
for (const path of process.argv.slice(2)) {
  const timestamp = new Date().toISOString();
  const response = await fetch('https://web3.okx.com' + path, {
    headers: {
      'OK-ACCESS-KEY': process.env.OKX_API_KEY,
      'OK-ACCESS-PASSPHRASE': process.env.OKX_PASSPHRASE,
      'OK-ACCESS-TIMESTAMP': timestamp,
      'OK-ACCESS-SIGN': createHmac('sha256', process.env.OKX_SECRET_KEY).update(timestamp + 'GET' + path).digest('base64'),
    }, signal: AbortSignal.timeout(20000),
  });
  const data = await response.json();
  console.log(JSON.stringify({ path, http: response.status, code: data.code, msg: data.msg,
    sample: Array.isArray(data.data) ? data.data.slice(0, 2) : data.data?.list ? { ...data.data, list: data.data.list.slice(0, 2) } : data.data }));
  await new Promise(resolve => setTimeout(resolve, 1200));
}
