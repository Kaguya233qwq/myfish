<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";
import { Upload } from "@lucide/vue";
import CategoryPicker from "../CategoryPicker.vue";

const {
  busy,
  accounts,
  modal,
  editing,
  draft,
  saveProduct,
  changeProductType,
  queryCategories,
  upload,
} = useWorkspace();
</script>
<template>
  <form @submit.prevent="saveProduct">
    <label
      >商品类型<select v-model="draft.product_type" @change="changeProductType">
        <option value="virtual">虚拟商品</option>
        <option value="physical">实物商品</option>
      </select></label
    >
    <div class="form-grid">
      <label
        >商品名称<input
          v-model="draft.title"
          maxlength="80"
          required
          placeholder="例如：设计素材包" /></label
      ><label
        >售价（元）<input
          v-model="draft.price"
          type="number"
          min="0.01"
          max="1000000"
          step="0.01"
          required /></label
      ><label
        >闲鱼账号<select v-model="draft.account_id" required>
          <option value="" disabled>选择已登录账号</option>
          <option v-for="a in accounts" :key="a.id" :value="a.id">
            {{ a.id }}
          </option>
        </select></label
      >
    </div>
    <CategoryPicker
      v-model="draft.category"
      :account="draft.account_id"
      :product-type="draft.product_type"
      :query="queryCategories"
      :required="!draft.item_id"
    />
    <div v-if="draft.product_type === 'physical'" class="form-grid">
      <label
        >发布数量<input
          v-model.number="draft.quantity"
          type="number"
          min="1"
          max="9999"
          required
      /></label>
      <label
        >配送运费<select v-model="draft.shipping">
          <option value="free">包邮</option>
          <option value="fixed">固定运费</option>
        </select></label
      >
      <label v-if="draft.shipping === 'fixed'"
        >运费（元）<input
          v-model="draft.postage"
          type="number"
          min="0.01"
          max="100000"
          step="0.01"
          required
      /></label>
      <p class="hint">请在闲鱼 App 完成寄件并填写物流单号。</p>
    </div>
    <label
      >商品描述<textarea
        v-model="draft.description"
        rows="3"
        maxlength="5000"
        placeholder="填写商品介绍和使用说明"
      ></textarea></label
    ><label v-if="draft.mode === 'reusable'"
      >交付内容<textarea
        v-model="draft.content"
        rows="3"
        maxlength="10000"
        :required="!editing"
        placeholder="买家付款后接收的文本或链接"
      ></textarea></label
    ><label
      >商品图片<span class="hint"
        >草稿自动同步至闲鱼草稿箱，发布前请上传商品图片。</span
      >
      <div class="image-list">
        <img
          v-for="name in draft.images"
          :key="name"
          :src="'/api/media/' + name"
          alt="已上传图片"
        /><label class="upload"
          ><Upload :size="20" /><span>上传图片</span
          ><input
            type="file"
            accept="image/*"
            multiple
            :disabled="busy"
            @change="upload"
        /></label></div></label
    ><label
      >已有闲鱼商品 ID（可选）<input
        v-model="draft.item_id"
        pattern="[0-9]*"
        placeholder="已有商品请填写 ID，新商品可留空"
    /></label>
    <div class="checks">
      <label
        ><input
          v-model="draft.auto_polish"
          type="checkbox"
        />上架后自动擦亮</label
      >
      <label v-if="draft.product_type === 'virtual'"
        ><input
          v-model="draft.auto_delivery"
          type="checkbox"
        />开启自动交付</label
      >
    </div>
    <div class="modal-actions">
      <button type="button" @click="modal = ''">取消</button
      ><button class="primary" :disabled="busy || !accounts.length">
        {{
          busy
            ? "保存中…"
            : draft.item_id
              ? "保存商品档案"
              : editing
                ? "保存并同步草稿"
                : "添加并同步草稿"
        }}
      </button>
    </div>
  </form>
</template>
