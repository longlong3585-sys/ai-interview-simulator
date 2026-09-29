/**
 * T-49 / FR-9.2：题库条目的**防御性读取** + 条目类型定义。
 *
 * 为什么要单独一个文件：本题库条目来自后端 JSON，`tags` 字段可能整个缺失
 * （历史数据、后端改字段、降级返回）。修复前 `QuestionBankModal` 里直接写
 * `q.tags.map(...)` —— 一条缺字段的题目就让整个题库弹窗白屏。
 *
 * 为什么**刻意不 import `../config`**：`config.ts` 读的是 `import.meta.env`，
 * 在 Node 里是 `undefined`，一旦被 import 就会抛。本文件是纯逻辑，
 * 因此能被 `tests/question-bank-robustness.test.mjs` 直接 import 并用真实数据断言
 * —— "缺字段不崩"是**行为测试**证明的，不是靠 grep 猜的。
 */

export interface QuestionBankEntry {
  text: string;
  difficulty: string;
  tags: string[];
}

export type QuestionBankData = Record<string, QuestionBankEntry[]>;

/**
 * 取题目标签，**永不抛异常**：
 *   · `tags` 缺失 / 为 `null` / 不是数组 → 返回 `[]`
 *   · 条目本身为 `null` / `undefined` → 返回 `[]`
 *   · 是数组 → 原样返回（不做去重、排序等加工，渲染层只负责显示）
 */
export function questionTags(entry: { tags?: unknown } | null | undefined): string[] {
  if (!entry) return [];
  return Array.isArray(entry.tags) ? entry.tags : [];
}
