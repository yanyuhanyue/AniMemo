import { useMemo, useState } from "react";
import { AuthDecorations } from "../components/auth/AuthDecorations.jsx";
import { AuthField } from "../components/auth/AuthField.jsx";
import { Icon } from "../components/Icon.jsx";
import { readableApiError, setupApi } from "../lib/api.js";


const setupSteps = [
  { icon: "key", title: "读取暗号", copy: "从服务器私有目录取得一次性初始化码" },
  { icon: "user", title: "创建管理员", copy: "填写用户名、邮箱与新密码" },
  { icon: "shield", title: "永久锁定", copy: "完成后初始化码立即失效并删除" },
];


export function SetupPage({ installation, onInitialized }) {
  const [form, setForm] = useState({ code: "", username: "", email: "", password: "", passwordConfirm: "" });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const expiresAt = useMemo(() => {
    if (!installation?.expires_at) return null;
    const value = new Date(installation.expires_at);
    return Number.isNaN(value.getTime()) ? null : value.toLocaleString();
  }, [installation?.expires_at]);
  const acceptingSetup = installation?.state === "uninitialized" && installation?.accepting_setup === true;

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));

  const submit = async (event) => {
    event.preventDefault();
    setError("");
    if (!acceptingSetup) {
      setError("初始化码尚未生成、已经失效，或安装状态不可用。请先在服务器重新运行初始化任务。");
      return;
    }
    if (form.password !== form.passwordConfirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    setLoading(true);
    try {
      await setupApi.complete({
        code: form.code.trim(),
        username: form.username.trim(),
        email: form.email.trim(),
        password: form.password,
        password_confirm: form.passwordConfirm,
      });
      onInitialized?.();
    } catch (requestError) {
      setError(readableApiError(requestError, "初始化失败，请检查输入后重试。"));
    } finally {
      setLoading(false);
    }
  };

  return (
    <main className="auth-page setup-page">
      <AuthDecorations />
      <div className="auth-page__frame">
        <div className="auth-page__topbar">
          <span className="back-showcase setup-page__brand"><Icon name="bolt" /> ANIMEMO · FIRST BOOT</span>
          <span className="private-zone"><Icon name="shield" /> PRIVATE SETUP · 私密初始化</span>
        </div>

        <section className="auth-shell setup-page__shell">
          <aside className="auth-promo auth-layout__left setup-page__promo">
            <span className="auth-brand"><i /> MY ANIME MEMORY / 我的动漫记忆库</span>
            <div className="auth-promo-copy">
              <span className="welcome-sticker">只需要完成一次</span>
              <h1 className="auth-hero-title">
                <span className="auth-hero-title__line">让这个实例</span>
                <span className="auth-hero-title__line auth-hero-title__line--highlight">认出</span>
                <span className="auth-hero-title__line">第一位管理员</span>
              </h1>
              <p>初始化码只保存在服务器私有目录，页面不会展示或保存它。完成后，首装入口会永久锁定。</p>
            </div>
            <div className="auth-features setup-page__steps">
              {setupSteps.map((step, index) => (
                <article className="auth-feature" key={step.title}>
                  <small>0{index + 1}</small><i><Icon name={step.icon} /></i>
                  <strong>{step.title}</strong><span>{step.copy}</span><em>ONE-TIME SETUP</em>
                </article>
              ))}
            </div>
          </aside>

          <div className="auth-form-wrap auth-layout__right setup-page__form-wrap">
            <div className="auth-dot-cluster" aria-hidden="true">{Array.from({ length: 12 }, (_, index) => <i key={index} />)}</div>
            <div className="auth-form-panel setup-page__form-panel">
              <header className="auth-form-heading">
                <span className="micro-label">INSTALLATION / 01</span>
                <h2>创建首位管理员</h2>
                <p className="auth-subtitle">使用服务器生成的一次性初始化码。邮箱为必填项。</p>
              </header>

              <div className={acceptingSetup ? "setup-ticket is-ready" : "setup-ticket is-waiting"} role="status">
                <strong>{acceptingSetup ? "初始化码可用" : "等待服务器初始化码"}</strong>
                <span>{expiresAt ? `有效期至 ${expiresAt}` : "在私有数据目录 private/setup-code 中读取"}</span>
              </div>

              <form onSubmit={submit}>
                <AuthField icon="key" label="一次性初始化码 SETUP CODE" type="password" value={form.code} onChange={(event) => update("code", event.target.value)} required autoComplete="one-time-code" showPasswordToggle />
                <div className="setup-page__identity-row">
                  <AuthField icon="user" label="管理员用户名 USERNAME" value={form.username} onChange={(event) => update("username", event.target.value)} required autoComplete="username" />
                  <AuthField icon="envelope" label="邮箱 EMAIL" type="email" inputMode="email" value={form.email} onChange={(event) => update("email", event.target.value)} required autoComplete="email" />
                </div>
                <div className="setup-page__identity-row">
                  <AuthField icon="lock" label="密码 PASSWORD" type="password" value={form.password} onChange={(event) => update("password", event.target.value)} required minLength="8" autoComplete="new-password" showPasswordToggle />
                  <AuthField icon="shield" label="确认密码 REPEAT" type="password" value={form.passwordConfirm} onChange={(event) => update("passwordConfirm", event.target.value)} required minLength="8" autoComplete="new-password" showPasswordToggle />
                </div>
                <div className="auth-message-slot" aria-live="polite">
                  {error && <p className="form-message error" role="alert">{error}</p>}
                </div>
                <button className="auth-submit" type="submit" disabled={loading || !acceptingSetup}>
                  <Icon name="shield" /> {loading ? "正在初始化..." : "创建管理员并锁定首装入口"}
                </button>
              </form>
              <footer className="auth-security">
                <span><Icon name="lock" /> 初始化码不进入浏览器存储</span>
                <span>ONE ADMIN · ONE TIME</span>
              </footer>
            </div>
          </div>
        </section>
      </div>
    </main>
  );
}
