import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createEventBus, createPluginHost, validatePluginRoute, collectPluginNavigation } from "../src/plugins/sdk/host.js";

test("Plugin SDK host exposes namespaced api and readonly auth surface", async () => {
  const calls = [];
  const client = {
    get: (...args) => { calls.push(args); return Promise.resolve({ data: { ok: true } }); },
    post: (...args) => { calls.push(args); return Promise.resolve({ data: { ok: true } }); },
    put: (...args) => { calls.push(args); return Promise.resolve({ data: { ok: true } }); },
    patch: (...args) => { calls.push(args); return Promise.resolve({ data: { ok: true } }); },
    delete: (...args) => { calls.push(args); return Promise.resolve({ data: { ok: true } }); },
  };
  let user = { id: 1, is_staff: true, role: "administrator", capabilities: ["manage_system"] };
  let authListener = null;
  const host = createPluginHost({
    slug: "demo-plugin",
    client,
    authStore: { getUser: () => user, subscribe: (listener) => { authListener = listener; return () => { authListener = null; }; } },
    navigation: { navigate() {}, replace() {}, back() {} },
    ui: { notify() {}, confirm: async () => true },
    site: { getSettings: () => ({ site_name: "Anime Journal", resend_api_key: "hidden" }) },
    eventBus: createEventBus(),
    manifest: { extensions: ["frontend.page"], permissions: [] },
  });
  await host.api.plugin("demo-plugin").get("status/");
  assert.equal(calls[0][0], "plugins/demo-plugin/status/");
  assert.equal(host.auth.isStaff(), true);
  assert.equal(host.auth.getRole(), "administrator");
  assert.equal(typeof host.auth.setToken, "undefined");
  assert.equal(host.site.getSettings().resend_api_key, undefined);
  let authSnapshot = null;
  host.auth.subscribe((snapshot) => { authSnapshot = snapshot; });
  authListener({ access: "must-not-leak", user });
  assert.equal(authSnapshot.access, undefined);
  user = null;
  assert.equal(host.auth.isAuthenticated(), false);
});

test("Plugin events are listenable but host-only events cannot be forged", () => {
  const bus = createEventBus();
  const host = createPluginHost({ slug: "demo-plugin", client: {}, authStore: { getUser: () => null }, navigation: { navigate() {}, replace() {}, back() {} }, ui: { notify() {}, confirm() {} }, site: { getSettings: () => ({}) }, eventBus: bus });
  let received = 0;
  const unsubscribe = host.events.on("plugin:demo-plugin:finished", () => { received += 1; });
  host.events.emit("plugin:demo-plugin:finished", { count: 2 });
  assert.equal(received, 1);
  unsubscribe();
  host.events.emit("plugin:demo-plugin:finished", { count: 3 });
  assert.equal(received, 1);
  assert.throws(() => host.events.emit("auth.login", {}), /only emit their own namespaced events/);
});

test("plugin route and navigation validation enforce manifest extensions", () => {
  const Component = () => null;
  assert.equal(validatePluginRoute({ path: "/plugins/demo-plugin/home", Component, area: "admin" }, "demo-plugin", { extensions: ["frontend.page"] }).valid, true);
  assert.equal(validatePluginRoute({ path: "/settings", Component, area: "admin" }, "demo-plugin", { extensions: ["frontend.page"] }).valid, false);
  const plugins = [
    { slug: "demo-plugin", status: "loaded", manifest: { extensions: ["frontend.navigation"] }, plugin: { navigation: [{ id: "home", area: "admin", label: "Home", path: "/plugins/demo-plugin/home", order: 2 }] } },
    { slug: "other-plugin", status: "loaded", manifest: { extensions: ["frontend.navigation"] }, plugin: { navigation: [{ id: "home", area: "admin", label: "Duplicate", path: "/plugins/other-plugin/home", order: 1 }] } },
  ];
  const items = collectPluginNavigation(plugins, "admin");
  assert.equal(items.length, 1);
  assert.equal(items[0].pluginSlug, "demo-plugin");
});

test("Docker build contexts include the plugins directory", () => {
  const backend = readFileSync(new URL("../deploy/backend.Dockerfile", import.meta.url), "utf8");
  const frontend = readFileSync(new URL("../deploy/frontend.Dockerfile", import.meta.url), "utf8");
  assert.match(backend, /COPY plugins \/app\/plugins/);
  assert.match(frontend, /COPY plugins \.\/plugins/);
});
