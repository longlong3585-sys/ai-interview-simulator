/**
 * T-46 / T-47：题库取数（`GET /api/question_bank`）。
 *
 * 纯函数：调用方（`QuestionBankModal`）自己持 loading / 数据状态。
 * 注意与仓库里的 `src/QuestionBank.tsx` 区分 —— 那个组件**没有任何地方引用**，
 * 它的处置属于 T-48（死代码清理），本次拆分不碰它，只新增本文件。
 */

import { API_BASE_URL } from '../config';

export interface QuestionBankEntry {
  text: string;
  difficulty: string;
  tags: string[];
}

export type QuestionBankData = Record<string, QuestionBankEntry[]>;

export async function fetchQuestionBank(): Promise<QuestionBankData> {
  const res = await fetch(`${API_BASE_URL}/api/question_bank`);
  return await res.json();
}
