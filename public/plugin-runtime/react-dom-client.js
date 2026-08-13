const runtime = globalThis.__ANIME_JOURNAL_REACT_RUNTIME__?.ReactDOMClient;
if (!runtime) throw new Error("AniMemo ReactDOM client runtime is not ready");
export const { createRoot, hydrateRoot, version } = runtime;
export default runtime;
