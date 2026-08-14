import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { gsap } from "gsap";
import { Icon } from "../components/Icon.jsx";
import { PluginManagementPanel } from "../components/admin/PluginManagementPanel.jsx";
import { AdminResourcePanel, AdminSystemPanel } from "../components/admin/AdminControlPanels.jsx";
import { AdminMediaStoragePanel } from "../components/admin/AdminMediaStoragePanel.jsx";
import { AdminExternalServicesPanel } from "../components/admin/AdminExternalServicesPanel.jsx";
import { AdminUpdatePanel } from "../components/admin/AdminUpdatePanel.jsx";
import { TagManagementPanel } from "../components/admin/TagManagementPanel.jsx";
import { api, authApi, clearTokens, getStoredTokens, readableApiError } from "../lib/api.js";
import { createLiveRefreshController } from "../lib/liveRefresh.js";
import { DEFAULT_TRUSTED_POSTER_HOSTS, normalizeTrustedPosterHosts } from "../lib/posterSources.js";
import { usePluginRuntime } from "../plugins/sdk/PluginRuntimeContext.jsx";

const EMPTY = {
  stats: { users: 0, active_users: 0, entries: 0, columns: 0, pending_columns: 0, published_columns: 0, removal_requests: 0, pending_journals: 0 },
  pending_columns: [],
  recent_columns: [],
  recent_entries: [],
  journal_requests: [],
  users: [],
  viewer: { id: null, is_superuser: false, role: "administrator", capabilities: [] },
};

const EMPTY_SITE_SETTINGS = {
  site_name: "AniMemo",
  homepage_title: "AniMemo · 我的动漫记忆库",
  homepage_owner_id: null,
  homepage_owner_options: [],
  site_avatar_url: "/assets/avatar.png",
  homepage_description: "",
  universe_description: "",
  social_handle: "X: @ANIMEMO",
  registration_enabled: true,
  email_delivery_enabled: true,
  email_sender_name: "",
  email_sender_address: "",
  trusted_poster_hosts: DEFAULT_TRUSTED_POSTER_HOSTS,
  resend_api_key: "",
  clear_resend_api_key: false,
  resend_api_key_configured: false,
  resend_api_key_source: "none",
  effective_email_from: "",
  email_delivery_ready: false,
};

const tabs = [
  ["overview", "总览", "chart", ""],
  ["columns", "专栏审核", "book", "moderate_content"],
  ["journals", "手账审核", "satellite-dish", "moderate_content"],
  ["users", "用户管理", "users", "manage_users"],
  ["entries", "记录监控", "table", "moderate_content"],
  ["recycle", "回收站", "trash", "moderate_content"],
  ["audit", "审计日志", "history", "view_audit"],
  ["operations", "系统运维", "bolt", ""],
  ["updates", "系统更新", "history", "manage_system"],
  ["storage", "媒体存储", "upload", "superuser"],
  ["tags", "标签管理", "tags", "manage_system"],
  ["plugins", "插件中心", "puzzle", "manage_system"],
  ["services", "外部服务", "link", "manage_system"],
  ["settings", "站点设置", "gear", "manage_system"],
];

const tabMeta = {
  overview: { kicker: "SYSTEM OVERVIEW", title: "今天的系统状态", description: "快速查看账号、内容与审核队列的关键变化。" },
  columns: { kicker: "CONTENT REVIEW", title: "专栏审核", description: "处理投稿、精选状态与内容下架请求。" },
  journals: { kicker: "PUBLIC JOURNALS", title: "手账审核", description: "审核用户公开手账申请与撤销公开状态。" },
  users: { kicker: "ACCOUNT DIRECTORY", title: "用户管理", description: "查看账号状态、角色权限与登录安全信息。" },
  entries: { kicker: "JOURNAL MONITOR", title: "记录监控", description: "检索全站番剧记录并处理异常或违规内容。" },
  recycle: { kicker: "RECYCLE BIN", title: "回收站", description: "恢复已隐藏内容，保留完整的管理操作轨迹。" },
  audit: { kicker: "AUDIT TRAIL", title: "审计日志", description: "按管理员、对象与时间追踪后台操作差异。" },
  operations: { kicker: "SYSTEM OPERATIONS", title: "系统运维", description: "检查服务状态、导出备份并管理两步验证。" },
  updates: { kicker: "RELEASE CONSUMER", title: "系统更新", description: "验证不可变 Release，安全切换 API / Web，并查看应用层回退能力。" },
  storage: { kicker: "MEDIA STORAGE POOL", title: "媒体存储", description: "管理 R2 与本地媒体后端、写入优先级和容量保护。" },
  tags: { kicker: "TAG DIRECTORY", title: "标签管理", description: "统一维护公共标签、默认颜色与快捷预设。" },
  plugins: { kicker: "PLUGIN CONTROL", title: "插件中心", description: "检查插件兼容性、运行状态和受控部署配置。" },
  services: { kicker: "EXTERNAL SERVICES", title: "外部服务", description: "管理 Bangumi 等第三方应用的授权配置与可用状态。" },
  settings: { kicker: "SITE SETTINGS", title: "站点设置", description: "管理公共品牌资料、邮件服务与媒体安全策略。" },
};

function prefersReducedMotion() {
  return typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function dateLabel(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(new Date(value));
}

function statusLabel(value) {
  return {
    pending: "待审核",
    approved: "已通过",
    rejected: "未通过",
    removal_requested: "申请下架",
    draft: "草稿",
    private: "未公开",
  }[value] || value;
}

function StatCard({ label, value, detail, tone, icon }) {
  return (
    <article className={`admin-stat-card is-${tone}`}>
      <div className="admin-stat-card__top"><span>{label}</span><Icon name={icon} /></div>
      <strong>{value}</strong>
      <small>{detail}</small>
    </article>
  );
}

function EmptyState({ children = "暂无数据" }) {
  return <div className="admin-empty-state"><Icon name="layers" /><span>{children}</span></div>;
}

export function AdminDashboardPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { navigation } = usePluginRuntime();
  const adminItems = navigation.filter((item) => item.area === "admin");
  const [data, setData] = useState(EMPTY);
  const [tab, setTab] = useState("overview");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState(null);
  const [siteSettings, setSiteSettings] = useState(EMPTY_SITE_SETTINGS);
  const [siteSettingsDraft, setSiteSettingsDraft] = useState(EMPTY_SITE_SETTINGS);
  const [siteSettingsLoading, setSiteSettingsLoading] = useState(true);
  const [siteSettingsSaving, setSiteSettingsSaving] = useState(false);
  const [emailTestRecipient, setEmailTestRecipient] = useState("");
  const [emailTesting, setEmailTesting] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);
  const [siteAvatarFile, setSiteAvatarFile] = useState(null);
  const [siteAvatarPreview, setSiteAvatarPreview] = useState(EMPTY_SITE_SETTINGS.site_avatar_url);
  const hasLoadedRef = useRef(false);
  const loadInFlightRef = useRef(null);
  const pageRef = useRef(null);
  const headerRef = useRef(null);
  const shellRef = useRef(null);
  const panelRef = useRef(null);
  const toastRef = useRef(null);

  const apiBase = import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_URL || `${window.location.origin}/api/v1`;
  const adminUrl = location.state?.adminUrl || `${apiBase.replace(/\/api(?:\/v1)?\/?$/, "")}/admin/`;

  const load = useCallback((options = {}) => {
    if (loadInFlightRef.current) return loadInFlightRef.current;

    const silent = options?.silent === true;
    const request = (async () => {
      if (!getStoredTokens().access) {
        navigate("/admin-login", { replace: true });
        return;
      }
      if (!silent) setLoading(true);
      setError("");
      try {
        const response = await api.get("staff/dashboard/");
        setData({ ...EMPTY, ...(response.data || {}) });
      } catch (requestError) {
        if (requestError.response?.status === 401 || requestError.response?.status === 403) {
          clearTokens();
          navigate("/admin-login", { replace: true });
          return;
        }
        setError(readableApiError(requestError, "控制室数据加载失败。"));
      } finally {
        hasLoadedRef.current = true;
        if (!silent) setLoading(false);
      }
    })();

    loadInFlightRef.current = request;
    request.then(
      () => { if (loadInFlightRef.current === request) loadInFlightRef.current = null; },
      () => { if (loadInFlightRef.current === request) loadInFlightRef.current = null; },
    );
    return request;
  }, [navigate]);

  const loadSiteSettings = useCallback(async () => {
    if (!getStoredTokens().access) return;
    setSiteSettingsLoading(true);
    try {
      const { data: nextSettings } = await api.get("staff/site-settings/");
      const normalized = { ...EMPTY_SITE_SETTINGS, ...(nextSettings || {}) };
      setSiteSettings(normalized);
      setSiteSettingsDraft(normalized);
      setSiteAvatarPreview(normalized.site_avatar_url || EMPTY_SITE_SETTINGS.site_avatar_url);
      setSiteAvatarFile(null);
    } catch (requestError) {
      if (requestError.response?.status === 401 || requestError.response?.status === 403) {
        clearTokens();
        navigate("/admin-login", { replace: true });
        return;
      }
      setError(readableApiError(requestError, "站点设置加载失败。"));
    } finally {
      setSiteSettingsLoading(false);
    }
  }, [navigate]);

  useEffect(() => {
    const liveRefresh = createLiveRefreshController({
      refresh: () => load({ silent: hasLoadedRef.current }),
      intervalMs: 20000,
    });
    void liveRefresh.refreshNow();
    return () => liveRefresh.dispose();
  }, [load]);

  useEffect(() => {
    void loadSiteSettings();
  }, [loadSiteSettings]);

  useEffect(() => {
    if (!siteAvatarFile) return undefined;
    const objectUrl = URL.createObjectURL(siteAvatarFile);
    setSiteAvatarPreview(objectUrl);
    return () => URL.revokeObjectURL(objectUrl);
  }, [siteAvatarFile]);

  useLayoutEffect(() => {
    if (!pageRef.current || prefersReducedMotion()) return undefined;
    const context = gsap.context(() => {
      const timeline = gsap.timeline({ defaults: { ease: "power2.out" } });
      timeline
        .fromTo(headerRef.current, { autoAlpha: 0, y: -10 }, { autoAlpha: 1, y: 0, duration: 0.28, clearProps: "transform" })
        .fromTo(shellRef.current, { autoAlpha: 0, y: 14 }, { autoAlpha: 1, y: 0, duration: 0.34, clearProps: "transform" }, "-=0.16")
        .fromTo(".admin-dashboard-sidebar > *", { autoAlpha: 0, x: -8 }, { autoAlpha: 1, x: 0, duration: 0.24, stagger: 0.035, clearProps: "transform" }, "-=0.18");
    }, pageRef);
    return () => context.revert();
  }, []);

  useLayoutEffect(() => {
    if (!panelRef.current || loading || prefersReducedMotion()) return undefined;
    const context = gsap.context(() => {
      gsap.fromTo(panelRef.current, { autoAlpha: 0, y: 8 }, { autoAlpha: 1, y: 0, duration: 0.24, ease: "power2.out", clearProps: "transform" });
    }, panelRef);
    return () => context.revert();
  }, [loading, tab]);

  useLayoutEffect(() => {
    if (!notice || !toastRef.current || prefersReducedMotion()) return undefined;
    const context = gsap.context(() => {
      gsap.fromTo(toastRef.current, { autoAlpha: 0, y: 14, scale: 0.98 }, { autoAlpha: 1, y: 0, scale: 1, duration: 0.28, ease: "back.out(1.5)" });
    }, toastRef);
    return () => context.revert();
  }, [notice]);

  const flash = useCallback((message) => {
    setNotice(message);
    window.clearTimeout(flash.timer);
    flash.timer = window.setTimeout(() => setNotice(""), 2600);
  }, []);

  useEffect(() => {
    if (!location.state?.recoveryNotice) return;
    flash(location.state.recoveryNotice);
    navigate(location.pathname, { replace: true, state: { ...location.state, recoveryNotice: "" } });
  }, [flash, location, navigate]);

  const updateSiteSettingsDraft = (field, value) => {
    setSiteSettingsDraft((current) => ({ ...current, [field]: value }));
  };

  const persistSiteSettings = async () => {
    const payload = new FormData();
    ["site_name", "homepage_title", "homepage_description", "universe_description", "social_handle", "email_sender_name", "email_sender_address"].forEach((field) => {
      payload.append(field, siteSettingsDraft[field] || "");
    });
    if (siteSettingsDraft.homepage_owner_id) payload.append("homepage_owner_id", String(siteSettingsDraft.homepage_owner_id));
    payload.append("registration_enabled", String(siteSettingsDraft.registration_enabled));
    payload.append("email_delivery_enabled", String(siteSettingsDraft.email_delivery_enabled));
    payload.append("trusted_poster_hosts", JSON.stringify(normalizeTrustedPosterHosts(siteSettingsDraft.trusted_poster_hosts)));
    if (siteSettingsDraft.resend_api_key?.trim()) payload.append("resend_api_key", siteSettingsDraft.resend_api_key.trim());
    if (siteSettingsDraft.clear_resend_api_key) payload.append("clear_resend_api_key", "true");
    if (siteAvatarFile) payload.append("site_avatar", siteAvatarFile);
    const { data: savedSettings } = await api.patch("staff/site-settings/", payload);
    const normalized = { ...EMPTY_SITE_SETTINGS, ...(savedSettings || {}), resend_api_key: "", clear_resend_api_key: false };
    setSiteSettings(normalized);
    setSiteSettingsDraft(normalized);
    setSiteAvatarPreview(normalized.site_avatar_url || EMPTY_SITE_SETTINGS.site_avatar_url);
    setSiteAvatarFile(null);
    window.dispatchEvent(new CustomEvent("animemo:site-settings-updated"));
    return normalized;
  };

  const saveSiteSettings = async (event) => {
    event.preventDefault();
    if (siteSettingsSaving || emailTesting) return;
    setSiteSettingsSaving(true);
    setError("");
    try {
      await persistSiteSettings();
      flash("站点设置已保存并同步到公共页面");
    } catch (requestError) {
      setError(readableApiError(requestError, "站点设置保存失败。"));
    } finally {
      setSiteSettingsSaving(false);
    }
  };

  const testActivationEmail = async () => {
    if (siteSettingsSaving || emailTesting) return;
    if (!emailTestRecipient.trim()) {
      setError("请先填写测试邮件的收件地址。");
      return;
    }
    setEmailTesting(true);
    setError("");
    try {
      await persistSiteSettings();
      const { data: result } = await api.post("staff/site-settings/test-email/", { email: emailTestRecipient.trim() });
      flash(result?.detail || "测试邮件已发送");
    } catch (requestError) {
      setError(readableApiError(requestError, "测试邮件发送失败。"));
    } finally {
      setEmailTesting(false);
    }
  };

  const review = async (column, payload) => {
    setBusyId(column.id);
    try {
      await api.patch(`staff/columns/${column.id}/review/`, payload);
      await load();
      flash(payload.status === "approved" ? "专栏已通过审核" : payload.status === "rejected" ? "专栏已驳回" : "精选状态已更新");
    } catch (requestError) {
      setError(readableApiError(requestError, "审核操作失败。"));
    } finally {
      setBusyId(null);
    }
  };

  const reviewJournal = async (journal, nextStatus) => {
    setBusyId(`journal-${journal.id}`);
    try {
      await api.patch(`staff/public-journals/${journal.id}/review/`, { status: nextStatus });
      await load();
      flash(nextStatus === "approved" ? "个人手账已通过审核并公开" : "个人手账已恢复私密");
    } catch (requestError) {
      setError(readableApiError(requestError, "手账审核操作失败。"));
    } finally {
      setBusyId(null);
    }
  };

  const updateUserPermissions = async (user, payload) => {
    setBusyId(`user-${user.id}`);
    try {
      await api.patch(`staff/users/${user.id}/permissions/`, payload);
      await load({ silent: true });
      if (Object.hasOwn(payload, "is_staff")) {
        flash(payload.is_staff ? "已授予管理员权限" : "已移除管理员权限");
      } else {
        flash(payload.is_active ? "账号已启用" : "账号已停用");
      }
    } catch (requestError) {
      setError(readableApiError(requestError, "账号权限更新失败。"));
    } finally {
      setBusyId(null);
    }
  };

  const filteredUsers = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return data.users;
    return data.users.filter((user) => `${user.username} ${user.email} ${user.nickname}`.toLowerCase().includes(needle));
  }, [data.users, query]);

  const filteredEntries = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return data.recent_entries;
    return data.recent_entries.filter((entry) => `${entry.title} ${entry.user} ${entry.email}`.toLowerCase().includes(needle));
  }, [data.recent_entries, query]);

  const logout = async () => {
    if (loggingOut) return;
    setLoggingOut(true);
    try {
      await authApi.logout();
    } finally {
      clearTokens();
      navigate("/admin-login", { replace: true });
      setLoggingOut(false);
    }
  };

  const visibleTabs = useMemo(() => tabs.filter(([, , , capability]) => capability === "superuser" ? data.viewer.is_superuser : !capability || data.viewer.is_superuser || data.viewer.capabilities?.includes(capability)), [data.viewer]);
  const pluginTabs = useMemo(() => adminItems.filter((item) => {
    const roles = Array.isArray(item.roles) ? item.roles : [];
    if (data.viewer.is_superuser || !roles.length) return true;
    return roles.includes(data.viewer.role) || (roles.includes("admin") && data.viewer.role === "administrator");
  }).map((item) => [`plugin:${item.id}`, item.label, item.icon || "puzzle", "", item.path, item.id]), [adminItems, data.viewer]);
  const sidebarTabs = useMemo(() => [...visibleTabs.map((item) => [...item, "", ""]), ...pluginTabs], [pluginTabs, visibleTabs]);
  const activeTab = tabMeta[tab] || tabMeta.overview;
  const roleLabel = data.viewer.is_superuser ? "超级管理员" : ({ unassigned: "未分配", reviewer: "内容审核员", user_manager: "用户管理员", operator: "系统运维员", administrator: "后台管理员" })[data.viewer.role] || "未分配";
  const selectTab = (value) => {
    setTab(value);
    setQuery("");
  };
  const tabCount = (value) => value === "columns" ? data.stats.pending_columns : value === "journals" ? data.stats.pending_journals : 0;

  return (
    <main className="admin-dashboard-page" ref={pageRef} data-admin-tab={tab}>
      <div className="admin-dashboard-grid" aria-hidden="true" />
      <div className="admin-dashboard-shape admin-dashboard-shape--square" aria-hidden="true" />
      <div className="admin-dashboard-shape admin-dashboard-shape--ring" aria-hidden="true" />
      <header className="admin-dashboard-header" ref={headerRef}>
        <div className="admin-dashboard-brand"><span className="admin-dashboard-brand__mark"><Icon name="shield" /></span><div><small>{siteSettings.site_name.toUpperCase()} / STAFF ONLY</small><h1>管理控制室</h1></div></div>
        <div className="admin-dashboard-header__actions">{data.viewer.is_superuser && <a className="admin-dashboard-button is-yellow" href={adminUrl} target="_blank" rel="noreferrer"><Icon name="gear" /> Django 高级后台</a>}<button className="admin-dashboard-button" type="button" onClick={() => navigate("/")}><Icon name="arrow-left" /> 返回主界面</button><button className="admin-dashboard-button is-coral" type="button" onClick={logout} disabled={loggingOut}><Icon name="logout" /> {loggingOut ? "正在退出..." : "退出工作人员"}</button></div>
      </header>

      <section className="admin-dashboard-shell" ref={shellRef}>
        <aside className="admin-dashboard-sidebar">
          <div className="admin-dashboard-user"><span><Icon name="shield" /></span><div><strong>STAFF CONTROL</strong><small>权限：{roleLabel}</small></div></div>
          <nav aria-label="控制室导航">{sidebarTabs.map(([value, label, icon, _capability, path]) => {
            const count = tabCount(value);
            const active = path ? location.pathname.startsWith(path) : tab === value;
            return <button key={value} type="button" className={active ? "is-active" : ""} aria-current={active ? "page" : undefined} onClick={() => path ? navigate(path) : selectTab(value)}><Icon name={icon} /> {label}{count > 0 && <b>{count}</b>}</button>;
          })}</nav>
          <label className="admin-dashboard-mobile-nav"><span>当前管理模块</span><select value={tab} onChange={(event) => { const item = sidebarTabs.find(([value]) => value === event.target.value); item?.[4] ? navigate(item[4]) : selectTab(event.target.value); }}>{sidebarTabs.map(([value, label]) => <option key={value} value={value}>{label}{tabCount(value) > 0 ? ` (${tabCount(value)})` : ""}</option>)}</select></label>
          <div className="admin-dashboard-sidebar__note"><span>LIVE MONITOR</span><p>审核队列每 20 秒自动同步。</p></div>
        </aside>

        <section className="admin-dashboard-content">
          <div className="admin-dashboard-content__head"><div><span className="admin-dashboard-kicker">{activeTab.kicker}</span><h2 id="admin-dashboard-title">{activeTab.title}</h2><p>{activeTab.description}</p></div><div className="admin-dashboard-live"><i /> LIVE / {dateLabel(new Date())}</div></div>

          <div className="admin-dashboard-panel-stage" ref={panelRef} key={tab} aria-labelledby="admin-dashboard-title">
            {error && <div className="admin-dashboard-alert" role="alert"><Icon name="bolt" /> {error}<button type="button" onClick={() => load()}>重试</button></div>}
            {loading ? <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取控制室数据</div> : <>
              {tab === "overview" && <>
                <div className="admin-stat-grid">
                  <StatCard label="注册用户" value={data.stats.users} detail={`${data.stats.active_users} 个活跃账号`} tone="pink" icon="users" />
                  <StatCard label="番剧记录" value={data.stats.entries} detail="全部用户手账记录" tone="yellow" icon="table" />
                  <StatCard label="待审核专栏" value={data.stats.pending_columns} detail={`${data.stats.removal_requests} 个下架申请`} tone="teal" icon="filter" />
                  <StatCard label="精选发布" value={data.stats.published_columns} detail={`${data.stats.columns} 个专栏总量`} tone="coral" icon="star" />
                </div>
                <div className="admin-dashboard-columns-grid"><section className="admin-panel"><header><div><span>REVIEW QUEUE</span><h3>待处理精选专栏</h3></div><button type="button" onClick={() => selectTab("columns")}>查看全部 <Icon name="arrow-right" /></button></header><ColumnTable columns={data.pending_columns.slice(0, 5)} busyId={busyId} onReview={review} compact /></section><section className="admin-panel admin-panel--teal"><header><div><span>RECENT USERS</span><h3>最近加入的用户</h3></div><button type="button" onClick={() => selectTab("users")}>用户列表 <Icon name="arrow-right" /></button></header><UserTable users={data.users.slice(0, 5)} /></section></div>
              </>}

              {["columns", "journals", "users", "entries", "recycle", "audit"].includes(tab) && <AdminResourcePanel kind={tab} viewer={data.viewer} onNotice={flash} onError={setError} />}
              {tab === "operations" && <AdminSystemPanel viewer={data.viewer} onNotice={flash} onError={setError} />}
              {tab === "updates" && <AdminUpdatePanel viewer={data.viewer} onNotice={flash} onError={setError} />}
              {tab === "storage" && <AdminMediaStoragePanel viewer={data.viewer} onNotice={flash} onError={setError} />}
              {tab === "tags" && <TagManagementPanel onNotice={flash} onError={setError} />}
              {tab === "plugins" && <PluginManagementPanel onNotice={flash} />}
              {tab === "services" && <AdminExternalServicesPanel onNotice={flash} onError={setError} />}
              {tab === "settings" && <SiteSettingsPanel settings={siteSettings} draft={siteSettingsDraft} loading={siteSettingsLoading} saving={siteSettingsSaving} avatarPreview={siteAvatarPreview} emailTestRecipient={emailTestRecipient} emailTesting={emailTesting} onChange={updateSiteSettingsDraft} onAvatarChange={setSiteAvatarFile} onEmailTestRecipientChange={setEmailTestRecipient} onTestEmail={testActivationEmail} onReload={loadSiteSettings} onSubmit={saveSiteSettings} />}
            </>}
          </div>
        </section>
      </section>
      {notice && <div className="admin-dashboard-toast" ref={toastRef} role="status"><Icon name="check" /> {notice}</div>}
    </main>
  );
}

function ColumnTable({ columns, busyId, onReview, compact = false }) {
  if (!columns.length) return <EmptyState>审核队列为空</EmptyState>;
  return <div className={`admin-data-table admin-column-table${compact ? " is-compact" : ""}`}>
    {columns.map((column) => <article key={column.id} className="admin-data-row"><div className="admin-data-main"><strong>{column.title}</strong><small>{column.author} · {column.author_email || "未填写邮箱"}</small></div><span className={`admin-status is-${column.status}`}>{statusLabel(column.status)}</span><span className="admin-data-meta">{column.entry_count} 条记录<br />{dateLabel(column.updated_at)}</span><div className="admin-row-actions">{column.status !== "approved" && <button type="button" className="is-approve" disabled={busyId === column.id} onClick={() => onReview(column, { status: "approved" })}><Icon name="check" /> 通过</button>}{!compact && column.status === "pending" && <button type="button" className="is-reject" disabled={busyId === column.id} onClick={() => onReview(column, { status: "rejected" })}><Icon name="close" /> 驳回</button>}<button type="button" className={column.featured ? "is-featured" : ""} disabled={busyId === column.id} onClick={() => onReview(column, { featured: !column.featured })}><Icon name="star" /> {column.featured ? "取消精选" : "设为精选"}</button></div></article>)}
  </div>;
}

function UserTable({ users, full = false, busyId = null, viewer = EMPTY.viewer, onUpdatePermissions }) {
  if (!users.length) return <EmptyState>还没有匹配的用户</EmptyState>;
  return <div className={`admin-data-table admin-user-table${full ? " is-full" : ""}`}>
    {users.map((user) => {
      const busy = busyId === `user-${user.id}`;
      const isCurrent = user.id === viewer.id;
      const avatarTone = user.is_superuser ? "is-superuser" : user.is_staff ? "is-staff" : "is-user";
      return <article key={user.id} className="admin-data-row"><div className={`admin-user-avatar ${avatarTone}`}><Icon name={user.is_staff ? "shield" : "user"} /></div><div className="admin-data-main"><strong>{user.nickname || user.username}</strong><small>{user.email || "未填写邮箱"}</small></div><div className="admin-user-permissions"><span className={`admin-status ${user.is_active ? "is-approved" : "is-pending"}`}>{user.is_active ? "已激活" : "已停用"}</span><span className={`admin-status admin-user-role ${user.is_staff ? "is-admin" : "is-user"}`}>{user.is_superuser ? "超级管理员" : user.is_staff ? "管理员" : "普通用户"}</span></div><span className="admin-data-meta">{user.entry_count} 条记录<br />{user.column_count} 个专栏</span>{full && <span className="admin-data-date">{dateLabel(user.date_joined)}</span>}{full && <div className="admin-row-actions admin-user-actions">{isCurrent ? <span className="admin-user-current">当前账号</span> : user.can_manage === false ? <span className="admin-user-current">更高权限账号</span> : !user.is_staff && !user.is_superuser ? <button type="button" className={user.is_active ? "is-reject" : "is-approve"} disabled={busy} onClick={() => onUpdatePermissions?.(user, { is_active: !user.is_active })}><Icon name={user.is_active ? "eye-slash" : "check"} /> {user.is_active ? "停用账号" : "启用账号"}</button> : <span className="admin-user-current">请打开详情操作</span>}</div>}</article>;
    })}
  </div>;
}

function JournalReviewTable({ journals, busyId, onReview }) {
  if (!journals.length) return <EmptyState>暂无公开手账申请</EmptyState>;
  return <div className="admin-data-table admin-journal-table">
    {journals.map((journal) => {
      const busy = busyId === `journal-${journal.id}`;
      return <article key={journal.id} className="admin-data-row"><div className="admin-data-main"><strong>{journal.nickname}</strong><small>{journal.email || journal.username}</small></div><span className={`admin-status is-${journal.public_status}`}>{statusLabel(journal.public_status)}</span><span className="admin-data-meta">{journal.entry_count} 条记录<br />{dateLabel(journal.updated_at)}</span><div className="admin-row-actions">{journal.public_status === "pending" && <><button type="button" className="is-approve" disabled={busy} onClick={() => onReview(journal, "approved")}><Icon name="check" /> 通过</button><button type="button" className="is-reject" disabled={busy} onClick={() => onReview(journal, "private")}><Icon name="close" /> 驳回</button></>}{journal.public_status === "approved" && <button type="button" className="is-reject" disabled={busy} onClick={() => onReview(journal, "private")}><Icon name="eye-slash" /> 撤销公开</button>}</div></article>;
    })}
  </div>;
}

function EntryTable({ entries }) {
  if (!entries.length) return <EmptyState>还没有匹配的番剧记录</EmptyState>;
  return <div className="admin-data-table admin-entry-table">{entries.map((entry) => <article key={entry.id} className="admin-data-row"><div className="admin-data-main"><strong>{entry.title}</strong><small>{entry.user} · {entry.email || "未填写邮箱"}</small></div><span className="admin-status is-teal">{entry.status}</span><span className="admin-score">{entry.score ?? "—"}</span><span className="admin-data-meta">{entry.visibility}<br />{dateLabel(entry.updated_at)}</span></article>)}</div>;
}

function SiteSettingsPanel({ settings, draft, loading, saving, avatarPreview, emailTestRecipient, emailTesting, onChange, onAvatarChange, onEmailTestRecipientChange, onTestEmail, onReload, onSubmit }) {
  const [activeSection, setActiveSection] = useState("identity");
  const sectionRef = useRef(null);
  useLayoutEffect(() => {
    if (!sectionRef.current || prefersReducedMotion()) return undefined;
    const context = gsap.context(() => {
      gsap.fromTo(sectionRef.current, { autoAlpha: 0, y: 8 }, { autoAlpha: 1, y: 0, duration: 0.22, ease: "power2.out", clearProps: "transform" });
    }, sectionRef);
    return () => context.revert();
  }, [activeSection]);
  if (loading) return <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取站点设置</div>;
  return (
    <section className="admin-panel admin-panel--full admin-site-settings">
      <header>
        <div className="admin-site-settings__title"><span>SITE CONTROL / SETTINGS</span><h3>公共站点设置</h3><p>把公共品牌资料与账号服务分开管理，修改后一次保存即可同步。</p></div>
        <button type="button" onClick={onReload}><Icon name="reset" /> 放弃修改</button>
      </header>
      <nav className="admin-site-settings__tabs" aria-label="站点设置分区">
        <button type="button" aria-pressed={activeSection === "identity"} aria-controls="admin-settings-identity" className={activeSection === "identity" ? "is-active" : ""} onClick={() => setActiveSection("identity")}><Icon name="image" /><span><b>站点资料</b><small>名称、头像与首页文案</small></span></button>
        <button type="button" aria-pressed={activeSection === "delivery"} aria-controls="admin-settings-delivery" className={activeSection === "delivery" ? "is-active" : ""} onClick={() => setActiveSection("delivery")}><Icon name="envelope" /><span><b>邮件与注册</b><small>激活邮件、Resend 与开放注册</small></span><em className={settings.email_delivery_ready ? "is-ready" : ""}>{settings.email_delivery_ready ? "READY" : "SETUP"}</em></button>
        <button type="button" aria-pressed={activeSection === "media"} aria-controls="admin-settings-media" className={activeSection === "media" ? "is-active" : ""} onClick={() => setActiveSection("media")}><Icon name="shield" /><span><b>媒体安全</b><small>用户自定义封面的可信来源</small></span><em className="is-ready">HTTPS</em></button>
      </nav>
      <form onSubmit={onSubmit}>
        {activeSection === "identity" ? <div id="admin-settings-identity" className="admin-site-settings__identity admin-site-settings__section" ref={sectionRef}>
          <div className="admin-site-settings__section-heading"><span>PUBLIC IDENTITY</span><h4>让访客先认出你的站点</h4><p>头像与首页文字会同步到公共首页和分享页。</p></div>
          <div className="admin-site-settings__identity-layout">
            <div className="admin-site-avatar">
              <img src={avatarPreview || EMPTY_SITE_SETTINGS.site_avatar_url} alt="当前站点头像预览" />
              <label><Icon name="image" /> 更换首页头像<input type="file" accept="image/png,image/jpeg,image/webp,image/gif" onChange={(event) => onAvatarChange(event.target.files?.[0] || null)} /></label>
              <small>用于公共首页，个人分享页仍显示用户自己的头像。</small>
            </div>
            <div className="admin-site-settings__fields">
              <label><span>网站名称</span><input value={draft.site_name} onChange={(event) => onChange("site_name", event.target.value)} maxLength="120" required /></label>
              <label><span>首页展示账号</span><select value={draft.homepage_owner_id || ""} onChange={(event) => onChange("homepage_owner_id", event.target.value ? Number(event.target.value) : null)} required><option value="">请选择后台账号</option>{(draft.homepage_owner_options || []).map((owner) => <option value={owner.id} key={owner.id}>{owner.label} · {owner.entry_count} 条记录</option>)}</select><small className="admin-site-settings__field-help">首页只显示这个账号的手账，其他账号仍可通过公开链接访问。</small></label>
              <label><span>首页主标题</span><input value={draft.homepage_title} onChange={(event) => onChange("homepage_title", event.target.value)} maxLength="160" required /></label>
              <label><span>社交账号文字</span><input value={draft.social_handle} onChange={(event) => onChange("social_handle", event.target.value)} maxLength="80" required /></label>
            </div>
          </div>
          <div className="admin-site-settings__copy">
            <label><span>首页说明文字</span><textarea value={draft.homepage_description} onChange={(event) => onChange("homepage_description", event.target.value)} maxLength="320" rows="4" required /></label>
            <label><span>番剧共创宇宙说明</span><textarea value={draft.universe_description} onChange={(event) => onChange("universe_description", event.target.value)} maxLength="320" rows="4" required /></label>
          </div>
        </div> : activeSection === "delivery" ? <section id="admin-settings-delivery" className="admin-site-settings__email admin-site-settings__section" ref={sectionRef} aria-labelledby="admin-email-settings-title">
          <div className="admin-email-settings__head">
            <div><span>TRANSACTIONAL EMAIL</span><h4 id="admin-email-settings-title">激活邮件服务</h4><p>管理注册激活与密码重置邮件。密钥只写入服务器，不会回传浏览器。</p></div>
            <div className={`admin-email-status${settings.email_delivery_ready ? " is-ready" : ""}`}><Icon name={settings.email_delivery_ready ? "circle-check" : "warning"} /> {settings.email_delivery_ready ? "可以发送" : "尚未就绪"}</div>
          </div>
          <div className="admin-email-settings__switches">
            <label className="admin-registration-switch admin-email-switch">
              <input type="checkbox" checked={draft.email_delivery_enabled} onChange={(event) => onChange("email_delivery_enabled", event.target.checked)} />
              <span aria-hidden="true"><i /></span>
              <strong><b>启用激活邮件</b><small>{draft.email_delivery_enabled ? "注册和密码重置将调用邮件服务" : "普通用户注册将暂时不可用"}</small></strong>
            </label>
            <label className="admin-registration-switch admin-email-switch">
              <input type="checkbox" checked={draft.registration_enabled} onChange={(event) => onChange("registration_enabled", event.target.checked)} />
              <span aria-hidden="true"><i /></span>
              <strong><b>开放用户注册</b><small>{draft.registration_enabled ? "新用户可以注册并接收激活邮件" : "注册页将关闭，后端也会拒绝注册请求"}</small></strong>
            </label>
          </div>
          <div className="admin-email-settings__grid">
            <label><span>发件人名称</span><input value={draft.email_sender_name} onChange={(event) => onChange("email_sender_name", event.target.value)} maxLength="120" placeholder={draft.site_name || "AniMemo"} /></label>
            <label><span>发件邮箱</span><input type="email" value={draft.email_sender_address} onChange={(event) => onChange("email_sender_address", event.target.value)} placeholder="noreply@mail.example.com" /></label>
            <label className="admin-email-key-field"><span>Resend API Key</span><input type="password" value={draft.resend_api_key} onChange={(event) => { onChange("resend_api_key", event.target.value); onChange("clear_resend_api_key", false); }} autoComplete="new-password" placeholder={settings.resend_api_key_configured ? "已配置，留空表示保持不变" : "re_xxxxxxxxx"} /><small>当前来源：{settings.resend_api_key_source === "database" ? "管理员后台" : settings.resend_api_key_source === "environment" ? "服务器环境变量" : "未配置"}</small></label>
            <div className="admin-email-key-actions"><button type="button" className={draft.clear_resend_api_key ? "is-clearing" : ""} disabled={!settings.resend_api_key_configured && !draft.resend_api_key} onClick={() => { onChange("resend_api_key", ""); onChange("clear_resend_api_key", !draft.clear_resend_api_key); }}><Icon name="trash" /> {draft.clear_resend_api_key ? "保存后清除" : "清除已存密钥"}</button><small>实际发件人：{settings.effective_email_from || "保存后生成"}</small></div>
          </div>
          <div className="admin-email-test">
            <label><span>测试收件邮箱</span><input type="email" value={emailTestRecipient} onChange={(event) => onEmailTestRecipientChange(event.target.value)} placeholder="admin@example.com" /></label>
            <button type="button" disabled={saving || emailTesting || !emailTestRecipient.trim()} onClick={onTestEmail}><Icon name={emailTesting ? "spinner" : "envelope"} spin={emailTesting} /> {emailTesting ? "正在发送..." : "保存并发送测试邮件"}</button>
          </div>
        </section> : <section id="admin-settings-media" className="admin-site-settings__media admin-site-settings__section" ref={sectionRef} aria-labelledby="admin-media-settings-title">
          <div className="admin-site-settings__section-heading"><span>MEDIA SECURITY</span><h4 id="admin-media-settings-title">可信封面来源</h4><p>普通用户可上传本地 JPG、PNG、WebP，也可填写下列域名提供的 HTTPS 图片地址。</p></div>
          <div className="admin-media-settings__notice"><Icon name="shield" /><div><strong>服务器只接受精确匹配的域名</strong><p>不支持通配符、子域名继承、HTTP、IP 地址、账号密码或任意外链，避免形成开放图片代理。</p></div></div>
          <label className="admin-media-settings__hosts"><span>可信图片域名 · 每行一个</span><textarea value={(Array.isArray(draft.trusted_poster_hosts) ? draft.trusted_poster_hosts : []).join("\n")} onChange={(event) => onChange("trusted_poster_hosts", event.target.value.split(/\r?\n/).map((host) => host.trim()).filter(Boolean))} rows="8" spellCheck="false" placeholder="lain.bgm.tv" /><small>只填写域名，例如 <b>lain.bgm.tv</b>。不要包含 <b>https://</b>、路径或端口；至少保留一个域名。</small></label>
          <div className="admin-media-settings__rules"><span><Icon name="upload" /><b>本地上传</b><small>最大 5MB，服务端重编码为 WebP 并移除元数据</small></span><span><Icon name="image" /><b>来源优先级</b><small>本地上传 ＞ 可信自定义 URL ＞ Bangumi 公共封面</small></span><span><Icon name="reset" /><b>恢复默认</b><small>清除用户上传与自定义 URL，回到公共封面</small></span></div>
        </section>}
        <div className="admin-site-settings__footer">
          <div className="admin-site-settings__footer-context"><Icon name={activeSection === "identity" ? "image" : activeSection === "delivery" ? "envelope" : "shield"} /><span><b>{activeSection === "identity" ? "站点资料" : activeSection === "delivery" ? "邮件与注册" : "媒体安全"}</b><small>修改内容会在保存后同步到公共页面</small></span></div>
          <div className="admin-site-settings__save">
            <small>上次保存：{dateLabel(settings.updated_at)}</small>
            <button type="submit" disabled={saving}><Icon name="check" /> {saving ? "正在保存..." : "保存站点设置"}</button>
          </div>
        </div>
      </form>
    </section>
  );
}
