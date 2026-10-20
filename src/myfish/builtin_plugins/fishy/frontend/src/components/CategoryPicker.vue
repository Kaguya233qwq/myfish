<script setup lang="ts">
import { computed, ref, watch } from "vue";

export interface Category {
  channelCatId: string;
  catName: string;
  [key: string]: string;
}
const props = defineProps<{
  account: string;
  productType: string;
  modelValue: Category | null;
  query: (keyword: string) => Promise<Category[]>;
  required: boolean;
}>();
const emit = defineEmits<{ "update:modelValue": [Category | null] }>();
const keyword = ref(""),
  options = ref<Category[]>([]),
  loading = ref(false),
  error = ref("");
let generation = 0;
const items = computed(() =>
  props.modelValue &&
  !options.value.some((c) => c.channelCatId === props.modelValue?.channelCatId)
    ? [props.modelValue, ...options.value]
    : options.value,
);
async function load() {
  const request = ++generation;
  options.value = [];
  error.value = "";
  if (!props.account) {
    loading.value = false;
    return;
  }
  loading.value = true;
  try {
    const result = await props.query(keyword.value);
    if (request === generation) options.value = result;
  } catch (exc) {
    if (request === generation)
      error.value = exc instanceof Error ? exc.message : "品类加载失败";
  } finally {
    if (request === generation) loading.value = false;
  }
}
watch(
  () => [props.account, props.productType],
  (_, old) => {
    if (old) {
      emit("update:modelValue", null);
      keyword.value = "";
    }
    void load();
  },
  { immediate: true },
);
function choose(event: Event) {
  emit(
    "update:modelValue",
    items.value.find(
      (c) => c.channelCatId === (event.target as HTMLSelectElement).value,
    ) || null,
  );
}
</script>

<template>
  <div class="category-picker">
    <label
      >查询品类关键词<input
        v-model="keyword"
        maxlength="200"
        placeholder="例如：电子资料、T恤、手机"
        @keydown.enter.prevent="load"
    /></label>
    <button type="button" :disabled="loading || !account" @click="load">
      {{ loading ? "加载品类中…" : "查询品类" }}
    </button>
    <label
      >商品品类<select
        :value="modelValue?.channelCatId || ''"
        :required="required"
        :disabled="loading || !account"
        @change="choose"
      >
        <option value="" disabled>请选择品类</option>
        <option
          v-for="c in items"
          :key="c.channelCatId"
          :value="c.channelCatId"
        >
          {{ c.catName }} · {{ c.channelCatId }}
        </option>
      </select></label
    >
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <p v-else class="hint">
      {{
        !account
          ? "请先选择闲鱼账号。"
          : !loading && !items.length
            ? "暂无匹配品类，请更换关键词重试。"
            : "请选择适合商品的品类。"
      }}
    </p>
  </div>
</template>
