<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { message } from "../utils/format";

interface Candidate {
  title: string;
  description: string;
  price: string;
  quantity: number;
  shipping: string;
  postage: string;
  images: string[];
}
interface EditorData {
  revision: string;
  state: string;
  error: string;
  candidate: Candidate;
  product_type: string;
  mode: string;
  remote_images: { ref: string; url: string }[];
}
const props = defineProps<{
  productId: number;
  request: <T = unknown>(
    path: string,
    method?: string,
    body?: unknown,
  ) => Promise<T>;
}>();
const emit = defineEmits<{ busy: [value: boolean]; updated: [] }>();
const data = ref<EditorData | null>(null),
  busy = ref(false),
  error = ref(""),
  notice = ref("");
const editable = computed(
  () => data.value && ["draft", "failed"].includes(data.value.state),
);
const states: Record<string, string> = {
  draft: "编辑草稿",
  queued: "编辑排队中",
  running: "正在提交编辑",
  reviewing: "审核中",
  uncertain: "编辑结果待核对，请查看任务日志",
  failed: "提交失败",
  applied: "编辑已生效",
};
async function perform(task: () => Promise<void>) {
  busy.value = true;
  emit("busy", true);
  error.value = "";
  try {
    await task();
  } catch (e) {
    error.value = (e as Error).message;
  } finally {
    busy.value = false;
    emit("busy", false);
  }
}
async function load(reload = false) {
  await perform(async () => {
    data.value = await props.request<EditorData>(
      `/products/${props.productId}/listing-edit/open${reload ? "?reload=true" : ""}`,
      "POST",
      {},
    );
    emit("updated");
  });
}
async function save(submit: boolean) {
  await perform(async () => {
    const current = data.value!;
    const result = await props.request<{ revision: string }>(
      `/products/${props.productId}/listing-edit`,
      "PUT",
      { revision: current.revision, candidate: current.candidate, submit },
    );
    current.revision = result.revision;
    current.state = submit ? "queued" : "draft";
    current.error = "";
    notice.value = submit ? "编辑任务已加入队列" : "草稿已保存";
    emit("updated");
  });
}
async function upload(event: Event) {
  const input = event.target as HTMLInputElement;
  const files = Array.from(input.files || []);
  await perform(async () => {
    for (const file of files) {
      if (data.value!.candidate.images.length >= 9)
        throw new Error("最多支持 9 张图片");
      if (file.size > 10 * 1024 * 1024)
        throw new Error("每张图片不能超过 10 MiB");
      const encoded = await new Promise<string>((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () =>
          resolve(String(reader.result).split(",")[1] || "");
        reader.onerror = reject;
        reader.readAsDataURL(file);
      });
      const result = await props.request<{ name: string }>("/media", "POST", {
        data: encoded,
      });
      data.value!.candidate.images.push(result.name);
    }
  });
  input.value = "";
}
function imageUrl(ref: string) {
  if (!ref.startsWith("remote:")) return "/api/media/" + ref;
  const url =
    data.value?.remote_images.find((image) => image.ref === ref)?.url || "";
  return url.startsWith("http://") ? "https://" + url.slice(7) : url;
}
function move(index: number, delta: number) {
  const images = data.value!.candidate.images,
    target = index + delta;
  if (target < 0 || target >= images.length) return;
  [images[index], images[target]] = [images[target]!, images[index]!];
}
onMounted(() => load());
</script>

<template>
  <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
  <p v-if="notice" class="hint" role="status">{{ notice }}</p>
  <form v-if="data" @submit.prevent="save(true)">
    <p class="hint">{{ states[data.state] || data.state }}</p>
    <p v-if="data.error" class="inline-error">{{ message(data.error) }}</p>
    <fieldset :disabled="busy || !editable" class="listing-edit-fields">
      <label
        >商品标题<input v-model="data.candidate.title" maxlength="80" required
      /></label>
      <label
        >商品描述<textarea
          v-model="data.candidate.description"
          maxlength="5000"
          rows="6"
        ></textarea>
      </label>
      <div class="form-grid">
        <label
          >商品售价（元）<input
            v-model="data.candidate.price"
            type="number"
            min="0.01"
            max="1000000"
            step="0.01"
            required
        /></label>
        <label v-if="data.product_type === 'physical'"
          >可售数量<input
            v-model.number="data.candidate.quantity"
            type="number"
            min="1"
            max="9999"
            required
        /></label>
        <label v-if="data.product_type === 'physical'"
          >商品配送<select v-model="data.candidate.shipping">
            <option value="keep">原配送设置</option>
            <option value="free">包邮</option>
            <option value="fixed">固定运费</option>
          </select></label
        >
        <label v-if="data.candidate.shipping === 'fixed'"
          >商品运费（元）<input
            v-model="data.candidate.postage"
            type="number"
            min="0.01"
            max="100000"
            step="0.01"
            required
        /></label>
      </div>
      <div class="listing-edit-images">
        <div v-for="(image, index) in data.candidate.images" :key="image">
          <img :src="imageUrl(image)" :alt="'商品图片 ' + (index + 1)" />
          <div class="row-actions">
            <button
              type="button"
              :disabled="index === 0"
              @click="move(index, -1)"
            >
              前移</button
            ><button
              type="button"
              :disabled="index === data.candidate.images.length - 1"
              @click="move(index, 1)"
            >
              后移</button
            ><button
              type="button"
              @click="data.candidate.images.splice(index, 1)"
            >
              移除
            </button>
          </div>
        </div>
      </div>
      <label
        >添加商品图片<input
          type="file"
          accept="image/*"
          multiple
          @change="upload"
      /></label>
      <p class="hint">至少上传一张图片。提交后可能需要审核。</p>
    </fieldset>
    <div class="modal-actions">
      <button
        v-if="['draft', 'failed', 'applied'].includes(data.state)"
        type="button"
        :disabled="busy"
        @click="load(true)"
      >
        放弃草稿并重新加载
      </button>
      <button v-else type="button" :disabled="busy" @click="load()">
        刷新编辑结果
      </button>
      <button
        v-if="editable"
        type="button"
        :disabled="busy || !data.candidate.images.length"
        @click="save(false)"
      >
        保存草稿
      </button>
      <button
        v-if="editable"
        class="primary"
        :disabled="busy || !data.candidate.images.length"
      >
        提交编辑并发布
      </button>
    </div>
  </form>
  <p v-else-if="busy" class="hint">正在读取在售商品…</p>
</template>

<style scoped>
.listing-edit-fields {
  border: 0;
  padding: 0;
  margin: 0;
  min-width: 0;
}
.listing-edit-images {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
  margin: 12px 0;
}
.listing-edit-images img {
  width: 140px;
  height: 120px;
  object-fit: cover;
  border-radius: 8px;
}
.listing-edit-images button {
  padding: 4px 6px;
}
</style>
