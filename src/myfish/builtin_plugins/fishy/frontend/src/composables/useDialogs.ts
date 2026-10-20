import { computed, reactive, ref } from "vue";
import type { Product, Order, User, Job } from "../types";
import { label } from "../utils/format";
export function useDialogs() {
  const modal = ref(""),
    selected = ref<Product | null>(null),
    selectedOrder = ref<Order | null>(null);
  const polishForm = reactive({ enabled: false, interval_hours: 24 });
  const deliveryContent = ref("");
  const dialogTrigger = ref<HTMLElement | null>(null);
  const scheduleAt = ref(""),
    action = ref("publish"),
    bindId = ref(""),
    chatId = ref("");
  const newUser = reactive({ username: "", password: "", role: "operator" }),
    password = reactive({ old_password: "", new_password: "" });
  const selectedUser = ref<User | null>(null),
    resetPassword = ref(""),
    selectedJob = ref<Job | null>(null),
    editing = ref<number | null>(null);
  const jobLogs = ref<{ message: string; location: string; created: number }[]>(
    [],
  );
  const modalTitle = computed(() => {
    const titles: Record<string, string> = {
      product: editing.value ? "编辑草稿" : "新建商品",
      published_edit: "编辑在售商品",
      deliveryContent: "编辑交付内容",
      schedule: label(action.value),
      user: "添加团队成员",
      bind: "关联平台商品",
      chat: "关联买家会话",
      password: "修改登录密码",
      resetUser: "重置成员密码",
      jobLogs: "任务错误日志",
      polish: "自动擦亮设置",
    };
    return titles[modal.value] || "管理操作";
  });

  return {
    modal,
    selected,
    selectedOrder,
    polishForm,
    deliveryContent,
    dialogTrigger,
    scheduleAt,
    action,
    bindId,
    chatId,
    newUser,
    password,
    selectedUser,
    resetPassword,
    selectedJob,
    editing,
    jobLogs,
    modalTitle,
  };
}
