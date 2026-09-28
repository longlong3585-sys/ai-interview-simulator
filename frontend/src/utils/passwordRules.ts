/**
 * T-08 / FR-1.1 + FR-1.5：密码强度的**前端单一来源**。
 *
 * 背景：修复前 App.tsx 内部就有 **3 份**重复实现 ——
 *   1. handleAuth 的提交校验（~916 行）
 *   2. 注册密码输入框的实时 ✓/✗ 提示（~2196 行）
 *   3. 修改密码的校验（~1173 行）—— 这一份还写错了，只要求 **≥6 位**，
 *      导致用户输入 6-7 位时前端放行、后端拒绝
 *
 * 现在三处统一调用本模块；规则与后端 `backend/auth.py: validate_password()`
 * **逐条一致**（长度 8-16 / 至少 2 类字符 / 无 6 位重复或升序）。
 * 任一侧修改规则时，必须同步另一侧——
 * 后端才是强制点，前端只负责提前提示。
 */

export const PASSWORD_MIN_LENGTH = 8;
export const PASSWORD_MAX_LENGTH = 16;

const HAS_LETTER = /[A-Za-z]/;
const HAS_DIGIT = /\d/;
const HAS_SYMBOL = /[!@#$%^&*()_+\-=[\]{};':"\\|,.<>/?]/;
const SIX_REPEAT = /(.)\1{5,}/;
const ASCENDING_RUN =
  /012345|123456|234567|345678|456789|567890|abcdef|bcdefg|cdefgh|defghi|efghij|fghijk/;

export interface PasswordRuleChecks {
  /** 长度 8-16 */
  length: boolean;
  /** 字母/数字/符号 至少 2 类 */
  kind: boolean;
  /** 不含 6 位连续重复或升序序列 */
  noRepeat: boolean;
}

/** 逐条规则的通过情况，供注册页的实时 ✓/✗ 提示使用。 */
export function checkPasswordRules(password: string): PasswordRuleChecks {
  const kindCount = [HAS_LETTER, HAS_DIGIT, HAS_SYMBOL].filter((re) =>
    re.test(password)
  ).length;
  return {
    length:
      password.length >= PASSWORD_MIN_LENGTH &&
      password.length <= PASSWORD_MAX_LENGTH,
    kind: kindCount >= 2,
    noRepeat: !SIX_REPEAT.test(password) && !ASCENDING_RUN.test(password),
  };
}

/**
 * 返回第一条不满足的规则文案；全部通过返回 null。
 * 文案与后端 `validate_password()` 的错误信息保持一致。
 */
export function passwordError(password: string): string | null {
  if (!password) return '密码不能为空';
  const checks = checkPasswordRules(password);
  if (!checks.length) {
    return `密码长度应为${PASSWORD_MIN_LENGTH}-${PASSWORD_MAX_LENGTH}位`;
  }
  if (!checks.kind) return '密码必须包含字母、数字、符号中至少2种';
  if (!checks.noRepeat) return '请勿输入连续、重复6位以上字母或数字';
  return null;
}
