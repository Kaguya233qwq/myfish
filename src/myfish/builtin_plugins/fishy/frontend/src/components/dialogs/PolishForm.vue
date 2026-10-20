<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { busy, api, run, selected, polishForm } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(
        () =>
          api(
            '/products/' + selected?.id + '/polish-settings',
            'PUT',
            polishForm,
          ),
        '自动擦亮设置已保存',
      )
    "
  >
    <p class="muted">
      仅限在售商品，默认每 24 小时擦亮一次。已擦亮的商品将按间隔等待下次执行。
    </p>
    <div class="checks">
      <label
        ><input
          v-model="polishForm.enabled"
          type="checkbox"
        />开启自动擦亮</label
      >
    </div>
    <label
      >擦亮间隔（小时）<input
        v-model.number="polishForm.interval_hours"
        type="number"
        min="24"
        max="168"
        required
    /></label>
    <p class="hint">关闭后将取消待执行的擦亮任务。</p>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">保存擦亮设置</button>
    </div>
  </form>
</template>
