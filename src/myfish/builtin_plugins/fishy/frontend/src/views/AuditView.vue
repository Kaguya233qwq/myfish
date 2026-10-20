<script setup lang="ts">
import { useWorkspace } from "../composables/useWorkspace";
import { date } from "../utils/format";

const { audits } = useWorkspace();
</script>
<template>
  <section class="panel">
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>操作者</th>
            <th>操作</th>
            <th>目标</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in audits" :key="a.id">
            <td>{{ date(a.created) }}</td>
            <td>{{ a.actor }}</td>
            <td>{{ a.action }}</td>
            <td>{{ a.target || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="!audits.length" class="empty">暂无操作记录</div>
    </div>
  </section>
</template>
