<template>
  <div class="account-page">
    <section class="panel">
      <div class="panel-head"><h2>{{ tr('我的', 'My account') }}</h2><RouterLink to="/watch">{{ tr('我的关注', 'My watchlist') }} →</RouterLink></div>
      <p v-if="!account.ready" role="status">{{ tr('加载中…','Loading…') }}</p>
      <template v-else-if="account.session">
        <p>{{ account.session.user.email }}</p>
        <div class="account-actions"><span class="hint">{{ tr('关注列表可在登录后同步到其他设备。','Your watchlist syncs across signed-in devices.') }}</span><button :disabled="busy" @click="signOut">{{ tr('退出登录','Sign out') }}</button></div>
        <RouterLink v-if="account.session.operator" to="/status">{{ tr('数据运行状态','Data operations') }} →</RouterLink>
      </template>
      <form v-else class="account-form" @submit.prevent="submit">
        <div class="view-tabs"><button type="button" :class="{active:mode==='login'}" @click="mode='login'">{{ tr('登录','Sign in') }}</button><button type="button" :class="{active:mode==='register'}" @click="mode='register'">{{ tr('邀请注册','Register') }}</button></div>
        <p class="hint">{{ tr('游客也可以关注；登录后可跨设备同步。已有 API 账号可直接登录。','Follow as a guest, or sign in to sync. Your existing API account works here.') }}</p>
        <label>{{ tr('邮箱','Email') }}<input v-model.trim="email" type="email" required autocomplete="email" maxlength="254"></label>
        <label v-if="mode==='register'">{{ tr('邀请码','Invitation code') }}<input v-model.trim="invite" required maxlength="128" autocomplete="off"></label>
        <label>{{ tr('密码','Password') }}<input v-model="password" type="password" required :minlength="mode==='register'?12:undefined" :autocomplete="mode==='register'?'new-password':'current-password'"></label>
        <button type="submit" :disabled="busy">{{ busy?tr('请稍候…','Please wait…'):mode==='register'?tr('注册','Register'):tr('登录','Sign in') }}</button>
      </form>
      <p v-if="error" class="down" role="alert">{{ error }}</p>
    </section>
    <section v-if="account.session && preferences" class="panel">
      <h2>{{ tr('提醒偏好','Alert preferences') }}</h2>
      <p class="hint">{{ telegram?.linked?tr('Telegram 已绑定。新池子和风险变化会按关注范围发送。','Telegram linked. New pools and risk changes follow your watchlist.'):telegram?.available?tr('绑定 Telegram 后可接收关注提醒。','Link Telegram to receive watchlist alerts.'):tr('Telegram 推送尚未开通，以下设置会保存到账号。','Telegram delivery is not enabled. These preferences are saved to your account.') }}</p>
      <div v-if="telegram?.available" class="account-actions"><button v-if="!telegram.linked" :disabled="busy" @click="bindTelegram">{{ tr('绑定 Telegram','Link Telegram') }}</button><button v-else :disabled="busy" @click="unlinkTelegram">{{ tr('解除绑定','Unlink') }}</button><a v-if="binding" :href="binding.startUrl" target="_blank" rel="noopener">{{ tr('打开机器人并点击 Start（10 分钟内）','Open bot and tap Start (within 10 minutes)') }} ↗</a><button v-if="binding" @click="refreshTelegram">{{ tr('刷新绑定状态','Refresh connection') }}</button></div>
      <form class="account-form" @submit.prevent="savePreferences">
        <label class="check-row"><input v-model="preferences.newPool" type="checkbox">{{ tr('关注资产的新池子','New pools for followed assets') }}</label>
        <label class="check-row"><input v-model="preferences.largeTrade" type="checkbox" :disabled="!telegram?.capabilities?.largeTrade?.available">{{ tr('大额成交','Large trades') }} <small v-if="!telegram?.capabilities?.largeTrade?.available">{{ tr('暂未开通','Not available yet') }}</small></label>
        <label>{{ tr('成交金额门槛（USD）','Trade threshold (USD)') }}<input v-model.number="preferences.tradeUsd" type="number" min="100" max="1000000000" step="100"></label>
        <label class="check-row"><input v-model="preferences.riskChange" type="checkbox">{{ tr('风险变化','Risk changes') }}</label>
        <button :disabled="busy">{{ saved?tr('已保存','Saved'):tr('保存设置','Save preferences') }}</button>
      </form>
    </section>
    <section class="panel"><div class="panel-head"><h2>API</h2><RouterLink to="/developer">{{ tr('接口与密钥','API docs and keys') }} →</RouterLink></div><p class="hint">{{ tr('用同一个账号管理 API Key 和调用用量。','Manage API keys and usage with the same account.') }}</p></section>
  </div>
</template>
<script setup>
import {ref,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import {tr} from '../i18n';
import {useAccountStore} from '../stores/account';
import {developerRequest,loginDeveloper,registerDeveloper} from '../api/developer';
const account=useAccountStore(),route=useRoute(),router=useRouter();
const telegram=ref(null),binding=ref(null);
const mode=ref('login'),email=ref(''),password=ref(''),invite=ref(''),busy=ref(false),error=ref(''),preferences=ref(null),saved=ref(false);
function failure(e){return e?.status===401?tr('邮箱或密码不正确。','Incorrect email or password.'):e?.status===429?tr('操作过于频繁，请稍后再试。','Too many attempts. Please try later.'):tr('未能完成，请检查输入后重试。','Could not complete. Check your entries and try again.');}
async function submit(){busy.value=true;error.value='';try{const s=mode.value==='register'?await registerDeveloper(email.value,invite.value,password.value):await loginDeveloper(email.value,password.value);password.value='';await account.useSession(s);const next=String(route.query.next??'');if(next.startsWith('/')&&!next.startsWith('//'))router.replace(next);}catch(e){error.value=failure(e);}finally{busy.value=false;}}
async function signOut(){busy.value=true;try{await account.logout();preferences.value=null;}catch(e){error.value=failure(e);}finally{busy.value=false;}}
async function savePreferences(){const id=account.session?.user.id;busy.value=true;saved.value=false;try{const result=await developerRequest('alerts',{method:'PUT',body:preferences.value,csrfToken:account.session.csrfToken});if(account.session?.user.id===id){preferences.value=result;saved.value=true;}}catch(e){error.value=failure(e);}finally{busy.value=false;}}
let preferenceRequest=0;
async function refreshTelegram(){const id=account.session?.user.id;if(!id)return;try{const value=await developerRequest('telegram');if(account.session?.user.id===id){telegram.value=value;if(value.linked)binding.value=null;}}catch{}}
async function bindTelegram(){busy.value=true;const id=account.session?.user.id;try{const result=await developerRequest('telegram/bind',{method:'POST',csrfToken:account.session.csrfToken});if(account.session?.user.id===id)binding.value=result;}catch(e){error.value=failure(e);}finally{busy.value=false;}}
async function unlinkTelegram(){busy.value=true;try{await developerRequest('telegram',{method:'DELETE',csrfToken:account.session.csrfToken});binding.value=null;await refreshTelegram();}catch(e){error.value=failure(e);}finally{busy.value=false;}}
watch(()=>account.session?.user.id,async id=>{const request=++preferenceRequest;preferences.value=null;telegram.value=null;binding.value=null;saved.value=false;if(!id)return;try{const value=await developerRequest('alerts');if(request===preferenceRequest&&account.session?.user.id===id)preferences.value=value;}catch{};refreshTelegram();},{immediate:true});
account.start();
</script>
<style scoped>
.account-page{display:grid;gap:16px;max-width:840px;margin:auto}.account-form{display:grid;gap:16px;max-width:460px;margin-top:18px}.account-form label{display:grid;gap:7px;font-size:14px}.account-form input{padding:10px;border:1px solid var(--border);border-radius:5px;background:var(--bg);color:var(--text);min-width:0}.account-form .check-row{display:flex;align-items:center;gap:8px}.account-actions{display:flex;justify-content:space-between;align-items:center;gap:16px;margin:18px 0}.account-form button,.account-actions button,.view-tabs button{padding:9px 14px;border:1px solid var(--border);border-radius:5px;background:var(--surface-raised);color:var(--text);cursor:pointer}.view-tabs{display:flex;gap:8px}.view-tabs .active{border-color:var(--accent);color:var(--accent)}
</style>
