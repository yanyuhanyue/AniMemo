import { Link } from "react-router-dom";
import { Icon } from "../Icon.jsx";
import { useSiteSettings } from "../../context/SiteSettingsContext.jsx";
import { DEFAULT_POSTER } from "../../lib/demoMedia.js";
import { selectDailyHeroPosters } from "../../lib/heroArtSelector.js";
import { demoAnimeRecords } from "@demo-data";

const [, dailyPoster] = selectDailyHeroPosters(demoAnimeRecords, { domain: "featured" });

export function FeaturedHero() {
  const { settings } = useSiteSettings();
  return (
    <header className="featured-hero">
      <div className="featured-hero__dots" aria-hidden="true" />
      <div className="featured-hero__inner">
        <div className="featured-hero__copy">
          <Link to="/" className="featured-back featured-header-reveal"><Icon name="arrow-left" /> 返回展示主界面</Link>
          <span className="featured-hero__eyebrow featured-header-reveal">EDITOR&apos;S CHOICE / CURATED STORIES</span>
          <h1 className="featured-header-reveal"><b>精选</b><strong>专栏</strong></h1>
          <p className="featured-header-reveal">不追求千篇一律的标准答案。这里收录来自各位同好们的真实观看体验与长篇表达。</p>
        </div>
        <div className="featured-hero__art" aria-hidden="true">
          <span className="featured-hero__account">{settings.social_handle}</span>
          <div className="featured-hero__burst"><Icon name="star" /></div>
          <div className="featured-portrait featured-portrait--back"><img src={dailyPoster} alt="" onError={(event) => { event.currentTarget.onerror = null; event.currentTarget.src = DEFAULT_POSTER; }} /></div>
          <div className="featured-portrait featured-portrait--front"><img src="/assets/avatar.png" alt="" /></div>
        </div>
      </div>
    </header>
  );
}
