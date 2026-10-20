<script setup lang="ts">
import { useWorkspace } from "../composables/useWorkspace";

const { me, busy, users, api, run, modal, selectedUser, resetPassword } =
  useWorkspace();
</script>
<template>
  <section class="panel">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>成员</th>
            <th>角色</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>
              <b>{{ u.username }}</b
              ><small v-if="u.id === me?.id">当前账号</small>
            </td>
            <td>{{ u.role === "admin" ? "管理员" : "运营人员" }}</td>
            <td>
              <span
                class="badge"
                :data-state="u.active ? 'succeeded' : 'cancelled'"
                >{{ u.active ? "正常" : "已停用" }}</span
              >
            </td>
            <td>
              <button
                @click="
                  selectedUser = u;
                  resetPassword = '';
                  modal = 'resetUser';
                "
              >
                重置密码
              </button>
              <button
                :disabled="u.id === me?.id || busy"
                @click="
                  run(
                    () =>
                      api('/users/' + u.id, 'PUT', {
                        role: u.role,
                        active: !u.active,
                      }),
                    '账号状态已更新，已有会话已撤销',
                  )
                "
              >
                {{ u.active ? "停用" : "启用" }}
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
