import { useEffect, useState } from "react";

import "./styles.css";

export default function BlankPluginPage({ host, api }) {
  const [state, setState] = useState({ status: "loading", data: null });

  useEffect(() => {
    const controller = new AbortController();

    (api || host.api.plugin("blank-plugin"))
      .get("status/", { signal: controller.signal })
      .then(({ data }) => setState({ status: "ready", data }))
      .catch((error) => {
        if (error?.code !== "ERR_CANCELED") {
          setState({ status: "error", data: null });
        }
      });

    return () => controller.abort();
  }, [host]);

  return (
    <main className="ajp-blank-plugin">
      <section className="ajp-blank-plugin__panel" aria-live="polite">
        <p className="ajp-blank-plugin__eyebrow">PLUGIN READY CHECK</p>
        <h1>空白插件</h1>
        {state.status === "loading" && <p>正在检查插件连接...</p>}
        {state.status === "error" && <p>插件 API 暂时不可用。</p>}
        {state.status === "ready" && (
          <p>后端已连接，当前版本 {state.data.plugin.version}。</p>
        )}
      </section>
    </main>
  );
}
