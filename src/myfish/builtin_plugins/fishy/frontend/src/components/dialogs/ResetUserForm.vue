<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";

const { busy, api, run, selectedUser, resetPassword } = useWorkspace();
</script>
<template>
  <form
    @submit.prevent="
      run(
        () =>
          api('/users/' + selectedUser?.id + '/password', 'POST', {
            password: resetPassword,
          }),
        '密码已重置，该账号的会话已撤销',
      )
    "
  >
    <p class="muted">账号：{{ selectedUser?.username }}</p>
    <label
      >新密码<input
        v-model="resetPassword"
        type="password"
        required
        minlength="12"
        maxlength="128"
        autocomplete="new-password"
    /></label>
    <div class="modal-actions">
      <button class="primary" :disabled="busy">重置密码</button>
    </div>
  </form>
</template>
