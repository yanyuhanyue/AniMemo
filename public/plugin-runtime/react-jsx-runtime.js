const runtime = globalThis.__ANIMEMO_REACT_RUNTIME__?.ReactJsxRuntime;
if (!runtime) throw new Error("AniMemo JSX runtime is not ready");
export const { Fragment, jsx, jsxs, jsxDEV } = runtime;
