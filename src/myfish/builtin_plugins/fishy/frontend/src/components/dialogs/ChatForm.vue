<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { busy, api, run, selectedOrder, chatId } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(
        () =>
          api('/orders/' + selectedOrder?.key + '/chat', 'PUT', {
            chat_id: chatId,
          }),
        '买家会话已关联',
      )
    "
  >
    <p class="muted">请填写该订单买家的会话 ID。收到交易卡片后会自动关联。</p>
    <label
      >会话 ID<input v-model="chatId" required pattern="[a-zA-Z0-9_-]+"
    /></label>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">确认关联</button>
    </div>
  </form>
</template>
