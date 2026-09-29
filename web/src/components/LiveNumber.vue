<template><span :title="fullText" :class="{ 'kpi-flash': flash, 'live-number-up': flash && direction > 0, 'live-number-down': flash && direction < 0 }">{{ text }}</span></template>
<script setup>
import {computed,ref,watch,onUnmounted} from 'vue';
import {money,num,pct,price,fullUsd} from '../utils/format';
const props=defineProps({value:Number,currency:{type:String,default:'USD'},format:{type:String,default:'money'}});
const text=computed(()=>props.format==='number'?num(props.value):props.format==='percent'?pct(props.value):props.format==='price'?price(props.value,props.currency):money(props.value,props.currency));
const fullText=computed(()=>props.value==null?'—':props.currency==='USD'&&props.format==='money'?fullUsd(props.value):`${props.value} ${props.currency??''}`.trim());
const flash=ref(false),direction=ref(0);let timer,frame;
watch(()=>props.value,(n,o)=>{
 if(n==null||o==null||n===o)return;
 direction.value=n>o?1:-1;flash.value=false;
 clearTimeout(timer);cancelAnimationFrame(frame);
 frame=requestAnimationFrame(()=>{flash.value=true;timer=setTimeout(()=>flash.value=false,1000);});
});
onUnmounted(()=>{clearTimeout(timer);cancelAnimationFrame(frame);});
</script>
