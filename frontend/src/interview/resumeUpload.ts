/**
 * T-46：简历上传的**纯函数**部分（校验 + 上传），从 App.tsx 的 `handleResumeFile` 里剥出来。
 *
 * 为什么拆成纯函数而不是 hook：上传成功后要改的状态横跨"简历 / 面试进度 / 倒计时"
 * 三个所有者，放进任何单个 hook 都会造成 Hook 之间的循环依赖。
 * 因此这里只回答"这个文件行不行 / 服务端怎么说"，状态迁移由 `useInterviewSession` 负责。
 */

import { authFetch } from '../services/api';

const ALLOWED_TYPES = [
  'application/pdf',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
];
const MAX_SIZE_BYTES = 5 * 1024 * 1024;

/** 校验简历文件；返回 `null` 表示通过，否则返回给用户看的错误文案。 */
export function validateResumeFile(file: File): string | null {
  if (!ALLOWED_TYPES.includes(file.type)) return '仅支持 PDF 或 DOCX 格式';
  if (file.size > MAX_SIZE_BYTES) return '文件大小不能超过 5MB';
  return null;
}

export type ResumeUploadResult =
  | { status: 'ok'; data: any }
  | { status: 'unauthorized' }
  | { status: 'error'; message: string };

/**
 * `POST /api/resume/upload`。
 *
 * 三个出口与原实现一一对应：
 *   · `res.ok && data.success` → `ok`
 *   · 令牌失效（authFetch 抛 `Unauthorized`）→ `unauthorized`（调用方负责登出）
 *   · 其余（含响应体不是 JSON）→ `error` + 可直接展示的文案
 */
export async function uploadResumeFile(file: File): Promise<ResumeUploadResult> {
  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await authFetch('/api/resume/upload', {
      method: 'POST',
      body: formData,
    });
    const data = await res.json();
    if (res.ok && data.success) return { status: 'ok', data };
    return { status: 'error', message: data.detail || data.error || '解析失败' };
  } catch (err: any) {
    console.error(err);
    if (err.message === 'Unauthorized') return { status: 'unauthorized' };
    return { status: 'error', message: '上传失败，请检查网络或后端服务' };
  }
}
