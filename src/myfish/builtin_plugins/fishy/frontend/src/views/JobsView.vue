<script setup lang="ts">
import { useWorkspace } from "../composables/useWorkspace";
import { ListChecks } from "@lucide/vue";
import { label, date, message } from "../utils/format";

const { me, busy, jobs, api, run, viewJobLogs } = useWorkspace();
</script>
<template>
  <section class="panel">
    <div class="table-toolbar">
      <span>最近 {{ jobs.length }} 项任务</span>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>目标</th>
            <th>计划执行时间</th>
            <th>状态</th>
            <th>说明</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td>
              <b>{{ label(j.kind) }}</b
              ><small>#{{ j.id }}</small>
            </td>
            <td>{{ j.target }}</td>
            <td>{{ date(j.run_at) }}</td>
            <td>
              <span class="badge" :data-state="j.status">{{
                label(j.status)
              }}</span>
            </td>
            <td class="wrap">{{ message(j.error) || "—" }}</td>
            <td>
              <button
                v-if="
                  me?.role === 'admin' &&
                  ['uncertain', 'failed'].includes(j.status)
                "
                :disabled="busy"
                @click="viewJobLogs(j)"
              >
                查看错误日志</button
              ><button
                v-if="j.status === 'pending'"
                :disabled="busy"
                @click="
                  run(
                    () => api('/jobs/' + j.id + '/cancel', 'POST'),
                    '任务已取消',
                  )
                "
              >
                取消
              </button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!jobs.length" class="empty">
        <ListChecks :size="36" />
        <h3>暂无任务</h3>
        <p>发布、交付与下架任务将在这里显示。</p>
      </div>
    </div>
  </section>
</template>
