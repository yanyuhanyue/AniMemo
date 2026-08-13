const runtime = globalThis.__ANIMEMO_REACT_RUNTIME__?.ReactDOM;
if (!runtime) throw new Error("AniMemo ReactDOM runtime is not ready");
export * from "./react-dom-client.js";
export default runtime;
