<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { me, busy, api, run, password } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(async () => {
        await api('/auth/password', 'POST', password);
        me = null;
      }, '密码已修改，请重新登录')
    "
  >
    <label
      >原密码<input
        v-model="password.old_password"
        type="password"
        required
        autocomplete="current-password" /></label
    ><label
      >新密码<input
        v-model="password.new_password"
        type="password"
        required
        minlength="12"
        maxlength="128"
        autocomplete="new-password"
    /></label>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">修改并退出登录</button>
    </div>
  </form>
</template>
