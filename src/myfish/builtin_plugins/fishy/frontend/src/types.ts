export interface User {
  id: number;
  username: string;
  role: string;
  active?: number;
  csrf: string;
  session_id: number;
}
export interface Product {
  edit_state: string;
  auto_polish: number;
  polish_interval_hours: number;
  polish_next_at: number;
  polish_last_at: number;
  polish_status: string;
  polish_error: string;
  draft_id: string;
  draft_state: string;
  draft_synced_at: number;
  draft_error: string | null;
  product_type: "virtual" | "physical" | "unknown";
  category: string;
  quantity: number;
  shipping: "free" | "fixed";
  postage: string;
  id: number;
  title: string;
  description: string;
  price: string;
  account_id: string;
  item_id: string;
  mode: string;
  images: string;
  auto_delivery: number;
  status: string;
}
export interface Order {
  delivery_mode: string;
  product_type: "virtual" | "physical" | "unknown";
  key: string;
  order_id: string;
  amount: string;
  buyer_id: string;
  buyer_name: string;
  account_id: string;
  quantity: number;
  chat_id: string;
  platform_status: string;
  delivery_state: string;
  error: string;
  updated: number;
}
export interface Job {
  id: number;
  kind: string;
  target: string;
  status: string;
  run_at: number;
  error: string;
}
export interface Account {
  id: string;
  connected: boolean;
}
export interface Audit {
  id: number;
  actor: string;
  action: string;
  target: string;
  created: number;
}
export interface Session {
  id: number;
  created: number;
  expires: number;
}
