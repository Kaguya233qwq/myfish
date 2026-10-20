import type { Product } from "../types";
export const labels: Record<string, string> = {
  draft: "草稿",
  published: "在售",
  unlisted: "已下架",
  reviewing: "审核中",
  status_unknown: "状态待确认",
  publishing: "发布待确认",
  failed: "处理失败",
  uncertain: "结果待核对",
  pending: "待处理",
  running: "执行中",
  succeeded: "已完成",
  cancelled: "已取消",
  reserved: "待交付",
  sending: "发送待核对",
  sent: "待确认发货",
  completed: "已完成",
  pending_payment: "待付款",
  pending_ship: "待发货",
  shipped: "已发货",
  closed: "已关闭",
  refunding: "退款中",
  refunded: "已退款",
  unknown: "未知状态",
  publish: "发布商品",
  delist: "下架商品",
  reshelf: "恢复上架",
  edit: "编辑在售商品",
  deliver: "自动交付",
  save_draft: "同步闲鱼草稿",
  polish: "擦亮商品",
  polished: "已擦亮",
  already_polished: "已擦亮",
  never: "尚未擦亮",
  paused: "已暂停",
  waiting: "等待擦亮",
  ineligible: "不支持擦亮",
  error: "擦亮失败",
  skipped: "已跳过",
  local: "尚未同步",
  queued: "等待同步草稿",
  syncing: "草稿同步中",
  synced: "已同步闲鱼草稿箱",
  dirty: "修改尚未同步",
};
export const label = (key: string) => labels[key] || key;
export function message(text: string) {
  if (text === "平台确认该商品已擦亮，本次未新增擦亮") return "已擦亮";
  const cooldown = "平台已确认该商品已擦亮，下次可擦亮时间：";
  if (text.startsWith(cooldown))
    return "已擦亮，下次擦亮时间：" + text.slice(cooldown.length);
  return text;
}
export const date = (time: number) =>
  new Date(time * 1000).toLocaleString("zh-CN", { hour12: false });
export const money = (value: string) => Number(value).toFixed(2);
export const image = (p: Product) => {
  try {
    return (JSON.parse(p.images) as string[])[0];
  } catch {
    return "";
  }
};
