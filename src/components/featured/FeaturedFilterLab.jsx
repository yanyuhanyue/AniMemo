import { Icon } from "../Icon.jsx";
import { ResetFilterButton } from "../ResetFilterButton.jsx";

export function FeaturedFilterLab({ filters, tags, years, resultCount, onChange, onReset }) {
  return (
    <section className="featured-filter" aria-label="精选专栏筛选">
      <span className="featured-filter__flag">FILTER LAB</span>
      <h2><Icon name="sliders" /> 数据检索与智能过滤</h2>
      <div className="featured-filter__grid">
        <label className="featured-filter-field"><span className="featured-filter-field__label">搜索番名 / 日文名</span><div className="featured-search"><Icon name="search" /><input value={filters.q} onChange={(event) => onChange("q", event.target.value)} placeholder="输入番剧中文或日文名..." /></div></label>
        <label className="featured-filter-field"><span className="featured-filter-field__label">标签过滤</span><select value={filters.tag} onChange={(event) => onChange("tag", event.target.value)}><option value="all">全部标签</option>{tags.map((tag) => <option value={tag} key={tag}>{tag}</option>)}</select></label>
        <label className="featured-filter-field"><span className="featured-filter-field__label">观看状态</span><select value={filters.status} onChange={(event) => onChange("status", event.target.value)}><option value="all">全部状态</option><option value="completed">看过</option><option value="watching">在看</option></select></label>
        <label className="featured-filter-field"><span className="featured-filter-field__label">年份区间</span><select value={filters.year} onChange={(event) => onChange("year", event.target.value)}><option value="all">全部年份</option>{years.map((year) => <option value={year} key={year}>{year} 年</option>)}</select></label>
        <div className="featured-filter-field featured-filter-field--sort">
          <div className="featured-filter-field__header">
            <label className="featured-filter-field__label" htmlFor="featured-filter-sort">排序规则（默认）</label>
            <ResetFilterButton onReset={onReset} />
          </div>
          <select id="featured-filter-sort" value={filters.sort} onChange={(event) => onChange("sort", event.target.value)}><option value="date-desc">开播时间 (新 → 旧)</option><option value="score-desc">评分 (高 → 低)</option><option value="score-asc">评分 (低 → 高)</option><option value="title">标题拼音</option></select>
        </div>
      </div>
      <div className="featured-filter__footer">
        <div className="featured-quick" role="group" aria-label="快速筛选">
          <span><Icon name="tag" /> 快速筛选:</span>
          <button className={filters.quick === "all" ? "is-active" : ""} type="button" onClick={() => onChange("quick", "all")}>全部</button>
          <button className={filters.quick === "yuri" ? "is-active" : ""} type="button" onClick={() => onChange("quick", "yuri")}><Icon name="filter" /> 萌系 &amp; 治愈</button>
        </div>
        <div className="featured-filter__result" aria-live="polite">
          <strong className="featured-filter-result"><Icon name="chart-line" /> 已筛选出 <b key={resultCount}>{resultCount}</b> 部番剧</strong>
        </div>
      </div>
    </section>
  );
}
