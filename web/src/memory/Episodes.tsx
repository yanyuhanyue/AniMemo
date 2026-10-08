import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Pager, Problem } from "../public/Public";
import { AnimeSelect, useMemoryRefresh, type Episode } from "./shared";
import { IdentityMerge } from "./Characters";
const roles = { required: "必看", optional: "可选", excluded: "排除" };
const kinds = { main: "正篇", special: "特别篇", ova: "OVA", movie: "剧场版" };
export function Episodes({
  userID,
  initialAnime = "",
}: {
  userID: string;
  initialAnime?: string;
}) {
  const [batch, setBatch] = useState(false);
  const [anime, setAnime] = useState(initialAnime);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [editor, setEditor] = useState<Episode | null | undefined>();
  const [merge, setMerge] = useState<Episode | null>(null);
  const [selected, setSelected] = useState<{ id: string; version: number }[]>(
    [],
  );
  const [scope, setScope] = useState<"mainline" | "all" | "custom">("custom");
  const [precision, setPrecision] = useState<
    "exact" | "approximate" | "caught_up"
  >("exact");
  const [note, setNote] = useState("");
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "episodes", anime, search, page],
    enabled: !!anime,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/episodes", {
          signal,
          params: { query: { anime_id: anime, search, page } },
        }),
      ),
  });
  const snapshots = useQuery({
    queryKey: ["memory", userID, "progress", anime],
    enabled: !!anime,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/progress", {
          signal,
          params: { query: { anime_id: anime } },
        }),
      ),
  });
  const assert = useMutation({
    mutationFn: () =>
      result(
        client.POST("/api/v1/memory/progress", {
          body: { anime_id: anime, episodes: selected, scope, precision, note },
        }),
      ),
    onSuccess: async () => {
      setSelected([]);
      setNote("");
      await refresh();
    },
  });
  const split = useMutation({
    mutationFn: (e: Episode) =>
      result(
        client.POST("/api/v1/memory/identities/{kind}/{id}", {
          params: { path: { kind: "episode", id: e.id } },
          body: { version: e.version, target_id: "" },
        }),
      ),
    onSuccess: refresh,
  });
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">EVERY CHAPTER HAS A PLACE</p>
          <h2>长篇与集数</h2>
          <p>正篇、特别篇与追平范围，分别记清楚。</p>
        </div>
        <Button
          className="button primary"
          disabled={!anime}
          onClick={() => setEditor(null)}
        >
          添加集数
        </Button>
        <Button
          className="button secondary"
          disabled={!anime}
          onClick={() => setBatch(true)}
        >
          批量建立目录
        </Button>
      </header>
      <AnimeSelect
        userID={userID}
        value={anime}
        onChange={(id) => {
          setAnime(id);
          setPage(1);
          setSelected([]);
        }}
        required
      />
      {anime && (
        <>
          <div className="memory-filters">
            <label>
              搜索集数
              <Input
                value={search}
                onChange={(e) => {
                  setSearch(e.target.value);
                  setPage(1);
                }}
              />
            </label>
          </div>
          {q.error ? (
            <Problem error={q.error} />
          ) : q.isPending ? (
            <p role="status">读取集数目录…</p>
          ) : (
            <>
              <div className="memory-episode-list">
                {q.data.items.map((e) => (
                  <article key={e.id}>
                    <label className="memory-check">
                      <Input
                        type="checkbox"
                        aria-label={`选择 ${e.title}`}
                        disabled={
                          !!e.redirect_id || e.progress_role === "excluded"
                        }
                        checked={selected.some((v) => v.id === e.id)}
                        onChange={(event) =>
                          setSelected((prev) =>
                            event.target.checked
                              ? [...prev, { id: e.id, version: e.version }]
                              : prev.filter((v) => v.id !== e.id),
                          )
                        }
                      />
                      <span className="memory-episode-number">{e.number}</span>
                    </label>
                    <div>
                      <h3>{e.title}</h3>
                      <p className="memory-meta">
                        {kinds[e.kind]} · {roles[e.progress_role]}
                        {e.redirect_id ? " · 已归并" : ""}
                      </p>
                    </div>
                    <div className="memory-row-actions">
                      <Button
                        className="text-button"
                        onClick={() => setEditor(e)}
                      >
                        编辑
                      </Button>
                      <Button
                        className="text-button"
                        onClick={() => setMerge(e)}
                      >
                        归并
                      </Button>
                      {e.redirect_id && (
                        <Button
                          className="text-button"
                          onClick={() => split.mutate(e)}
                        >
                          恢复独立身份
                        </Button>
                      )}
                    </div>
                  </article>
                ))}
              </div>
              {!q.data.items.length && (
                <p className="memory-empty">
                  还没有正式集数目录，添加集数不会生成观看记录。
                </p>
              )}
              <Pager
                page={page}
                size={q.data.page_size}
                total={q.data.total}
                onChange={setPage}
              />
            </>
          )}
          <section className="memory-panel">
            <h3>保留一次进度表达</h3>
            <p className="muted">
              所选 {selected.length}{" "}
              话会固定为当次范围。目录之后新增或调整，不会改变这个快照，也不会生成逐话观看记录。
            </p>
            <div className="memory-form-grid">
              <label>
                观看范围
                <select
                  aria-label="观看范围"
                  value={scope}
                  onChange={(e) => setScope(e.target.value as typeof scope)}
                >
                  <option value="custom">自定义</option>
                  <option value="mainline">主线</option>
                  <option value="all">全部类型</option>
                </select>
              </label>
              <label>
                进度精度
                <select
                  aria-label="进度精度"
                  value={precision}
                  onChange={(e) =>
                    setPrecision(e.target.value as typeof precision)
                  }
                >
                  <option value="exact">确定看到这里</option>
                  <option value="approximate">大约看到这里</option>
                  <option value="caught_up">当时已追平</option>
                </select>
              </label>
            </div>
            <label>
              补充说明
              <Input
                value={note}
                maxLength={4000}
                onChange={(e) => setNote(e.target.value)}
              />
            </label>
            <Button
              className="button primary"
              disabled={!selected.length || assert.isPending}
              onClick={() => assert.mutate()}
            >
              保存范围快照
            </Button>
            {assert.error && <Problem error={assert.error} />}
            <details>
              <summary>已保存的进度快照</summary>
              {snapshots.data?.items.map((p) => (
                <p key={p.id}>
                  {new Date(p.created_at).toLocaleDateString("zh-CN")} ·{" "}
                  {p.episode_ids.length} 话 ·{" "}
                  {p.precision === "caught_up"
                    ? "当时已追平"
                    : p.precision === "approximate"
                      ? "近似进度"
                      : "精确范围"}{" "}
                  {p.note}
                </p>
              ))}
            </details>
          </section>
          <Franchise userID={userID} anime={anime} />
        </>
      )}
      {batch && (
        <EpisodeBatch
          userID={userID}
          anime={anime}
          onClose={() => setBatch(false)}
        />
      )}{" "}
      {editor !== undefined && (
        <EpisodeEditor
          userID={userID}
          anime={anime}
          episode={editor}
          onClose={() => setEditor(undefined)}
        />
      )}{" "}
      {merge && (
        <IdentityMerge
          userID={userID}
          kind="episode"
          resource={merge}
          onClose={() => setMerge(null)}
        />
      )}{" "}
      {(split.error || snapshots.error) && (
        <Problem error={split.error || snapshots.error} />
      )}
    </>
  );
}
function EpisodeEditor({
  userID,
  anime,
  episode: e,
  onClose,
}: {
  userID: string;
  anime: string;
  episode: Episode | null;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(e?.title ?? "");
  const [number, setNumber] = useState(e?.number ?? 1);
  const [kind, setKind] = useState(e?.kind ?? "main");
  const [role, setRole] = useState(e?.progress_role ?? "required");
  const [identities, setIdentities] = useState(
    e?.identities.map((i) => `${i.provider}:${i.external_id}`).join("\n") ?? "",
  );
  const refresh = useMemoryRefresh(userID);
  const save = useMutation({
    mutationFn: () => {
      const body: components["schemas"]["EpisodeInput"] = {
        version: e?.version ?? 0,
        anime_id: anime,
        title,
        number,
        kind,
        progress_role: role,
        identities: identities
          .split("\n")
          .filter((s) => s.trim())
          .map((s) => {
            const i = s.indexOf(":");
            if (i < 1)
              throw new ApiError(
                400,
                "validation_error",
                "资料源对应请按 来源:编号 填写。",
              );
            return {
              provider: s.slice(0, i).trim(),
              external_id: s.slice(i + 1).trim(),
            };
          }),
      };
      return e
        ? result(
            client.PUT("/api/v1/memory/episodes/{id}", {
              params: { path: { id: e.id } },
              body,
            }),
          )
        : result(client.POST("/api/v1/memory/episodes", { body }));
    },
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return (
    <Dialog title={e ? "编辑集数" : "添加集数"} onClose={onClose}>
      <form
        className="memory-form"
        onSubmit={(ev) => {
          ev.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={save.isPending}>
          <label>
            集数标题
            <Input
              data-initial-focus
              required
              maxLength={160}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label>
            显示编号
            <Input
              type="number"
              min={0}
              max={100000}
              value={number}
              onChange={(e) => setNumber(Number(e.target.value))}
            />
          </label>
          <label>
            类型
            <select
              aria-label="类型"
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
            >
              {Object.entries(kinds).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            在观看范围中的角色
            <select
              aria-label="在观看范围中的角色"
              value={role}
              onChange={(e) => setRole(e.target.value as typeof role)}
            >
              {Object.entries(roles).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            资料源对应（每行 来源:编号，可留空）
            <textarea
              value={identities}
              rows={3}
              onChange={(e) => setIdentities(e.target.value)}
            />
          </label>
        </fieldset>
        {save.error && <Problem error={save.error} />}
        <Button className="button primary" disabled={save.isPending}>
          保存集数
        </Button>
      </form>
    </Dialog>
  );
}
function Franchise({ userID, anime }: { userID: string; anime: string }) {
  const [target, setTarget] = useState("");
  const [relation, setRelation] =
    useState<components["schemas"]["AnimeRelation"]["relation"]>("sequel");
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "relations"],
    queryFn: ({ signal }) =>
      result(client.GET("/api/v1/memory/relations", { signal })),
  });
  const labels = {
    sequel: "续作",
    prequel: "前作",
    side_story: "外传",
    same_franchise: "同系列",
  };
  const save = useMutation({
    mutationFn: () =>
      result(
        client.PUT("/api/v1/memory/relations", {
          body: { from_id: anime, to_id: target, relation },
        }),
      ),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: (body: components["schemas"]["AnimeRelation"]) =>
      result(client.DELETE("/api/v1/memory/relations", { body })),
    onSuccess: refresh,
  });
  return (
    <details className="memory-panel">
      <summary>作品系列与篇章关系</summary>
      <AnimeSelect userID={userID} value={target} onChange={setTarget} />
      <label>
        关系
        <select
          aria-label="关系"
          value={relation}
          onChange={(e) => setRelation(e.target.value as typeof relation)}
        >
          {Object.entries(labels).map(([k, v]) => (
            <option key={k} value={k}>
              {v}
            </option>
          ))}
        </select>
      </label>
      <Button
        className="button secondary"
        disabled={!target || target === anime || save.isPending}
        onClick={() => save.mutate()}
      >
        添加关系
      </Button>
      {q.data?.items
        .filter((r) => r.from_id === anime || r.to_id === anime)
        .map((r) => (
          <p key={r.from_id + r.to_id + r.relation}>
            {r.from_id === anime ? "关联" : "来自其他作品的"}
            {labels[r.relation]}{" "}
            <a
              href={`/memory?anime_id=${r.from_id === anime ? r.to_id : r.from_id}#episodes`}
            >
              查看关联作品
            </a>{" "}
            <Button className="text-button" onClick={() => remove.mutate(r)}>
              移除关系
            </Button>
          </p>
        ))}
      {(q.error || save.error || remove.error) && (
        <Problem error={q.error || save.error || remove.error} />
      )}
    </details>
  );
}
function EpisodeBatch({
  userID,
  anime,
  onClose,
}: {
  userID: string;
  anime: string;
  onClose: () => void;
}) {
  const [from, setFrom] = useState(1);
  const [to, setTo] = useState(12);
  const [kind, setKind] = useState<Episode["kind"]>("main");
  const [role, setRole] = useState<Episode["progress_role"]>("required");
  const refresh = useMemoryRefresh(userID);
  const save = useMutation({
    mutationFn: () =>
      result(
        client.POST("/api/v1/memory/episodes/batch", {
          body: { anime_id: anime, from, to, kind, progress_role: role },
        }),
      ),
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return (
    <Dialog title="批量建立集数目录" onClose={onClose}>
      <form
        className="memory-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <p>
          将明确创建第 {from}–{to} 话，共 {Math.max(0, to - from + 1)}{" "}
          个本地身份。不会自动记录观看；范围与已有同类型集数重叠时，整批拒绝。
        </p>
        <fieldset disabled={save.isPending}>
          <label>
            起始编号
            <Input
              type="number"
              min={1}
              max={100000}
              value={from}
              onChange={(e) => setFrom(Number(e.target.value))}
            />
          </label>
          <label>
            结束编号
            <Input
              type="number"
              min={from}
              max={Math.min(from + 1999, 100000)}
              value={to}
              onChange={(e) => setTo(Number(e.target.value))}
            />
          </label>
          <label>
            类型
            <select
              aria-label="类型"
              value={kind}
              onChange={(e) => setKind(e.target.value as Episode["kind"])}
            >
              {Object.entries(kinds).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <label>
            观看角色
            <select
              aria-label="观看角色"
              value={role}
              onChange={(e) =>
                setRole(e.target.value as Episode["progress_role"])
              }
            >
              {Object.entries(roles).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <Button className="button primary">确认建立目录</Button>
        </fieldset>
        {save.error && <Problem error={save.error} />}
      </form>
    </Dialog>
  );
}
