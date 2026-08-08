import { forwardRef, useEffect, useImperativeHandle, useRef } from "react";

const SCRIPT_ID = "cloudflare-turnstile-api";
const DEVELOPMENT_SITE_KEY = "1x00000000000000000000AA";
const SITE_KEY = import.meta.env.VITE_TURNSTILE_SITE_KEY?.trim()
  || (import.meta.env.DEV ? DEVELOPMENT_SITE_KEY : "");
let turnstileScriptPromise = null;

function loadTurnstileScript() {
  if (typeof window === "undefined") return Promise.reject(new Error("Turnstile requires a browser."));
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (turnstileScriptPromise) return turnstileScriptPromise;

  turnstileScriptPromise = new Promise((resolve, reject) => {
    const existing = document.getElementById(SCRIPT_ID);
    const script = existing || document.createElement("script");
    const resolveWhenReady = () => {
      if (window.turnstile) resolve(window.turnstile);
      else reject(new Error("Turnstile loaded without an API surface."));
    };

    if (existing) {
      if (existing.dataset.loaded === "true") {
        resolveWhenReady();
        return;
      }
      existing.addEventListener("load", resolveWhenReady, { once: true });
      existing.addEventListener("error", reject, { once: true });
      return;
    }

    script.id = SCRIPT_ID;
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.onload = () => {
      script.dataset.loaded = "true";
      resolveWhenReady();
    };
    script.onerror = reject;
    document.head.appendChild(script);
  }).catch((error) => {
    turnstileScriptPromise = null;
    throw error;
  });

  return turnstileScriptPromise;
}

export const TurnstileWidget = forwardRef(function TurnstileWidget({
  onTokenChange,
  variant = "user",
  size = variant === "staff" ? "flexible" : "normal",
  mountDelay = 300,
}, ref) {
  const containerRef = useRef(null);
  const widgetIdRef = useRef(null);
  const tokenRef = useRef("");
  const onTokenChangeRef = useRef(onTokenChange);
  const theme = variant === "staff" ? "dark" : "light";

  useEffect(() => {
    onTokenChangeRef.current = onTokenChange;
  }, [onTokenChange]);

  useImperativeHandle(ref, () => ({
    getToken: () => tokenRef.current,
    reset: () => {
      tokenRef.current = "";
      onTokenChangeRef.current?.("");
      if (window.turnstile && widgetIdRef.current !== null) {
        window.turnstile.reset(widgetIdRef.current);
      }
    },
  }), []);

  useEffect(() => {
    let cancelled = false;
    let mountTimer = null;
    if (!SITE_KEY) {
      tokenRef.current = "";
      onTokenChangeRef.current?.("");
      return undefined;
    }

    mountTimer = window.setTimeout(() => {
      loadTurnstileScript()
        .then((turnstile) => {
          if (cancelled || !containerRef.current || !turnstile) return;
          widgetIdRef.current = turnstile.render(containerRef.current, {
            sitekey: SITE_KEY,
            action: "turnstile-spin-v2",
            theme,
            size,
            callback: (token) => {
              tokenRef.current = token || "";
              onTokenChangeRef.current?.(tokenRef.current);
            },
            "expired-callback": () => {
              tokenRef.current = "";
              onTokenChangeRef.current?.("");
            },
            "error-callback": () => {
              tokenRef.current = "";
              onTokenChangeRef.current?.("");
            },
          });
        })
        .catch(() => {
          tokenRef.current = "";
          onTokenChangeRef.current?.("");
        });
    }, Math.max(0, mountDelay));

    return () => {
      cancelled = true;
      window.clearTimeout(mountTimer);
      if (window.turnstile && widgetIdRef.current !== null) {
        window.turnstile.remove?.(widgetIdRef.current);
      }
      widgetIdRef.current = null;
      tokenRef.current = "";
    };
  }, [mountDelay, size, theme]);

  if (!SITE_KEY) {
    return <div className="turnstile-widget" role="alert">安全验证配置错误</div>;
  }

  return (
    <div className={`turnstile-widget turnstile-widget--${variant}`} aria-label="安全验证" data-variant={variant}>
      <div
        ref={containerRef}
        className="cf-turnstile turnstile-widget__slot"
        data-sitekey={SITE_KEY}
        data-action="turnstile-spin-v2"
        data-theme={theme}
        data-size={size}
      />
    </div>
  );
});
