import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Problem } from "../public/Public";
import {
  ShareMemory,
  Visibility,
  useMemoryRefresh,
  type Collection,
} from "./shared";
type Item = components["schemas"]["CollectionItem"];
export function Collections({ userID }: { userID: string }) {
  const [editor, setEditor] = useState<Collection | null | undefined>();
  const [share, setShare] = useState("");
  const [remove, setRemove] = useState<Collection | null>(null);
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "collections"],
    queryFn: ({ signal }) =>
      result(client.GET("/api/v1/memory/collections", { signal })),
  });
  const del = useMutation({
    mutationFn: (c: Collection) =>
      result(
        client.DELETE("/api/v1/memory/collections/{id}", {
          params: { path: { id: c.id }, query: { version: c.version } },
        }),
      ),
    onSuccess: async () => {
      setRemove(null);
      await refresh();
    },
  });
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">YOUR OWN LITTLE SHELVES</p>
          <h2>收藏小册</h2>
          <p>作品、角色和记忆，可以有你自己的排列方式。</p>
        </div>
        <Button className="button primary" onClick={() => setEditor(null)}>
          新建收藏夹
        </Button>
      </header>
      {q.isPending ? (
        <p role="status">正在打开收藏…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : (
        <div className="memory-collection-grid">
          {q.data.items.map((c) => (
            <article className="memory-collection" key={c.id}>
              <p className="memory-meta">{c.items.length} 段收藏</p>
              <h3>{c.title}</h3>
              <p className="memory-excerpt">{c.description}</p>
              <div className="memory-actions">
                <Button className="text-button" onClick={() => setEditor(c)}>
                  翻开与编辑
                </Button>
                <Button className="text-button" onClick={() => setShare(c.id)}>
                  分享
                </Button>
                <Button
                  className="text-button danger-text"
                  onClick={() => setRemove(c)}
                >
                  移除
                </Button>
              </div>
            </article>
          ))}
          {!q.data.items.length && (
            <p className="memory-empty">为喜欢的作品和记忆留一个共同的位置。</p>
          )}
        </div>
      )}
      {editor !== undefined && (
        <CollectionEditor
          userID={userID}
          collection={editor}
          onClose={() => setEditor(undefined)}
        />
      )}{" "}
      {share && (
        <ShareMemory
          kind="collection"
          id={share}
          onClose={() => setShare("")}
        />
      )}{" "}
      {remove && (
        <Dialog title="移除收藏夹" onClose={() => setRemove(null)}>
          <div className="memory-form">
            <p>
              移除「{remove.title}」的组织方式，作品、角色和记忆正文都会保留。
            </p>
            <Button
              className="button danger"
              disabled={del.isPending}
              onClick={() => del.mutate(remove)}
            >
              确认移除收藏夹
            </Button>
            {del.error && <Problem error={del.error} />}
          </div>
        </Dialog>
      )}
    </>
  );
}
function CollectionEditor({
  userID,
  collection: c,
  onClose,
}: {
  userID: string;
  collection: Collection | null;
  onClose: () => void;
}) {
  const [title, setTitle] = useState(c?.title ?? "");
  const [description, setDescription] = useState(c?.description ?? "");
  const [visibility, setVisibility] = useState(c?.visibility ?? "private");
  const [items, setItems] = useState<Item[]>(c?.items ?? []);
  const [kind, setKind] = useState<Item["kind"]>("anime");
  const [search, setSearch] = useState("");
  const [names, setNames] = useState<Record<string, string>>({});
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "collection-picker", kind, search],
    queryFn: async ({ signal }) => {
      if (kind === "anime")
        return (
          await result(
            client.GET("/api/v1/entries", {
              signal,
              params: { query: { search, page_size: 100 } },
            }),
          )
        ).items.map((e) => ({ id: e.anime_id, title: e.title }));
      if (kind === "character")
        return (
          await result(
            client.GET("/api/v1/memory/characters", {
              signal,
              params: { query: { search } },
            }),
          )
        ).items.map((c) => ({ id: c.id, title: c.name }));
      return (
        await result(
          client.GET("/api/v1/memory/notes", {
            signal,
            params: { query: { search, kind } },
          }),
        )
      ).items.map((n) => ({ id: n.id, title: n.title }));
    },
  });
  const refs = useQuery({
    queryKey: ["memory", userID, "references", items],
    queryFn: ({ signal }) =>
      result(
        client.POST("/api/v1/memory/references", { signal, body: { items } }),
      ),
  });
  const save = useMutation({
    mutationFn: () => {
      const body = {
        version: c?.version ?? 0,
        title,
        description,
        visibility,
        items,
      };
      return c
        ? result(
            client.PUT("/api/v1/memory/collections/{id}", {
              params: { path: { id: c.id } },
              body,
            }),
          )
        : result(client.POST("/api/v1/memory/collections", { body }));
    },
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  const labels = {
    anime: "作品",
    character: "角色",
    note: "札记",
    moment: "瞬间",
  };
  function move(index: number, step: number) {
    setItems((prev) => {
      const next = [...prev];
      [next[index], next[index + step]] = [next[index + step]!, next[index]!];
      return next;
    });
  }
  return (
    <Dialog title={c ? "翻开收藏小册" : "新建收藏小册"} onClose={onClose} wide>
      <form
        className="memory-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={save.isPending}>
          <label>
            收藏夹名称
            <Input
              data-initial-focus
              required
              value={title}
              maxLength={160}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label>
            这本小册的故事
            <textarea
              value={description}
              rows={3}
              maxLength={4000}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <Visibility value={visibility} onChange={setVisibility} />
          <div className="memory-form-grid">
            <label>
              查找类型
              <select
                aria-label="查找类型"
                value={kind}
                onChange={(e) => setKind(e.target.value as Item["kind"])}
              >
                {Object.entries(labels).map(([k, v]) => (
                  <option value={k} key={k}>
                    {v}
                  </option>
                ))}
              </select>
            </label>
            <label>
              搜索标题
              <Input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          </div>
          <div className="memory-picker-results">
            {q.data?.map((v) => (
              <Button
                type="button"
                className="button quiet"
                key={v.id}
                disabled={items.some((i) => i.kind === kind && i.id === v.id)}
                onClick={() => {
                  setItems([...items, { kind, id: v.id }]);
                  setNames({ ...names, [v.id]: v.title });
                }}
              >
                {v.title} ＋
              </Button>
            ))}
          </div>
          {q.error && <Problem error={q.error} />}
          <h3>小册顺序 · {items.length} 项</h3>
          <ol className="memory-selected-items">
            {items.map((item, index) => (
              <li key={item.kind + item.id}>
                <span>
                  {names[item.id] ||
                    refs.data?.items.find((v) => v.id === item.id)?.title ||
                    q.data?.find((v) => v.id === item.id)?.title ||
                    `${labels[item.kind]}已失效（可移出）`}
                </span>
                <div>
                  <Button
                    type="button"
                    className="text-button"
                    disabled={index === 0}
                    aria-label={`上移第 ${index + 1} 项`}
                    onClick={() => move(index, -1)}
                  >
                    上移
                  </Button>
                  <Button
                    type="button"
                    className="text-button"
                    disabled={index === items.length - 1}
                    aria-label={`下移第 ${index + 1} 项`}
                    onClick={() => move(index, 1)}
                  >
                    下移
                  </Button>
                  <Button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      setItems(items.filter((_, i) => i !== index))
                    }
                  >
                    移出
                  </Button>
                </div>
              </li>
            ))}
          </ol>
        </fieldset>
        {save.error && <Problem error={save.error} />}
        <Button className="button primary" disabled={save.isPending}>
          保存收藏夹
        </Button>
      </form>
    </Dialog>
  );
}
