const runtime = globalThis.__ANIME_JOURNAL_REACT_RUNTIME__?.ReactDOM;
if (!runtime) throw new Error("Anime Journal ReactDOM runtime is not ready");
export * from "./react-dom-client.js";
export default runtime;
