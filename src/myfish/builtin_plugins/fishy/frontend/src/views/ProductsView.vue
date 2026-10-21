<script setup lang="ts">
import { useWorkspace } from "../composables/useWorkspace";
import { Package, Plus, RefreshCw, Search } from "@lucide/vue";
import { label, date, money, image, message } from "../utils/format";
import { computed } from "vue";
import type { Product } from "../types";

const {
  busy,
  accounts,
  products,
  search,
  api,
  run,
  editDeliveryContent,
  schedule,
  toggle,
  modal,
  selected,
  polishForm,
  bindId,
  createProduct,
  editProduct,
} = useWorkspace();
const filtered = computed(() =>
  products.value.filter((product) =>
    `${product.title} ${product.item_id}`.includes(search.value),
  ),
);
async function configureType(product: Product, event: Event) {
  const target = event.target as HTMLSelectElement;
  const productType = target.value;
  target.value = "";
  if (productType !== "virtual" && productType !== "physical") return;
  await run(
    () =>
      api(`/products/${product.id}/product-type`, "PUT", {
        product_type: productType,
      }),
    "商品类型已设置",
  );
}
</script>
<template>
  <section class="panel">
    <div class="table-toolbar">
      <span
        ><b>{{ products.length }}</b> 个商品</span
      >
      <button
        v-for="account in accounts"
        :key="account.id"
        :disabled="busy || !account.connected"
        @click="
          run(
            () => api('/accounts/' + account.id + '/products/sync', 'POST'),
            '在售商品已同步',
          )
        "
      >
        <RefreshCw :size="15" />同步商品 {{ account.id }}
      </button>
      <div class="search">
        <Search :size="16" /><input
          v-model="search"
          placeholder="搜索商品名称或平台 ID"
        />
      </div>
    </div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>商品</th>
            <th>价格</th>
            <th>交付方式</th>
            <th>自动交付</th>
            <th>商品擦亮</th>
            <th>状态</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in filtered" :key="p.id">
            <td>
              <div class="product-name">
                <img
                  v-if="image(p)"
                  :src="'/api/media/' + image(p)"
                  alt="商品图片"
                /><span v-else class="product-image"
                  ><Package :size="22"
                /></span>
                <div>
                  <b>{{ p.title }}</b
                  ><small
                    >{{
                      p.product_type === "unknown"
                        ? "待设置商品类型"
                        : p.product_type === "physical"
                          ? "实物商品"
                          : "虚拟商品"
                    }}
                    ·
                    {{ JSON.parse(p.category).catName || "未选品类" }}</small
                  ><small>{{
                    p.item_id ? "闲鱼 ID · " + p.item_id : "尚未关联平台商品"
                  }}</small>
                </div>
              </div>
            </td>
            <td>
              <b>¥ {{ money(p.price) }}</b>
            </td>
            <td>
              <select
                v-if="p.product_type === 'unknown'"
                :disabled="busy"
                aria-label="设置商品类型"
                @change="configureType(p, $event)"
              >
                <option value="">请选择商品类型</option>
                <option value="virtual">虚拟商品</option>
                <option value="physical">实物商品</option>
              </select>
              <template v-else-if="p.mode === 'disabled'"
                >未设置交付内容</template
              ><template v-else-if="p.product_type === 'physical'"
                >快递发货<small
                  >发布数量 {{ p.quantity }} ·
                  {{
                    p.shipping === "free" ? "包邮" : "运费 ¥" + p.postage
                  }}</small
                ></template
              ><template v-else>固定内容<small>可重复交付</small></template>
            </td>
            <td>
              <button
                class="switch"
                :class="{ on: p.auto_delivery }"
                :aria-pressed="Boolean(p.auto_delivery)"
                :disabled="busy || p.mode !== 'reusable'"
                @click="toggle(p)"
              >
                <span></span>
              </button>
            </td>
            <td>
              <button
                class="text-button"
                @click="
                  selected = p;
                  polishForm.enabled = Boolean(p.auto_polish);
                  polishForm.interval_hours = p.polish_interval_hours;
                  modal = 'polish';
                "
              >
                {{ p.auto_polish ? "自动擦亮已开启" : "设置自动擦亮" }}
              </button>
              <small>{{
                p.polish_status === "queued"
                  ? "等待擦亮"
                  : p.polish_status === "running"
                    ? "擦亮中"
                    : label(p.polish_status)
              }}</small>
              <small v-if="p.polish_last_at"
                >最近成功 · {{ date(p.polish_last_at) }}</small
              >
              <small v-if="p.polish_next_at"
                >下次可执行 · {{ date(p.polish_next_at) }}</small
              >
              <small v-if="p.polish_error" class="error-text">{{
                message(p.polish_error)
              }}</small>
            </td>
            <td>
              <span class="badge" :data-state="p.status">{{
                label(p.status)
              }}</span>
              <small v-if="!p.item_id">{{ label(p.draft_state) }}</small>
              <small v-if="p.status === 'reviewing'"
                >审核中，请稍后同步状态</small
              >
              <small v-if="p.status === 'status_unknown'"
                >商品状态待核对，请同步状态</small
              >
              <small
                v-if="
                  !p.item_id &&
                  p.draft_error &&
                  ['local', 'dirty', 'uncertain'].includes(p.draft_state)
                "
                class="error-text"
                >草稿同步提示：{{ message(p.draft_error) }}</small
              >
              <small v-if="!p.item_id && p.draft_id"
                >草稿 ID · {{ p.draft_id }}</small
              >
            </td>
            <td>
              <div class="row-actions">
                <button
                  v-if="!p.item_id && p.status === 'draft'"
                  :disabled="
                    busy ||
                    ['queued', 'syncing', 'uncertain'].includes(p.draft_state)
                  "
                  @click="editProduct(p)"
                >
                  编辑</button
                ><button
                  v-if="p.item_id && (p.status === 'published' || p.edit_state)"
                  :disabled="busy"
                  @click="
                    selected = p;
                    modal = 'published_edit';
                  "
                >
                  {{
                    ["queued", "running", "reviewing", "uncertain"].includes(
                      p.edit_state,
                    )
                      ? "查看商品编辑"
                      : "编辑并发布"
                  }}</button
                ><button
                  v-if="
                    !p.item_id && ['local', 'dirty'].includes(p.draft_state)
                  "
                  :disabled="busy"
                  @click="
                    run(
                      () =>
                        api('/products/' + p.id + '/save_draft', 'POST', {}),
                      '草稿已重新加入同步队列',
                    )
                  "
                >
                  重试同步草稿</button
                ><button
                  v-if="
                    p.product_type === 'virtual' &&
                    ['reusable', 'disabled'].includes(p.mode)
                  "
                  :disabled="busy"
                  @click="editDeliveryContent(p)"
                >
                  编辑交付内容</button
                ><button
                  v-if="!p.item_id"
                  @click="
                    selected = p;
                    bindId = '';
                    modal = 'bind';
                  "
                >
                  关联 ID</button
                ><button
                  v-if="!p.item_id && p.status !== 'publishing'"
                  :disabled="
                    busy ||
                    ['queued', 'syncing', 'uncertain'].includes(p.draft_state)
                  "
                  @click="schedule(p, 'publish')"
                >
                  上架</button
                ><button
                  v-if="p.item_id && p.status === 'published'"
                  @click="schedule(p, 'delist')"
                >
                  下架
                </button>
                <button
                  v-if="p.item_id && p.status === 'unlisted'"
                  :disabled="busy"
                  @click="schedule(p, 'reshelf')"
                >
                  恢复上架
                </button>
                <button
                  v-if="p.item_id"
                  :disabled="busy"
                  @click="
                    run(
                      () =>
                        api(
                          '/products/' + p.id + '/refresh_listing',
                          'POST',
                          {},
                        ),
                      '商品状态已同步',
                    )
                  "
                >
                  同步状态
                </button>
                <button
                  v-if="p.item_id && p.status === 'published'"
                  :disabled="
                    busy ||
                    ['queued', 'running', 'uncertain'].includes(
                      p.polish_status,
                    ) ||
                    p.polish_next_at > Date.now() / 1000
                  "
                  @click="
                    run(
                      () => api('/products/' + p.id + '/polish', 'POST', {}),
                      '擦亮任务已加入队列',
                    )
                  "
                >
                  立即擦亮
                </button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!filtered.length" class="empty">
        <Package :size="36" />
        <h3>
          {{ products.length ? "没有匹配的商品" : "暂无商品" }}
        </h3>
        <p>支持固定文本、下载链接和实物商品。</p>
        <button v-if="!products.length" class="primary" @click="createProduct">
          <Plus :size="16" />新建商品
        </button>
      </div>
    </div>
  </section>
</template>
