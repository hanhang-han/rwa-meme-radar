<template><section class="panel" role="status"><template v-if="!failed"><h2>{{ tr('打开配对资产…','Opening paired asset…') }}</h2><div class="loading-skeleton"><div class="skeleton-line"></div></div></template><template v-else><h2>{{ tr('未找到这个配对池','Pair pool not found') }}</h2><p>{{ tr('可以在池子列表中按股票合约查找。','Search the pool list by stock contract.') }}</p><RouterLink :to="{path:'/meme',query:{chain:chain,view:'pool',q:stock}}">{{ tr('查看池子','View pools') }} →</RouterLink> <button @click="resolvePair">{{ tr('重试','Retry') }}</button></template></section></template>
<script setup>
import {onBeforeUnmount,ref,watch} from 'vue';
import {useRoute,useRouter} from 'vue-router';
import {useDashboardStore} from '../stores/dashboard';
import {getDetail} from '../api/client';
import {pairRelationMatches} from '../utils/detail-presentation';
import {tr} from '../i18n';
const props=defineProps({chain:String,stock:String}),route=useRoute(),router=useRouter(),dash=useDashboardStore(),failed=ref(false);
let generation=0;
async function resolvePair(){
 const gen=++generation,chain=props.chain,stock=props.stock,pool=String(route.query.pool??'').toLowerCase(),query={...route.query};
 failed.value=false;
 if(!pool){router.replace({path:'/meme',query:{...query,chain,view:'pool',q:stock}});return;}
 const matches=relation=>pairRelationMatches(relation,chain,stock,pool);
 let match=dash.relations.find(matches);
 try{let offset=0;while(!match&&offset!=null&&offset<1000){const response=await getDetail(chain,stock,{section:'relations',offset,limit:100});if(gen!==generation)return;match=response.relations?.find(matches);const next=response.next;offset=next!=null&&Number(next)>offset?Number(next):null;}}
 catch{if(gen===generation)failed.value=true;return;}
 if(gen!==generation)return;
 if(match?.token)router.replace({path:`/asset/${chain}/${match.token}`,query:{...query,tab:'relation',from:'stock',ticker:match.ticker||query.ticker}});else failed.value=true;
}
watch(()=>[props.chain,props.stock,route.query.pool],resolvePair,{immediate:true});
onBeforeUnmount(()=>{generation++;});
</script>
