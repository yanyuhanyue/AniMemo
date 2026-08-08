import { lazy } from "react";

const BlankPluginPage = lazy(() => import("./BlankPluginPage.jsx"));

export default function createPlugin(host) {
  if (!host?.api) {
    throw new Error("blank-plugin requires host.api");
  }

  return Object.freeze({
    id: "com.example.anime-journal.blank",
    version: "0.2.0",
    routes: [
      {
        path: "/plugins/blank-plugin",
        Component: (props) => <BlankPluginPage {...props} host={host} api={host.api.plugin("blank-plugin")} />,
        area: "dashboard",
        access: "staff",
      },
    ],
    navigation: [
      {
        id: "blank-plugin.home",
        area: "dashboard",
        label: "空白插件",
        path: "/plugins/blank-plugin",
        icon: "puzzle",
        order: 100,
      },
    ],
    dispose() {},
  });
}
