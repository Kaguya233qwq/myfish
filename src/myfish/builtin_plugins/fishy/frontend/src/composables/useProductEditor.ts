import { reactive } from "vue";
import type { Ref } from "vue";
import type { Account, Product } from "../types";
import type { Category } from "../components/CategoryPicker.vue";
import type { ApiClient } from "../api/client";
import type { useDialogs } from "./useDialogs";
export function useProductEditor(
  api: ApiClient,
  accounts: Ref<Account[]>,
  busy: Ref<boolean>,
  error: Ref<string>,
  dialogs: ReturnType<typeof useDialogs>,
  run: (task: () => Promise<unknown>, message?: string) => Promise<void>,
) {
  const { editing, modal } = dialogs;
  const draft = reactive({
    title: "",
    description: "",
    price: "9.90",
    account_id: "",
    item_id: "",
    mode: "reusable",
    product_type: "virtual",
    category: null as Category | null,
    quantity: 1,
    shipping: "free",
    postage: "0",
    content: "",
    images: [] as string[],
    auto_delivery: false,
    auto_polish: false,
    polish_interval_hours: 24,
  });
  function createProduct() {
    editing.value = null;
    Object.assign(draft, {
      title: "",
      description: "",
      price: "9.90",
      account_id: accounts.value[0]?.id || "",
      item_id: "",
      mode: "reusable",
      product_type: "virtual",
      category: null,
      quantity: 1,
      shipping: "free",
      postage: "0",
      content: "",
      images: [],
      auto_delivery: false,
      auto_polish: false,
      polish_interval_hours: 24,
    });
    modal.value = "product";
  }
  function editProduct(product: Product) {
    editing.value = product.id;
    Object.assign(draft, {
      ...product,
      mode: product.product_type === "physical" ? "physical" : "reusable",
      content: "",
      images: JSON.parse(product.images),
      category: Object.keys(JSON.parse(product.category)).length
        ? JSON.parse(product.category)
        : null,
      auto_delivery: Boolean(product.auto_delivery),
      auto_polish: Boolean(product.auto_polish),
    });
    modal.value = "product";
  }
  async function saveProduct() {
    await run(
      () =>
        api(
          editing.value ? "/products/" + editing.value : "/products",
          editing.value ? "PUT" : "POST",
          draft,
        ),
      draft.item_id ? "商品档案已保存" : "草稿已保存，同步任务已加入队列",
    );
  }
  function changeProductType() {
    draft.mode = draft.product_type === "physical" ? "physical" : "reusable";
    draft.content = "";
    draft.auto_delivery = false;
    draft.category = null;
  }
  function queryCategories(keyword: string) {
    return api<Category[]>("/catalog/categories", "POST", {
      account_id: draft.account_id,
      product_type: draft.product_type,
      keyword: keyword || draft.title,
    });
  }
  async function upload(event: Event) {
    const files = (event.target as HTMLInputElement).files;
    if (!files) return;
    busy.value = true;
    try {
      for (const file of Array.from(files)) {
        if (draft.images.length >= 9 || file.size > 10 * 1024 * 1024)
          throw new Error("最多 9 张图片，每张不超过 10 MiB");
        const data = await new Promise<string>((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () =>
            resolve(String(reader.result).split(",")[1] || "");
          reader.onerror = reject;
          reader.readAsDataURL(file);
        });
        draft.images.push(
          (await api<{ name: string }>("/media", "POST", { data })).name,
        );
      }
    } catch (e) {
      error.value = (e as Error).message;
    } finally {
      busy.value = false;
    }
  }

  return {
    draft,
    createProduct,
    editProduct,
    saveProduct,
    changeProductType,
    queryCategories,
    upload,
  };
}
