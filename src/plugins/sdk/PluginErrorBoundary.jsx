import { Component } from "react";

export class PluginErrorBoundary extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error(`[plugin:${this.props.pluginSlug || "unknown"}] route crashed`, error, info);
  }

  reset = () => {
    this.setState({ error: null });
    this.props.onReload?.();
  };

  render() {
    if (!this.state.error) return this.props.children;
    const development = import.meta.env?.DEV;
    return (
      <main className="plugin-runtime-error" role="alert">
        <section className="plugin-runtime-error__panel">
          <span className="plugin-runtime-error__kicker">PLUGIN RUNTIME</span>
          <h1>插件加载失败</h1>
          <p>这个插件页面遇到了一点问题，其他 Anime Journal 页面仍然可以正常使用。</p>
          {development && <pre>{String(this.state.error?.message || this.state.error)}</pre>}
          <div className="plugin-runtime-error__actions">
            <button type="button" onClick={this.reset}>重新加载插件</button>
            <button type="button" onClick={this.props.onHome}>返回首页</button>
          </div>
        </section>
      </main>
    );
  }
}
