<script setup lang="ts">
import { useWorkspace } from "../composables/useWorkspace";
import { ShoppingBag, RefreshCw } from "@lucide/vue";
import { label, date, message } from "../utils/format";

const { busy, orders, accounts, api, run, modal, selectedOrder, chatId } =
  useWorkspace();
</script>
<template>
  <section class="panel">
    <div class="table-toolbar">
      <span>最近 {{ orders.length }} 笔订单</span
      ><button
        v-for="a in accounts"
        :key="a.id"
        :disabled="busy"
        @click="
          run(() => api('/accounts/' + a.id + '/sync', 'POST'), '订单已同步')
        "
      >
        <RefreshCw :size="15" />同步 {{ a.id }}
      </button>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>订单</th>
            <th>订单金额</th>
            <th>交易对象</th>
            <th>数量</th>
            <th>交易状态</th>
            <th>交付状态</th>
            <th>更新时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="o in orders" :key="o.key">
            <td>
              <b>{{ o.order_id }}</b
              ><small>{{ o.account_id }}</small>
            </td>
            <td>{{ o.amount ? "¥" + o.amount : "—" }}</td>
            <td>
              <b>{{ o.buyer_name || "—" }}</b>
              <small>ID: {{ o.buyer_id || "—" }}</small>
            </td>
            <td>{{ o.quantity }}</td>
            <td>
              <span class="badge" :data-state="o.platform_status">{{
                label(o.platform_status)
              }}</span>
            </td>
            <td>
              <template v-if="o.product_type === 'physical'"
                >快递发货<small>以交易状态为准</small></template
              >
              <span v-else class="badge" :data-state="o.delivery_state">{{
                label(o.delivery_state)
              }}</span
              ><small class="error-text" v-if="o.error">{{
                message(o.error)
              }}</small
              ><small v-if="!o.chat_id && o.product_type !== 'physical'"
                >交付时自动查找会话</small
              >
            </td>
            <td>{{ date(o.updated) }}</td>
            <td>
              <div class="row-actions">
                <button
                  v-if="!o.chat_id && o.product_type !== 'physical'"
                  @click="
                    selectedOrder = o;
                    chatId = '';
                    modal = 'chat';
                  "
                >
                  关联会话</button
                ><button
                  v-if="
                    o.product_type !== 'physical' &&
                    o.delivery_mode !== 'disabled'
                  "
                  :disabled="
                    busy || ['sending', 'completed'].includes(o.delivery_state)
                  "
                  @click="
                    run(
                      () => api('/orders/' + o.key + '/deliver', 'POST'),
                      '交付任务已加入队列',
                    )
                  "
                >
                  {{
                    o.delivery_state === "sent" ? "继续确认发货" : "请求交付"
                  }}
                </button>
                <span v-if="o.product_type === 'physical'" class="hint"
                  >请在闲鱼 App 寄件并填写物流</span
                >
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!orders.length" class="empty">
        <ShoppingBag :size="36" />
        <h3>暂无订单</h3>
        <p>关联闲鱼商品后可同步交易订单。</p>
      </div>
    </div>
  </section>
</template>
