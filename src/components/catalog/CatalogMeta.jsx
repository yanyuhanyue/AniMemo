import { Icon } from "../Icon.jsx";

export function CatalogMeta({ resultCount, pageSize, onPageSizeChange, unscoredCount, pageSizeOptions = ["12", "24"] }) {
  return (
    <section className="list-meta control-piece">
      <label className="page-size-card yellow">
        <span><Icon name="table" /> 展示数量 <b>共 {resultCount} 条记录</b></span>
        <select aria-label="选择每页显示数量" value={pageSize} onChange={(event) => onPageSizeChange(event.target.value)}>
          <option value="all">全部</option>
          {pageSizeOptions.map((value) => <option value={value} key={value}>{value} 条/页</option>)}
        </select>
      </label>
      <div className="ranking-card"><span><Icon name="layers" /> 已评分与“看过”作品优先展示</span><small>{unscoredCount} 部未评分作品置于底部</small></div>
    </section>
  );
}
