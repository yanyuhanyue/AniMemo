import { Icon } from "./Icon.jsx";
import { usePageColorTransition } from "./PageColorTransition.jsx";
import { DashboardPreviewActions } from "./dashboard/DashboardJournalControls.jsx";

function safeFileName(value) {
  return String(value || "public-journal")
    .replace(/[\\/:*?"<>|]+/g, "-")
    .replace(/\s+/g, "-")
    .slice(0, 64);
}

export function SharedShowcaseHeader({
  profile,
  records,
  ownerPreview = false,
  modeTransition = false,
  publicStatus = "private",
  onShareChange,
  onEdit,
  onReturnHome,
}) {
  const { isTransitioning, navigateWithTransition } = usePageColorTransition();
  const nickname = profile?.nickname || "公开同好";
  const subtitle = profile?.subtitle || "这位同好正在公开分享自己的观看轨道。";
  const avatar = profile?.avatar || profile?.avatar_url || "/assets/avatar.png";

  const exportRecords = () => {
    const payload = {
      exported_at: new Date().toISOString(),
      profile: {
        nickname,
        subtitle,
        public_slug: profile?.public_slug || "",
      },
      records,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `${safeFileName(nickname)}-anime-journal.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  };

  return (
    <header className={`showcase-hero shared-showcase-hero${ownerPreview ? " shared-showcase-hero--owner-preview" : ""}${modeTransition ? " dashboard-mode-transition" : ""}`}>
      <div className="hero-grid" aria-hidden="true" />
      <div className="hero-circle" aria-hidden="true"><Icon name="star" /></div>
      <div className="showcase-hero__inner shared-showcase-hero__inner">
        <div className="showcase-hero__copy shared-showcase-hero__copy shared-hero-piece">
          <span className="journal-kicker"><Icon name="heart" /> 个人手账 / 追番记录</span>
          <div className="showcase-title-row">
            <div className="avatar-frame"><img src={avatar} alt={`${nickname} 的头像`} /></div>
            <h1><span>{nickname} 的番剧汇总</span></h1>
          </div>
          <p>{subtitle}</p>
        </div>

        {ownerPreview ? (
          <div className="shared-showcase-hero__actions shared-showcase-hero__actions--preview shared-hero-piece">
            <DashboardPreviewActions
              publicStatus={publicStatus}
              onShareChange={onShareChange}
              onEdit={onEdit}
              onReturnHome={onReturnHome}
            />
          </div>
        ) : (
          <div className="shared-showcase-hero__actions shared-hero-piece">
            <button className="shared-showcase-action shared-showcase-action--export" type="button" onClick={exportRecords}>
              <span><Icon name="export" /> 导出数据</span>
            </button>
            <button
              className="shared-showcase-action shared-showcase-action--return"
              type="button"
              disabled={isTransitioning}
              onClick={() => navigateWithTransition("/universe")}
            >
              <span><Icon name="arrow-left" /> 返回共创大厅</span>
            </button>
          </div>
        )}
      </div>
    </header>
  );
}
