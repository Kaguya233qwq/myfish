<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";
import { label, date, message } from "../../utils/format";

const { selectedJob, jobLogs } = useWorkspace();
</script>
<template>
  <div>
    <p>任务 #{{ selectedJob?.id }} · {{ label(selectedJob?.kind || "") }}</p>
    <div v-for="(log, index) in jobLogs" :key="index">
      <small>{{ date(log.created) }}</small>
      <pre class="job-error-log"
        >{{ message(log.message)
        }}{{ log.location ? "\n" + log.location : "" }}</pre>
    </div>
    <p v-if="!jobLogs.length" class="hint">暂无错误日志。</p>
  </div>
</template>
