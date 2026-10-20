<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";
import { label } from "../../utils/format";

const { busy, api, run, selected, scheduleAt, action } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(
        () =>
          api('/products/' + selected?.id + '/' + action, 'POST', {
            run_at: scheduleAt ? new Date(scheduleAt).getTime() / 1000 : null,
          }),
        '任务已加入队列',
      )
    "
  >
    <p>{{ selected?.title }}</p>
    <p class="muted">闲鱼账号：{{ selected?.account_id }}</p>
    <label
      >执行时间<input v-model="scheduleAt" type="datetime-local" /><span
        class="hint"
        >留空后立即加入执行队列。</span
      ></label
    >
    <div class="modal-actions">
      <button class="primary" :disabled="busy">确认{{ label(action) }}</button>
    </div>
  </form>
</template>
