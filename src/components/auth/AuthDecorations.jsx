export function AuthDecorations() {
  return (
    <div className="auth-decorations" aria-hidden="true">
      <div className="auth-dot-pattern" />
      <div className="auth-geometry">
        <div className="auth-geo-position auth-triangle-position">
          <svg className="auth-geo-motion auth-triangle-rotor" viewBox="0 0 120 120">
            <path d="M60 8 112 106H8Z" className="auth-triangle-shape" />
            <path d="m52 42 25 25-33 13Z" className="auth-triangle-core" />
          </svg>
        </div>
        <div className="auth-geo-position auth-ball-position">
          <div className="auth-geo-motion auth-ball-motion"><div className="auth-ball-shape" /></div>
        </div>
        <div className="auth-geo-position auth-square-position">
          <div className="auth-geo-motion auth-square-rotor">
            <span className="auth-square-cyan" />
            <span className="auth-square-yellow" />
          </div>
        </div>
        <div className="auth-geo-position auth-zigzag-position">
          <svg className="auth-geo-motion auth-zigzag-pendulum" viewBox="0 0 220 80">
            <path d="m5 58 30-34 30 34 30-34 30 34 30-34 30 34 30-34" className="auth-zigzag-shape auth-zigzag-shape--ink" />
            <path d="m5 47 30-34 30 34 30-34 30 34 30-34 30 34 30-34" className="auth-zigzag-shape auth-zigzag-shape--coral" />
          </svg>
        </div>
      </div>
    </div>
  );
}
