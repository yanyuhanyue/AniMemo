import { Icon } from "./Icon.jsx";

export function ResetFilterButton({ onReset, disabled = false, className = "" }) {
  return (
    <button
      className={["filter-reset", className].filter(Boolean).join(" ")}
      type="button"
      onClick={onReset}
      disabled={disabled}
      title="一键恢复所有默认筛选"
    >
      <span className="filter-reset__icon" aria-hidden="true">
        <Icon name="reset" />
      </span>
      <span className="filter-reset__label">恢复默认</span>
    </button>
  );
}
