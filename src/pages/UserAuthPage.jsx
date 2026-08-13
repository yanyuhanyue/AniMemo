import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import gsap from "gsap";
import { AuthDecorations } from "../components/auth/AuthDecorations.jsx";
import { AuthField } from "../components/auth/AuthField.jsx";
import { AuthModeTransition } from "../components/auth/AuthModeTransition.jsx";
import { TurnstileWidget } from "../components/auth/TurnstileWidget.jsx";
import { Icon } from "../components/Icon.jsx";
import { authApi, readableApiError, storeTokens } from "../lib/api.js";
import { useSiteSettings } from "../context/SiteSettingsContext.jsx";
import { demoEnabled, getDemoAuthMessage } from "@demo-data";

const modeCopy = {
  login: { badge: "PLAYER ONE", title: "登录手账房", subtitle: "输入暗号，继续整理你的动画宇宙。" },
  register: { badge: "NEW PLAYER", title: "创建专属手账", subtitle: "先验证邮箱，再由你设置用户名和密码。" },
  registerVerify: { badge: "EMAIL CHECK", title: "验证你的邮箱", subtitle: "正在确认注册链接，请稍候。" },
  registerComplete: { badge: "FINISH PROFILE", title: "完成账号设置", subtitle: "邮箱已验证，现在由你设置登录信息。" },
  reset: { badge: "PASSWORD RESCUE", title: "找回手账密码", subtitle: "留下邮箱，重置入口马上飞进你的收件箱。" },
  resetNew: { badge: "NEW PASSWORD", title: "设置新的暗号", subtitle: "输入两次新密码，就能重新进入你的手账房。" },
};

const featureData = [
  { icon: "layers", title: "多视图", copy: "列表 / 海报墙", note: "LIST + GRID" },
  { icon: "tag", title: "自由筛选", copy: "状态 / 标签 / 年份", note: "FILTER IT!" },
  { icon: "edit", title: "私人长评", copy: "记录只属于你的感受", note: "YOUR STORY" },
];

export function UserAuthPage() {
  const { settings: siteSettings } = useSiteSettings();
  const rootRef = useRef(null);
  const shellRef = useRef(null);
  const modeTransitionRef = useRef(null);
  const accountRef = useRef(null);
  const emailRef = useRef(null);
  const usernameRef = useRef(null);
  const passwordRef = useRef(null);
  const turnstileRef = useRef(null);
  const navigate = useNavigate();
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  const resetUid = params.get("reset_uid");
  const resetToken = params.get("reset_token");
  const verified = params.get("verified");
  const isRegisterRoute = location.pathname === "/register";
  const isRegisterVerifyRoute = location.pathname === "/register/verify";
  const registrationToken = params.get("token");
  const modeRequestRef = useRef(resetUid && resetToken ? "resetNew" : "login");
  const modeRequestIdRef = useRef(0);
  const [mode, setMode] = useState(resetUid && resetToken ? "resetNew" : isRegisterRoute || isRegisterVerifyRoute ? (isRegisterVerifyRoute ? "registerVerify" : "register") : "login");
  const [form, setForm] = useState({ account: "", email: "", username: "", password: "", passwordConfirm: "" });
  const [message, setMessage] = useState(location.state?.message || (verified === "success" ? "邮箱已验证，现在可以登录了。" : ""));
  const [error, setError] = useState(verified === "invalid" || (isRegisterVerifyRoute && !registrationToken) ? "注册链接无效、已过期或已经使用。" : "");
  const [loading, setLoading] = useState(isRegisterVerifyRoute && Boolean(registrationToken));
  const [completionToken, setCompletionToken] = useState(null);
  const [staffNavigating, setStaffNavigating] = useState(false);
  const [modeSequence, setModeSequence] = useState(0);
  const copy = modeCopy[mode];
  const registrationEnabled = siteSettings.registration_enabled;
  const requiresTurnstile = ["login", "register", "registerComplete", "reset", "resetNew"].includes(mode);

  useEffect(() => {
    if (registrationEnabled || !["register", "registerComplete"].includes(mode)) return;
    modeRequestRef.current = "login";
    setMode("login");
    setModeSequence((value) => value + 1);
    setError("当前暂未开放注册。");
  }, [mode, registrationEnabled]);

  useEffect(() => {
    if (!isRegisterVerifyRoute || !registrationToken) return undefined;
    let active = true;
    setLoading(true);
    setError("");
    authApi.verifyRegistration(registrationToken)
      .then(({ data }) => {
        if (!active) return;
        setCompletionToken(data?.completion_token || null);
        setForm((current) => ({ ...current, email: data?.email || "" }));
        setMessage(data?.detail || "邮箱验证成功，请完成账号设置。");
        setMode("registerComplete");
        setModeSequence((value) => value + 1);
      })
      .catch((requestError) => {
        if (!active) return;
        setError(readableApiError(requestError, "注册链接无效、已过期或已经使用。"));
      })
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [isRegisterVerifyRoute, registrationToken]);

  useLayoutEffect(() => {
    const context = gsap.context(() => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const entrancePieces = rootRef.current?.querySelectorAll(
        ".auth-page__topbar .login-piece, .auth-promo > .login-piece"
      ) || [];

      gsap.timeline({ defaults: { overwrite: "auto" } })
        .fromTo(shellRef.current, {
          autoAlpha: 0,
          y: 34,
          scale: .94,
          rotation: -1.1,
        }, {
          autoAlpha: 1,
          y: 0,
          scale: 1,
          rotation: -.45,
          duration: .62,
          ease: "back.out(1.45)",
          clearProps: "transform,opacity,visibility",
        })
        .fromTo(entrancePieces, {
          autoAlpha: 0,
          y: 18,
        }, {
          autoAlpha: 1,
          y: 0,
          duration: .34,
          stagger: .055,
          ease: "back.out(1.3)",
          clearProps: "transform,opacity,visibility",
        }, "-=.28");
      gsap.ticker.wake();
    }, rootRef);
    const fallbackTimer = window.setTimeout(() => {
      gsap.set(shellRef.current, { clearProps: "transform,opacity,visibility" });
      gsap.set(rootRef.current?.querySelectorAll(
        ".auth-page__topbar .login-piece, .auth-promo > .login-piece"
      ), { clearProps: "transform,opacity,visibility" });
    }, 1200);
    return () => {
      window.clearTimeout(fallbackTimer);
      context.revert();
    };
  }, []);

  useEffect(() => {
    const previousBodyOverflow = document.body.style.overflow;
    const previousBodyPadding = document.body.style.paddingBottom;
    document.body.style.overflow = "hidden";
    document.body.style.paddingBottom = "0px";
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.body.style.paddingBottom = previousBodyPadding;
    };
  }, []);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      if (mode === "login") accountRef.current?.focus({ preventScroll: true });
      else if (mode === "register" || mode === "reset") emailRef.current?.focus({ preventScroll: true });
      else if (mode === "registerComplete") usernameRef.current?.focus({ preventScroll: true });
      else if (mode === "resetNew") passwordRef.current?.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [mode]);

  const switchMode = async (nextMode) => {
    if (nextMode === "register" && !registrationEnabled) {
      setError("当前暂未开放注册。");
      return;
    }
    if (loading || (nextMode === mode && nextMode === modeRequestRef.current)) return;
    modeRequestRef.current = nextMode;
    const requestId = ++modeRequestIdRef.current;
    await modeTransitionRef.current?.exit();
    if (requestId !== modeRequestIdRef.current) return;
    setError("");
    setMessage("");
    setMode(nextMode);
    setModeSequence((value) => value + 1);
  };

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event) => {
    event.preventDefault();
    if (mode === "registerVerify") return;
    setError("");
    setMessage("");
    if ((mode === "registerComplete" || mode === "resetNew") && form.password !== form.passwordConfirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    if (mode === "register" && !registrationEnabled) {
      setError("当前暂未开放注册。");
      return;
    }

    const turnstileToken = requiresTurnstile ? turnstileRef.current?.getToken() : "";
    if (requiresTurnstile && !turnstileToken) {
      setError("请先完成安全验证。");
      return;
    }

    setLoading(true);
    try {
      if (mode === "login") {
        const { data } = await authApi.login(form.account.trim(), form.password, turnstileToken);
        storeTokens(data);
        navigate("/dashboard", { replace: true });
      } else if (mode === "register") {
        await authApi.registerRequest(form.email.trim(), turnstileToken);
        setMessage("验证邮件已发送，请检查邮箱。");
      } else if (mode === "registerComplete") {
        if (!completionToken) {
          setError("注册完成凭证无效、已过期或已经使用。");
          return;
        }
        await authApi.completeRegistration({
          completion_token: completionToken,
          username: form.username.trim(),
          password: form.password,
          password_confirm: form.passwordConfirm,
        }, turnstileToken);
        setCompletionToken(null);
        setMessage("注册完成，请使用新账号登录。");
        navigate("/login", { replace: true, state: { message: "注册完成，请使用新账号登录。" } });
      } else if (mode === "reset") {
        await authApi.reset(form.email.trim(), turnstileToken);
        setMessage("重置邮件已发送，请查收。");
      } else {
        await authApi.resetConfirm({ uid: resetUid, token: resetToken, password: form.password, password_confirm: form.passwordConfirm }, turnstileToken);
        setMessage("密码已更新，请使用新密码登录。");
        window.history.replaceState({}, "", "/login");
        modeRequestRef.current = "login";
        setMode("login");
        setModeSequence((value) => value + 1);
      }
    } catch (requestError) {
      if (demoEnabled && !requestError.response && mode !== "login") {
        setMessage(getDemoAuthMessage(mode));
      } else if (demoEnabled && !requestError.response && mode === "login" && form.account && form.password) {
        localStorage.setItem("animemo_demo", "true");
        navigate("/dashboard", { replace: true });
      } else {
        setError(readableApiError(requestError, "请求失败，请稍后重试。"));
      }
    } finally {
      turnstileRef.current?.reset();
      setLoading(false);
    }
  };

  const openStaffLogin = async () => {
    if (staffNavigating) return;
    setStaffNavigating(true);
    navigate("/admin-login");
  };

  return (
    <main className="auth-page user-auth-page" ref={rootRef}>
      <AuthDecorations />
      <div className="auth-page__frame">
        <div className="auth-page__topbar">
          <Link className="back-showcase login-piece" to="/"><Icon name="arrow-left" /> 返回展示主界面</Link>
          <span className="private-zone login-piece"><Icon name="bolt" /> PRIVATE ZONE · 私人手账</span>
        </div>

        <section className="auth-shell auth-page__shell" ref={shellRef}>
          <aside className="auth-promo auth-layout__left">
            <span className="auth-brand login-piece"><i /> {siteSettings.site_name.toUpperCase()} / 追番记录</span>
            <div className="auth-promo-copy login-piece">
              <span className="welcome-sticker">嘿！欢迎回来</span>
              <h1 className="auth-hero-title">
                <span className="auth-hero-title__line">把喜欢的</span>
                <span className="auth-hero-title__line auth-hero-title__line--highlight">动画</span>
                <span className="auth-hero-title__line">全部记下来!</span>
              </h1>
              <p>评分、标签、长评、海报墙——这里没有无聊的工作台，只有属于你的动画收藏宇宙。</p>
            </div>
            <div className="auth-features login-piece">
              {featureData.map((feature, index) => (
                <article className="auth-feature" key={feature.title}>
                  <small>0{index + 1}</small><i><Icon name={feature.icon} /></i><strong>{feature.title}</strong><span>{feature.copy}</span><em>{feature.note}</em>
                </article>
              ))}
            </div>
          </aside>

          <div className="auth-form-wrap auth-layout__right">
            <div className="auth-dot-cluster" aria-hidden="true">{Array.from({ length: 12 }, (_, index) => <i key={index} />)}</div>
            <div className="auth-form-panel">
              <AuthModeTransition mode={mode} sequenceKey={modeSequence} ref={modeTransitionRef}>
              <header className="auth-form-heading login-piece" data-auth-step>
                <span className="micro-label">{copy.badge}</span>
                <h2>{copy.title}</h2>
                <p className="auth-subtitle">{copy.subtitle}</p>
              </header>
              {!["reset", "resetNew", "registerVerify", "registerComplete"].includes(mode) ? (
                <div className="auth-tabs login-piece" data-auth-step role="tablist" aria-label="账号操作">
                  <button type="button" role="tab" aria-selected={mode === "login"} className={mode === "login" ? "active" : ""} onClick={() => switchMode("login")}><Icon name="login" /> 登录</button>
                  <button type="button" role="tab" aria-selected={mode === "register"} aria-disabled={!registrationEnabled} disabled={!registrationEnabled} title={registrationEnabled ? "创建账号" : "当前暂未开放注册"} className={mode === "register" ? "active" : ""} onClick={() => switchMode("register")}><Icon name="user-plus" /> {registrationEnabled ? "注册" : "注册关闭"}</button>
                </div>
              ) : (
                <button className="return-login login-piece" data-auth-step type="button" onClick={() => switchMode("login")}><Icon name="arrow-left" /> 返回登录</button>
              )}
              <form onSubmit={submit}>
                {mode === "login" && <>
                  <AuthField inputRef={accountRef} icon="user" label="账号 ACCOUNT" value={form.account} onChange={(event) => update("account", event.target.value)} placeholder="请输入用户名或注册邮箱" required autoComplete="username" />
                  <AuthField inputRef={passwordRef} icon="key" label="密码 PASSWORD" type="password" value={form.password} onChange={(event) => update("password", event.target.value)} placeholder="请输入登录密码" required autoComplete="current-password" showPasswordToggle />
                </>}
                {mode !== "login" && mode !== "resetNew" && mode !== "registerVerify" && <AuthField inputRef={emailRef} icon="envelope" label="邮箱 EMAIL" type="email" inputMode="email" value={form.email} onChange={(event) => update("email", event.target.value)} placeholder={mode === "register" ? "请输入邮箱地址（支持主流邮箱）" : "请输入注册时使用的邮箱"} required autoComplete="email" readOnly={mode === "registerComplete"} />}
                {mode === "registerComplete" && <AuthField inputRef={usernameRef} icon="user" label="用户名 USERNAME" value={form.username} onChange={(event) => update("username", event.target.value)} placeholder="设置你的登录用户名" required autoComplete="username" />}
                {mode !== "login" && mode !== "reset" && mode !== "register" && mode !== "registerVerify" && <AuthField inputRef={passwordRef} icon="lock" label="密码 PASSWORD" type="password" value={form.password} onChange={(event) => update("password", event.target.value)} placeholder={mode === "registerComplete" ? "请设置安全密码" : "请输入密码"} required minLength="8" autoComplete={mode === "registerComplete" ? "new-password" : "current-password"} />}
                {mode === "resetNew" && <AuthField inputRef={passwordRef} icon="lock" label="密码 PASSWORD" type="password" value={form.password} onChange={(event) => update("password", event.target.value)} placeholder="请输入新密码" required minLength="8" autoComplete="new-password" />}
                {(mode === "registerComplete" || mode === "resetNew") && <AuthField icon="shield" label="再次确认 REPEAT" type="password" value={form.passwordConfirm} onChange={(event) => update("passwordConfirm", event.target.value)} placeholder="请再次输入密码" required minLength="8" autoComplete="new-password" />}
                {mode === "registerVerify" && <p className="form-message success">{loading ? "正在验证邮箱链接……" : "请使用邮件中的有效链接继续。"}</p>}
                {mode === "login" && <button className="forgot-password" type="button" onClick={() => switchMode("reset")} data-auth-step>忘记密码？→</button>}
                <div className="auth-message-slot" aria-live="polite">
                  {error && <p className="form-message error" role="alert">{error}</p>}
                  {message && <p className="form-message success">{message}</p>}
                </div>
                {requiresTurnstile && <TurnstileWidget key={`turnstile-${mode}`} ref={turnstileRef} variant="user" size="normal" mountDelay={900} />}
                <button className="auth-submit" type="submit" disabled={loading || mode === "registerVerify"} data-auth-step>
                  <Icon name={mode === "register" ? "user-plus" : mode === "registerComplete" ? "user-plus" : mode === "reset" ? "arrow-right" : "login"} />
                  {loading ? "正在处理..." : mode === "login" ? "进入我的手账房" : mode === "register" ? "发送验证邮件" : mode === "registerComplete" ? "完成注册" : mode === "resetNew" ? "保存新密码" : "发送重置邮件"}
                </button>
              </form>
              <footer className="auth-security login-piece" data-auth-step>
                <span><Icon name="lock" /> 安全连接</span>
                <button className="staff-link" type="button" onClick={openStaffLogin} disabled={staffNavigating}><Icon name="users" /> STAFF ONLY</button>
              </footer>
              </AuthModeTransition>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
