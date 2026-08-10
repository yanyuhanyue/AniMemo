const bridge = window.AstrBotPluginPage;

async function apiGet(endpoint) {
  if (!bridge || typeof bridge.apiGet !== "function") throw new Error("AstrBot Plugin Page API 不可用");
  return bridge.apiGet(endpoint);
}

async function apiPost(endpoint, body = {}) {
  if (!bridge || typeof bridge.apiPost !== "function") throw new Error("AstrBot Plugin Page API 不可用");
  return bridge.apiPost(endpoint, body);
}

function text(value) {
  return document.createTextNode(value == null ? "" : String(value));
}

const DISPLAY_TIME_ZONE = "Asia/Shanghai";

function formatStatusTimestamp(value) {
  if (!value) return "未运行";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return String(value);
  return `${new Intl.DateTimeFormat("zh-CN", {
    timeZone: DISPLAY_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed)} (UTC+08:00)`;
}

function renderStatus(status) {
  const summary = document.querySelector("#summary");
  summary.replaceChildren();
  const rows = {
    服务: status.server,
    凭证: status.configured ? "已配置" : "未配置",
    Key: status.key_id,
    轮询器: status.poller,
    HMAC: status.last_ping,
    游标: status.cursor,
    已投递缓存: status.delivered_event_count,
    路由数: status.route_count,
    最近成功轮询: formatStatusTimestamp(status.last_successful_poll),
    最近错误: status.last_error || "无",
  };
  const dl = document.createElement("dl");
  for (const [label, value] of Object.entries(rows)) {
    const dt = document.createElement("dt");
    dt.append(text(label));
    const dd = document.createElement("dd");
    dd.append(text(value));
    dl.append(dt, dd);
  }
  summary.append(dl);

  const routes = document.querySelector("#routes");
  routes.replaceChildren();
  if (!status.routes?.length) {
    routes.append(text("暂无私聊路由。"));
    return;
  }
  for (const route of status.routes) {
    const row = document.createElement("div");
    row.className = "route";
    const label = document.createElement("span");
    label.append(text(`${route.platform} ${route.external_user_id}`));
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "清除路由";
    button.addEventListener("click", () => clearRoute(route.platform, route.external_user_id));
    row.append(label, button);
    routes.append(row);
  }
}

async function refresh() {
  const message = document.querySelector("#message");
  message.textContent = "正在读取状态…";
  try {
    renderStatus(await apiGet("status"));
    message.textContent = "状态已更新。";
  } catch (_error) {
    message.textContent = "状态暂时不可用。";
  }
}

async function clearRoute(platform, externalUserHash) {
  if (!window.confirm("确认清除这个已脱敏的私聊路由？")) return;
  const message = document.querySelector("#message");
  try {
    await apiPost("routes/clear", { platform, external_user_hash: externalUserHash });
    message.textContent = "路由已清除。";
    await refresh();
  } catch (_error) {
    message.textContent = "路由清除失败。";
  }
}

async function ping() {
  const message = document.querySelector("#message");
  message.textContent = "正在测试连接…";
  try {
    const result = await apiPost("ping");
    message.textContent = `HMAC connectivity: ${result.status}`;
    await refresh();
  } catch (_error) {
    message.textContent = "连接测试失败。";
  }
}

async function restart() {
  const message = document.querySelector("#message");
  message.textContent = "正在重启轮询器…";
  try {
    const result = await apiPost("restart");
    message.textContent = `Event poller: ${result.status}`;
    await refresh();
  } catch (_error) {
    message.textContent = "轮询器重启失败。";
  }
}

async function boot() {
  if (!bridge || typeof bridge.ready !== "function") throw new Error("AstrBot Plugin Page API 不可用");
  await bridge.ready();
  document.querySelector("#refresh").addEventListener("click", refresh);
  document.querySelector("#ping").addEventListener("click", ping);
  document.querySelector("#restart").addEventListener("click", restart);
  await refresh();
}

boot().catch(() => {
  document.querySelector("#message").textContent = "AstrBot Plugin Page API 不可用。";
});
