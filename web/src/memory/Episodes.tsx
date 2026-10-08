import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Icon } from "../components/ui/Icon";
import { Pager, Problem } from "../public/Public";
import { AnimeSelect, useMemoryRefresh, type Episode } from "./shared";
import { IdentityMerge } from "./Characters";
const roles = { required: "计入范围", optional: "可选", excluded: "不计入范围" };
const kinds = { main: "正篇", special: "特别篇", ova: "OVA", movie: "剧场版" };
export function Episodes({
  userID,
  anime = "",
  onAnimeChange,
}: {
  userID: string;
  anime?: string;
  onAnimeChange: (id: string) => void;
}) {
  const [batch, setBatch] = useState(false);
  const [recording, setRecording] = useState(false);
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
      setRecording(false);
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
    <section className="episode-page" aria-label="集数整理">
      <header className="memory-section-heading">
        <div><h2>集数目录</h2><p>整理篇章，为记忆找到具体的一集。</p></div>
        {anime && <div className="memory-heading-actions">
          <Button className="button secondary" onClick={() => setBatch(true)}>批量添加</Button>
          <Button className="button primary" onClick={() => setEditor(null)}><Icon name="plus" />添加集数</Button>
        </div>}
      </header>
      {!anime ? <section className="memory-panel episode-pick-work"><Icon name="book" /><h3>先选择一部作品</h3><p>打开这部作品的目录、观看范围和系列关系。</p><AnimeSelect userID={userID} value={anime} onChange={onAnimeChange} required /></section> : <div className="episode-workspace">
        <section className="episode-catalog" aria-label="集数目录列表">
          <div className="episode-catalog-toolbar">
            <strong>{q.data ? q.data.total + ' 个集数' : '集数列表'}</strong>
            <label className="episode-search"><Icon name="search" /><span className="sr-only">搜索集数</span><Input type="search" value={search} placeholder="查找集数标题" maxLength={160} onChange={e => { setSearch(e.target.value); setPage(1); }} /></label>
          </div>
          {recording && <div className="episode-selection-hint"><span>选择要记下的集数 · 已选 {selected.length} 话</span><Button className="text-button" onClick={() => setSelected([])}>清空选择</Button></div>}
          {q.error ? <Problem error={q.error} /> : q.isPending ? <p className="memory-loading" role="status">正在读取集数目录…</p> : <>
            <div className="memory-episode-list">
              {q.data.items.map(e => <article key={e.id} data-selected={selected.some(v => v.id === e.id) || undefined}>
                {recording && <label className="memory-check episode-select"><Input type="checkbox" aria-label={'选择 ' + e.title} disabled={!!e.redirect_id || e.progress_role === 'excluded'} checked={selected.some(v => v.id === e.id)} onChange={event => setSelected(prev => event.target.checked ? [...prev, {id: e.id, version: e.version}] : prev.filter(v => v.id !== e.id))} /></label>}
                <span className="memory-episode-number"><small>{e.kind === 'main' ? 'EP' : e.kind === 'special' ? 'SP' : e.kind.toUpperCase()}</small>{String(e.number).padStart(2,'0')}</span>
                <div className="episode-title"><h3>{e.title}</h3><p className="memory-meta"><span className={'episode-kind kind-' + e.kind}>{kinds[e.kind]}</span><span>{roles[e.progress_role]}</span>{e.redirect_id && <span>已归并</span>}</p></div>
                <div className="memory-row-actions"><Button className="icon-button" aria-label={'编辑 ' + e.title} onClick={() => setEditor(e)}><Icon name="edit" /></Button><details className="episode-more"><summary aria-label={e.title + '的更多操作'}>更多</summary><div><Button className="text-button" onClick={() => setMerge(e)}>归并重复集数</Button>{e.redirect_id && <Button className="text-button" onClick={() => split.mutate(e)}>恢复独立身份</Button>}</div></details></div>
              </article>)}
            </div>
            {!q.data.items.length && <div className="memory-empty episode-empty"><Icon name="book" /><h3>{search ? '没有找到这集' : '还没有集数目录'}</h3><p>{search ? '换个标题关键词试试。' : '可以逐集添加，也可以批量建立目录。\n添加目录不会生成观看记录。'}</p>{search ? <Button className="button secondary" onClick={() => setSearch('')}>清除搜索</Button> : <Button className="button secondary" onClick={() => setEditor(null)}>从第一集开始</Button>}</div>}
            {q.data.total > q.data.page_size && <Pager page={page} size={q.data.page_size} total={q.data.total} onChange={setPage} />}
          </>}
        </section>
        <aside className="episode-side">
          <section className="episode-range-panel">
            <div className="episode-panel-heading"><Icon name="check" /><h3>记下观看范围</h3></div>
            {recording ? <>
              <p className="episode-selected-count">已选择 <strong>{selected.length}</strong> 话</p>
              <label>观看范围<select aria-label="观看范围" value={scope} onChange={e => setScope(e.target.value as typeof scope)}><option value="custom">自定义范围</option><option value="mainline">正篇主线</option><option value="all">包含全部类型</option></select></label>
              <label>记得有多清楚<select aria-label="进度精度" value={precision} onChange={e => setPrecision(e.target.value as typeof precision)}><option value="exact">确定看过所选集数</option><option value="approximate">大约看到这里</option><option value="caught_up">当时已看到已播部分</option></select></label>
              <label>补充说明<textarea value={note} rows={3} maxLength={4000} onChange={e => setNote(e.target.value)} placeholder="例如：那年暑假看到这里" /></label>
              <p className="field-hint">保留本次选择的范围，不会生成逐话观看记录。</p>
              <Button className="button primary" disabled={!selected.length || assert.isPending} onClick={() => assert.mutate()}>{assert.isPending ? '正在保存…' : '保存观看范围'}</Button>
              <Button className="text-button" disabled={assert.isPending} onClick={() => { setRecording(false); setSelected([]); }}>取消选择</Button>
            </> : <><p>记得看过哪些集数时，可以把范围留在这里。记不清，也可以只写一篇回忆。</p><Button className="button secondary" disabled={!q.data?.total} onClick={() => setRecording(true)}>选择集数并记录</Button></>}
            {assert.error && <Problem error={assert.error} />}
          </section>
          <details className="episode-saved-ranges"><summary>已保存的观看范围<span>{snapshots.data?.items.length ?? 0}</span></summary>{snapshots.data?.items.length ? snapshots.data.items.map(p => <article key={p.id}><strong>{p.episode_ids.length} 话 · {p.precision === 'caught_up' ? '当时看到已播部分' : p.precision === 'approximate' ? '大约的范围' : '确定看过'}</strong><small>记录于 {new Date(p.created_at).toLocaleDateString('zh-CN')}</small>{p.note && <p>{p.note}</p>}</article>) : <p className="muted">还没有保存过观看范围。</p>}</details>
          <Franchise userID={userID} anime={anime} />
        </aside>
      </div>}
      {batch && <EpisodeBatch userID={userID} anime={anime} onClose={() => setBatch(false)} />}
      {editor !== undefined && <EpisodeEditor userID={userID} anime={anime} episode={editor} onClose={() => setEditor(undefined)} />}
      {merge && <IdentityMerge userID={userID} kind="episode" resource={merge} onClose={() => setMerge(null)} />}
      {(split.error || snapshots.error) && <Problem error={split.error || snapshots.error} />}
    </section>
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
                是否计入观看范围
            <select
              aria-label="是否计入观看范围"
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
        <p className="field-hint">将添加第 {from}–{to} 话，共 {Math.max(0, to - from + 1)} 集。若编号与现有目录重复，请调整范围后再保存。建立目录不会记录观看。</p>
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
            是否计入观看范围
            <select
              aria-label="是否计入观看范围"
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
