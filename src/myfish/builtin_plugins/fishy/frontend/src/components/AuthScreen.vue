<script setup lang="ts">
import { Fish, Boxes, ShieldCheck, ArrowUpRight } from "@lucide/vue";

defineProps<{
  setup: boolean;
  busy: boolean;
  error: string;
  login: { username: string; password: string; token: string };
}>();
defineEmits<{ submit: [] }>();
</script>
<template>
  <main class="login-page">
    <section class="login-story">
      <div class="brand">
        <Fish :size="30" /><b>渔渔<span>MyFish / Goods manager</span></b>
      </div>
      <div class="story-copy">
        <span class="eyebrow">商品运营工作台</span>
        <h1>商品管理。<br />自动交付。</h1>
        <p>集中管理商品和订单。<br />支持自动交付与商品擦亮。</p>
        <div class="story-line"></div>
        <div class="story-features">
          <span><Boxes :size="18" />商品统一管理</span
          ><span><ShieldCheck :size="18" />交付全程可查</span>
        </div>
      </div>
      <small>MYFISH · GOODS WORKSPACE</small>
    </section>
    <section class="login-form">
      <div class="form-content">
        <span class="eyebrow">你的商品运营工作空间</span>
        <h2>{{ setup ? "创建第一个管理员" : "欢迎回来" }}</h2>
        <p class="muted">
          {{
            setup
              ? "请使用插件数据目录中的 setup.token 创建管理员。"
              : "登录后继续管理商品与订单。"
          }}
        </p>
        <form @submit.prevent="$emit('submit')">
          <label v-if="setup"
            >初始化令牌<input
              v-model="login.token"
              type="password"
              autocomplete="off"
              required
              placeholder="粘贴 setup.token 文件中的令牌" /></label
          ><label
            >账号<input
              v-model="login.username"
              autocomplete="username"
              required
              minlength="3"
              maxlength="40"
              placeholder="请输入后台账号" /></label
          ><label
            >密码<input
              v-model="login.password"
              type="password"
              :autocomplete="setup ? 'new-password' : 'current-password'"
              :minlength="setup ? 12 : 1"
              maxlength="128"
              required
              placeholder="请输入密码"
          /></label>
          <p v-if="setup" class="hint">密码至少 12 个字符。</p>
          <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
          <button class="primary full" :disabled="busy">
            {{ busy ? "请稍候…" : setup ? "初始化并登录" : "登录工作台"
            }}<ArrowUpRight :size="18" />
          </button>
        </form>
        <div class="login-note">
          <ShieldCheck :size="16" />闲鱼登录与后台账号独立管理
        </div>
      </div>
      <small class="muted"
        >忘记密码？请由管理员重置，或使用本机恢复命令。</small
      >
    </section>
  </main>
</template>
