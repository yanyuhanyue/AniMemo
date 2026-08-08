import { Icon } from "../Icon.jsx";

export function AddAnimePlaceholder({ layout = "list", onAdd }) {
  if (layout === "grid") {
    return (
      <button
        type="button"
        className="catalog-add-placeholder catalog-add-placeholder--grid catalog-reveal-entry"
        onClick={onAdd}
        aria-label="添加一部新番剧"
      >
        <span className="catalog-add-placeholder__dots" aria-hidden="true" />
        <span className="catalog-add-placeholder__mark" aria-hidden="true"><Icon name="plus" /></span>
        <strong>添加番剧</strong>
        <span className="catalog-add-placeholder__caption">EXPAND YOUR JOURNAL</span>
        <span className="catalog-add-placeholder__sticker">ADD +1</span>
      </button>
    );
  }

  return (
    <div className="catalog-add-placeholder-row catalog-reveal-entry">
      <button
        type="button"
        className="catalog-add-placeholder catalog-add-placeholder--list"
        onClick={onAdd}
      >
        <span className="catalog-add-placeholder__mark" aria-hidden="true"><Icon name="plus" /></span>
        <span className="catalog-add-placeholder__copy">
          <strong>继续扩充你的番剧手账</strong>
          <small>ADD A NEW ANIME RECORD</small>
        </span>
        <span className="catalog-add-placeholder__action">添加番剧 <Icon name="arrow-right" /></span>
      </button>
    </div>
  );
}
