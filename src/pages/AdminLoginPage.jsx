import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import gsap from "gsap";
import { AuthField } from "../components/auth/AuthField.jsx";
import { TurnstileWidget } from "../components/auth/TurnstileWidget.jsx";
import { Icon } from "../components/Icon.jsx";
import { authApi, readableApiError, storeTokens } from "../lib/api.js";
import { useSiteSettings } from "../context/SiteSettingsContext.jsx";

export function AdminLoginPage() {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const turnstileRef = useRef(null);
  const [form, setForm] = useState({ account: "", password: "", otp: "", recoveryCode: "" });
  const [twoFactorRequired, setTwoFactorRequired] = useState(false);
  const [verificationMode, setVerificationMode] = useState("totp");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const location = useLocation();
  const completionMessage = typeof location.state?.message === "string" ? location.state.message : "";
  const { settings: siteSettings } = useSiteSettings();
  const turnstile = siteSettings.turnstile || { enabled: false, site_key: "" };

  useLayoutEffect(() => {
    const context = gsap.context(() => {
      if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
      const entrancePieces = panelRef.current?.querySelectorAll(
        "header, .staff-auth-warning, form > .auth-field, .staff-auth-submit, .staff-return-user"
      ) || [];

      gsap.timeline({ defaults: { overwrite: "auto" } })
        .fromTo(panelRef.current, {
          autoAlpha: 0,
          y: -28,
          scale: .96,
          rotation: -.8,
        }, {
          autoAlpha: 1,
          y: 0,
          scale: 1,
          rotation: 0,
          duration: .56,
          ease: "back.out(1.45)",
          clearProps: "transform,opacity,visibility",
        })
        .fromTo(entrancePieces, {
          autoAlpha: 0,
          y: 18,
        }, {
          autoAlpha: 1,
          y: 0,
          duration: .32,
          stagger: .055,
          ease: "back.out(1.3)",
          clearProps: "transform,opacity,visibility",
        }, "-=.22");
      gsap.ticker.wake();
    }, rootRef);
    const fallbackTimer = window.setTimeout(() => {
      gsap.set(panelRef.current, { clearProps: "transform,opacity,visibility" });
      gsap.set(panelRef.current?.querySelectorAll(
        "header, .staff-auth-warning, form > .auth-field, .staff-auth-submit, .staff-return-user"
      ), { clearProps: "transform,opacity,visibility" });
    }, 1200);
    return () => {
      window.clearTimeout(fallbackTimer);
      context.revert();
    };
  }, []);

  useEffect(() => {
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousBodyOverflow;
    };
  }, []);

  const submit = async (event) => {
    event.preventDefault();
    setError("");
    const turnstileToken = turnstile.enabled ? turnstileRef.current?.getToken() : "";
    if (turnstile.enabled && !turnstileToken) {
      setError(turnstile.site_key ? "请先完成安全验证。" : "安全验证配置异常，请联系站点管理员。");
      return;
    }
    setLoading(true);
    try {
      const { data } = await authApi.staffLogin(
        form.account.trim(),
        form.password,
        verificationMode === "totp" ? form.otp.trim() : "",
        verificationMode === "recovery" ? form.recoveryCode.trim().toUpperCase() : "",
        new URLSearchParams(location.search).get("next") || "",
        turnstileToken,
      );
      storeTokens(data);
      setForm((current) => ({ ...current, otp: "", recoveryCode: "" }));
      const next = new URLSearchParams(location.search).get("next");
      if (next && data.admin_access) {
        window.location.assign(data.admin_url);
        return;
      }
      navigate("/admin-control", {
        state: {
          adminUrl: data.admin_url,
          admin2faNotice: next && !data.admin_access ? "Django 高级后台要求先启用两步验证。" : "",
          recoveryNotice: data.used_recovery_code ? `本次已使用恢复码，当前还剩 ${data.remaining_recovery_codes ?? 0} 枚。` : "",
        },
      });
    } catch (requestError) {
      if (requestError.response?.data?.two_factor_required) setTwoFactorRequired(true);
      setError(readableApiError(requestError, "工作人员认证失败，请检查账号或网络连接。"));
    } finally {
      turnstileRef.current?.reset();
      setLoading(false);
    }
  };

  return (
    <main className="staff-auth-page" ref={rootRef}>
      <div className="staff-auth-grid" aria-hidden="true" />
      <div className="staff-auth-square" aria-hidden="true" />
      <div className="staff-auth-ring" aria-hidden="true" />
      <section className="staff-auth-panel" ref={panelRef}>
        <header>
          <div className="staff-auth-heading"><i><Icon name="shield" /></i><span><small>RESTRICTED AREA</small><h1>超级管理员认证</h1></span></div>
          <b>STAFF ONLY</b>
        </header>
        <p className="staff-auth-warning">此入口仅用于精选专栏审核与系统管理，普通手账账号无法进入。</p>
        <form onSubmit={submit}>
          <AuthField icon="users" label="管理员账号" value={form.account} onChange={(event) => setForm((current) => ({ ...current, account: event.target.value }))} placeholder="输入超级管理员账号" required autoComplete="username" />
          <AuthField icon="key" label="安全口令" type="password" value={form.password} onChange={(event) => setForm((current) => ({ ...current, password: event.target.value }))} placeholder="输入管理员密码" required autoComplete="current-password" />
          {twoFactorRequired && <>
            <div className="staff-auth-verification-switch" role="group" aria-label="二次验证方式">
              <button type="button" className={verificationMode === "totp" ? "is-active" : ""} onClick={() => setVerificationMode("totp")}>身份验证器验证码</button>
              <button type="button" className={verificationMode === "recovery" ? "is-active" : ""} onClick={() => setVerificationMode("recovery")}>恢复码</button>
            </div>
            {verificationMode === "totp"
              ? <AuthField icon="shield" label="两步验证码" inputMode="numeric" value={form.otp} onChange={(event) => setForm((current) => ({ ...current, otp: event.target.value.replace(/\D/g, "").slice(0, 6) }))} placeholder="输入 6 位验证码" required autoComplete="one-time-code" />
              : <AuthField icon="shield" label="一次性恢复码" type="password" value={form.recoveryCode} onChange={(event) => setForm((current) => ({ ...current, recoveryCode: event.target.value.toUpperCase().replace(/\s/g, "") }))} placeholder="例如 ABCD-EFGH-IJKL" required autoComplete="off" />}
          </>}
          <div className="staff-auth-message" aria-live="polite">
            {!error && completionMessage && <p className="form-message success">{completionMessage}</p>}
            {error && <p className="form-message error" role="alert">{error}</p>}
          </div>
          <TurnstileWidget enabled={turnstile.enabled} siteKey={turnstile.site_key} ref={turnstileRef} variant="staff" size="flexible" mountDelay={900} />
          <button className="staff-auth-submit" type="submit" disabled={loading}><Icon name="login" /> {loading ? "正在验证工作人员权限..." : "进入管理控制室"}</button>
        </form>
        <Link className="staff-return-user" to="/login"><Icon name="arrow-left" /> 返回普通用户登录</Link>
      </section>
    </main>
  );
}
