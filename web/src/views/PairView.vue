<template>
  <section class="panel" role="status">
    <template v-if="!failed"><h2>{{ tr('打开配对资产…','Opening paired asset…') }}</h2><div class="loading-skeleton"><div class="skeleton-line"></div></div></template>
    <template v-else>
      <h2>{{ failureKind==='request'?tr('配对资料暂时无法加载','Pair details could not be loaded'):tr('未找到这个配对池','Pair pool not found') }}</h2>
      <p>{{ failureKind==='request'?tr('请稍后重试，也可以在池子列表中按股票合约查找。','Retry shortly, or search the pool list by stock contract.'):tr('可以在池子列表中按股票合约查找。','Search the pool list by stock contract.') }}</p>
      <RouterLink :to="poolListLink">{{ tr('查看池子','View pools') }} →</RouterLink> <button @click="resolvePair">{{ tr('重试','Retry') }}</button>
    </template>
  </section>
</template>
<script setup>
import {computed,onBeforeUnmount,ref,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import {useDashboardStore} from '../stores/dashboard';
import {getDetail} from '../api/client';
import {pairRelationMatches} from '../utils/detail-presentation';
import {chainScope} from '../utils/chain-scope';
import {navigationRoute,navigationSource,pageNavigationLink} from '../utils/navigation-context';
import {tr} from '../i18n';
const props=defineProps({chain:String,stock:String}),route=useRoute(),router=useRouter(),dash=useDashboardStore(),failed=ref(false),failureKind=ref(null);
const poolListLink=computed(()=>pageNavigationLink('/meme',{route,scope:chainScope(route.query),query:{view:'pool',q:props.stock}}));
let generation=0;
async function resolvePair(){
 const gen=++generation,chain=props.chain,stock=props.stock,pool=String(route.query.pool??'').toLowerCase(),query={...route.query};
 failed.value=false;failureKind.value=null;
 if(!pool){router.replace(poolListLink.value);return;}
 const matches=relation=>pairRelationMatches(relation,chain,stock,pool);
 let match=dash.relations.find(matches);
 try{let offset=0;while(!match&&offset!=null&&offset<1000){const response=await getDetail(chain,stock,{section:'relations',offset,limit:100});if(gen!==generation)return;match=response.relations?.find(matches);const next=response.next;offset=next!=null&&Number(next)>offset?Number(next):null;}}
 catch{if(gen===generation){failed.value=true;failureKind.value='request';}return;}
 if(gen!==generation)return;
 if(match?.token){
   const saved=navigationRoute(query.back),ownPath=String(route.path??'').replace(/^\/pools\//,'/pair/');
   // This route only resolves a pair. Returning to the same bridge would
   // immediately redirect back to the asset instead of showing its source.
   const back=saved && saved.path.replace(/^\/pools\//,'/pair/')!==ownPath ? saved.fullPath : null;
   const fallback=poolListLink.value;
   const source=back?navigationSource({path:route.path,query:{...query,back}}):'meme';
   router.replace({path:`/asset/${chain}/${match.token}`,query:{...query,tab:'relation',from:source,
     back:back??`${fallback.path}?${new URLSearchParams(fallback.query)}`,ticker:match.ticker||query.ticker}});
 }else{failed.value=true;failureKind.value='not-found';}
}
watch(()=>[props.chain,props.stock,route.query.pool,route.query.from,route.query.back,route.query.chain],resolvePair,{immediate:true});
onBeforeUnmount(()=>{generation++;});
</script>
