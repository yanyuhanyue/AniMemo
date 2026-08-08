const GLYPHS = Object.freeze({
  "arrow-left": "←",
  "arrow-up-right": "↗",
  check: "✓",
  "file-upload": "⇧",
  history: "◷",
  layers: "▦",
  reset: "↻",
  save: "▣",
  search: "⌕",
  spinner: "◌",
  warning: "!",
});

export function Icon({ name, spin = false, className = "" }) {
  return (
    <span
      aria-hidden="true"
      className={`ajp-icon${spin ? " is-spinning" : ""}${className ? ` ${className}` : ""}`}
      data-icon={name}
    >
      {GLYPHS[name] || "•"}
    </span>
  );
}
