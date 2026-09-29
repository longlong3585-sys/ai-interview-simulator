/**
 * T-47：登录 / 注册弹窗（原 App.tsx 的 JSX 第 2119~2260 行 + `loadCaptcha` /
 * `validateEmail` / `handleAuth`）。为了把 App.tsx 降到"路由与页面装配"，
 * 这 14 个认证表单 state 必须跟着弹窗一起搬走 —— 它们没有任何外部消费者。
 *
 * 认证状态的**唯一真源**仍是 `AuthContext`：本组件只调用 `signIn()`，
 * 持久化与登出清理不在这里（T-44 的边界，不许回退）。
 * 登录成功后 App 级的数据加载由 `onSignedIn` 回调转交（历史 / 通知 / 资料各自
 * 监听 token 变化，因此这里不再逐个调用 loadXxx）。
 *
 * T-48：删掉了只写不读的 `_passwordError` 状态（连同它的 3 处写入）。
 */

import { useEffect, useState } from 'react';
import { API_BASE_URL } from '../config';
import { useAuth } from './AuthContext';
import { checkPasswordRules, passwordError } from '../utils/passwordRules';

interface AuthModalProps {
  open: boolean;
  onClose: () => void;
  /** 登录成功后由 App 收尾（管理员的"进后台"路径、关闭面板等）。 */
  onSignedIn: (payload: { token: string; role: string; username: string }) => void;
}

export function AuthModal({ open, onClose, onSignedIn }: AuthModalProps) {
  const { signIn } = useAuth();
  const [authMode, setAuthMode] = useState<'login' | 'register'>('login');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [authError, setAuthError] = useState('');
  const [emailError, setEmailError] = useState('');
  const [email, setEmail] = useState('');
  const [agreeTerms, setAgreeTerms] = useState(false);
  const [captchaId, setCaptchaId] = useState('');
  const [captchaImage, setCaptchaImage] = useState('');
  const [captchaCode, setCaptchaCode] = useState('');
  const [captchaLoading, setCaptchaLoading] = useState(false);
  const [authLoading, setAuthLoading] = useState(false);
  const [passwordRules, setPasswordRules] = useState({
    length: false,
    kind: false,
    noRepeat: false
  });

  const loadCaptcha = async () => {
    setCaptchaLoading(true);
    setCaptchaCode('');
    try {
      const res = await fetch(`${API_BASE_URL}/api/captcha`);
      if (!res.ok) {
        console.error('验证码加载失败 HTTP', res.status);
        return;
      }
      const id = res.headers.get('X-Captcha-Id');
      if (id) setCaptchaId(id);
      const blob = await res.blob();
      if (captchaImage) URL.revokeObjectURL(captchaImage);
      setCaptchaImage(URL.createObjectURL(blob));
    } catch (err) {
      console.error('验证码加载失败', err);
    } finally {
      setCaptchaLoading(false);
    }
  };

  // 弹窗打开即取一张验证码（原实现由"登录 / 注册"与"免费开始"两个按钮各调一次）。
  // `loadCaptcha` 的 setState 发生在 async 函数里（拉图片 → 建 object URL），
  // 这是"与外部系统同步"的正当用法，因此显式豁免本行。
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (open) void loadCaptcha();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const validateEmail = (value: string) => {
    return /^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$/.test(value);
  };

  const handleAuth = async () => {
    setAuthError('');
    setEmailError('');
    if (!captchaCode) {
      setAuthError('请输入验证码');
      return;
    }
    if (authMode === 'register') {
      const usernamePattern = /^[\u4e00-\u9fa5a-zA-Z0-9_]{3,16}$/;
      if (!usernamePattern.test(username)) {
        setAuthError('用户名必须为3-16位字母、数字、下划线或中文');
        return;
      }
      if (!email) {
        setEmailError('邮箱不能为空');
        return;
      }
      if (!validateEmail(email)) {
        setEmailError('邮箱格式不正确');
        return;
      }
      const pwd = password;
      // T-08：规则统一来自 src/utils/passwordRules.ts（与后端 validate_password 一致）
      const pwdError = passwordError(pwd);
      if (pwdError) { setAuthError(pwdError); return; }
      if (password !== confirmPassword) { setAuthError('两次输入的密码不一致'); return; }
      if (!agreeTerms) { setAuthError('请先阅读并同意用户协议'); return; }
    }
    setAuthLoading(true);
    try {
      const url = authMode === 'login' ? `${API_BASE_URL}/api/login` : `${API_BASE_URL}/api/register`;
      const params = authMode === 'login'
        ? new URLSearchParams({ username, password, captcha_id: captchaId, captcha_code: captchaCode })
        : new URLSearchParams({ username, password, email, captcha_id: captchaId, captcha_code: captchaCode });
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: params.toString()
      });
      const data = await res.json();
      if (res.ok) {
        if (authMode === 'login') {
          // T-44：状态与持久化的唯一写路径
          signIn({
            token: data.access_token,
            userId: data.user_id,
            role: data.role,
            username,
          });
          onClose();
          setUsername('');
          setPassword('');
          setCaptchaCode('');
          setCaptchaId('');
          setEmail('');
          setEmailError('');
          onSignedIn({ token: data.access_token, role: data.role, username });
        } else {
          setAuthMode('login');
          setAuthError('注册成功，请登录');
          setEmail('');
          setAgreeTerms(false);
        }
      } else {
        setAuthError(data.detail || '操作失败');
        loadCaptcha();
      }
    } catch (err) {
      setAuthError('网络错误');
      loadCaptcha();
    } finally {
      setAuthLoading(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 bg-black/40 backdrop-blur-sm flex items-center justify-center z-50" onClick={onClose}>
      <div className="animate-scale-in bg-white rounded-2xl p-6 w-96 max-h-[90vh] overflow-auto shadow-float" onClick={(e) => e.stopPropagation()}>
        <div className="flex justify-between items-center mb-5">
          <h2 className="text-xl font-bold text-slate-800">{authMode === 'login' ? '登录' : '注册'}</h2>
          <button onClick={onClose} className="btn-ghost w-8 h-8 flex items-center justify-center rounded-lg text-slate-400 hover:text-slate-600">
            <svg className="w-5 h-5" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>
        <input
          type="text"
          placeholder="用户名"
          value={username}
          onChange={e => setUsername(e.target.value)}
          className="input-field mb-2"
        />
        {authMode === 'register' && (
          <div className="text-xs mb-2 text-slate-400">
            3-16位字母、数字、下划线或中文
          </div>
        )}
        <input
          type="password"
          placeholder="密码"
          value={password}
          onChange={e => {
            const pwd = e.target.value;
            setPassword(pwd);
            if (authMode === 'register') {
              // T-08：规则统一来自 src/utils/passwordRules.ts
              // T-48：原先这里还往一个只写不读的 `_passwordError` 写值，已删除；
              // 真正的错误文案由 handleAuth 里的 passwordError(pwd) 给到 authError。
              setPasswordRules(checkPasswordRules(pwd));
            }
          }}
          className="input-field mb-2"
        />
        {authMode === 'register' && (
          <>
            <div className="text-xs mb-2 space-y-1">
              <div className={passwordRules.length ? 'text-emerald-600' : 'text-slate-400'}>
                {passwordRules.length ? '✓' : '✗'} 长度8-16个字
              </div>
              <div className={passwordRules.kind ? 'text-emerald-600' : 'text-slate-400'}>
                {passwordRules.kind ? '✓' : '✗'} 必须包含字母、数字、符号中至少2种
              </div>
              <div className={passwordRules.noRepeat ? 'text-emerald-600' : 'text-slate-400'}>
                {passwordRules.noRepeat ? '✓' : '✗'} 请勿输入连续、重复6位以上字母或数字
              </div>
            </div>
            <input
              type="email"
              placeholder="邮箱（必填，如 user@example.com）"
              value={email}
              onChange={e => {
                const val = e.target.value;
                setEmail(val);
                if (val && !validateEmail(val)) {
                  setEmailError('邮箱格式不正确');
                } else {
                  setEmailError('');
                }
              }}
              className={`input-field mb-2 ${emailError ? 'border-red-400 focus:ring-red-400' : ''}`}
            />
            {emailError && <p className="text-red-500 text-xs mb-2">{emailError}</p>}
            <input
              type="password"
              placeholder="确认密码"
              value={confirmPassword}
              onChange={e => setConfirmPassword(e.target.value)}
              className="input-field mb-3"
            />
            <label className="flex items-center text-sm text-slate-600 mb-3 cursor-pointer">
              <input
                type="checkbox"
                checked={agreeTerms}
                onChange={e => setAgreeTerms(e.target.checked)}
                className="mr-2 rounded"
              />
              我已阅读并同意<a href="#" className="text-primary-600 underline hover:text-primary-800">《用户协议》</a>
            </label>
          </>
        )}
        <div className="mb-3">
          <div className="flex gap-2 items-center mb-2">
            {captchaImage ? (
              <img src={captchaImage} alt="验证码" className="h-10 border border-slate-200 rounded-lg" />
            ) : (
              <div className="h-10 bg-slate-100 rounded-lg flex items-center justify-center text-sm text-slate-400 flex-1">
                {captchaLoading ? '加载中...' : '点击刷新获取'}
              </div>
            )}
            <button
              type="button"
              onClick={loadCaptcha}
              disabled={captchaLoading}
              className="btn-ghost text-xs shrink-0"
            >
              {captchaLoading ? '加载中...' : '换一张'}
            </button>
          </div>
          <input
            type="text"
            placeholder="请输入验证码"
            value={captchaCode}
            onChange={e => setCaptchaCode(e.target.value)}
            className="input-field"
            autoComplete="off"
          />
        </div>
        {authError && <p className="text-red-500 text-sm mb-3">{authError}</p>}
        <button onClick={handleAuth} disabled={authLoading || (authMode === 'register' && (!username || !password || !email || !validateEmail(email) || !agreeTerms || password !== confirmPassword))} className="btn-primary w-full py-2.5 text-sm">
          {authLoading ? '验证中...' : authMode === 'login' ? '登录' : '注册'}
        </button>
        <p className="text-sm text-center mt-4 text-slate-500">
          {authMode === 'login' ? '没有账号？' : '已有账号？'}
          <button
            onClick={() => {
              setAuthMode(authMode === 'login' ? 'register' : 'login');
              setAuthError('');
              setEmailError('');
              setPassword('');
              setConfirmPassword('');
              setEmail('');
              setAgreeTerms(false);
              setPasswordRules({ length: false, kind: false, noRepeat: false });
              setCaptchaCode('');
              loadCaptcha();
            }}
            className="text-primary-600 hover:text-primary-800 font-medium ml-1"
          >
            {authMode === 'login' ? '立即注册' : '去登录'}
          </button>
        </p>
      </div>
    </div>
  );
}
