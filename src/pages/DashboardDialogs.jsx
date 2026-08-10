import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import gsap from "gsap";

import { Icon } from "../components/Icon.jsx";
import { ExternalAccountPanel } from "../components/dashboard/ExternalAccountPanel.jsx";
import { readableApiError } from "../lib/api.js";


function useDashboardDialogMotion({ rootRef, panelRef, pieceSelector, variant, onClose }) {
  const closingRef = useRef(false);
  const onCloseRef = useRef(onClose);
  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useLayoutEffect(() => {
    const root = rootRef.current;
    const panel = panelRef.current;
    if (!root || !panel) return undefined;
    const pieces = panel.querySelectorAll(pieceSelector);
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      gsap.set([root, panel, ...pieces], { clearProps: "all" });
      return undefined;
    }
    const isProfile = variant === "profile";
    const timeline = gsap.timeline();
    timeline
      .fromTo(root, { autoAlpha: 0 }, { autoAlpha: 1, duration: .14, ease: "power1.out" })
      .fromTo(panel, {
        autoAlpha: 0,
        scale: .7,
        y: isProfile ? -50 : -40,
        rotation: isProfile ? -2 : -1.5,
      }, {
        autoAlpha: 1,
        scale: 1,
        y: 0,
        rotation: 0,
        duration: isProfile ? .72 : .7,
        ease: "elastic.out(1, 0.6)",
        clearProps: "transform,opacity,visibility",
      }, 0)
      .fromTo(pieces, {
        autoAlpha: 0,
        y: -18,
        scale: isProfile ? .94 : .96,
      }, {
        autoAlpha: 1,
        y: 0,
        scale: 1,
        duration: .34,
        stagger: isProfile ? .05 : .1,
        ease: isProfile ? "back.out(1.7)" : "back.out(1.5)",
        clearProps: "transform,opacity,visibility",
      }, .18);
    gsap.ticker.wake();
    return () => timeline.kill();
  }, [panelRef, pieceSelector, rootRef, variant]);

  const closeWithMotion = useCallback((afterClose) => {
    if (closingRef.current) return;
    closingRef.current = true;
    const done = () => (afterClose || onCloseRef.current)?.();
    const root = rootRef.current;
    const panel = panelRef.current;
    if (!root || !panel || window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      done();
      return;
    }
    gsap.killTweensOf([root, panel]);
    gsap.timeline({ onComplete: done })
      .to(panel, {
        y: 50,
        scale: variant === "profile" ? .82 : .9,
        rotation: variant === "profile" ? 2 : 0,
        autoAlpha: 0,
        duration: variant === "profile" ? .24 : .22,
        ease: "power3.in",
      })
      .to(root, { autoAlpha: 0, duration: .12, ease: "power2.in" }, variant === "profile" ? "-=0.08" : "-=0.07");
  }, [panelRef, rootRef, variant]);

  useEffect(() => {
    const closeOnEscape = (event) => {
      if (event.key === "Escape") closeWithMotion();
    };
    document.body.classList.add("modal-open");
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.classList.remove("modal-open");
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [closeWithMotion]);

  return closeWithMotion;
}

export function QuickFilterEditor({ filters, onClose, onSave, onDelete }) {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const customFilters = filters.filter((filter) => !["all", "mix", "daily", "school", "healing", "extras"].includes(String(filter.id)));
  const [selectedId, setSelectedId] = useState(customFilters[0]?.id || "new");
  const selected = customFilters.find((filter) => String(filter.id) === String(selectedId));
  const [draft, setDraft] = useState(() => selected || { name: "", tags: [], title_keywords: [], match_mode: "any", color: "#ffe66d" });
  const [busy, setBusy] = useState(false);
  const [mutationError, setMutationError] = useState("");
  const syncDraft = (filter) => setDraft(filter || { name: "", tags: [], title_keywords: [], match_mode: "any", color: "#ffe66d" });
  const selectFilter = (filter) => { setSelectedId(filter?.id || "new"); syncDraft(filter); setMutationError(""); };
  const update = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const closeWithMotion = useDashboardDialogMotion({ rootRef, panelRef, pieceSelector: ".dashboard-filter-editor__piece", variant: "filter", onClose });
  const save = async (event) => {
    event.preventDefault();
    if (busy || !draft.name.trim()) return;
    setBusy(true);
    setMutationError("");
    try {
      await onSave({ ...draft, id: selected?.id, name: draft.name.trim() });
      closeWithMotion();
    } catch (error) {
      setMutationError(readableApiError(error, "筛选保存失败，请稍后重试。"));
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!selected || busy) return;
    setBusy(true);
    setMutationError("");
    try {
      await onDelete(selected);
      closeWithMotion();
    } catch (error) {
      setMutationError(readableApiError(error, "筛选删除失败，请稍后重试。"));
      setBusy(false);
    }
  };
  return (
    <div className="dashboard-modal-backdrop" ref={rootRef} role="dialog" aria-modal="true" aria-label="编辑自定义快速筛选">
      <button className="dashboard-modal-backdrop__dismiss" type="button" onClick={() => closeWithMotion()} aria-label="关闭筛选编辑器" disabled={busy} />
      <section className="dashboard-filter-editor" ref={panelRef}>
        <header className="dashboard-filter-editor__piece"><div><span className="dashboard-modal-kicker">CUSTOM FILTER LAB</span><h2>自定义快速筛选</h2><p>组合标签和标题关键词，保存常用检索方式。</p></div><button type="button" className="dashboard-square-button" onClick={() => closeWithMotion()} aria-label="关闭" disabled={busy}><Icon name="close" /></button></header>
        <div className="dashboard-filter-editor__body">
          <aside className="dashboard-filter-editor__piece"><button type="button" className={selectedId === "new" ? "is-active" : ""} onClick={() => selectFilter(null)} disabled={busy}><Icon name="plus" /> 新建筛选</button>{customFilters.map((filter) => <button type="button" className={String(selectedId) === String(filter.id) ? "is-active" : ""} key={filter.id} onClick={() => selectFilter(filter)} disabled={busy}><i style={{ background: filter.color || "#ffe66d" }} /> <span>{filter.name}</span></button>)}</aside>
          <form className="dashboard-filter-editor__piece" onSubmit={save}>
            <label><span>筛选名称</span><input value={draft.name || ""} maxLength={80} onChange={(event) => update("name", event.target.value)} placeholder="例如：周末治愈清单" required /></label>
            <label><span>标签组合</span><input value={(draft.tags || []).join("、")} onChange={(event) => update("tags", event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="日常、治愈、原创" /></label>
            <label><span>标题关键词</span><input value={(draft.title_keywords || []).join("、")} onChange={(event) => update("title_keywords", event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="可选，多个词用逗号分隔" /></label>
            <fieldset><legend>匹配方式</legend><button type="button" className={(draft.match_mode || "any") === "any" ? "is-active" : ""} onClick={() => update("match_mode", "any")}><Icon name="filter" /> 匹配任意条件</button><button type="button" className={draft.match_mode === "all" ? "is-active" : ""} onClick={() => update("match_mode", "all")}><Icon name="check" /> 匹配全部条件</button></fieldset>
            <label className="dashboard-filter-color"><span>标记颜色</span><input type="color" value={draft.color || "#ffe66d"} onChange={(event) => update("color", event.target.value)} /><b style={{ background: draft.color || "#ffe66d" }}>{draft.color || "#ffe66d"}</b></label>
            {mutationError && <p className="dashboard-security-status is-error" role="alert">{mutationError}</p>}
            <footer>{selected && <button className="is-delete" type="button" onClick={remove} disabled={busy}><Icon name="trash" /> {busy ? "处理中..." : "删除筛选"}</button>}<button type="button" onClick={() => closeWithMotion()} disabled={busy}>取消</button><button className="is-save" type="submit" disabled={busy}><Icon name="save" /> {busy ? "正在保存..." : "保存筛选"}</button></footer>
          </form>
        </div>
      </section>
    </div>
  );
}

export function DashboardAvatar({ src, alt = "" }) {
  const [failed, setFailed] = useState(false);
  useEffect(() => setFailed(false), [src]);

  if (!src || failed) {
    return <span className="dashboard-profile-avatar__fallback" aria-label={alt || "默认头像"}><Icon name="star" /></span>;
  }

  return <img src={src} alt={alt} onError={() => setFailed(true)} />;
}

export function ProfileMenu({ settings, onEdit, onLogout, onDelete, onKeyDown }) {
  const identity = settings.nickname.trim() || settings.email || "当前账户";
  return (
    <div className="dashboard-account-menu" role="menu" aria-label="账户菜单" onKeyDown={onKeyDown}>
      <div className="dashboard-account-menu__identity">
        <strong>{identity}</strong>
        <small>{settings.email || "未设置邮箱"}</small>
      </div>
      <button type="button" role="menuitem" className="is-settings" onClick={onEdit}>
        <span className="dashboard-account-menu__icon"><Icon name="edit" /></span>
        <span><strong>设置 / 修改资料</strong></span>
      </button>
      <div className="dashboard-account-menu__divider" aria-hidden="true" />
      <button type="button" role="menuitem" onClick={onLogout}>
        <span className="dashboard-account-menu__icon"><Icon name="logout" /></span>
        <span><strong>退出登录</strong><small>保留账户和全部数据</small></span>
      </button>
      <button type="button" role="menuitem" className="is-danger" onClick={onDelete}>
        <span className="dashboard-account-menu__icon"><Icon name="trash" /></span>
        <span><strong>永久注销账户</strong><small>彻底删除全部私人数据</small></span>
      </button>
    </div>
  );
}

export function ProfilePanel({ settings, initialTab = "profile", isDemo, onClose, onSave, onChangePassword, onOpenBangumiImport }) {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const tabRefs = useRef([]);
  const [draft, setDraft] = useState(settings);
  const [avatarPreview, setAvatarPreview] = useState(settings.avatar);
  const [avatarFile, setAvatarFile] = useState(null);
  const [activeTab, setActiveTab] = useState(initialTab);
  const [security, setSecurity] = useState({ currentPassword: "", password: "", passwordConfirm: "" });
  const [securityStatus, setSecurityStatus] = useState({ type: "", message: "" });
  const [securityBusy, setSecurityBusy] = useState(false);
  const [profileBusy, setProfileBusy] = useState(false);
  const [profileError, setProfileError] = useState("");
  const fileRef = useRef(null);
  const update = (key, value) => setDraft((current) => ({ ...current, [key]: value }));
  const updateSecurity = (key, value) => setSecurity((current) => ({ ...current, [key]: value }));
  const closeWithMotion = useDashboardDialogMotion({ rootRef, panelRef, pieceSelector: ".dashboard-profile-modal__piece", variant: "profile", onClose });
  const selectAvatar = (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    setAvatarFile(file);
    reader.onload = () => { setAvatarPreview(String(reader.result)); update("avatar", String(reader.result)); };
    reader.readAsDataURL(file);
  };
  const changeTab = (nextTab) => {
    if (nextTab === activeTab) return;
    setSecurityStatus({ type: "", message: "" });
    setActiveTab(nextTab);
  };
  const handleTabKeyDown = (event) => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs = ["profile", "security", "external"];
    const currentIndex = Math.max(0, tabs.indexOf(activeTab));
    const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : event.key === "ArrowLeft" ? (currentIndex - 1 + tabs.length) % tabs.length : (currentIndex + 1) % tabs.length;
    const nextTab = tabs[nextIndex];
    changeTab(nextTab);
    tabRefs.current[nextIndex]?.focus();
  };
  const submitPassword = async (event) => {
    event.preventDefault();
    if (security.password !== security.passwordConfirm) {
      setSecurityStatus({ type: "error", message: "两次输入的新密码不一致。" });
      return;
    }
    setSecurityBusy(true);
    setSecurityStatus({ type: "", message: "" });
    try {
      await onChangePassword({ current_password: security.currentPassword, password: security.password, password_confirm: security.passwordConfirm });
      setSecurity({ currentPassword: "", password: "", passwordConfirm: "" });
      setSecurityStatus({ type: "success", message: isDemo ? "演示模式：安全口令交互已完成。" : "密码已更新，即将返回登录页。" });
    } catch (error) {
      setSecurityStatus({ type: "error", message: readableApiError(error, "密码更新失败，请检查当前密码。") });
    } finally {
      setSecurityBusy(false);
    }
  };
  const submitProfile = async () => {
    if (profileBusy) return;
    setProfileBusy(true);
    setProfileError("");
    try {
      await onSave({ ...draft, avatarFile });
      closeWithMotion();
    } catch (error) {
      setProfileError(readableApiError(error, "个人资料保存失败，请稍后重试。"));
      setProfileBusy(false);
    }
  };
  return (
    <div className="dashboard-modal-backdrop" ref={rootRef} role="dialog" aria-modal="true" aria-label="设置个人资料">
      <button className="dashboard-modal-backdrop__dismiss" type="button" onClick={() => closeWithMotion()} aria-label="关闭个人资料设置" disabled={profileBusy || securityBusy} />
      <section className={`dashboard-profile-modal dashboard-profile-modal--${activeTab}`} ref={panelRef}>
        <header className="dashboard-profile-modal__head dashboard-profile-modal__piece"><div><span className="dashboard-modal-kicker">PERSONAL PROFILE</span><h2>设置个人资料</h2><p>打造专属于你的手账名片</p></div><button type="button" className="dashboard-square-button" onClick={() => closeWithMotion()} aria-label="关闭" disabled={profileBusy || securityBusy}><Icon name="close" /></button></header>
        <div className="dashboard-profile-modal__tabs dashboard-profile-modal__piece" role="tablist" aria-label="个人设置" onKeyDown={handleTabKeyDown}>
          <button ref={(node) => { tabRefs.current[0] = node; }} id="dashboard-profile-tab" type="button" role="tab" aria-selected={activeTab === "profile"} aria-controls="dashboard-profile-panel" className={activeTab === "profile" ? "is-active" : ""} onClick={() => changeTab("profile")}><Icon name="user" /> 基础资料</button>
          <button ref={(node) => { tabRefs.current[1] = node; }} id="dashboard-security-tab" type="button" role="tab" aria-selected={activeTab === "security"} aria-controls="dashboard-security-panel" className={activeTab === "security" ? "is-active" : ""} onClick={() => changeTab("security")}><Icon name="shield" /> 安全设置</button>
          <button ref={(node) => { tabRefs.current[2] = node; }} id="dashboard-external-tab" type="button" role="tab" aria-selected={activeTab === "external"} aria-controls="dashboard-external-panel" className={activeTab === "external" ? "is-active" : ""} onClick={() => changeTab("external")}><Icon name="link" /> 外部账号</button>
        </div>
        <div className="dashboard-profile-modal__content">
          {activeTab === "profile" ? <div id="dashboard-profile-panel" role="tabpanel" aria-labelledby="dashboard-profile-tab">
            <div className="dashboard-profile-card dashboard-profile-modal__piece"><div className="dashboard-profile-card__avatar"><DashboardAvatar src={avatarPreview} alt="当前头像" /></div><div><strong>{draft.nickname.trim() || settings.email || "未设置昵称"}</strong><small>{settings.email || "未设置邮箱"}</small><button type="button" onClick={() => fileRef.current?.click()}><Icon name="camera" /> 选择本地头像</button><em>支持 JPG、PNG、WebP，最大 5MB</em></div><input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" onChange={selectAvatar} hidden /></div>
            <label className="dashboard-profile-field dashboard-profile-modal__piece"><span><Icon name="signature" /> 昵称</span><input value={draft.nickname} maxLength={50} onChange={(event) => update("nickname", event.target.value)} placeholder={settings.email || "输入昵称"} /><small>{draft.nickname.length}/50</small></label>
            <label className="dashboard-profile-field dashboard-profile-modal__piece"><span><Icon name="quote" /> 预览页副标题</span><textarea value={draft.subtitle} maxLength={200} onChange={(event) => update("subtitle", event.target.value)} placeholder="写一句属于你的追番宣言吧" /><small>{draft.subtitle.length}/200</small></label>
            {profileError && <p className="dashboard-security-status is-error dashboard-profile-modal__piece" role="alert">{profileError}</p>}
            <div className="dashboard-profile-actions dashboard-profile-modal__piece"><button type="button" onClick={() => closeWithMotion()} disabled={profileBusy}>暂不修改</button><button type="button" onClick={submitProfile} disabled={profileBusy}><Icon name="save" /> {profileBusy ? "正在保存..." : "保存资料"}</button></div>
          </div> : activeTab === "security" ? <form id="dashboard-security-panel" role="tabpanel" aria-labelledby="dashboard-security-tab" className="dashboard-security-panel" onSubmit={submitPassword}>
            <div className="dashboard-security-intro dashboard-profile-modal__piece">
              <span className="dashboard-security-intro__icon"><Icon name="key" /></span>
              <div><strong>修改登录密码</strong><small>修改成功后将自动退出登录，请使用新密码重新进入手账房。</small></div>
            </div>
            <label className="dashboard-security-field dashboard-profile-modal__piece"><span><Icon name="lock" /> 旧密码</span><input type="password" autoComplete="current-password" value={security.currentPassword} onChange={(event) => updateSecurity("currentPassword", event.target.value)} placeholder="请输入当前登录密码" required /></label>
            <label className="dashboard-security-field dashboard-profile-modal__piece"><span><Icon name="key" /> 新密码</span><input type="password" autoComplete="new-password" minLength={8} value={security.password} onChange={(event) => updateSecurity("password", event.target.value)} placeholder="请输入新的安全密码" required /></label>
            <label className="dashboard-security-field dashboard-profile-modal__piece"><span><Icon name="shield" /> 确认新密码</span><input type="password" autoComplete="new-password" minLength={8} value={security.passwordConfirm} onChange={(event) => updateSecurity("passwordConfirm", event.target.value)} placeholder="请再次输入新密码" required /></label>
            {securityStatus.message && <p className={`dashboard-security-status is-${securityStatus.type}`} role="status">{securityStatus.message}</p>}
            <div className="dashboard-profile-actions dashboard-security-actions dashboard-profile-modal__piece"><button type="button" onClick={() => closeWithMotion()}>暂不修改</button><button type="submit" disabled={securityBusy}>{securityBusy ? "正在修改..." : "修改密码"}</button></div>
          </form> : <ExternalAccountPanel isDemo={isDemo} onOpenImport={onOpenBangumiImport} />}
        </div>
      </section>
    </div>
  );
}

export function DangerZoneDialog({ settings, isDemo, onClose, onDeleteAccount }) {
  const rootRef = useRef(null);
  const panelRef = useRef(null);
  const passwordRef = useRef(null);
  const [form, setForm] = useState({ currentPassword: "", confirmation: "", verificationMode: "otp", otp: "", recoveryCode: "" });
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);
  const closeWithMotion = useDashboardDialogMotion({ rootRef, panelRef, pieceSelector: ".dashboard-danger-modal__piece", variant: "danger", onClose });
  const identity = settings.nickname.trim() || settings.email || "当前账户";
  const isStaffAccount = Boolean(settings.isStaff || settings.isSuperuser);
  const verificationValue = form.verificationMode === "recovery" ? form.recoveryCode : form.otp;
  const canDelete = !busy && Boolean(isDemo || form.currentPassword) && form.confirmation === "永久注销" && (!isStaffAccount || (settings.twoFactorEnabled && Boolean(verificationValue)));
  useEffect(() => {
    const timer = window.setTimeout(() => passwordRef.current?.focus({ preventScroll: true }), 420);
    return () => window.clearTimeout(timer);
  }, []);
  const submit = async (event) => {
    event.preventDefault();
    if (!canDelete) return;
    setBusy(true);
    setStatus("");
    try {
      await onDeleteAccount({
        current_password: form.currentPassword,
        otp: isStaffAccount && form.verificationMode === "otp" ? form.otp : "",
        recovery_code: isStaffAccount && form.verificationMode === "recovery" ? form.recoveryCode : "",
      });
    } catch (error) {
      setStatus(readableApiError(error, "注销失败，请检查当前密码。"));
      setBusy(false);
    }
  };
  return (
    <div className="dashboard-modal-backdrop dashboard-danger-backdrop" ref={rootRef} role="dialog" aria-modal="true" aria-label="永久注销账户">
      <button className="dashboard-modal-backdrop__dismiss" type="button" onClick={() => closeWithMotion()} aria-label="关闭永久注销窗口" />
      <section className="dashboard-danger-modal" ref={panelRef}>
        <div className="dashboard-danger-modal__top dashboard-danger-modal__piece"><span className="dashboard-danger-modal__icon"><Icon name="user-slash" /></span><button type="button" className="dashboard-square-button" onClick={() => closeWithMotion()} aria-label="关闭"><Icon name="close" /></button></div>
        <span className="dashboard-modal-kicker dashboard-danger-modal__piece">DANGER ZONE</span>
        <div className="dashboard-danger-modal__copy dashboard-danger-modal__piece"><h2>永久注销账户</h2><p>这会彻底删除 <mark>{identity}</mark> 的头像、追番记录、筛选配置和个性化方案，且无法恢复。</p></div>
        <div className="dashboard-danger-modal__warning dashboard-danger-modal__piece"><Icon name="warning" /><strong>注销后该邮箱可以重新注册，但新账号将从空白手账重新开始。</strong></div>
        <form className="dashboard-danger-modal__form dashboard-danger-modal__piece" onSubmit={submit}>
          <label><span>当前密码</span><div className="dashboard-danger-input"><Icon name="lock" /><input ref={passwordRef} type="password" autoComplete="current-password" value={form.currentPassword} onChange={(event) => setForm((current) => ({ ...current, currentPassword: event.target.value }))} placeholder="请输入当前登录密码" required={!isDemo} /></div></label>
          {isStaffAccount && <div className="dashboard-danger-reauth">
            <div className="dashboard-danger-reauth__heading"><span>工作人员二次验证</span><small>{settings.twoFactorEnabled ? "请选择 TOTP 或一枚恢复码" : "请先在安全设置中启用两步验证"}</small></div>
            <div className="dashboard-danger-reauth__switch" role="group" aria-label="二次验证方式">
              <button type="button" className={form.verificationMode === "otp" ? "is-active" : ""} onClick={() => setForm((current) => ({ ...current, verificationMode: "otp" }))}>TOTP 验证码</button>
              <button type="button" className={form.verificationMode === "recovery" ? "is-active" : ""} onClick={() => setForm((current) => ({ ...current, verificationMode: "recovery" }))}>恢复码</button>
            </div>
            <input inputMode={form.verificationMode === "otp" ? "numeric" : "text"} autoComplete="one-time-code" value={verificationValue} onChange={(event) => setForm((current) => form.verificationMode === "otp" ? ({ ...current, otp: event.target.value.replace(/\D/g, "").slice(0, 6) }) : ({ ...current, recoveryCode: event.target.value.toUpperCase().slice(0, 20) }))} placeholder={form.verificationMode === "otp" ? "输入 6 位 TOTP 验证码" : "输入一枚恢复码"} disabled={!settings.twoFactorEnabled} />
          </div>}
          <label><span>输入“永久注销”以确认</span><input value={form.confirmation} onChange={(event) => setForm((current) => ({ ...current, confirmation: event.target.value }))} placeholder="永久注销" required /></label>
          {status && <p className="dashboard-security-status is-error" role="status">{status}</p>}
          <div className="dashboard-danger-modal__actions"><button type="button" onClick={() => closeWithMotion()}>保留账户</button><button type="submit" disabled={!canDelete}><Icon name="trash" /> {busy ? "正在删除..." : "永久删除"}</button></div>
        </form>
      </section>
    </div>
  );
}

