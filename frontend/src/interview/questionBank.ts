/**
 * T-46：题库取数（`GET /api/question_bank`）。
 *
 * 纯函数：调用方（`QuestionBankModal`）自己持 loading / 数据状态。
 * 条目类型与"缺字段不崩"的读取函数在 `./questionBankEntry.ts`（T-49 / FR-9.2），
 * 这里只负责发请求 —— 因此本文件 import 了 `config`（依赖 `import.meta.env`），
 * 纯逻辑刻意留在另一个文件里，好让 node:test 能直接跑。
 *
 * 仓库里原有的 `src/QuestionBank.tsx`（从未被任何地方 import 的旧组件）
 * 已在 T-48 删除，不再存在第二份题库实现。
 */

import { API_BASE_URL } from '../config';
import type { QuestionBankData } from './questionBankEntry';

export type { QuestionBankEntry, QuestionBankData } from './questionBankEntry';

export async function fetchQuestionBank(): Promise<QuestionBankData> {
  const res = await fetch(`${API_BASE_URL}/api/question_bank`);
  return await res.json();
}
