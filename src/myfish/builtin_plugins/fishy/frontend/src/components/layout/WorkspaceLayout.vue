<script setup lang="ts">
import { label, date } from "../../utils/format";
import { useWorkspace } from "../../composables/useWorkspace";
import {
  Fish,
  LogOut,
  Plus,
  RefreshCw,
  CircleCheck,
  AlertCircle,
  ChevronRight,
} from "@lucide/vue";
import { computed } from "vue";
import {
  LayoutDashboard,
  Package,
  ShoppingBag,
  ListChecks,
  Users,
  ShieldCheck,
  KeyRound,
} from "@lucide/vue";
import Overview from "../Overview.vue";
import ProductsView from "../../views/ProductsView.vue";
import OrdersView from "../../views/OrdersView.vue";
import JobsView from "../../views/JobsView.vue";
import UsersView from "../../views/UsersView.vue";
import AuditView from "../../views/AuditView.vue";
import SecurityView from "../../views/SecurityView.vue";

const {
  me,
  refreshing,
  page,
  notice,
  error,
  jobs,
  accounts,
  metrics,
  refresh,
  logout,
  modal,
  newUser,
  createProduct,
} = useWorkspace();
const nav = computed(() => [
  { key: "overview", label: "工作台", icon: LayoutDashboard },
  { key: "products", label: "商品管理", icon: Package },
  { key: "orders", label: "交易订单", icon: ShoppingBag },
  { key: "jobs", label: "任务队列", icon: ListChecks },
  ...(me.value?.role === "admin"
    ? [
        { key: "users", label: "团队账号", icon: Users },
        { key: "audit", label: "操作记录", icon: ShieldCheck },
      ]
    : []),
  { key: "security", label: "账号安全", icon: KeyRound },
]);
const heading = computed(
  () => nav.value.find((n) => n.key === page.value)?.label || "",
);
</script>
<template>
  <div v-if="me" class="workspace">
    <aside class="sidebar">
      <div class="brand">
        <Fish :size="27" /><b>渔渔<span>商品运营工作台</span></b>
      </div>
      <div class="nav-title">工作空间</div>
      <nav>
        <button
          v-for="item in nav"
          :key="item.key"
          :class="{ active: page === item.key }"
          :aria-current="page === item.key ? 'page' : undefined"
          :aria-label="item.label"
          @click="page = item.key"
        >
          <component :is="item.icon" :size="19" />{{ item.label
          }}<span
            v-if="item.key === 'jobs' && metrics.attention"
            class="nav-count"
            >{{ metrics.attention }}</span
          >
        </button>
      </nav>
      <div class="side-bottom">
        <span class="connection-dot"></span>插件服务运行中<small
          >MYFISH / GOODS MANAGER</small
        >
      </div>
    </aside>
    <div class="main">
      <header>
        <div class="breadcrumb">
          工作空间<ChevronRight :size="14" /><strong>{{ heading }}</strong>
        </div>
        <div class="header-right">
          <span class="local-tag">插件后台</span
          ><span class="avatar">{{ me.username[0]?.toUpperCase() }}</span
          ><span
            >{{ me.username
            }}<small>{{
              me.role === "admin" ? "管理员" : "运营人员"
            }}</small></span
          ><button class="icon-button" title="退出登录" @click="logout">
            <LogOut :size="18" />
          </button>
        </div>
      </header>
      <div class="content">
        <div class="page-title">
          <div>
            <span class="eyebrow">DIGITAL GOODS MANAGEMENT</span>
            <h1>{{ heading }}</h1>
            <p>
              {{
                page === "overview"
                  ? "查看商品、订单及待处理任务。"
                  : page === "products"
                    ? "管理商品、草稿与固定交付。"
                    : page === "orders"
                      ? "查看交易进度和交付状态。"
                      : page === "jobs"
                        ? "查看排队任务、执行状态和错误日志。"
                        : page === "users"
                          ? "为团队成员分配合适的后台权限。"
                          : page === "security"
                            ? "管理密码及已登录的会话。"
                            : "查看后台操作记录。"
              }}
            </p>
          </div>
          <div class="actions">
            <button :disabled="refreshing" @click="refresh">
              <RefreshCw
                :size="16"
                :class="{ spinning: refreshing }"
              />刷新</button
            ><button
              v-if="page === 'products' || page === 'overview'"
              class="primary"
              @click="createProduct"
            >
              <Plus :size="17" />新建商品</button
            ><button
              v-if="page === 'users'"
              class="primary"
              @click="
                Object.assign(newUser, {
                  username: '',
                  password: '',
                  role: 'operator',
                });
                modal = 'user';
              "
            >
              <Plus :size="17" />添加成员
            </button>
          </div>
        </div>
        <Transition name="feedback">
          <div v-if="error" class="toast error" role="alert">
            <AlertCircle :size="18" />{{ error
            }}<button @click="error = ''">×</button>
          </div>
        </Transition>
        <Transition name="feedback">
          <div v-if="notice" class="toast success" role="status">
            <CircleCheck :size="18" />{{ notice
            }}<button @click="notice = ''">×</button>
          </div>
        </Transition>
        <div :key="page" class="page-body">
          <Overview
            v-if="page === 'overview'"
            :metrics="metrics"
            :jobs="jobs"
            :accounts="accounts"
            :label="label"
            :date="date"
            @navigate="page = $event"
          />
          <ProductsView v-if="page === 'products'" />
          <OrdersView v-if="page === 'orders'" />
          <JobsView v-if="page === 'jobs'" />
          <UsersView v-if="page === 'users'" />
          <AuditView v-if="page === 'audit'" />
          <SecurityView v-if="page === 'security'" />
        </div>
        <footer>渔渔 · MyFish 商品管理</footer>
      </div>
    </div>
  </div>
</template>
