import { useState, type CSSProperties } from "react";
import { Button } from "../components/ui/Button";
import type { Entry } from "../api/client";

// Sample a tiny same-origin thumbnail; the color only decorates the poster frame.
// Text, progress, focus and destructive controls keep their semantic colors.
function posterTint(image: HTMLImageElement): string | undefined {
  try {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 8;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return;
    context.drawImage(image, 0, 0, 8, 8);
    const pixels = context.getImageData(0, 0, 8, 8).data;
    let red = 0,
      green = 0,
      blue = 0,
      weight = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      const alpha = pixels[i + 3]! / 255;
      red += pixels[i]! * alpha;
      green += pixels[i + 1]! * alpha;
      blue += pixels[i + 2]! * alpha;
      weight += alpha;
    }
    if (weight)
      return `rgb(${Math.round(red / weight)} ${Math.round(green / weight)} ${Math.round(blue / weight)} / 22%)`;
  } catch {
    /* Missing/cross-origin pixels use the ordinary poster frame. */
  }
}

export function CoverImage({
  entry,
  card = false,
}: {
  entry: Entry;
  card?: boolean;
}) {
  const [failed, setFailed] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [tint, setTint] = useState<{ source: string; color: string }>();
  if (!entry.cover_revision) return null;
  const src = `/api/v1/entries/${encodeURIComponent(entry.id)}/cover/${encodeURIComponent(entry.cover_revision)}?retry=${attempt}`;
  if (failed === src)
    return card ? null : (
      <div className="cover-error">
        <p className="muted" role="status">
          封面暂时无法显示。
        </p>
        <Button
          className="button secondary"
          onClick={() => {
            setFailed("");
            setAttempt((n) => n + 1);
          }}
        >
          重试封面
        </Button>
      </div>
    );
  const style =
    tint?.source === src
      ? ({ "--cover-glow": tint.color } as CSSProperties)
      : undefined;
  return (
    <span className={card ? "cover-photo" : "detail-cover-photo"} style={style}>
      <img
        src={src}
        alt={card ? "" : `${entry.title}的封面`}
        loading={card ? "lazy" : "eager"}
        onLoad={(e) => {
          const color = posterTint(e.currentTarget);
          if (color) setTint({ source: src, color });
        }}
        onError={() => setFailed(src)}
      />
    </span>
  );
}
