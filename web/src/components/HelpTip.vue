<template>
  <button class="v2-tip" :title="label" @click.stop="open = true" @keydown.esc.stop="open = false">
    ?
  </button>
  <Teleport to="body">
    <div v-if="open" class="v2-help-overlay" @click.self="open = false">
      <div class="v2-help-dialog" role="dialog" aria-modal="true">
        <div class="panel-head">
          <h2>{{ title }}</h2>
          <button data-help-close aria-label="close" @click="open = false">×</button>
        </div>
        <p>{{ body }}</p>
      </div>
    </div>
  </Teleport>
</template>

<script setup>
import { ref } from 'vue';

defineProps({
  title: { type: String, default: '' },
  body: { type: String, default: '' },
  label: { type: String, default: '?' },
});

const open = ref(false);
</script>
