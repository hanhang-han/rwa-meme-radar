<template>
  <div class="developer-page">
    <div class="developer-heading">
      <div>
        <p class="developer-eyebrow">CLIPERX API · {{ tr('邀请试用', 'Invited trial') }}</p>
        <h2>{{ tr('把关系证据接入你的产品', 'Bring relationship evidence into your product') }}</h2>
        <p>{{ tr('通过只读接口查询股票与 Meme 的关系、代币风险及最新公开快照。受邀试用免费，使用统一邀请码注册。', 'Read stock–Meme relationships, token risk checks, and the latest public snapshot. Invited users can register for free with the shared trial invitation code.') }}</p>
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
        <div><h3>{{ mode === 'register' ? tr('注册开发者账号', 'Create a developer account') : tr('登录开发者账号', 'Developer sign in') }}</h3><p>{{ tr('创建账号后可生成 API Key，并查看调用用量。', 'Create an account to issue API keys and view usage.') }}</p></div>
        <div class="developer-mode-switch" role="group" :aria-label="tr('账号操作', 'Account action')">
          <button type="button" :class="{ selected: mode === 'register' }" @click="setMode('register')">{{ tr('注册', 'Register') }}</button>
          <button type="button" :class="{ selected: mode === 'login' }" @click="setMode('login')">{{ tr('登录', 'Sign in') }}</button>
        </div>
      </div>
      <form class="developer-form" @submit.prevent="submitAccount">
        <label>{{ tr('邮箱', 'Email') }}<input v-model.trim="email" type="email" autocomplete="email" required maxlength="254" :placeholder="tr('你的邮箱', 'Your email')"></label>
        <label v-if="mode === 'register'">{{ tr('统一试用邀请码', 'Shared trial invitation code') }}<input v-model.trim="inviteCode" autocomplete="off" required maxlength="128" :placeholder="tr('请输入邀请码', 'Enter the invitation code')"></label>
        <label>{{ tr('密码', 'Password') }}<input v-model="password" type="password" :autocomplete="mode === 'register' ? 'new-password' : 'current-password'" required :minlength="mode === 'register' ? 12 : undefined"></label>
        <p v-if="mode === 'register'" class="developer-help">{{ tr('统一试用邀请码可供多位受邀者重复使用；密码至少 12 位。', 'The shared trial invitation code can be reused by multiple invitees. Use a password of at least 12 characters.') }}</p>
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
          <div><span>{{ tr('本 UTC 日调用', 'Calls today (UTC)') }}</span><strong>{{ usage?.used ?? '—' }} <small>/ {{ usage?.limit ?? session?.limits?.perDay ?? '—' }}</small></strong></div>
          <div><span>{{ tr('本 UTC 日剩余', 'Remaining today (UTC)') }}</span><strong>{{ usage?.remaining ?? '—' }}</strong></div>
          <div><span>{{ tr('速率上限', 'Rate limit') }}</span><strong>{{ session?.limits?.perMinute ?? '—' }} <small>{{ tr('次/分钟', 'req/min') }}</small></strong></div>
        </div>
        <p class="developer-help">{{ tr('试用配额按账号计算，每天 UTC 00:00（北京时间 08:00）重置。触及上限时接口返回 429 与 Retry-After。', 'Trial quotas are per account and reset daily at 00:00 UTC (08:00 China Standard Time). Exceeding a limit returns 429 with Retry-After.') }} <button class="developer-link-button" type="button" :disabled="busy" @click="refreshAccount">{{ tr('刷新用量', 'Refresh usage') }}</button></p>
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
      <div class="developer-account-head"><div><p class="developer-eyebrow">{{ tr('快速开始', 'Quick start') }}</p><h3>{{ tr('三个只读接口', 'Three read-only endpoints') }}</h3></div><span class="developer-version">v1</span></div>
      <p class="developer-help">{{ tr('使用 Authorization: Bearer API_KEY 请求。以下命令可复制到终端，把 YOUR_API_KEY 换成创建时保存的 Key。', 'Send Authorization: Bearer API_KEY. Copy a command below and replace YOUR_API_KEY with the key you saved.') }}</p>
      <div v-for="endpoint in endpoints" :key="endpoint.id" class="developer-endpoint">
        <div class="developer-endpoint-head"><span class="developer-method">GET</span><code>{{ endpoint.path }}</code></div>
        <h4>{{ tr(endpoint.zh, endpoint.en) }}</h4>
        <p>{{ tr(endpoint.descriptionZh, endpoint.descriptionEn) }}</p>
        <div class="developer-code"><code>{{ command(endpoint.path) }}</code><button type="button" :aria-label="tr('复制请求命令', 'Copy request command')" @click="copyText(command(endpoint.path), endpoint.id)">{{ copied === endpoint.id ? tr('已复制', 'Copied') : tr('复制', 'Copy') }}</button></div>
      </div>
      <p class="developer-help">{{ tr('关系结果区分已核验与名称线索；缺失数据为 null，不把未知写成零。行情和风险数据带有采集时间，可能延迟。', 'Relationships distinguish verified evidence from name candidates. Missing data is null, not zero. Market and risk data include observation times and may be delayed.') }}</p>
      <p class="developer-help">{{ tr('常见状态：401 Key 无效、429 触及限流、503 暂时不可用。试用接口可能调整字段，正式接入前请以返回内容为准。', 'Common statuses: 401 invalid key, 429 rate limit, 503 temporarily unavailable. Trial fields may change; inspect responses before production integration.') }}</p>
    </section>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { API_BASE } from '../api/client.js';
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
function command(path) { return `curl -H 'Authorization: Bearer YOUR_API_KEY' '${apiRoot.value}${path}'`; }
function displayDate(value) {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleString(lang.lang === 'en' ? 'en-US' : 'zh-CN');
}
function errorLabel(exception) {
  if (exception?.status === 429) return tr('操作太频繁，请稍后再试。', 'Too many requests. Please try again later.');
  if (exception?.status === 401) return tr('邮箱、密码或会话无效。', 'Invalid email, password, or session.');
  if (exception?.status === 409) return tr('这个邮箱已注册，请直接登录。', 'This email is already registered. Please sign in.');
  if (exception?.status === 400 || exception?.status === 403 || exception?.status === 422) return tr('请检查注册信息或试用码。', 'Please check your registration details or trial code.');
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
