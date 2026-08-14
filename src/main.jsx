import React from "react";
import * as ReactDOM from "react-dom";
import * as ReactDOMClient from "react-dom/client";
import * as ReactJsxRuntime from "react/jsx-runtime";
import * as ReactRouterDOM from "react-router-dom";
import { createRoot } from "react-dom/client";
import { App } from "./App.jsx";
import "./styles.css";
import "./admin.css";

const initialColorTransition = document.getElementById("initialColorTransition");

// Runtime plugins resolve these browser modules through the import map. The host
// publishes the exact instances used by the core bundle before any plugin load.
globalThis.__ANIMEMO_REACT_RUNTIME__ = Object.freeze({
  React,
  ReactDOM,
  ReactDOMClient,
  ReactJsxRuntime,
  ReactRouterDOM,
});

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

if (initialColorTransition) {
  const removeInitialTransition = () => initialColorTransition.remove();
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    removeInitialTransition();
  } else {
    const yellowSlash = initialColorTransition.querySelector(".initial-color-transition__slash--yellow");
    yellowSlash?.addEventListener("animationend", removeInitialTransition, { once: true });
    window.setTimeout(removeInitialTransition, 900);
  }
}
