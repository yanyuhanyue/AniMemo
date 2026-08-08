import { useState } from "react";
import { Icon } from "../Icon.jsx";
import { ResetFilterButton } from "../ResetFilterButton.jsx";

const DEFAULT_STATUS_OPTIONS = [
  ["all", "全部状态"],
  ["completed", "看过"],
  ["watching", "在看"],
  ["planned", "想看"],
];

const DEFAULT_SORT_OPTIONS = [
  ["date-desc", "开播时间 (新 → 旧) [推荐]"],
  ["date-asc", "开播时间 (旧 → 新)"],
  ["score-desc", "评分 (高 → 低)"],
  ["score-asc", "评分 (低 → 高)"],
];

export function CatalogFilterLab({
  filters,
  onFilterChange,
  onReset,
  viewMode,
  onViewChange,
  resultCount,
  tags,
  years,
  quickFilters,
  onEditQuickFilters,
  statusOptions = DEFAULT_STATUS_OPTIONS,
  sortOptions = DEFAULT_SORT_OPTIONS,
}) {
  const [expanded, setExpanded] = useState(() => window.innerWidth >= 768);

  return (
    <section className="filter-lab control-piece">
      <span className="filter-lab__flag">FILTER LAB</span>
      <div className="filter-lab__heading">
        <span><Icon name="sliders" /> 数据检索与智能过滤</span>
        <button
          className="filter-lab__mobile-toggle"
          type="button"
          aria-label={expanded ? "收起筛选条件" : "展开筛选条件"}
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <span className="filter-lab__mobile-toggle-visual"><Icon name={expanded ? "arrow-up" : "arrow-right"} /></span>
        </button>
      </div>

      <div className={`filter-lab__content${expanded ? " is-open" : ""}`}>
        <div className="filter-grid">
          <label className="filter-field">
            <span>搜索番名 / 日文名</span>
            <div className="input-with-icon"><Icon name="search" /><input value={filters.search} onChange={(event) => onFilterChange("search", event.target.value)} placeholder="输入番剧中文或日文名..." /></div>
          </label>
          <label className="filter-field">
            <span>标签过滤</span>
            <span className="filter-control-hitbox">
              <select aria-label="标签过滤" value={filters.tag} onChange={(event) => onFilterChange("tag", event.target.value)}>
                <option value="all">全部标签</option>
                {tags.map((tag) => <option key={tag} value={tag}>{tag}</option>)}
              </select>
            </span>
          </label>
          <label className="filter-field">
            <span>观看状态</span>
            <span className="filter-control-hitbox">
              <select aria-label="观看状态" value={filters.status} onChange={(event) => onFilterChange("status", event.target.value)}>
                {statusOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </span>
          </label>
          <label className="filter-field">
            <span>年份区间</span>
            <span className="filter-control-hitbox">
              <select aria-label="年份区间" value={filters.year} onChange={(event) => onFilterChange("year", event.target.value)}>
                <option value="all">全部年份</option>
                {years.map((year) => <option key={year} value={year}>{year}</option>)}
              </select>
            </span>
          </label>
          <div className="filter-field filter-field--sort">
            <div className="filter-field__header">
              <label htmlFor="catalog-sort">排序规则 (默认)</label>
              <ResetFilterButton onReset={onReset} />
            </div>
            <span className="filter-control-hitbox">
              <select id="catalog-sort" aria-label="排序规则 (默认)" value={filters.sort} onChange={(event) => onFilterChange("sort", event.target.value)}>
                {sortOptions.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </span>
          </div>
        </div>

        <div className="quick-filter-row">
          <div className="quick-filters">
            <span><Icon name="tag" /> 快速筛选：</span>
            {quickFilters.map((quick) => {
              const label = quick.label ?? quick.name;
              return (
                <button
                  key={quick.id}
                  type="button"
                  className={String(filters.quick) === String(quick.id) ? "active" : ""}
                  onClick={() => onFilterChange("quick", String(quick.id))}
                >
                  <span className="quick-filter__visual">{String(quick.id) !== "all" && <Icon name="filter" />} {label}</span>
                </button>
              );
            })}
            {onEditQuickFilters && (
              <button className="quick-filter-edit" type="button" onClick={onEditQuickFilters} aria-label="编辑自定义快速筛选">
                <span className="quick-filter__visual"><Icon name="edit" /></span>
              </button>
            )}
          </div>
          <div className="view-result-row">
            <div className="view-toggle" role="group" aria-label="视图切换">
              <button type="button" className={`view-toggle__button${viewMode === "list" ? " active" : ""}`} onClick={() => onViewChange("list")} aria-pressed={viewMode === "list"}>
                <span className="view-toggle__icon"><Icon name="list" /></span><span>列表</span>
              </button>
              <button type="button" className={`view-toggle__button${viewMode === "grid" ? " active" : ""}`} onClick={() => onViewChange("grid")} aria-pressed={viewMode === "grid"}>
                <span className="view-toggle__icon"><Icon name="grid" /></span><span>海报墙</span>
              </button>
            </div>
            <div className="result-count"><Icon name="chart-line" /> 已筛选出 <strong key={resultCount} className="result-count__value">{resultCount}</strong> 部番剧</div>
          </div>
        </div>
      </div>
    </section>
  );
}
