<script setup lang="ts">
import {
  Package,
  ShoppingBag,
  AlertCircle,
  Clock3,
  ListChecks,
  ArrowUpRight,
  Fish,
  ShieldCheck,
} from "@lucide/vue";
import type { Job, Account } from "../types";

defineProps<{
  metrics: {
    products: number;
    pending_jobs: number;
    orders: number;
    attention: number;
  };
  jobs: Job[];
  accounts: Account[];
  label: (key: string) => string;
  date: (time: number) => string;
}>();
defineEmits<{ navigate: [page: string] }>();
</script>
<template>
  <div class="metric-grid">
    <article
      v-for="card in [
        {
          label: '管理商品',
          value: metrics.products,
          icon: Package,
          note: '已建立的商品档案',
        },
        {
          label: '待处理任务',
          value: metrics.pending_jobs,
          icon: Clock3,
          note: '排队或执行中的任务',
        },
        {
          label: '跟踪订单',
          value: metrics.orders,
          icon: ShoppingBag,
          note: '已关联商品的交易订单',
        },
        {
          label: '需要关注',
          value: metrics.attention,
          icon: AlertCircle,
          note: '执行失败或异常的任务',
        },
      ]"
      :key="card.label"
      class="metric"
    >
      <div>
        <span>{{ card.label }}</span
        ><component :is="card.icon" :size="20" />
      </div>
      <strong>{{ card.value }}</strong
      ><small>{{ card.note }}</small>
    </article>
  </div>
  <div class="overview-grid">
    <section class="panel">
      <div class="panel-heading">
        <h3>最近任务</h3>
        <button class="text-button" @click="$emit('navigate', 'jobs')">
          查看全部<ArrowUpRight :size="15" />
        </button>
      </div>
      <div v-if="!jobs.length" class="empty">
        <ListChecks :size="34" />
        <h3>任务队列为空</h3>
        <p>创建商品后，即可安排发布和交付。</p>
      </div>
      <div v-for="job in jobs.slice(0, 5)" :key="job.id" class="activity">
        <span class="activity-icon"><Clock3 :size="18" /></span>
        <div>
          <b>{{ label(job.kind) }} · {{ job.target }}</b
          ><small>{{ date(job.run_at) }}</small>
        </div>
        <span class="badge" :data-state="job.status">{{
          label(job.status)
        }}</span>
      </div>
    </section>
    <section class="panel">
      <div class="panel-heading">
        <h3>闲鱼账号</h3>
        <span class="muted">{{ accounts.length }} 个</span>
      </div>
      <div v-if="!accounts.length" class="empty">
        <Fish :size="34" />
        <h3>等待闲鱼账号登录</h3>
        <p>请在主程序终端完成闲鱼扫码登录。</p>
      </div>
      <div v-for="account in accounts" :key="account.id" class="account">
        <div class="account-icon"><Fish :size="23" /></div>
        <div>
          <b>{{ account.id }}</b
          ><small>{{
            account.connected ? "消息连接在线" : "消息连接断开"
          }}</small>
        </div>
        <span
          class="badge"
          :data-state="account.connected ? 'succeeded' : 'failed'"
          >{{ account.connected ? "在线" : "离线" }}</span
        >
      </div>
      <div class="panel-note">
        <ShieldCheck :size="17" /><span>交付前自动核对订单状态。</span>
      </div>
    </section>
  </div>
  <section class="panel guide">
    <div>
      <span class="eyebrow">从这里开始</span>
      <h3>管理商品与交付</h3>
      <p>建立商品档案，填写交付内容，再关联平台商品并开启自动交付。</p>
    </div>
    <button @click="$emit('navigate', 'products')">
      管理商品<ArrowUpRight :size="17" />
    </button>
  </section>
</template>
