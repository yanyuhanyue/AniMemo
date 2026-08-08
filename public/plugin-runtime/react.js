const runtime = globalThis.__ANIME_JOURNAL_REACT_RUNTIME__?.React;
if (!runtime) throw new Error("Anime Journal React runtime is not ready");
export default runtime;
export const {
  Children, Component, Fragment, Profiler, PureComponent, StrictMode, Suspense,
  cloneElement, createContext, createElement, forwardRef, isValidElement,
  lazy, memo, startTransition, useCallback, useContext, useDebugValue, useDeferredValue,
  useEffect, useId, useImperativeHandle, useInsertionEffect, useLayoutEffect, useMemo,
  useOptimistic, useReducer, useRef, useState, useSyncExternalStore, useTransition,
  version,
} = runtime;
