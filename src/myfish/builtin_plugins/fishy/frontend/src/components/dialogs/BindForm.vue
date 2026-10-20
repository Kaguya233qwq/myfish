<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { busy, api, run, selected, bindId } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(
        () =>
          api('/products/' + selected?.id + '/bind', 'POST', {
            item_id: bindId,
          }),
        '已关联平台商品',
      )
    "
  >
    <p class="muted">请确认该商品属于所选闲鱼账号，并与交付内容一致。</p>
    <label
      >平台商品 ID<input v-model="bindId" pattern="[0-9]+" required
    /></label>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">确认关联</button>
    </div>
  </form>
</template>
