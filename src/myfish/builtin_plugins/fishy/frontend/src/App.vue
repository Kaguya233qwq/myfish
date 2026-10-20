<script setup lang="ts">
import AuthScreen from "./components/AuthScreen.vue";
import WorkspaceLayout from "./components/layout/WorkspaceLayout.vue";
import DialogHost from "./components/dialogs/DialogHost.vue";
import { provideWorkspace } from "./composables/useWorkspace";

const { ready, me, setup, busy, error, login, signIn } = provideWorkspace();
</script>
<template>
  <Transition name="auth" mode="out-in">
    <div v-if="!ready" class="loading">正在连接渔渔…</div>
    <AuthScreen
      v-else-if="!me"
      :setup="setup"
      :busy="busy"
      :error="error"
      :login="login"
      @submit="signIn"
    />
    <WorkspaceLayout v-else />
  </Transition>
  <DialogHost />
</template>
