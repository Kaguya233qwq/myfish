<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { busy, api, run, newUser } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(() => api('/users', 'POST', newUser), '团队账号已创建')
    "
  >
    <label
      >账号<input
        v-model="newUser.username"
        required
        minlength="3"
        maxlength="40"
        autocomplete="off"
        pattern="[a-z0-9_.-]+" /></label
    ><label
      >初始密码<input
        v-model="newUser.password"
        type="password"
        required
        minlength="12"
        maxlength="128"
        autocomplete="new-password" /></label
    ><label
      >角色<select v-model="newUser.role">
        <option value="operator">运营人员</option>
        <option value="admin">管理员</option>
      </select></label
    >
    <p class="hint">
      运营人员可管理商品与订单；管理员还可管理团队、查看操作记录。
    </p>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">创建账号</button>
    </div>
  </form>
</template>
