import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Problem } from "../public/Public";
import { badgeMarks, metricNames } from "../memory/Achievements";
type Rule = components["schemas"]["AchievementRule"];
const blank: Rule = {
  id: "",
  series_id: "",
  series_title: "",
  tier: 1,
  revision: 0,
  title: "",
  description: "",
  badge: "spark",
  metric: "watch_records",
  threshold: 1,
  active: true,
};
export function AdminAchievements() {
  const cache = useQueryClient();
  const refresh = () =>
    cache.invalidateQueries({ queryKey: ["admin", "achievements"] });
  const [editing, setEditing] = useState<Rule | null>(null);
  const [search, setSearch] = useState("");
  const [owner, setOwner] = useState("");
  const [tier, setTier] = useState("");
  const [reason, setReason] = useState("");
  const [grant, setGrant] = useState(true);
  const [done, setDone] = useState("");
  const rules = useQuery({
    queryKey: ["admin", "achievements", "rules"],
    queryFn: ({ signal }) =>
      result(client.GET("/api/v1/admin/achievements/rules", { signal })),
  });
  const jobs = useQuery({
    queryKey: ["admin", "achievements", "backfills"],
    queryFn: ({ signal }) =>
      result(client.GET("/api/v1/admin/achievements/backfills", { signal })),
    refetchInterval: 5000,
  });
  const users = useQuery({
    queryKey: ["admin", "achievement-users", search],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/admin/users", {
          signal,
          params: { query: { search, page: 1 } },
        }),
      ),
  });
  const preview = useMutation({
    mutationFn: () =>
      result(client.POST("/api/v1/admin/achievements/backfills")),
    onSuccess: refresh,
  });
  const action = useMutation({
    mutationFn: ({ id, action }: { id: string; action: "run" | "pause" }) =>
      result(
        client.POST("/api/v1/admin/achievements/backfills/{id}", {
          params: { path: { id } },
          body: { action },
        }),
      ),
    onSuccess: refresh,
  });
  const award = useMutation({
    mutationFn: () =>
      result(
        client.POST("/api/v1/admin/achievements/grants", {
          body: { owner_id: owner, tier_id: tier, reason, grant },
        }),
      ),
    onSuccess: () => {
      setDone(
        grant ? "已授予徽章并记录操作原因。" : "已撤回授予，原解锁历史仍保留。",
      );
      setReason("");
    },
  });
  return (
    <div className="admin-stack">
      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <h2>受控成就规则</h2>
            <p>仅使用核心统计。编辑会创建新修订，不改写已经解锁的历史。</p>
          </div>
          <Button className="button primary" onClick={() => setEditing(blank)}>
            新增规则
          </Button>
        </div>
        {rules.error ? (
          <Problem error={rules.error} />
        ) : (
          <div className="admin-table-wrap">
            <table className="admin-table">
              <thead>
                <tr>
                  <th>系列 / 等级</th>
                  <th>徽章</th>
                  <th>条件</th>
                  <th>状态</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {rules.data?.items.map((r) => (
                  <tr key={r.id}>
                    <td>
                      {r.series_title} / {r.tier}
                    </td>
                    <td>
                      {badgeMarks[r.badge]} {r.title}
                      <small>修订 {r.revision}</small>
                    </td>
                    <td>
                      {metricNames[r.metric]} ≥ {r.threshold}
                    </td>
                    <td>{r.active ? "启用" : "停用"}</td>
                    <td>
                      <Button
                        className="button secondary"
                        onClick={() => setEditing(r)}
                      >
                        编辑
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <h2>人工授予与撤回</h2>
            <p>选择账号与徽章，并留下这次操作的原因。</p>
          </div>
        </div>
        <form
          className="editor-form admin-panel-body"
          onSubmit={(e) => {
            e.preventDefault();
            setDone("");
            award.mutate();
          }}
        >
          <fieldset disabled={award.isPending}>
            <label>
              查找账号
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
            <label>
              目标账号
              <select
                aria-label="目标账号"
                required
                value={owner}
                onChange={(e) => setOwner(e.target.value)}
              >
                <option value="">选择账号</option>
                {users.data?.items.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.display_name} · {u.email}
                  </option>
                ))}
              </select>
            </label>
            <label>
              徽章
              <select
                aria-label="徽章"
                required
                value={tier}
                onChange={(e) => setTier(e.target.value)}
              >
                <option value="">选择徽章</option>
                {rules.data?.items.map((r) => (
                  <option key={r.id} value={r.id}>
                    {r.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              操作
              <select
                aria-label="操作"
                value={grant ? "grant" : "revoke"}
                onChange={(e) => setGrant(e.target.value === "grant")}
              >
                <option value="grant">授予 / 恢复授予</option>
                <option value="revoke">撤回授予</option>
              </select>
            </label>
            <label>
              操作原因
              <Input
                value={reason}
                maxLength={1000}
                required
                onChange={(e) => setReason(e.target.value)}
              />
            </label>
            <Button className="button primary">执行并写入审计</Button>
          </fieldset>
          {award.error && <Problem error={award.error} />}
          {done && <p role="status">{done}</p>}
        </form>
      </section>
      <section className="admin-panel">
        <div className="admin-panel-heading">
          <div>
            <h2>历史补算</h2>
            <p>
              先冻结规则修订与账号名单，再确认执行。每批最多 10
              个账号，暂停后可续跑；重复执行不重复授予。
            </p>
          </div>
          <Button
            className="button secondary"
            disabled={preview.isPending}
            onClick={() => preview.mutate()}
          >
            生成补算预览
          </Button>
        </div>
        {jobs.data?.items.map((j) => (
          <article className="admin-panel-body admin-section-divider" key={j.id}>
            <h3>
              {j.state === "preview"
                ? "待确认预览"
                : j.state === "running"
                  ? "正在补算"
                  : j.state === "paused"
                    ? "已暂停"
                    : "补算完成"}
            </h3>
            <p>
              {j.rules.length} 条冻结规则 · 已处理 {j.cursor} / {j.total} 个账号
              · 新增 {j.granted} 次授予
            </p>
            <details>
              <summary>查看规则范围</summary>
              {j.rules.map((r) => (
                <p key={r.id}>
                  {r.title} · 修订 {r.revision} · {metricNames[r.metric]} ≥{" "}
                  {r.threshold}
                </p>
              ))}
            </details>
            {j.state !== "done" && (
              <Button
                className="button secondary"
                disabled={action.isPending}
                onClick={() =>
                  action.mutate({
                    id: j.id,
                    action: j.state === "running" ? "pause" : "run",
                  })
                }
              >
                {j.state === "running"
                  ? "暂停"
                  : j.state === "preview"
                    ? "确认开始补算"
                    : "继续补算"}
              </Button>
            )}
          </article>
        ))}
        {(jobs.error || preview.error || action.error) && (
          <Problem error={jobs.error || preview.error || action.error} />
        )}
      </section>
      {editing && (
        <RuleEditor
          value={editing}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}
    </div>
  );
}
function RuleEditor({
  value,
  onClose,
  onSaved,
}: {
  value: Rule;
  onClose: () => void;
  onSaved: () => Promise<unknown>;
}) {
  const [form, setForm] = useState(value);
  const set = <K extends keyof Rule>(k: K, v: Rule[K]) =>
    setForm((p) => ({ ...p, [k]: v }));
  const save = useMutation({
    mutationFn: () =>
      result(client.PUT("/api/v1/admin/achievements/rules", { body: form })),
    onSuccess: async () => {
      await onSaved();
      onClose();
    },
  });
  return (
    <Dialog
      title={value.id ? "创建规则新修订" : "新增成就规则"}
      onClose={onClose}
    >
      <form
        className="editor-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={save.isPending}>
          <label>
            系列标识
            <Input
              required
              value={form.series_id}
              disabled={!!value.id}
              pattern="[a-z0-9][a-z0-9-]{0,39}"
              onChange={(e) => set("series_id", e.target.value)}
            />
          </label>
          <label>
            系列名称
            <Input
              required
              maxLength={80}
              value={form.series_title}
              onChange={(e) => set("series_title", e.target.value)}
            />
          </label>
          <label>
            等级
            <Input
              required
              type="number"
              min={1}
              max={20}
              disabled={!!value.id}
              value={form.tier}
              onChange={(e) => set("tier", Number(e.target.value))}
            />
          </label>
          <label>
            徽章名称
            <Input
              required
              maxLength={100}
              value={form.title}
              onChange={(e) => set("title", e.target.value)}
            />
          </label>
          <label>
            说明
            <textarea
              value={form.description}
              maxLength={1000}
              onChange={(e) => set("description", e.target.value)}
            />
          </label>
          <label>
            图案
            <select
              aria-label="图案"
              value={form.badge}
              onChange={(e) => set("badge", e.target.value as Rule["badge"])}
            >
              {Object.entries(badgeMarks).map(([k, v]) => (
                <option key={k} value={k}>
                  {v} {k}
                </option>
              ))}
            </select>
          </label>
          <label>
            统计条件
            <select
              aria-label="统计条件"
              value={form.metric}
              onChange={(e) => set("metric", e.target.value as Rule["metric"])}
            >
              {Object.entries(metricNames).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            阈值
            <Input
              type="number"
              required
              min={1}
              max={1000000}
              value={form.threshold}
              onChange={(e) => set("threshold", Number(e.target.value))}
            />
          </label>
          <label>
            <Input
              type="checkbox"
              checked={form.active}
              onChange={(e) => set("active", e.target.checked)}
            />
            启用规则
          </label>
          <Button className="button primary">保存规则修订</Button>
        </fieldset>
        {save.error && <Problem error={save.error} />}
      </form>
    </Dialog>
  );
}
