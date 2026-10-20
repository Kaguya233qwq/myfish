import { nextTick, watch } from "vue";
import type { Ref } from "vue";

export function useDialogFocus(
  element: Ref<HTMLElement | null>,
  open: Ref<boolean>,
  busy: Ref<boolean>,
  close: () => void,
  origin?: Ref<HTMLElement | null>,
) {
  let trigger: HTMLElement | null = null;
  const controls = () =>
    Array.from(
      element.value?.querySelectorAll<HTMLElement>(
        'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]',
      ) || [],
    ).filter((control) => control.getClientRects().length > 0);

  watch(open, async (visible) => {
    if (visible) {
      trigger = origin?.value || (document.activeElement as HTMLElement | null);
      await nextTick();
      if (open.value) (controls()[0] || element.value)?.focus();
    } else {
      await nextTick();
      if (trigger?.isConnected) trigger.focus();
      trigger = null;
      if (origin) origin.value = null;
    }
  });

  return (event: KeyboardEvent) => {
    if (event.key === "Escape" && !busy.value) {
      event.preventDefault();
      close();
    }
    if (event.key !== "Tab") return;
    const available = controls();
    const first = available[0];
    const last = available[available.length - 1];
    if (!first) {
      event.preventDefault();
      element.value?.focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };
}
