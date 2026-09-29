/**
 * Toast 消息 id 的生成器。
 *
 * 为什么单独一个文件（而不是塞在 `Toast.tsx` 里）：`Toast.tsx` 只导出组件，
 * 才能保住 react-refresh 的"组件文件只导出组件"边界（否则热更新会整页刷新）。
 * 业务方需要的是一个"每次调用都不同"的 id —— 用来重置自动关闭计时，
 * 且不会像 `Date.now()` 那样被 React 的纯函数规则判为不可预测。
 */

let toastSeq = 0;

export function nextToastId(): number {
  toastSeq += 1;
  return toastSeq;
}
