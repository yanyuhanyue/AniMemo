import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Pager, Problem } from "../public/Public";
import {
  AnimeSelect,
  ShareMemory,
  MemoryRevisions,
  Visibility,
  useMemoryRefresh,
  type Character,
} from "./shared";
export function Characters({ userID }: { userID: string }) {
  const [share, setShare] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [favorite, setFavorite] = useState(false);
  const [editor, setEditor] = useState<Character | null | undefined>();
  const [reading, setReading] = useState<Character | null>(null);
  const [merge, setMerge] = useState<Character | null>(null);
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "characters", search, page, favorite],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/characters", {
          signal,
          params: { query: { search, page, highlight: favorite } },
        }),
      ),
  });
  const split = useMutation({
    mutationFn: (c: Character) =>
      result(
        client.POST("/api/v1/memory/identities/{kind}/{id}", {
          params: { path: { kind: "character", id: c.id } },
          body: { version: c.version, target_id: "" },
        }),
      ),
    onSuccess: async () => {
      setReading(null);
      await refresh();
    },
  });
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">PEOPLE WHO STAY WITH US</p>
          <h2>记住那些角色</h2>
          <p>名字之外，是他们在你的故事里留下的片段。</p>
        </div>
        <Button className="button primary" onClick={() => setEditor(null)}>
          记住一个角色
        </Button>
      </header>
      <div className="memory-filters">
        <label>
          查找名字或别名
          <Input
            type="search"
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            maxLength={160}
          />
        </label>
        <label className="memory-check">
          <Input
            type="checkbox"
            checked={favorite}
            onChange={(e) => {
              setFavorite(e.target.checked);
              setPage(1);
            }}
          />
          只看收藏
        </label>
      </div>
      {q.isPending ? (
        <p role="status">正在读取角色…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : (
        <>
          <div className="memory-character-grid">
            {q.data.items.map((c) => (
              <article className="memory-character" key={c.id}>
                <span className="memory-character-mark" aria-hidden="true">
                  {Array.from(c.name)[0]}
                </span>
                <div>
                  <p className="memory-meta">
                    {c.favorite ? "珍藏的角色" : "角色记忆"}
                    {c.redirect_id ? " · 已归并" : ""}
                  </p>
                  <h3>
                    <Button
                      className="memory-title-button"
                      onClick={() => setReading(c)}
                    >
                      {c.name}
                    </Button>
                  </h3>
                  <p className="muted">{c.aliases.join(" / ")}</p>
                  <p className="memory-excerpt">
                    {c.description || "还没有写下故事。"}
                  </p>
                </div>
              </article>
            ))}
          </div>
          {!q.data.items.length && (
            <div className="memory-empty">
              <h3>谁曾让你念念不忘？</h3>
              <p>收藏名字，慢慢写下与他们有关的记忆。</p>
            </div>
          )}
          <Pager
            page={page}
            size={q.data.page_size}
            total={q.data.total}
            onChange={setPage}
          />
        </>
      )}
      {editor !== undefined && (
        <CharacterEditor
          userID={userID}
          character={editor}
          onClose={() => setEditor(undefined)}
        />
      )}
      {reading && (
        <Dialog title={reading.name} onClose={() => setReading(null)} wide>
          <div className="memory-reading">
            <p className="muted">{reading.aliases.join(" / ")}</p>
            <p className="memory-prose">{reading.description}</p>
            <a
              className="button primary"
              href={`/memory?character_id=${reading.id}#notes`}
            >
              翻开相关记忆
            </a>
            <a
              className="button secondary"
              href={`/memory?character_id=${reading.id}#search`}
            >
              角色记忆串
            </a>
            <Button
              className="button secondary"
              onClick={() => {
                setShare(reading.id);
                setReading(null);
              }}
            >
              分享角色
            </Button>
            <MemoryRevisions userID={userID} id={reading.id} />
            <div className="memory-actions">
              <Button
                className="button secondary"
                onClick={() => {
                  setEditor(reading);
                  setReading(null);
                }}
              >
                编辑角色
              </Button>
              <Button
                className="button secondary"
                onClick={() => {
                  setMerge(reading);
                  setReading(null);
                }}
              >
                归并重复身份
              </Button>
            </div>
            {reading.redirect_id && (
              <div className="memory-confirm">
                <p>
                  此身份已归并。拆分会恢复其独立身份，原先关联的笔记仍保留。
                </p>
                <Button
                  className="button secondary"
                  disabled={split.isPending}
                  onClick={() => split.mutate(reading)}
                >
                  拆分并恢复独立身份
                </Button>
              </div>
            )}
            {split.error && <Problem error={split.error} />}
          </div>
        </Dialog>
      )}
      {share && (
        <ShareMemory kind="character" id={share} onClose={() => setShare("")} />
      )}{" "}
      {merge && (
        <IdentityMerge
          userID={userID}
          kind="character"
          resource={merge}
          onClose={() => setMerge(null)}
        />
      )}
    </>
  );
}
function CharacterEditor({
  userID,
  character: c,
  onClose,
}: {
  userID: string;
  character: Character | null;
  onClose: () => void;
}) {
  const [name, setName] = useState(c?.name ?? "");
  const [aliases, setAliases] = useState(c?.aliases.join(", ") ?? "");
  const [description, setDescription] = useState(c?.description ?? "");
  const [animeIDs, setAnimeIDs] = useState(c?.anime_ids ?? []);
  const [anime, setAnime] = useState("");
  const [favorite, setFavorite] = useState(c?.favorite ?? false);
  const [visibility, setVisibility] = useState(c?.visibility ?? "private");
  const refresh = useMemoryRefresh(userID);
  const refs = useQuery({
    queryKey: ["memory", userID, "anime-references", animeIDs],
    queryFn: ({ signal }) =>
      result(
        client.POST("/api/v1/memory/references", {
          signal,
          body: {
            items: animeIDs.map((id) => ({ kind: "anime" as const, id })),
          },
        }),
      ),
  });
  const save = useMutation({
    mutationFn: () => {
      const body: components["schemas"]["CharacterInput"] = {
        version: c?.version ?? 0,
        name,
        aliases: aliases
          .split(/[,，]/)
          .map((s) => s.trim())
          .filter(Boolean),
        description,
        anime_ids: animeIDs,
        favorite,
        visibility,
      };
      return c
        ? result(
            client.PUT("/api/v1/memory/characters/{id}", {
              params: { path: { id: c.id } },
              body,
            }),
          )
        : result(client.POST("/api/v1/memory/characters", { body }));
    },
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return (
    <Dialog title={c ? "编辑角色" : "记住一个角色"} onClose={onClose} wide>
      <form
        className="memory-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={save.isPending}>
          <label>
            角色名字
            <Input
              data-initial-focus
              value={name}
              required
              maxLength={160}
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            别名（逗号分隔）
            <Input
              value={aliases}
              maxLength={2000}
              onChange={(e) => setAliases(e.target.value)}
            />
          </label>
          <label>
            关于这个角色
            <textarea
              value={description}
              rows={6}
              maxLength={10000}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <AnimeSelect userID={userID} value={anime} onChange={setAnime} />
          <Button
            type="button"
            className="button secondary"
            disabled={!anime || animeIDs.includes(anime)}
            onClick={() => {
              setAnimeIDs([...animeIDs, anime]);
              setAnime("");
            }}
          >
            关联这部作品
          </Button>
          <p className="muted">已关联 {animeIDs.length} 部作品</p>
          {animeIDs.length > 0 && (
            <details>
              <summary>调整已有作品关联</summary>
              {animeIDs.map((id, index) => (
                <div key={id}>
                  <a href={`/memory?anime_id=${id}#notes`}>
                    {refs.data?.items.find((v) => v.id === id)?.title ||
                      `关联作品 ${index + 1}`}{" "}
                    的记忆
                  </a>
                  <Button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      setAnimeIDs(animeIDs.filter((v) => v !== id))
                    }
                  >
                    移除关联
                  </Button>
                </div>
              ))}
            </details>
          )}
          <Visibility value={visibility} onChange={setVisibility} />
          <label className="memory-check">
            <Input
              type="checkbox"
              checked={favorite}
              onChange={(e) => setFavorite(e.target.checked)}
            />
            收藏这个角色
          </label>
        </fieldset>
        {save.error && <Problem error={save.error} />}
        <div className="memory-actions">
          <Button type="button" className="button secondary" onClick={onClose}>
            取消
          </Button>
          <Button className="button primary" disabled={save.isPending}>
            保存角色
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
export function IdentityMerge({
  userID,
  kind,
  resource,
  onClose,
}: {
  userID: string;
  kind: "character" | "episode";
  resource: { id: string; version: number; anime_id?: string };
  onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const [target, setTarget] = useState("");
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "merge", kind, resource.anime_id, search],
    queryFn: async ({ signal }) =>
      kind === "character"
        ? (
            await result(
              client.GET("/api/v1/memory/characters", {
                signal,
                params: { query: { search } },
              }),
            )
          ).items.map((c) => ({
            id: c.id,
            title: c.name,
            redirect: c.redirect_id,
          }))
        : (
            await result(
              client.GET("/api/v1/memory/episodes", {
                signal,
                params: { query: { search, anime_id: resource.anime_id } },
              }),
            )
          ).items.map((e) => ({
            id: e.id,
            title: e.title,
            redirect: e.redirect_id,
          })),
  });
  const merge = useMutation({
    mutationFn: () =>
      result(
        client.POST("/api/v1/memory/identities/{kind}/{id}", {
          params: { path: { kind, id: resource.id } },
          body: { version: resource.version, target_id: target },
        }),
      ),
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return (
    <Dialog title="归并重复身份" onClose={onClose}>
      <div className="memory-form">
        <p>
          原身份和历史引用会保留，通过重定向归入所选身份。不会删除笔记，也不会合并观看事实；之后可以拆分恢复。
        </p>
        <label>
          查找目标
          <Input value={search} onChange={(e) => setSearch(e.target.value)} />
        </label>
        <label>
          归并到
          <select
            aria-label="归并到"
            value={target}
            onChange={(e) => setTarget(e.target.value)}
          >
            <option value="">请选择同一个角色或集数</option>
            {q.data
              ?.filter((v) => v.id !== resource.id && !v.redirect)
              .map((v) => (
                <option key={v.id} value={v.id}>
                  {v.title}
                </option>
              ))}
          </select>
        </label>
        {(q.error || merge.error) && <Problem error={q.error || merge.error} />}
        <div className="memory-actions">
          <Button className="button secondary" onClick={onClose}>
            取消
          </Button>
          <Button
            className="button primary"
            disabled={!target || merge.isPending}
            onClick={() => merge.mutate()}
          >
            确认归并
          </Button>
        </div>
      </div>
    </Dialog>
  );
}
