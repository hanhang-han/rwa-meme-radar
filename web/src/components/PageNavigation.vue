<template>
  <nav v-if="ancestors.length" class="page-navigation" :aria-label="tr('页面位置','Breadcrumb')">
    <div class="page-navigation-trail">
      <template v-for="(item,index) in ancestors" :key="item.path+index"><RouterLink :to="item.to">{{ navigationLabel(item.path,lang.lang) }}</RouterLink><span aria-hidden="true">/</span></template>
      <span aria-current="page">{{ navigationLabel(route.path,lang.lang) }}</span>
    </div>
    <RouterLink :to="parent" custom v-slot="{href}"><a :href="href" class="page-navigation-back" @click="goBack">← {{ tr('返回','Back to') }} {{ parentLabel }}</a></RouterLink>
  </nav>
</template>
<script setup>
import { computed } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { tr, useI18n } from '../i18n';
import { chainScope } from '../utils/chain-scope';
import { navigationAncestors, navigationLabel, navigationParent, returnNavigation } from '../utils/navigation-context';
const route=useRoute(),router=useRouter(),{lang}=useI18n();
const scope=computed(()=>chainScope(route.query));
const ancestors=computed(()=>navigationAncestors(route,scope.value));
const parent=computed(()=>navigationParent(route,scope.value));
const parentLabel=computed(()=>navigationLabel(typeof parent.value==='string'?decodeURIComponent(parent.value.split('?')[0]):parent.value.path,lang.lang));
function goBack(event){
  if(event.button!==0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey)return;
  event.preventDefault();
  returnNavigation(router,parent.value,window.history.state?.back);
}
</script>
<style scoped>
.page-navigation{display:flex;justify-content:space-between;align-items:center;gap:10px 20px;flex-wrap:wrap;margin:0 0 16px;color:var(--muted);font-size:12px;line-height:1.6}.page-navigation-trail{display:flex;align-items:center;gap:7px 9px;flex-wrap:wrap;min-width:0}.page-navigation a{color:var(--muted);text-decoration:none}.page-navigation a:hover{color:var(--accent)}.page-navigation-trail>[aria-current]{color:var(--text)}.page-navigation-back{white-space:nowrap;font-size:12px}.page-navigation a:focus-visible{outline:2px solid var(--accent);outline-offset:3px}@media(max-width:700px){.page-navigation{gap:7px;font-size:11px;margin-bottom:13px}.page-navigation-back{margin-left:auto;min-height:32px;display:flex;align-items:center}}
</style>
