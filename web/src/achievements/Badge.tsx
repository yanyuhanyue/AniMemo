import { useId, useState } from "react";
import { badgeGrade, type BadgeKind } from "./presentation";
import "./badges.css";

/** Decorative art; the containing card or option supplies its name and state. */
export function AchievementBadge({
  badge,
  tier,
  imageID = "",
  state = "earned",
  size = "medium",
}: {
  badge: BadgeKind;
  tier: number;
  imageID?: string;
  state?: "earned" | "locked" | "revoked";
  size?: "small" | "medium" | "large";
}) {
  const id = useId();
  const [failedImage, setFailedImage] = useState("");
  const grade = badgeGrade(tier);
  return (
    <span
      className={`achievement-badge badge-size-${size}`}
      data-badge={badge}
      data-state={state}
      data-grade={grade.id}
      aria-hidden="true"
    >
      <svg viewBox="0 0 128 136" focusable="false">
        <defs>
          <clipPath id={`${id}-image`}>
            <circle cx="64" cy="56" r="33" />
          </clipPath>
          <linearGradient id={`${id}-rim`} x1="0" y1="0" x2="1" y2="1">
            <stop stopColor="var(--badge-metal-light)" />
            <stop offset=".45" stopColor="var(--badge-metal)" />
            <stop offset=".65" stopColor="var(--badge-metal-light)" />
            <stop offset="1" stopColor="var(--badge-metal)" />
          </linearGradient>
          <linearGradient id={`${id}-face`} x1="0" y1="0" x2=".8" y2="1">
            <stop stopColor="var(--badge-paper)" />
            <stop offset="1" stopColor="var(--badge-tint)" />
          </linearGradient>
          <linearGradient id={`${id}-enamel`} x1="0" y1="0" x2=".7" y2="1">
            <stop stopColor="var(--badge-light)" />
            <stop offset="1" stopColor="var(--badge-color)" />
          </linearGradient>
        </defs>
        <ellipse
          cx="64"
          cy="128"
          rx="34"
          ry="4"
          fill="var(--badge-ink)"
          opacity=".08"
        />
        <path
          d="m43 87-7 37 16-8 12 10 8-33M65 93l9 33 12-10 15 8-13-39"
          fill="var(--badge-color)"
          stroke="var(--badge-ink)"
          strokeWidth="1.4"
        />
        <path
          d="m48 97-4 19 9-4m26-15 5 15 8 4"
          fill="none"
          stroke="var(--badge-light)"
          strokeWidth="2"
        />
        {tier >= 3 && (
          <g
            className="badge-laurels"
            fill={`url(#${id}-rim)`}
            stroke="var(--badge-metal-edge)"
            strokeWidth="1"
          >
            <path d="M20 91C6 71 4 49 15 29M12 42C2 39 1 30 3 26c9 1 12 7 9 16Zm-3 17C0 53 0 46 2 42c8 4 10 10 7 17Zm4 17C2 72 0 63 2 58c9 3 13 9 11 18Zm7 15C8 89 3 81 5 77c10 1 15 6 15 14Z" />
            <path d="M108 91c14-20 16-42 5-62m3 13c10-3 11-12 9-16-9 1-12 7-9 16Zm3 17c9-6 9-13 7-17-8 4-10 10-7 17Zm-4 17c11-4 13-13 11-18-9 3-13 9-11 18Zm-7 15c12-2 17-10 15-14-10 1-15 6-15 14Z" />
          </g>
        )}
        <path
          d="M64 7 92 15 112 37 117 66 105 93 80 109H48L23 93 11 66 16 37 36 15Z"
          fill={`url(#${id}-rim)`}
          stroke="var(--badge-metal-edge)"
          strokeWidth="1.4"
        />
        {tier >= 4 && (
          <g
            className="badge-crown"
            fill={`url(#${id}-rim)`}
            stroke="var(--badge-metal-edge)"
            strokeWidth="1"
          >
            <path d="m44 18-3-13 13 7L64 1l10 11 13-7-3 13-20 5Z" />
            <path d="m64 4 5 8-5 5-5-5Z" fill="var(--badge-jewel)" />
          </g>
        )}
        <circle
          cx="64"
          cy="58"
          r="44"
          fill={`url(#${id}-face)`}
          stroke="var(--badge-metal-edge)"
          strokeWidth="1.2"
        />
        <circle
          cx="64"
          cy="58"
          r="39"
          fill="none"
          stroke="var(--badge-metal)"
          strokeWidth=".9"
          strokeDasharray={tier > 1 ? undefined : "1 5"}
        />
        {tier > 1 && (
          <path
            d="M30 52a35 35 0 0 1 68 0M31 70a35 35 0 0 0 66 0"
            fill="none"
            stroke="var(--badge-metal)"
            strokeWidth="1.3"
            strokeDasharray="2 4"
          />
        )}
        <g
          stroke="var(--badge-ink)"
          strokeWidth="1.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          {imageID && imageID !== failedImage ? (
            <image
              href={`/api/v1/memory/achievement-images/${imageID}`}
              x="31"
              y="23"
              width="66"
              height="66"
              preserveAspectRatio="xMidYMid meet"
              clipPath={`url(#${id}-image)`}
              onError={() => setFailedImage(imageID)}
            />
          ) : (
            <BadgeMotif badge={badge} enamel={`url(#${id}-enamel)`} />
          )}
        </g>
        {tier >= 5 && (
          <g
            className="badge-gems"
            fill="var(--badge-jewel)"
            stroke="var(--badge-metal-edge)"
            strokeWidth="1"
          >
            <path d="m17 47 6 10-6 10-6-10Zm94 0 6 10-6 10-6-10Z" />
            <path d="m39 9 2-5 2 5 5 2-5 2-2 5-2-5-5-2Zm47 9 2-5 2 5 5 2-5 2-2 5-2-5-5-2Z" />
          </g>
        )}
        <path
          d="m24 30 2-5 2 5 5 2-5 2-2 5-2-5-5-2Zm76 52 1.5-4 1.5 4 4 1.5-4 1.5-1.5 4-1.5-4-4-1.5Z"
          fill="var(--badge-paper)"
        />
        <path
          d="M24 89c8 7 20 10 40 10s32-3 40-10v15c-10 7-23 10-40 10s-30-3-40-10Z"
          fill="var(--badge-ink)"
          stroke="var(--badge-metal-light)"
          strokeWidth="1.5"
        />
        <path
          d="m37 100 3-2 3 2-3 2Zm48 0 3-2 3 2-3 2Z"
          fill="var(--badge-metal-light)"
        />
        <text
          x="64"
          y="109"
          textAnchor="middle"
          fill="var(--badge-paper)"
          fontSize="13"
          fontWeight="700"
          fontFamily="Georgia,serif"
          letterSpacing="2"
        >
          {String(tier).padStart(2, "0")}
        </text>
      </svg>
    </span>
  );
}

function BadgeMotif({ badge, enamel }: { badge: BadgeKind; enamel: string }) {
  switch (badge) {
    case "ticket":
      return (
        <>
          <path
            d="M32 36h64v13a9 9 0 0 0 0 18v13H32V67a9 9 0 0 0 0-18Z"
            fill={enamel}
          />
          <path d="M78 38v40" strokeDasharray="3 4" fill="none" />
          <path
            d="m55 43 4 9 10 1-8 7 2 10-8-5-9 5 2-10-7-7 10-1Z"
            fill="var(--badge-paper)"
          />
        </>
      );
    case "shelf":
      return (
        <>
          <path d="M33 31h62v53H33Z" fill={enamel} />
          <path d="M38 35h52v23H38Zm0 28h52v16H38Z" fill="var(--badge-paper)" />
          <path
            d="M44 39h9v19h-9Zm14 2h7v17h-7Zm13 1 7-3 6 16-7 3Z"
            fill="var(--badge-color)"
          />
          <path d="M48 68h31v6H48Z" fill="var(--badge-light)" />
          <path d="M39 84v5m50-5v5" />
        </>
      );
    case "album":
      return (
        <>
          <path d="M38 28h53v59H38Z" fill={enamel} />
          <path d="M43 28v59m-9-47h11m-11 15h11m-11 15h11" fill="none" />
          <path d="m52 39 27-3 4 30-27 3Z" fill="var(--badge-paper)" />
          <path d="m56 58 7-9 6 5 6-9 5 16-24 3Z" fill="var(--badge-color)" />
          <circle cx="60" cy="44" r="2" fill="var(--badge-metal)" />
          <path d="M57 78h21" fill="none" />
        </>
      );
    case "spark":
      return (
        <>
          <path d="m38 77 7-16 20-21 13 13-22 20Z" fill="var(--badge-light)" />
          <path d="m39 84 9-9m-17 1 7-9m9 21 9-9" fill="none" />
          <path
            d="m65 27 8 18 19 3-14 14 3 20-17-9-18 9 4-20-14-14 20-3Z"
            fill={enamel}
          />
          <path
            d="m65 35 5 14 14 2-11 10 2 13-11-6-12 6 3-14-10-9 14-2Z"
            fill="var(--badge-paper)"
            opacity=".6"
            stroke="none"
          />
          <path
            d="m87 29 2-6 2 6 6 2-6 2-2 6-2-6-6-2Z"
            fill="var(--badge-metal-light)"
          />
        </>
      );
    case "moon":
      return (
        <>
          <path d="M72 28a29 29 0 1 0 18 47A27 27 0 0 1 72 28Z" fill={enamel} />
          <path
            d="M49 39a23 23 0 0 0 9 42"
            fill="none"
            stroke="var(--badge-paper)"
            strokeWidth="3"
          />
          <circle
            cx="48"
            cy="62"
            r="3"
            fill="var(--badge-tint)"
            stroke="none"
          />
          <circle
            cx="58"
            cy="75"
            r="2"
            fill="var(--badge-tint)"
            stroke="none"
          />
          <path
            d="m83 38 3 7 8 1-6 6 1 8-7-4-7 4 2-8-6-6 8-1Z"
            fill="var(--badge-metal-light)"
          />
          <path
            d="m57 21 1.5 4 4 1.5-4 1.5-1.5 4-1.5-4-4-1.5 4-1.5Z"
            fill="var(--badge-color)"
          />
        </>
      );
    case "orbit":
      return (
        <>
          <ellipse
            cx="64"
            cy="60"
            rx="38"
            ry="12"
            transform="rotate(-27 64 60)"
            fill="none"
            stroke="var(--badge-color)"
            strokeWidth="5"
          />
          <circle cx="64" cy="57" r="23" fill={enamel} />
          <path
            d="M50 41c11-3 19 5 29 5M44 55c12-5 26 10 40 6M52 73c10-3 14 3 21 1"
            fill="none"
            stroke="var(--badge-paper)"
            strokeWidth="3"
            opacity=".75"
          />
          <path
            d="M32 65c-13 25 54 3 65-20"
            fill="none"
            stroke="var(--badge-ink)"
            strokeWidth="6"
          />
          <path
            d="M32 65c-13 25 54 3 65-20"
            fill="none"
            stroke="var(--badge-metal-light)"
            strokeWidth="3.5"
          />
          <circle cx="90" cy="27" r="4" fill="var(--badge-color)" />
          <path
            d="m38 32 2-5 2 5 5 2-5 2-2 5-2-5-5-2Z"
            fill="var(--badge-metal-light)"
          />
        </>
      );
    case "flower":
      return (
        <>
          <path
            d="M65 78c-15 1-22-3-29-13 16-3 25 0 29 13Zm1 1c14 1 24-7 28-18-16-1-25 6-28 18Z"
            fill="var(--badge-color)"
          />
          {[0, 72, 144, 216, 288].map((angle) => (
            <path
              key={angle}
              d="M64 58C39 47 47 24 59 27l5 6 5-6c12-3 20 20-5 31Z"
              transform={`rotate(${angle} 64 58)`}
              fill={enamel}
            />
          ))}
          <circle cx="64" cy="58" r="8" fill="var(--badge-metal-light)" />
          <path
            d="M64 49v4m0 10v4m-9-9h4m10 0h4"
            stroke="var(--badge-metal-edge)"
          />
          <circle cx="64" cy="58" r="2" fill="var(--badge-ink)" stroke="none" />
        </>
      );
    case "book":
      return (
        <>
          <path
            d="M32 39c13-4 23-2 32 5 9-7 19-9 32-5v43c-13-4-23-2-32 4-9-6-19-8-32-4Z"
            fill={enamel}
          />
          <path
            d="M36 34c13-2 21 2 28 9 7-7 15-11 28-9v42c-13-2-21 1-28 7-7-6-15-9-28-7Z"
            fill="var(--badge-paper)"
          />
          <path
            d="M64 43v40M43 48l12 4m-12 5 12 4m-12 5 12 4m18-21 11-5m-11 14 11-5m-11 14 11-5"
            fill="none"
          />
          <path d="M75 34v25l5-5 5 1V30Z" fill="var(--badge-color)" />
          <path
            d="m51 24 2-5 2 5 5 2-5 2-2 5-2-5-5-2Z"
            fill="var(--badge-metal-light)"
          />
        </>
      );
  }
}
