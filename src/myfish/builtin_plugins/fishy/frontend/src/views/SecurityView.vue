<script setup lang="ts">
import { useWorkspace } from "../composables/useWorkspace";
import { ShieldCheck, KeyRound } from "@lucide/vue";
import { date } from "../utils/format";

const { me, sessions, api, run, modal, password } = useWorkspace();
</script>
<template>
  <section class="panel security-card">
    <div class="security-icon"><KeyRound :size="24" /></div>
    <div>
      <h3>登录密码</h3>
      <p>修改密码后，所有已登录设备均需重新登录。</p>
    </div>
    <button
      @click="
        password.old_password = '';
        password.new_password = '';
        modal = 'password';
      "
    >
      修改密码
    </button>
  </section>
  <section class="panel">
    <div class="panel-heading">
      <h3>有效会话</h3>
      <span class="muted">{{ sessions.length }} 个</span>
    </div>
    <div v-for="s in sessions" :key="s.id" class="activity">
      <span class="activity-icon"><ShieldCheck :size="20" /></span>
      <div>
        <b>会话 #{{ s.id }} {{ s.id === me?.session_id ? "· 当前会话" : "" }}</b
        ><small>登录 {{ date(s.created) }} · 到期 {{ date(s.expires) }}</small>
      </div>
      <button
        @click="
          run(async () => {
            await api('/auth/sessions/' + s.id, 'DELETE');
            if (s.id === me?.session_id) me = null;
          }, '会话已撤销')
        "
      >
        撤销
      </button>
    </div>
  </section>
</template>
