import { lazy } from "react";

const WatchHistoryImporterPage = lazy(() => import("./WatchHistoryImporterPage.jsx"));

export default function createPlugin(host) {
  if (!host?.api) throw new Error("watch-history-importer requires host.api");
  const pluginApi = host.api.plugin("watch-history-importer");
  return Object.freeze({
    id: "com.anime-journal.watch-history-importer",
    version: "0.2.0",
    routes: [{
      path: "/plugins/watch-history-importer",
      Component: (props) => <WatchHistoryImporterPage {...props} host={host} api={pluginApi} />,
      area: "admin",
      access: "staff",
      permission: "watch-history-importer.run",
    }],
    navigation: [{
      id: "watch-history-importer.home",
      area: "admin",
      label: "忆往昔导入",
      path: "/plugins/watch-history-importer",
      icon: "history",
      order: 120,
    }],
    dispose() {},
  });
}
