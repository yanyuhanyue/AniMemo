const bridge = typeof window === "undefined" ? null : window.AstrBotPluginPage;

const STATUS_LABELS = Object.freeze({
  RUNNING: "运行中",
  STOPPED: "已停止",
  OK: "正常",
  FAIL: "失败",
  NONE: "无",
  "NOT RUN": "未运行",
});

const ERROR_LABELS = Object.freeze({
  BridgeAuthError: "认证错误（BridgeAuthError）",
  BridgeConnectionError: "连接错误（BridgeConnectionError）",
  BridgeEventError: "事件错误（BridgeEventError）",
  BridgeProtocolError: "协议错误（BridgeProtocolError）",
  BridgeRateLimitError: "限流错误（BridgeRateLimitError）",
});

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

function rawText(value, fallback = "无") {
  if (value == null) return fallback;
  const normalized = String(value).trim();
  return normalized || fallback;
}

export function translateStatus(value) {
  const normalized = rawText(value);
  return STATUS_LABELS[normalized] || normalized;
}

export function translateError(value) {
  const normalized = rawText(value);
  if (normalized === "无" || normalized === "NONE") return "无";
  return ERROR_LABELS[normalized] || normalized;
}

function formatUtcOffset(offsetMinutes) {
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const absolute = Math.abs(offsetMinutes);
  const hours = String(Math.floor(absolute / 60)).padStart(2, "0");
  const minutes = String(absolute % 60).padStart(2, "0");
  return `UTC${sign}${hours}:${minutes}`;
}

export function formatLocalDateTime(value) {
  if (value == null || String(value).trim() === "") return translateStatus("NOT RUN");
  const raw = String(value);
  try {
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return raw;
    const parts = Object.fromEntries(
      new Intl.DateTimeFormat("zh-CN-u-ca-gregory-nu-latn", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23",
      })
        .formatToParts(parsed)
        .filter((part) => part.type !== "literal")
        .map((part) => [part.type, part.value]),
    );
    const required = ["year", "month", "day", "hour", "minute", "second"];
    if (required.some((part) => !parts[part])) return raw;
    const localTime = `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
    return `${localTime} (${formatUtcOffset(-parsed.getTimezoneOffset())})`;
  } catch (_error) {
    return raw;
  }
}

function renderStatus(status) {
  const routeCount = Number.isFinite(Number(status.route_count)) ? Number(status.route_count) : 0;
  const rows = [
    ["AniMemo Bridge", status.enabled ? "已启用" : "未启用"],
    ["服务地址", rawText(status.server, "未配置")],
    ["Key ID", rawText(status.key_id, "未配置")],
    ["凭证", status.configured ? "已配置" : "未配置"],
    ["事件轮询器", translateStatus(status.poller)],
    ["当前路由", routeCount > 0 ? "已绑定本地投递路由" : "暂无私聊路由"],
    ["HMAC 连通性", translateStatus(status.last_ping)],
    ["最近成功轮询", formatLocalDateTime(status.last_successful_poll)],
    ["最近错误", translateError(status.last_error)],
    ["路由数", rawText(status.route_count, "0")],
    ["已投递事件缓存", rawText(status.delivered_event_count, "0")],
    ["游标", rawText(status.cursor, "0")],
  ];
  if (rawText(status.configuration_error, "")) {
    rows.push(["配置错误", rawText(status.configuration_error)]);
  }

  const summary = document.querySelector("#summary");
  summary.replaceChildren();
  const dl = document.createElement("dl");
  for (const [label, value] of rows) {
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
    label.append(text(`平台：${rawText(route.platform, "未知")} · 已脱敏标识：${rawText(route.external_user_id)}`));
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = "清除路由";
    button.addEventListener("click", () => clearRoute(route.platform, route.external_user_id));
    row.append(label, button);
    routes.append(row);
  }
}

async function refresh({ announce = true } = {}) {
  const message = document.querySelector("#message");
  if (announce) message.textContent = "正在读取状态…";
  try {
    renderStatus(await apiGet("status"));
    if (announce) message.textContent = "状态已更新。";
    return true;
  } catch (_error) {
    if (announce) message.textContent = "状态暂时不可用。";
    return false;
  }
}

async function clearRoute(platform, externalUserHash) {
  if (!window.confirm("确认清除这个已脱敏的私聊路由？")) return;
  const message = document.querySelector("#message");
  try {
    await apiPost("routes/clear", { platform, external_user_hash: externalUserHash });
    const refreshed = await refresh({ announce: false });
    message.textContent = refreshed ? "路由已清除。" : "路由已清除，但状态刷新失败。";
  } catch (_error) {
    message.textContent = "路由清除失败。";
  }
}

async function ping() {
  const message = document.querySelector("#message");
  message.textContent = "正在测试连接…";
  try {
    const result = await apiPost("ping");
    const refreshed = await refresh({ announce: false });
    const resultMessage = `AniMemo HMAC 连通性：${translateStatus(result.status)}`;
    message.textContent = refreshed ? resultMessage : `${resultMessage}；状态刷新失败。`;
  } catch (_error) {
    message.textContent = "连接测试失败。";
  }
}

async function restart() {
  const message = document.querySelector("#message");
  message.textContent = "正在重启轮询器…";
  try {
    const result = await apiPost("restart");
    const refreshed = await refresh({ announce: false });
    const resultMessage = `事件轮询器：${translateStatus(result.status)}`;
    message.textContent = refreshed ? resultMessage : `${resultMessage}；状态刷新失败。`;
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

if (typeof document !== "undefined") {
  boot().catch(() => {
    document.querySelector("#message").textContent = "AstrBot Plugin Page API 不可用。";
  });
}
