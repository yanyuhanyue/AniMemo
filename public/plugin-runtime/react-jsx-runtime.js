const runtime = globalThis.__ANIME_JOURNAL_REACT_RUNTIME__?.ReactJsxRuntime;
if (!runtime) throw new Error("Anime Journal JSX runtime is not ready");
export const { Fragment, jsx, jsxs, jsxDEV } = runtime;
