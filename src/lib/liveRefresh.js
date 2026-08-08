export function createLiveRefreshController({
  refresh,
  intervalMs = 4000,
  windowTarget = window,
  documentTarget = document,
}) {
  let disposed = false;
  let inFlight = null;

  const refreshNow = () => {
    if (disposed) return Promise.resolve();
    if (inFlight) return inFlight;

    try {
      inFlight = Promise.resolve(refresh());
    } catch (error) {
      inFlight = Promise.reject(error);
    }

    inFlight = inFlight.finally(() => {
      inFlight = null;
    });
    return inFlight;
  };

  const refreshWhenVisible = () => {
    if (documentTarget.visibilityState === "hidden") return;
    void refreshNow();
  };

  const handleVisibilityChange = () => {
    if (documentTarget.visibilityState === "visible") refreshWhenVisible();
  };

  const intervalId = windowTarget.setInterval(refreshWhenVisible, intervalMs);
  windowTarget.addEventListener("focus", refreshWhenVisible);
  documentTarget.addEventListener("visibilitychange", handleVisibilityChange);

  return {
    refreshNow,
    dispose() {
      if (disposed) return;
      disposed = true;
      windowTarget.clearInterval(intervalId);
      windowTarget.removeEventListener("focus", refreshWhenVisible);
      documentTarget.removeEventListener("visibilitychange", handleVisibilityChange);
    },
  };
}
