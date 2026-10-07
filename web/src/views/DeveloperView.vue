<template>
  <div class="developer-page">
    <div class="developer-heading">
      <div>
        <p class="developer-eyebrow">CliperX API</p>
        <h2>{{ tr('股票与 Meme 数据 API', 'Stock and Meme data API') }}</h2>
        <p>{{ tr('查询股票与 Meme 配对、代币风险及数据快照。', 'Query stock–Meme pairs, token risks and data snapshots.') }}</p>
      </div>
      <span class="developer-beta">{{ tr('邀请试用', 'Invited trial') }}</span>
    </div>

    <section v-if="state === 'loading'" class="panel developer-account" role="status">{{ tr('正在读取开发者账号…', 'Loading your developer account…') }}</section>
    <section v-else-if="state === 'unavailable'" class="panel developer-account" role="alert">
      <p>{{ tr('开发者服务暂时无法连接，请稍后重试。', 'The developer service is temporarily unavailable. Please try again.') }}</p>
      <button class="developer-button" type="button" @click="loadSession">{{ tr('重试', 'Retry') }}</button>
    </section>
    <section v-else-if="state === 'guest'" class="panel developer-account">
      <div class="developer-account-head">
        <div><h3>{{ mode === 'register' ? tr('注册', 'Register') : tr('登录', 'Sign in') }}</h3></div>
        <div class="developer-mode-switch" role="group" :aria-label="tr('账号操作', 'Account action')">
          <button type="button" :class="{ selected: mode === 'register' }" @click="setMode('register')">{{ tr('注册', 'Register') }}</button>
          <button type="button" :class="{ selected: mode === 'login' }" @click="setMode('login')">{{ tr('登录', 'Sign in') }}</button>
        </div>
      </div>
      <form class="developer-form" @submit.prevent="submitAccount">
        <label>{{ tr('邮箱', 'Email') }}<input v-model.trim="email" type="email" autocomplete="email" required maxlength="254"></label>
        <label v-if="mode === 'register'">{{ tr('邀请码', 'Invitation code') }}<input v-model.trim="inviteCode" autocomplete="off" required maxlength="128"></label>
        <label>{{ tr('密码', 'Password') }}<input v-model="password" type="password" :autocomplete="mode === 'register' ? 'new-password' : 'current-password'" required :minlength="mode === 'register' ? 12 : undefined"></label>
        <p v-if="mode === 'register'" class="developer-help">{{ tr('密码至少 12 位。', 'Use a password of at least 12 characters.') }}</p>
        <p v-if="error" class="developer-error" role="alert">{{ error }}</p>
        <button class="developer-button developer-primary" type="submit" :disabled="busy">{{ busy ? tr('请稍候…', 'Please wait…') : mode === 'register' ? tr('注册并进入', 'Create account') : tr('登录', 'Sign in') }}</button>
      </form>
    </section>

    <template v-else-if="state === 'ready'">
      <section class="panel developer-account">
        <div class="developer-account-head">
          <div><p class="developer-eyebrow">{{ tr('开发者账号', 'Developer account') }}</p><h3>{{ session?.user?.email }}</h3></div>
          <button class="developer-button developer-subtle" type="button" :disabled="busy" @click="signOut">{{ tr('退出登录', 'Sign out') }}</button>
        </div>
        <p v-if="error" class="developer-error" role="alert">{{ error }}</p>
        <div class="developer-usage">
          <div><span>{{ tr('今日调用', 'Calls today') }}</span><strong :title="tr('当天已计入配额的API请求数 / 每日请求上限；北京时间08:00重置','API calls counted toward today’s quota / daily limit; resets at 00:00 UTC')">{{ usage?.used ?? '—' }} <small>/ {{ usage?.limit ?? session?.limits?.perDay ?? '—' }}</small></strong></div>
          <div><span>{{ tr('今日剩余', 'Remaining today') }}</span><strong :title="tr('当天可用的剩余请求配额；缺失值不表示已用完','Remaining daily request quota; missing does not mean exhausted')">{{ usage?.remaining ?? '—' }}</strong></div>
          <div><span>{{ tr('速率上限', 'Rate limit') }}</span><strong :title="tr('每分钟允许的请求次数','Allowed requests per minute')">{{ session?.limits?.perMinute ?? '—' }} <small>{{ tr('次/分钟', 'req/min') }}</small></strong></div>
        </div>
        <div v-if="usage?.limit > 0" class="developer-usage-track" role="progressbar" :aria-label="tr('今日调用进度','Calls used today')" :aria-valuenow="Math.max(0,Math.min(Number(usage.used) || 0, Number(usage.limit)))" :aria-valuemax="Number(usage.limit)"><span :style="{width:usageWidth}"></span></div>
        <p class="developer-help">{{ tr('每天北京时间 08:00 重置。', 'Resets daily at 00:00 UTC.') }} <button class="developer-link-button" type="button" :disabled="busy" @click="refreshAccount">{{ tr('刷新用量', 'Refresh usage') }}</button></p>
      </section>

      <section class="panel developer-keys">
        <div class="developer-account-head"><div><h3>API Key</h3><p>{{ tr('仅用于服务端调用。Key 创建后只显示一次。', 'Use keys only on your server. The secret is shown once when created.') }}</p></div></div>
        <div v-if="newSecret" class="developer-secret" role="status">
          <div><strong>{{ tr('请立即保存新 Key', 'Save your new key now') }}</strong><p>{{ tr('关闭后无法再查看；不要放入前端代码或公开仓库。', 'It cannot be viewed again after closing. Never put it in frontend code or a public repository.') }}</p></div>
          <code>{{ newSecret }}</code>
          <div class="developer-actions"><button class="developer-button" type="button" @click="copyText(newSecret, 'secret')">{{ copied === 'secret' ? tr('已复制', 'Copied') : tr('复制 Key', 'Copy key') }}</button><button class="developer-button developer-subtle" type="button" @click="newSecret = ''">{{ tr('我已保存，关闭', 'Saved, close') }}</button></div>
        </div>
        <form class="developer-key-form" @submit.prevent="issueKey">
          <label>{{ tr('Key 名称', 'Key name') }}<input v-model.trim="keyName" maxlength="60" required :placeholder="tr('例如：我的研究服务', 'e.g. Research service')"></label>
          <button class="developer-button developer-primary" type="submit" :disabled="busy || !!newSecret">{{ tr('创建 Key', 'Create key') }}</button>
        </form>
        <div v-if="keys.length" class="developer-key-list">
          <div v-for="key in keys" :key="key.id" class="developer-key-row">
            <div><strong>{{ key.name }}</strong><code>{{ key.prefix }}…</code><small>{{ tr('创建于', 'Created') }} {{ displayDate(key.createdAt) }}<template v-if="key.lastUsedAt"> · {{ tr('最近调用', 'Last used') }} {{ displayDate(key.lastUsedAt) }}</template></small></div>
            <span v-if="key.revokedAt" class="developer-revoked">{{ tr('已撤销', 'Revoked') }}</span>
            <button v-else class="developer-button developer-danger" type="button" :disabled="busy" @click="revokeKey(key)">{{ tr('撤销', 'Revoke') }}</button>
          </div>
        </div>
        <p v-else class="developer-help">{{ tr('还没有 API Key。创建后复制到你的服务端环境变量。', 'No API key yet. Create one and store it in your server environment.') }}</p>
      </section>
    </template>

    <section class="panel developer-docs">
      <div class="developer-account-head"><div><h3>{{ tr('接口文档', 'API reference') }}</h3></div><span class="developer-version">v1</span></div>
      <p class="developer-help">{{ tr('复制命令前，将 YOUR_API_KEY 换成创建时保存的 Key。', 'Replace YOUR_API_KEY with your saved key before using a command.') }}</p>
      <div v-for="(endpoint,index) in endpoints" :key="endpoint.id" class="developer-endpoint">
        <span class="developer-endpoint-number" aria-hidden="true">0{{ index + 1 }}</span>
        <div class="developer-endpoint-head"><span class="developer-method">GET</span><code>{{ endpoint.path }}</code></div>
        <h4>{{ tr(endpoint.zh, endpoint.en) }}</h4>
        <p>{{ tr(endpoint.descriptionZh, endpoint.descriptionEn) }}</p>
        <div class="developer-code"><code>{{ command(endpoint.path) }}</code><button type="button" :aria-label="tr('复制请求命令', 'Copy request command')" @click="copyText(command(endpoint.path), endpoint.id)">{{ copied === endpoint.id ? tr('已复制', 'Copied') : tr('复制', 'Copy') }}</button></div>
      </div>
      <details class="developer-help"><summary>{{ tr('数据口径与错误码', 'Data notes and errors') }}</summary><p>{{ tr('关系区分已核验配对和名称匹配；缺失值为 null，行情与风险数据附更新时间。', 'Relationships separate verified pairs from name matches. Missing values are null; quotes and risk data include timestamps.') }}</p><p>{{ tr('401：Key 无效 · 429：触及限流 · 503：暂不可用。试用字段可能调整。', '401: invalid key · 429: rate limit · 503: unavailable. Trial fields may change.') }}</p></details>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { API_BASE } from '../api/client.js';
import {useAccountStore} from '../stores/account.js';
const account=useAccountStore();
import { createDeveloperKey, getDeveloperSession, getDeveloperUsage, listDeveloperKeys, loginDeveloper, logoutDeveloper, registerDeveloper, revokeDeveloperKey } from '../api/developer.js';
import { tr, useI18n } from '../i18n';

const lang = useI18n().lang;
const state = ref('loading');
const mode = ref('register');
const email = ref('');
const inviteCode = ref('');
const password = ref('');
const keyName = ref('');
const session = ref(null);
const keys = ref([]);
const usage = ref(null);
const newSecret = ref('');
const error = ref('');
const busy = ref(false);
const copied = ref('');
let copyTimer;

const endpoints = [
  { id: 'relations', path: '/relations?stock=NVDA', zh: '股票与 Meme 关系', en: 'Stock–Meme relationships', descriptionZh: '按股票代码查找对应代币与关系证据。', descriptionEn: 'Find tokens and relationship evidence by stock ticker.' },
  { id: 'risk', path: '/token/196/REPLACE_TOKEN_ADDRESS/risk', zh: '代币风险', en: 'Token risk', descriptionZh: '将示例中的链 ID 与合约地址换成目标代币。', descriptionEn: 'Replace the example chain ID and contract address with your token.' },
  { id: 'snapshot', path: '/snapshot/latest', zh: '最新公开快照', en: 'Latest public snapshot', descriptionZh: '获取当前发布版本、覆盖与更新时间。', descriptionEn: 'Get the current published revision, coverage, and update time.' },
];
const apiRoot = computed(() => `${window.location.origin}${API_BASE}v1`);
const usageWidth = computed(() => {
  const used = Number(usage.value?.used), limit = Number(usage.value?.limit);
  return Number.isFinite(used) && Number.isFinite(limit) && limit > 0 ? `${Math.max(0,Math.min(100,used / limit * 100))}%` : '0%';
});
function command(path) { return `curl -H 'Authorization: Bearer YOUR_API_KEY' '${apiRoot.value}${path}'`; }
function displayDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString(lang.lang === 'en' ? 'en-US' : 'zh-CN');
}
function errorLabel(exception) {
  if (exception?.message === 'invalid-invitation') return tr('邀请码无效，请核对后重试。', 'Invalid invitation code. Check it and try again.');
  if (exception?.message === 'weak-password') return tr('密码至少需要 12 位。', 'Password must have at least 12 characters.');
  if (exception?.status === 429) return tr('操作太频繁，请稍后再试。', 'Too many requests. Please try again later.');
  if (exception?.status === 401) return tr('邮箱、密码或会话无效。', 'Invalid email, password, or session.');
  if (exception?.status === 409) return tr('这个邮箱已注册，请直接登录。', 'This email is already registered. Please sign in.');
  if (exception?.status === 400 || exception?.status === 403 || exception?.status === 422) return tr('请检查邀请码或注册信息。', 'Please check the invitation code or registration details.');
  return tr('请求失败，请稍后重试。', 'Request failed. Please try again.');
}
function setMode(next) { mode.value = next; error.value = ''; password.value = ''; }
async function loadAccount() {
  const [keyResponse, usageResponse] = await Promise.all([listDeveloperKeys(), getDeveloperUsage()]);
  keys.value = keyResponse?.items ?? [];
  usage.value = usageResponse;
}
async function loadSession() {
  error.value = '';
  state.value = 'loading';
  try {
    session.value = await getDeveloperSession();
    state.value = 'ready';
    await loadAccount();
  } catch (exception) {
    if (exception?.status === 401) { session.value = null; state.value = 'guest'; }
    else { state.value = session.value ? 'ready' : 'unavailable'; error.value = errorLabel(exception); }
  }
}
async function submitAccount() {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try {
    session.value = mode.value === 'register' ? await registerDeveloper(email.value, inviteCode.value, password.value) : await loginDeveloper(email.value, password.value);
    await account.useSession(session.value);
    password.value = ''; inviteCode.value = '';
    state.value = 'ready';
    await loadAccount();
  } catch (exception) { error.value = errorLabel(exception); }
  finally { busy.value = false; }
}
async function signOut() {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try {
    await logoutDeveloper(session.value?.csrfToken);
    await account.start(true);
    session.value = null; keys.value = []; usage.value = null; newSecret.value = ''; password.value = '';
    state.value = 'guest';
  } catch (exception) { error.value = errorLabel(exception); }
  finally { busy.value = false; }
}
async function refreshAccount() {
  if (busy.value) return;
  busy.value = true; error.value = '';
  try { await loadAccount(); }
  catch (exception) { error.value = errorLabel(exception); }
  finally { busy.value = false; }
}
async function issueKey() {
  if (busy.value || newSecret.value) return;
  busy.value = true; error.value = '';
  try {
    const result = await createDeveloperKey(keyName.value, session.value?.csrfToken);
    newSecret.value = result?.secret ?? '';
    keyName.value = '';
    await loadAccount();
  } catch (exception) { error.value = exception?.status === 409 ? tr('已达到有效 Key 数量上限，请先撤销不用的 Key。', 'You have reached the active key limit. Revoke an unused key first.') : errorLabel(exception); }
  finally { busy.value = false; }
}
async function revokeKey(key) {
  if (busy.value || !window.confirm(tr(`确定撤销“${key.name}”吗？使用它的程序会立即无法访问。`, `Revoke “${key.name}”? Requests using it will stop working immediately.`))) return;
  busy.value = true; error.value = '';
  try { await revokeDeveloperKey(key.id, session.value?.csrfToken); await loadAccount(); }
  catch (exception) { error.value = errorLabel(exception); }
  finally { busy.value = false; }
}
async function copyText(value, id) {
  try {
    await navigator.clipboard.writeText(value);
    copied.value = id;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => { copied.value = ''; }, 1800);
  } catch { error.value = tr('复制失败，请手动复制。', 'Copy failed. Please copy manually.'); }
}
onMounted(loadSession);
onUnmounted(() => { clearTimeout(copyTimer); newSecret.value = ''; });
</script>

<style scoped>
.developer-page { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:20px; max-width:1150px; margin:0 auto; align-items:start; }
.developer-heading { grid-column:1/-1; display:flex; justify-content:space-between; align-items:flex-start; gap:20px; padding:0 0 4px; }
.developer-heading h2 { margin:6px 0 0; font-family:inherit; font-size:26px; font-weight:650; letter-spacing:-.025em; }
.developer-heading p:last-child { margin:8px 0 0; max-width:600px; color:var(--muted); font-size:12px; line-height:1.6; }
.developer-eyebrow { margin:0 0 6px; color:var(--accent); font-size:10px; font-weight:650; letter-spacing:.12em; }
.developer-beta,.developer-version { flex:none; padding:6px 10px; border:1px solid var(--border); border-radius:6px; background:var(--accent-soft); color:var(--accent); font-size:11px; }
.developer-page section.panel { min-width:0; margin:0; padding:22px; border:1px solid var(--border); border-radius:11px; background:var(--panel); overflow:visible; }
.developer-account-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:18px; }
.developer-account-head h3 { margin:0; font-family:inherit; font-size:16px; font-weight:650; overflow-wrap:anywhere; }
.developer-account-head p { margin:7px 0 0; font-size:12px; color:var(--muted); line-height:1.6; }
.developer-mode-switch { display:flex; gap:3px; flex:none; padding:3px; border-radius:7px; background:var(--bg); }
.developer-mode-switch button { padding:7px 10px; border:0; border-radius:5px; background:transparent; color:var(--muted); cursor:pointer; font:inherit; font-size:11px; }
.developer-mode-switch button.selected { background:var(--panel); color:var(--accent); box-shadow:0 1px 3px #1727380d; }
.developer-form { display:grid; gap:15px; max-width:620px; }
.developer-form label,.developer-key-form label { display:grid; gap:7px; min-width:0; color:var(--muted); font-size:12px; }
.developer-form input,.developer-key-form input { min-width:0; width:100%; min-height:40px; padding:9px 12px; border:1px solid var(--border); border-radius:7px; background:var(--bg); color:var(--text); font:inherit; font-size:13px; }
.developer-form input:focus-visible,.developer-key-form input:focus-visible { outline:2px solid var(--accent-soft); border-color:var(--accent); }
.developer-button { width:max-content; max-width:100%; min-height:36px; padding:8px 14px; border:1px solid var(--border); border-radius:7px; background:var(--panel); color:var(--text); font:inherit; font-size:12px; cursor:pointer; }
.developer-button:hover:not(:disabled) { border-color:var(--accent); }
.developer-button:disabled { opacity:.55; cursor:wait; }
.developer-primary { border-color:var(--accent); background:var(--accent); color:#fff; font-weight:600; }
.developer-danger,.developer-error { color:var(--down); }
.developer-help { color:var(--muted); font-size:12px; line-height:1.7; }
.developer-link-button { border:0; background:none; color:var(--accent); font:inherit; cursor:pointer; }
.developer-usage { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
.developer-usage>div { display:grid; gap:6px; min-width:0; padding:13px 12px; border:1px solid var(--border); border-radius:7px; background:var(--bg); }
.developer-usage span { color:var(--muted); font-size:11px; }
.developer-usage strong { font-family:var(--number-font,inherit); font-size:24px; font-weight:600; font-variant-numeric:tabular-nums; }
.developer-usage small { color:var(--muted); font-size:10px; font-weight:400; }
.developer-usage-track { height:5px; margin-top:12px; background:var(--border); overflow:hidden; border-radius:4px; }
.developer-usage-track span { display:block; height:100%; background:var(--accent); }
.developer-key-form { display:flex; align-items:flex-end; flex-wrap:wrap; gap:12px; max-width:620px; margin:18px 0; }
.developer-key-form label { flex:1; }
.developer-key-row { display:flex; align-items:center; justify-content:space-between; gap:15px; padding:15px 0; border-top:1px solid var(--border); }
.developer-key-row>div { display:grid; gap:5px; min-width:0; }
.developer-key-row strong { font-size:12px; font-weight:600; }
.developer-key-row code { color:var(--accent); font-size:12px; overflow-wrap:anywhere; }
.developer-key-row small,.developer-revoked { font-size:11px; color:var(--muted); }
.developer-secret { padding:15px; border:1px solid var(--accent); border-radius:8px; background:var(--accent-soft); }
.developer-secret strong { font-size:13px; }
.developer-secret p { color:var(--muted); font-size:12px; line-height:1.6; }
.developer-secret code { display:block; padding:11px; border:1px solid var(--border); border-radius:6px; background:var(--panel); color:var(--text); overflow-wrap:anywhere; font-size:12px; }
.developer-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
.developer-docs { grid-column:1/-1; }
.developer-docs .developer-account-head { margin-bottom:6px; }
.developer-endpoint { display:grid; grid-template-columns:28px minmax(0,1fr); gap:0 14px; padding:19px 0; border-top:1px solid var(--border); }
.developer-endpoint-number { grid-column:1; grid-row:1/5; padding-top:3px; color:var(--accent); font-size:12px; font-variant-numeric:tabular-nums; }
.developer-endpoint-head,.developer-endpoint h4,.developer-endpoint p,.developer-code { grid-column:2; }
.developer-endpoint-head { display:flex; flex-wrap:wrap; align-items:center; gap:9px; min-width:0; }
.developer-endpoint-head code { font-size:12px; overflow-wrap:anywhere; }
.developer-method { padding:3px 6px; border:1px solid var(--border); border-radius:4px; color:var(--up); font-size:10px; font-weight:600; }
.developer-endpoint h4 { margin:10px 0 4px; font-family:inherit; font-size:14px; font-weight:600; }
.developer-endpoint p { margin:0 0 12px; color:var(--muted); font-size:12px; line-height:1.6; }
.developer-code { display:flex; align-items:flex-start; gap:12px; padding:13px; border:1px solid var(--border); border-radius:7px; background:var(--bg); min-width:0; }
.developer-code code { flex:1; min-width:0; color:var(--text); font-size:11px; line-height:1.7; white-space:pre-wrap; overflow-wrap:anywhere; }
.developer-code button { border:0; background:none; color:var(--accent); cursor:pointer; font-size:11px; white-space:nowrap; }
.developer-page :is(button,a,summary):focus-visible { outline:2px solid var(--accent); outline-offset:3px; }
@media(max-width:950px) { .developer-page { grid-template-columns:minmax(0,1fr); gap:16px; } }
@media(max-width:600px) { .developer-heading { gap:12px; } .developer-heading h2 { font-size:23px; } .developer-page section.panel { padding:18px; } .developer-account-head { flex-wrap:wrap; } .developer-usage { gap:8px; } .developer-usage>div { padding:12px 8px; } .developer-usage strong { font-size:21px; } .developer-endpoint { grid-template-columns:19px minmax(0,1fr); column-gap:8px; } .developer-code { padding:11px; } }
</style>
