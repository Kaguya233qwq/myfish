import type { User } from "../types";
import { message } from "../utils/format";
export function createApiClient(
  session: () => User | null,
  unauthorized: () => void,
) {
  async function request<T = unknown>(
    path: string,
    method = "GET",
    body?: unknown,
  ): Promise<T> {
    const headers: Record<string, string> = {};
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (session()?.csrf) headers["X-CSRF-Token"] = session()!.csrf;
    const response = await fetch("/api" + path, {
      method,
      headers,
      credentials: "same-origin",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json();
    if (!response.ok) {
      if (response.status === 401) unauthorized();
      throw new Error(
        typeof data.detail === "string"
          ? message(data.detail)
          : "请求内容不符合要求，请检查输入",
      );
    }
    return data as T;
  }

  return request;
}
export type ApiClient = ReturnType<typeof createApiClient>;
