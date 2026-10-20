<script setup lang="ts">
import { useWorkspace } from "../../composables/useWorkspace";
import { X } from "@lucide/vue";
import PasswordForm from "./PasswordForm.vue";
import ResetUserForm from "./ResetUserForm.vue";
import UserForm from "./UserForm.vue";
import ChatForm from "./ChatForm.vue";
import BindForm from "./BindForm.vue";
import ScheduleForm from "./ScheduleForm.vue";
import DeliveryContentForm from "./DeliveryContentForm.vue";
import PolishForm from "./PolishForm.vue";
import ProductForm from "./ProductForm.vue";
import JobLogs from "./JobLogs.vue";
import PublishedEditor from "../PublishedEditor.vue";
import { computed, ref } from "vue";
import { useDialogFocus } from "../../composables/useDialogFocus";

const {
  me,
  busy,
  error,
  api,
  refresh,
  modal,
  selected,
  modalTitle,
  dialogTrigger,
} = useWorkspace();
const dialogElement = ref<HTMLElement | null>(null);
const onKeydown = useDialogFocus(
  dialogElement,
  computed(() => Boolean(modal.value && me.value)),
  busy,
  () => {
    modal.value = "";
  },
  dialogTrigger,
);
</script>
<template>
  <Transition name="dialog">
    <div
      v-if="modal && me"
      class="modal-backdrop"
      @click.self="!busy && (modal = '')"
    >
      <section
        ref="dialogElement"
        tabindex="-1"
        @keydown="onKeydown"
        class="modal"
        :class="{ wide: modal === 'product' || modal === 'published_edit' }"
        role="dialog"
        aria-modal="true"
        :aria-label="
          modal === 'product'
            ? '新建商品'
            : modal === 'published_edit'
              ? '编辑在售商品'
              : '管理操作'
        "
      >
        <div class="modal-heading">
          <h2>{{ modalTitle }}</h2>
          <button
            class="icon-button"
            aria-label="关闭弹窗"
            :disabled="busy"
            @click="modal = ''"
          >
            <X :size="20" />
          </button>
        </div>
        <p v-if="error" class="inline-error">{{ error }}</p>
        <PublishedEditor
          v-if="modal === 'published_edit' && selected"
          :product-id="selected.id"
          :request="api"
          @busy="busy = $event"
          @updated="refresh()"
        />
        <ProductForm v-if="modal === 'product'" />
        <PolishForm v-if="modal === 'polish'" />
        <DeliveryContentForm v-if="modal === 'deliveryContent'" />
        <ScheduleForm v-if="modal === 'schedule'" />
        <BindForm v-if="modal === 'bind'" />
        <ChatForm v-if="modal === 'chat'" />
        <UserForm v-if="modal === 'user'" />
        <ResetUserForm v-if="modal === 'resetUser'" />
        <JobLogs v-if="modal === 'jobLogs'" />
        <PasswordForm v-if="modal === 'password'" />
      </section>
    </div>
  </Transition>
</template>
