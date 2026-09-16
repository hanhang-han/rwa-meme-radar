<template>
  <button ref="trigger" class="v2-tip" :title="label" @click.stop="toggle">
    ?
  </button>
  <Teleport to="body">
    <div v-if="open" class="v2-help-overlay" @click.self="close">
      <div class="v2-help-dialog" role="dialog" aria-modal="true">
        <div class="panel-head">
          <h2>{{ title }}</h2>
          <button ref="closeBtn" data-help-close aria-label="close" @click="close">×</button>
        </div>
        <p>{{ body }}</p>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue';

defineProps({
  title: { type: String, default: '' },
  body: { type: String, default: '' },
  label: { type: String, default: '?' },
});

const open = ref(false);
const trigger = ref(null);
const closeBtn = ref(null);

function toggle() {
  open.value ? close() : (open.value = true, onOpen());
}
function onOpen() {
  document.body.classList.add('v2-help-open');
  document.addEventListener('keydown', onKey);
  requestAnimationFrame(() => closeBtn.value?.focus());
}
function close() {
  open.value = false;
  document.body.classList.remove('v2-help-open');
  document.removeEventListener('keydown', onKey);
  trigger.value?.focus();
}
function onKey(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    close();
  } else if (e.key === 'Tab') {
    e.preventDefault();
    closeBtn.value?.focus();
  }
}

onBeforeUnmount(() => {
  document.body.classList.remove('v2-help-open');
  document.removeEventListener('keydown', onKey);
});
</script>
