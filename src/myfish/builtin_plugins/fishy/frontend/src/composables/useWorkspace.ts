import { inject, onMounted, onUnmounted, provide, reactive, ref } from "vue";
import type { InjectionKey } from "vue";
import type {
  User,
  Product,
  Order,
  Job,
  Account,
  Audit,
  Session,
} from "../types";
import { createApiClient } from "../api/client";
import { useDialogs } from "./useDialogs";
import { useProductEditor } from "./useProductEditor";
const workspaceKey: InjectionKey<ReturnType<typeof provideWorkspace>> =
  Symbol("fishy-workspace");
export function provideWorkspace() {
  const me = ref<User | null>(null),
    setup = ref(false),
    ready = ref(false),
    busy = ref(false),
    refreshing = ref(false);
  const page = ref("overview"),
    search = ref(""),
    notice = ref(""),
    error = ref("");
  const products = ref<Product[]>([]),
    orders = ref<Order[]>([]),
    jobs = ref<Job[]>([]),
    users = ref<User[]>([]),
    accounts = ref<Account[]>([]),
    audits = ref<Audit[]>([]),
    sessions = ref<Session[]>([]);
  const metrics = ref({
    products: 0,
    pending_jobs: 0,
    orders: 0,
    attention: 0,
  });
  const login = reactive({ username: "", password: "", token: "" });

  const dialogs = useDialogs();
  const {
    modal,
    selected,
    deliveryContent,
    dialogTrigger,
    scheduleAt,
    action,
    newUser,
    password,
    resetPassword,
    selectedJob,
    jobLogs,
  } = dialogs;
  const api = createApiClient(
    () => me.value,
    () => {
      me.value = null;
    },
  );
  const editor = useProductEditor(api, accounts, busy, error, dialogs, run);
  async function refresh() {
    if (!me.value || refreshing.value) return;
    refreshing.value = true;
    try {
      const result = await Promise.all([
        api<Product[]>("/products"),
        api<Order[]>("/orders"),
        api<Job[]>("/jobs"),
        api<Account[]>("/accounts"),
        api<typeof metrics.value>("/dashboard"),
        api<Session[]>("/auth/sessions"),
      ]);
      [
        products.value,
        orders.value,
        jobs.value,
        accounts.value,
        metrics.value,
        sessions.value,
      ] = result;
      if (me.value?.role === "admin")
        [users.value, audits.value] = await Promise.all([
          api<User[]>("/users"),
          api<Audit[]>("/audit"),
        ]);
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      refreshing.value = false;
    }
  }
  async function run(task: () => Promise<unknown>, message = "操作已保存") {
    busy.value = true;
    error.value = "";
    notice.value = "";
    try {
      await task();
      deliveryContent.value = "";
      newUser.password = "";
      password.old_password = "";
      password.new_password = "";
      resetPassword.value = "";
      editor.draft.content = "";
      notice.value = message;
      modal.value = "";
      await refresh();
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      busy.value = false;
    }
  }
  async function signIn() {
    await run(async () => {
      if (setup.value) {
        await api("/auth/setup", "POST", login);
        setup.value = false;
      }
      await api("/auth/login", "POST", {
        username: login.username,
        password: login.password,
      });
      me.value = await api<User>("/auth/me");
      login.password = "";
      login.token = "";
    }, "欢迎回来");
  }
  async function logout() {
    await run(async () => {
      await api("/auth/logout", "POST");
      me.value = null;
    }, "已退出登录");
  }
  async function viewJobLogs(job: Job) {
    const trigger = document.activeElement as HTMLElement | null;
    busy.value = true;
    error.value = "";
    try {
      jobLogs.value = await api(`/jobs/${job.id}/logs`);
      selectedJob.value = job;
      dialogTrigger.value = trigger;
      modal.value = "jobLogs";
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      busy.value = false;
    }
  }
  async function editDeliveryContent(product: Product) {
    const trigger = document.activeElement as HTMLElement | null;
    busy.value = true;
    error.value = "";
    deliveryContent.value = "";
    try {
      const result = await api<{ content: string }>(
        `/products/${product.id}/delivery-content`,
      );
      selected.value = product;
      deliveryContent.value = result.content;
      dialogTrigger.value = trigger;
      modal.value = "deliveryContent";
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      busy.value = false;
    }
  }
  function schedule(product: Product, kind: string) {
    selected.value = product;
    action.value = kind;
    scheduleAt.value = "";
    modal.value = "schedule";
  }
  async function toggle(product: Product) {
    await run(
      () =>
        api(`/products/${product.id}/automation`, "PUT", {
          auto_delivery: !product.auto_delivery,
        }),
      "自动交付设置已更新",
    );
  }
  let timer: ReturnType<typeof setInterval>;
  onMounted(async () => {
    try {
      setup.value = (
        await api<{ needs_setup: boolean }>("/auth/status")
      ).needs_setup;
      if (!setup.value) {
        try {
          me.value = await api<User>("/auth/me");
        } catch {}
      }
      await refresh();
    } catch (e) {
      error.value = (e as Error).message;
    }
    ready.value = true;
    timer = setInterval(refresh, 15000);
  });
  onUnmounted(() => clearInterval(timer));

  const workspace = {
    me,
    setup,
    ready,
    busy,
    refreshing,
    page,
    search,
    notice,
    error,
    products,
    orders,
    jobs,
    users,
    accounts,
    audits,
    sessions,
    metrics,
    login,
    api,
    refresh,
    run,
    signIn,
    logout,
    viewJobLogs,
    editDeliveryContent,
    schedule,
    toggle,
    ...dialogs,
    ...editor,
  };
  provide(workspaceKey, workspace);
  return workspace;
}
export function useWorkspace() {
  const workspace = inject(workspaceKey);
  if (!workspace) throw new Error("Fishy workspace is unavailable");
  return workspace;
}
