import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const apiSource = readFileSync(new URL("../src/lib/api.js", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../src/App.jsx", import.meta.url), "utf8");
const pluginRuntimeSource = readFileSync(new URL("../src/plugins/sdk/PluginRuntimeContext.jsx", import.meta.url), "utf8");
const adminPanelsSource = readFileSync(new URL("../src/components/admin/AdminSystemPanel.jsx", import.meta.url), "utf8");
const adminLoginSource = readFileSync(new URL("../src/pages/AdminLoginPage.jsx", import.meta.url), "utf8");
const adminDashboardSource = readFileSync(new URL("../src/pages/AdminDashboardPage.jsx", import.meta.url), "utf8");
const dashboardSource = [
  "../src/pages/DashboardPage.jsx",
  "../src/pages/DashboardDialogs.jsx",
  "../src/pages/useDashboardImport.js",
].map((path) => readFileSync(new URL(path, import.meta.url), "utf8")).join("\n");
const nginxSource = readFileSync(new URL("../deploy/nginx.conf", import.meta.url), "utf8");
const openrestySource = readFileSync(new URL("../deploy/openresty-re-anime.conf", import.meta.url), "utf8");
const removedRouteAuthField = ["route", "requires", "Auth"].join("\\.");
const removedRouteStaffField = ["route", "requires", "Admin"].join("\\.");

test("keeps JWTs out of browser storage and restores access through the refresh cookie", () => {
  assert.doesNotMatch(apiSource, /localStorage\.setItem\([^\n]*(?:access|refresh)/i);
  assert.doesNotMatch(apiSource, /sessionStorage\.setItem\([^\n]*(?:access|refresh)/i);
  assert.match(apiSource, /let accessToken = null/);
  assert.match(apiSource, /localStorage\.removeItem\(LEGACY_ACCESS_KEY\)/);
  assert.match(apiSource, /cookiePost\("token\/refresh\/"\)/);
  assert.match(apiSource, /"X-CSRFToken"/);
  assert.match(apiSource, /withCredentials: true/);
});

test("keeps the browser and cookie API on the same origin", () => {
  assert.match(apiSource, /window\.location\.origin}\/api/);
  assert.doesNotMatch(apiSource, /window\.location\.hostname}:8000\/api/);
});

test("preserves staff claims when profile data is merged after cookie refresh", () => {
  assert.match(apiSource, /authUser = \{ \.\.\.\(authUser \|\| \{\}\), \.\.\.\(data \|\| \{\}\) \}/);
  assert.doesNotMatch(apiSource, /authUser = data \|\| authUser/);
});

test("shares one refresh promise across concurrent 401 responses", () => {
  assert.match(apiSource, /let refreshPromise = null/);
  assert.match(apiSource, /if \(!refreshPromise\)/);
  assert.match(apiSource, /const access = await refreshAccessToken\(\)/);
  assert.match(apiSource, /refreshPromise = null/);
  assert.match(apiSource, /request\._retry/);
});

test("loads only backend-enabled plugin frontends without eager execution", () => {
  assert.doesNotMatch(appSource, /import\.meta\.glob\(/);
  assert.doesNotMatch(appSource, /eager\s*:\s*true/);
  assert.match(pluginRuntimeSource, /api\.get\("plugins\/enabled\/"\)/);
  assert.match(pluginRuntimeSource, /frontendEntry/);
  assert.match(pluginRuntimeSource, /frontend entry failed to load/);
  assert.match(pluginRuntimeSource, /loading/);
  assert.match(pluginRuntimeSource, /import\(\/\* @vite-ignore \*\/ url\.href\)/);
  assert.match(pluginRuntimeSource, /styleNode\.href = new URL\(metadata\.styleEntry/);
  assert.doesNotMatch(pluginRuntimeSource, /getAccessToken|createObjectURL|blobUrl|styleUrl/);
  assert.doesNotMatch(appSource, new RegExp(removedRouteAuthField));
  assert.doesNotMatch(appSource, new RegExp(removedRouteStaffField));
  assert.match(appSource, /access === "staff"/);
  assert.match(appSource, /pluginsLoading && location\.pathname\.startsWith\("\/plugins\/"\)/);
  assert.match(appSource, /access === "staff" \? "\/admin-login"/);
});

test("renders QR setup, manual-secret fallback, numeric OTP filtering and one-time recovery codes", () => {
  assert.match(adminPanelsSource, /QRCodeSVG/);
  assert.match(adminPanelsSource, /无法扫码？手动输入密钥/);
  assert.match(adminPanelsSource, /replace\(\/\\D\/g, ""\)\.slice\(0, 6\)/);
  assert.match(adminPanelsSource, /autoComplete="one-time-code"/);
  assert.match(adminPanelsSource, /恢复码只显示一次，请保存在安全的位置/);
  assert.doesNotMatch(adminPanelsSource, /<small>\{setup\.otpauth_uri\}<\/small>/);
});

test("turns HTTP 429 into a retryable user-facing message", () => {
  assert.match(apiSource, /status === 429/);
  assert.match(apiSource, /操作过于频繁/);
  assert.match(apiSource, /retry-after/);
});

test("rotates staff CSRF and sends the current access token before logout cleanup", () => {
  assert.match(apiSource, /cookiePost\("auth\/staff-login\/"/);
  assert.match(apiSource, /ensureCsrfToken\(\{ force: true \}\)/);
  assert.match(apiSource, /includeAccess: true/);
  assert.match(apiSource, /headers\.Authorization = `Bearer \$\{accessToken\}`/);
  assert.match(apiSource, /finally \{\s*clearTokens\(\);\s*clearCsrfToken\(\)/s);
  assert.match(adminDashboardSource, /await authApi\.logout\(\)/);
  assert.match(dashboardSource, /await authApi\.logout\(\)/);
});

test("keeps the staff dashboard refresh interval bounded without four-second polling", () => {
  assert.match(adminDashboardSource, /intervalMs: 20000/);
  assert.doesNotMatch(adminDashboardSource, /intervalMs: 4000/);
  assert.match(adminDashboardSource, /审核队列每 20 秒自动同步/);
  assert.doesNotMatch(adminDashboardSource, /审核队列每 4 秒自动同步/);
});

test("uses the shared CSRF cookie flow for ordinary login and handles unavailable security services", () => {
  assert.match(apiSource, /cookiePost\("token\/", \{ username, password, "cf-turnstile-response": turnstileToken \}\)/);
  assert.match(apiSource, /authApi = \{[\s\S]*clearCsrfToken\(\);[\s\S]*ensureCsrfToken\(\{ force: true \}\)/);
  assert.match(apiSource, /status === 503/);
  assert.match(apiSource, /安全服务暂时繁忙/);
});

test("offers TOTP or recovery-code staff login without persisting the recovery code", () => {
  assert.match(adminLoginSource, /身份验证器验证码/);
  assert.match(adminLoginSource, /一次性恢复码/);
  assert.match(adminLoginSource, /recoveryCode\.trim\(\)\.toUpperCase\(\)/);
  assert.match(adminLoginSource, /type="password"/);
  assert.doesNotMatch(adminLoginSource, /localStorage|sessionStorage/);
  assert.match(adminPanelsSource, /系统固定生成 6 枚/);
  assert.match(adminLoginSource, /new URLSearchParams\(location\.search\)\.get\("next"\)/);
  assert.match(adminLoginSource, /data\.admin_access/);
});

test("rejects oversized journal imports before uploading", () => {
  assert.match(dashboardSource, /file\.size > 2 \* 1024 \* 1024/);
  assert.match(dashboardSource, /文件不能超过 2 MB/);
});

test("requires a staff second factor before self-account deletion", () => {
  assert.match(apiSource, /deleteAccount: \(payload\).*data: payload/);
  assert.match(dashboardSource, /工作人员二次验证/);
  assert.match(dashboardSource, /verificationMode === "otp"/);
  assert.match(dashboardSource, /recovery_code:/);
  assert.match(dashboardSource, /twoFactorEnabled/);
});

test("keeps production CSP in Nginx and allows React style attributes only", () => {
  for (const source of [nginxSource, openrestySource]) {
    assert.match(source, /default-src 'self'/);
    assert.match(source, /script-src 'self' 'sha256-[^']+'/);
    assert.match(source, /style-src 'self';/);
    assert.doesNotMatch(source, /script-src[^;]*blob:|style-src[^;]*blob:/);
    assert.match(source, /style-src-attr 'unsafe-inline'/);
    assert.match(source, /img-src 'self' data: blob:/);
    assert.match(source, /font-src 'self' data:/);
    assert.match(source, /connect-src 'self'/);
    assert.match(source, /object-src 'none'/);
    assert.match(source, /base-uri 'self'/);
    assert.match(source, /frame-ancestors 'none'/);
    assert.match(source, /form-action 'self'/);
    assert.doesNotMatch(source, /unsafe-eval|script-src \*/);
  }
});

test("overwrites forwarding headers at the trusted proxy boundary", () => {
  assert.doesNotMatch(nginxSource, /proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for/);
  assert.doesNotMatch(nginxSource, /proxy_set_header X-Forwarded-For \$http_x_forwarded_for/);
  assert.match(nginxSource, /proxy_set_header X-Forwarded-For \$remote_addr/);
  assert.match(nginxSource, /set_real_ip_from 172\.28\.0\.0\/16/);
  assert.match(openrestySource, /proxy_set_header X-Forwarded-For \$remote_addr/);
  assert.match(openrestySource, /proxy_set_header X-Forwarded-Proto https/);
});
