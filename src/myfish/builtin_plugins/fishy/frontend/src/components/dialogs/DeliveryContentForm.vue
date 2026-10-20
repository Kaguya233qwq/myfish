<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { busy, api, run, selected, deliveryContent } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(
        () =>
          api('/products/' + selected?.id + '/delivery-content', 'PUT', {
            content: deliveryContent,
          }),
        '交付内容已更新',
      )
    "
  >
    <p>{{ selected?.title }}</p>
    <label
      >固定交付内容<textarea
        v-model="deliveryContent"
        maxlength="10000"
        rows="10"
        required
      ></textarea>
    </label>
    <p class="hint">
      保存后用于尚未开始发送的订单。已发送或结果待核对的订单不会自动补发。
    </p>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">保存交付内容</button>
    </div>
  </form>
</template>
