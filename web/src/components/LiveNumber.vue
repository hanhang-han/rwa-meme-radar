<template><span :title="fullText" :class="{ 'kpi-flash': flash, 'live-number-up': flash && direction > 0, 'live-number-down': flash && direction < 0 }">{{ text }}</span></template>
<script setup>
import {computed,ref,watch,onUnmounted} from 'vue';
import {money,num,livePercent,livePrice,fullUsd} from '../utils/format';
import {tr} from '../i18n';
const props=defineProps({value:Number,currency:{type:String,default:'USD'},format:{type:String,default:'money'},compact:{type:Boolean,default:true},label:{type:String,default:''},description:{type:String,default:''}});
const text=computed(()=>props.format==='number'?num(props.value):props.format==='percent'?livePercent(props.value):props.format==='price'?livePrice(props.value,props.currency):!props.compact&&props.currency==='USD'?fullUsd(props.value):money(props.value,props.currency));
const fullText=computed(()=>{
 const value=props.value;
 const amount=value==null||!Number.isFinite(Number(value))?tr('暂无数据；不代表零','Unavailable; does not mean zero')
  :props.format==='percent'?`${value}%`
  :props.format==='number'?String(value)
  :props.currency==='USD'&&props.format==='money'?fullUsd(value)
  :`${value} ${props.currency??''}`.trim();
 return [props.label?`${props.label}: ${amount}`:amount,props.description].filter(Boolean).join(' · ');
});
const flash=ref(false),direction=ref(0);let timer,frame;
watch(()=>[props.value,text.value,props.currency,props.format],([n,label,currency,format],[o,oldLabel,oldCurrency,oldFormat])=>{
 if(n==null||o==null||!Number.isFinite(n)||!Number.isFinite(o)||n===o||label===oldLabel||currency!==oldCurrency||format!==oldFormat)return;
 direction.value=n>o?1:-1;flash.value=false;
 clearTimeout(timer);cancelAnimationFrame(frame);
 frame=requestAnimationFrame(()=>{flash.value=true;timer=setTimeout(()=>flash.value=false,1000);});
});
onUnmounted(()=>{clearTimeout(timer);cancelAnimationFrame(frame);});
</script>
<style scoped>
.live-number-up.kpi-flash{animation:live-price-up .65s ease-out}.live-number-down.kpi-flash{animation:live-price-down .65s ease-out}
@keyframes live-price-up{from{color:var(--up);background:color-mix(in srgb,var(--up) 12%,transparent)}to{color:inherit;background:transparent}}
@keyframes live-price-down{from{color:var(--down);background:color-mix(in srgb,var(--down) 12%,transparent)}to{color:inherit;background:transparent}}
@media(prefers-reduced-motion:reduce){.kpi-flash{animation:none!important}}
</style>
