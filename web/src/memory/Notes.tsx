import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ApiError, client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Icon } from "../components/ui/Icon";
import { Pager, Problem } from "../public/Public";
import { TagField } from "../journal/ManageTools";
import {
  AnimeSelect,
  MemoryImage,
  MemoryRevisions,
  ShareMemory,
  Visibility,
  useMemoryRefresh,
  useAnimeReference,
  visibilityLabels,
  type Note,
} from "./shared";
type NoteInput = components["schemas"]["MemoryNoteInput"];
function initialNote(
  note: Note | null,
  kind: "note" | "moment",
  anime: string,
): NoteInput {
  return {
    version: note?.version ?? 0,
    kind: note?.kind ?? kind,
    title: note?.title ?? "",
    body: note?.body ?? "",
    anime_id: note?.anime_id ?? anime,
    character_id: note?.character_id ?? "",
    episode_id: note?.episode_id ?? "",
    watch_id: note?.watch_id ?? "",
    anchor: note?.anchor ?? {
      kind: "",
      quote: "",
      scene: "",
      timestamp_seconds: null,
    },
    media_ids: note?.media_ids ?? [],
    tags: note?.tags ?? [],
    occurred_on: note?.occurred_on ?? "",
    time_precision: note?.time_precision ?? "unknown",
    visibility: note?.visibility ?? "private",
    spoiler: note?.spoiler ?? false,
    highlight: note?.highlight ?? false,
  };
}
export function NoteEditor({
  userID,
  note,
  kind,
  anime = "",
  animeTitle,
  onClose,
}: {
  userID: string;
  note: Note | null;
  kind: "note" | "moment";
  anime?: string;
  animeTitle?: string;
  onClose: () => void;
}) {
  const [tagText, setTagText] = useState(note?.tags.join(", ") ?? "");
  const [form, setForm] = useState(() => ({
    ...initialNote(note, kind, anime),
    ...(!note && animeTitle ? { title: animeTitle } : {}),
  }));
  const [extras, setExtras] = useState(false);
  const linkedAnime = useAnimeReference(userID, form.anime_id ?? "");
  const linkedTitle = linkedAnime.data?.available ? linkedAnime.data.title : form.anime_id === anime ? animeTitle : undefined;
  const [references, setReferences] = useState(false);
  const [characterSearch, setCharacterSearch] = useState("");
  const [episodeSearch, setEpisodeSearch] = useState("");
  const [watchSearch, setWatchSearch] = useState("");
  const [watchPage, setWatchPage] = useState(1);
  const refresh = useMemoryRefresh(userID);
  const set = <K extends keyof NoteInput>(key: K, value: NoteInput[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }));
  const characters = useQuery({
    queryKey: ["memory", userID, "character-picker", characterSearch],
    enabled: extras && references,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/characters", {
          signal,
          params: { query: { search: characterSearch } },
        }),
      ),
  });
  const episodes = useQuery({
    queryKey: [
      "memory",
      userID,
      "episode-picker",
      form.anime_id,
      episodeSearch,
    ],
    enabled: extras && references && !!form.anime_id,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/episodes", {
          signal,
          params: { query: { anime_id: form.anime_id, search: episodeSearch } },
        }),
      ),
  });
  const watches = useQuery({
    queryKey: [
      "memory",
      userID,
      "watch-picker",
      form.anime_id,
      watchSearch,
      watchPage,
    ],
    enabled: extras && references && !!form.anime_id,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/search", {
          signal,
          params: {
            query: {
              anime_id: form.anime_id,
              kind: "watch",
              search: watchSearch,
              page: watchPage,
            },
          },
        }),
      ),
  });
  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      if (files.length + (form.media_ids?.length ?? 0) > 8)
        throw new ApiError(400, "validation_error", "每份记忆最多 8 张图片。");
      for (const file of files) {
        if (
          !["image/png", "image/jpeg"].includes(file.type) ||
          !file.size ||
          file.size > 2 * 1024 * 1024
        )
          throw new ApiError(
            400,
            "validation_error",
            "请选择不超过 2 MiB 的 JPG 或 PNG 图片。",
          );
      }
      for (const file of files) {
        const reservation = await result(
          client.POST("/api/v1/memory/media", {
            body: { byte_size: file.size },
          }),
        );
        const m = await result(
          client.PUT("/api/v1/memory/media/{id}", {
            params: { path: { id: reservation.id } },
            headers: { "Content-Type": file.type },
            body: "",
            bodySerializer: () => file,
          }),
        );
        setForm((prev) => ({
          ...prev,
          media_ids: [...(prev.media_ids ?? []), m.id],
        }));
      }
    },
  });
  const save = useMutation({
    mutationFn: () =>
      note
        ? result(
            client.PUT("/api/v1/memory/notes/{id}", {
              params: { path: { id: note.id } },
              body: {
                ...form,
                tags: tagText
                  .split(/[,，]/)
                  .map((s) => s.trim())
                  .filter(Boolean),
              },
            }),
          )
        : result(
            client.POST("/api/v1/memory/notes", {
              body: {
                ...form,
                tags: tagText
                  .split(/[,，]/)
                  .map((s) => s.trim())
                  .filter(Boolean),
              },
            }),
          ),
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return (
    <Dialog
      title={
        note
          ? "编辑这份记忆"
          : kind === "moment"
            ? "收藏一个瞬间"
            : "写下一页记忆"
      }
      onClose={onClose}
      wide
    >
      <form
        className="memory-form note-editor"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={save.isPending || upload.isPending}>
          <label>
            记忆标题
            <Input
              data-initial-focus={animeTitle ? undefined : true}
              value={form.title}
              maxLength={160}
              required
              onChange={(e) => set("title", e.target.value)}
              placeholder="那时的自己，想留下什么？"
            />
          </label>
          <label>
            记忆正文
            <textarea
              data-initial-focus={animeTitle ? true : undefined}
              value={form.body}
              maxLength={40000}
              rows={8}
              onChange={(e) => set("body", e.target.value)}
              placeholder="一个片段、一句台词，或那天的心情…"
            />
          </label>
          {form.anime_id && (
            <p className="field-hint">
              {linkedTitle ? `关联《${linkedTitle}》` : "已关联作品"} ·{" "}
              {visibilityLabels[form.visibility ?? "private"]}
              。不记得日期和集数，也能留下这份回忆。
            </p>
          )}
          <details
            className="memory-options"
            open={extras}
            onToggle={(e) => setExtras(e.currentTarget.open)}
            onInvalidCapture={(e) => {
              let parent = (e.target as HTMLElement).closest("details");
              while (parent) {
                parent.open = true;
                parent = parent.parentElement?.closest("details") ?? null;
              }
            }}
          >
            <summary>补充时间、图片与其他细节</summary>
            <div className="memory-form-grid">
              <label>
                日期精度
                <select
                  aria-label="日期精度"
                  value={form.time_precision}
                  onChange={(e) => {
                    set(
                      "time_precision",
                      e.target.value as NoteInput["time_precision"],
                    );
                    set("occurred_on", "");
                  }}
                >
                  <option value="unknown">记不清日期</option>
                  <option value="day">记得哪一天</option>
                  <option value="month">只记得月份</option>
                  <option value="year">只记得年份</option>
                  <option value="approximate">大约在某一天</option>
                </select>
              </label>
              {form.time_precision !== "unknown" && (
                <label>
                  记忆发生时间
                  <Input
                    type={
                      form.time_precision === "month"
                        ? "month"
                        : form.time_precision === "year"
                          ? "number"
                          : "date"
                    }
                    value={form.occurred_on}
                    required
                    min={form.time_precision === "year" ? 1900 : undefined}
                    max={form.time_precision === "year" ? 2100 : undefined}
                    onChange={(e) => set("occurred_on", e.target.value)}
                  />
                </label>
              )}
            </div>
            {extras && (
              <AnimeSelect
                userID={userID}
                value={form.anime_id ?? ""}
                onChange={(v) => {
                  set("anime_id", v);
                  set("episode_id", "");
                  set("watch_id", "");
                }}
              />
            )}
            <details onToggle={(e) => setReferences(e.currentTarget.open)}>
              <summary>关联角色、集数与定位</summary>
              <div className="memory-form-grid">
                <label>
                  查找角色
                  <Input
                    value={characterSearch}
                    onChange={(e) => setCharacterSearch(e.target.value)}
                  />
                </label>
                <label>
                  关联角色
                  <select
                    aria-label="关联角色"
                    value={form.character_id}
                    onChange={(e) => set("character_id", e.target.value)}
                  >
                    <option value="">不关联角色</option>
                    {form.character_id &&
                      !characters.data?.items.some(
                        (c) => c.id === form.character_id,
                      ) && (
                        <option value={form.character_id}>已关联角色</option>
                      )}
                    {characters.data?.items.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                        {c.redirect_id ? "（已归并）" : ""}
                      </option>
                    ))}
                  </select>
                </label>
                {form.anime_id && (
                  <>
                    <label>
                      查找集数
                      <Input
                        value={episodeSearch}
                        onChange={(e) => setEpisodeSearch(e.target.value)}
                      />
                    </label>
                    <label>
                      关联集数
                      <select
                        aria-label="关联集数"
                        value={form.episode_id}
                        onChange={(e) => set("episode_id", e.target.value)}
                      >
                        <option value="">不关联集数</option>
                        {form.episode_id &&
                          !episodes.data?.items.some(
                            (c) => c.id === form.episode_id,
                          ) && (
                            <option value={form.episode_id}>已关联集数</option>
                          )}
                        {episodes.data?.items.map((c) => (
                          <option key={c.id} value={c.id}>
                            {c.number} · {c.title}
                          </option>
                        ))}
                      </select>
                    </label>
                  </>
                )}
              </div>
              {form.anime_id && (
                <>
                  <label>
                    查找观看上下文
                    <Input
                      value={watchSearch}
                      onChange={(e) => {
                        setWatchSearch(e.target.value);
                        setWatchPage(1);
                      }}
                    />
                  </label>
                  <label>
                    关联观看记录
                    <select
                      aria-label="关联观看记录"
                      value={form.watch_id}
                      onChange={(e) => set("watch_id", e.target.value)}
                    >
                      <option value="">不关联观看记录</option>
                      {form.watch_id &&
                        !watches.data?.items.some(
                          (w) => w.id === form.watch_id,
                        ) && (
                          <option value={form.watch_id}>
                            {note?.watch_missing
                              ? "已撤回的原观看记录"
                              : "已关联的观看记录"}
                          </option>
                        )}
                      {watches.data?.items.map((w) => (
                        <option value={w.id} key={w.id}>
                          {w.occurred_on || "未知日期"} · {w.excerpt || w.title}
                        </option>
                      ))}
                    </select>
                  </label>
                  {watches.data && (
                    <Pager
                      page={watchPage}
                      total={watches.data.total}
                      size={watches.data.page_size}
                      onChange={setWatchPage}
                    />
                  )}
                  {watches.error && <Problem error={watches.error} />}
                </>
              )}
              <label>
                定位方式
                <select
                  aria-label="定位方式"
                  value={form.anchor?.kind}
                  onChange={(e) =>
                    set("anchor", {
                      ...form.anchor!,
                      kind: e.target.value as NonNullable<
                        NoteInput["anchor"]
                      >["kind"],
                    })
                  }
                >
                  <option value="">不添加定位</option>
                  <option value="quote">台词引用</option>
                  <option value="timestamp">播放时间点</option>
                  <option value="scene">场景</option>
                  <option value="episode">集数</option>
                  <option value="freeform">自由描述</option>
                </select>
              </label>
              {form.anchor?.kind === "timestamp" && (
                <label>
                  时间点（秒）
                  <Input
                    type="number"
                    min={0}
                    max={86400}
                    value={form.anchor.timestamp_seconds ?? ""}
                    onChange={(e) =>
                      set("anchor", {
                        ...form.anchor!,
                        timestamp_seconds:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  />
                </label>
              )}
              {form.anchor?.kind === "quote" && (
                <label>
                  引用台词
                  <textarea
                    value={form.anchor.quote}
                    maxLength={4000}
                    rows={3}
                    onChange={(e) =>
                      set("anchor", { ...form.anchor!, quote: e.target.value })
                    }
                  />
                </label>
              )}
              {form.anchor?.kind && (
                <label>
                  定位说明
                  <Input
                    value={form.anchor.scene}
                    maxLength={4000}
                    onChange={(e) =>
                      set("anchor", { ...form.anchor!, scene: e.target.value })
                    }
                  />
                </label>
              )}
              {note?.watch_missing && (
                <p className="muted">原观看记录已撤回，正文和原定位仍保留。</p>
              )}
              {(characters.error || episodes.error) && (
                <Problem error={characters.error || episodes.error} />
              )}
            </details>
            <label>
              记忆图片（最多 8 张，JPG / PNG，每张 ≤ 2 MiB）
              <Input
                type="file"
                accept="image/png,image/jpeg"
                multiple
                onChange={(e) => {
                  const files = Array.from(e.target.files ?? []);
                  e.target.value = "";
                  if (files.length) upload.mutate(files);
                }}
              />
            </label>
            <div className="memory-thumbnails">
              {form.media_ids?.map((id) => (
                <figure key={id}>
                  <MemoryImage id={id} small />
                  <Button
                    type="button"
                    className="text-button"
                    onClick={() =>
                      set(
                        "media_ids",
                        form.media_ids?.filter((v) => v !== id),
                      )
                    }
                  >
                    移出本页
                  </Button>
                </figure>
              ))}
            </div>
            <TagField userID={userID} value={tagText} onChange={setTagText} limit={20} maxLength={1600} />
            <Visibility
              value={form.visibility ?? "private"}
              onChange={(v) => set("visibility", v)}
            />
            <div className="memory-checks">
              <label>
                <Input
                  type="checkbox"
                  checked={form.spoiler}
                  onChange={(e) => set("spoiler", e.target.checked)}
                />
                包含剧透
              </label>
              <label>
                <Input
                  type="checkbox"
                  checked={form.highlight}
                  onChange={(e) => set("highlight", e.target.checked)}
                />
                标为珍藏
              </label>
            </div>
          </details>
        </fieldset>
        {(save.error || upload.error) && (
          <Problem error={save.error || upload.error} />
        )}
        <div className="memory-actions">
          <Button className="button secondary" type="button" onClick={onClose}>
            取消
          </Button>
          <Button
            className="button primary"
            disabled={save.isPending || upload.isPending}
          >
            {upload.isPending
              ? "正在上传图片…"
              : save.isPending
                ? "正在保存…"
                : "保存记忆"}
          </Button>
        </div>
      </form>
    </Dialog>
  );
}
export function Notes({
  userID,
  kind = "note",
  anime = "",
  animeTitle,
  character = "",
}: {
  userID: string;
  kind?: "note" | "moment";
  anime?: string;
  animeTitle?: string;
  character?: string;
}) {
  const [search, setSearch] = useState("");
  const [year, setYear] = useState("");
  const [highlight, setHighlight] = useState(false);
  const [page, setPage] = useState(1);
  const [editor, setEditor] = useState<Note | null | undefined>();
  const [opened, setOpened] = useState<Note | null>(null);
  const [sharing, setSharing] = useState("");
  const [confirm, setConfirm] = useState(false);
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: [
      "memory",
      userID,
      "notes",
      kind,
      anime,
      character,
      search,
      year,
      highlight,
      page,
    ],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/notes", {
          signal,
          params: {
            query: {
              kind,
              anime_id: anime,
              character_id: character,
              search,
              year,
              highlight,
              page,
            },
          },
        }),
      ),
  });
  const remove = useMutation({
    mutationFn: (n: Note) =>
      result(
        client.DELETE("/api/v1/memory/notes/{id}", {
          params: { path: { id: n.id }, query: { version: n.version } },
        }),
      ),
    onSuccess: async () => {
      setOpened(null);
      setConfirm(false);
      await refresh();
    },
  });
  return (
    <section className="notes-page">
      <header className="memory-section-heading">
        <div>
          <h2>{kind === "moment" ? "值得留住的瞬间" : "记忆札记"}</h2>
          <p>
            {kind === "moment"
              ? "把那一帧和当时的心情放在一起。"
              : "留下一段感想，也留住当时的自己。"}
          </p>
        </div>
        <Button className="button primary" onClick={() => setEditor(null)}>
          <Icon name="plus" />
          {kind === "moment" ? "收藏瞬间" : "写一页记忆"}
        </Button>
      </header>
      <div className="memory-filters notes-filters">
        <label className="notes-search">
          <span className="sr-only">查找记忆</span><Icon name="search" />
          <Input
            type="search"
            value={search}
            maxLength={160}
            placeholder="正文、标题或标签"
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
          />
        </label>
        <label className="notes-year">
          <span className="sr-only">年份</span>
          <Input
            type="number"
            min={1900}
            max={2100}
            value={year}
            placeholder="全部年份"
            onChange={(e) => {
              setYear(e.target.value);
              setPage(1);
            }}
          />
        </label>
        <label className="memory-check notes-highlight">
          <Input
            type="checkbox"
            checked={highlight}
            onChange={(e) => {
              setHighlight(e.target.checked);
              setPage(1);
            }}
          />
          <Icon name="star" />只看珍藏
        </label>
      </div>
      {q.isPending ? (
        <p role="status">正在翻开记忆…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : (
        <>
          <p className="notes-result-count">{q.data.total} {kind === "moment" ? "个瞬间" : "篇札记"}{(search || year || highlight) && " · 筛选结果"}</p>
          <div
            className={
              kind === "moment" ? "memory-gallery" : "memory-note-list"
            }
          >
            {q.data.items.map((n) => (
              <article className="memory-note-card" key={n.id}>
                {kind === "moment" && n.media_ids[0] && !n.spoiler && (
                  <MemoryImage id={n.media_ids[0]} small />
                )}
                <div>
                  <div className="note-card-meta"><span><Icon name="clock" />{n.occurred_on || "日期未记"}{n.time_precision === "approximate" ? " · 大约" : ""}</span>{n.highlight ? <span className="note-highlight"><Icon name="star" />珍藏</span> : <span>{visibilityLabels[n.visibility]}</span>}</div>
                  <h3>
                    <Button
                      className="memory-title-button"
                      onClick={() => {
                        setOpened(n);
                        setConfirm(false);
                      }}
                    >
                      {n.title}
                    </Button>
                  </h3>
                  {n.spoiler ? (
                    <p className="note-spoiler">包含剧透 · 打开后查看</p>
                  ) : (
                    <p className="memory-excerpt">
                      {n.body || "一帧画面，也是一段记忆。"}
                    </p>
                  )}
                  <div className="note-card-footer"><div className="entry-tags">
                    {n.tags.map((t) => (
                      <span key={t}>{t}</span>
                    ))}
                  </div>
                  <Button className="text-button note-read" aria-label={'阅读 ' + n.title} onClick={() => { setOpened(n); setConfirm(false); }}>阅读<Icon name="arrow" /></Button></div>
                </div>
              </article>
            ))}
          </div>
          {!q.data.items.length && (
            <div className="memory-empty">
              <Icon name="book" />
              <h3>{search || year || highlight ? "没有找到匹配的记忆" : animeTitle ? '写下关于《' + animeTitle + '》的第一份记忆' : "从一段想留下的回忆开始"}</h3>
              <p>{search || year || highlight ? "试试其他关键词，或清除筛选条件。" : "一句感想、一个片段就可以。日期和集数可以以后再补。"}</p>
              <Button className="button secondary" onClick={() => { if(search || year || highlight) { setSearch(''); setYear(''); setHighlight(false); setPage(1); } else setEditor(null); }}>{search || year || highlight ? "清除筛选" : "写下第一份记忆"}</Button>
            </div>
          )}
          {q.data.total > q.data.page_size && <Pager
            page={page}
            total={q.data.total}
            size={q.data.page_size}
            onChange={setPage}
          />}
        </>
      )}
      {editor !== undefined && (
        <NoteEditor
          userID={userID}
          note={editor}
          kind={kind}
          anime={anime}
          animeTitle={animeTitle}
          onClose={() => setEditor(undefined)}
        />
      )}
      {opened && (
        <Dialog title={opened.title} onClose={() => setOpened(null)} wide>
          <div className="memory-reading note-reading">
            <p className="memory-meta">
              {opened.occurred_on || "不记日期"} ·{" "}
              {visibilityLabels[opened.visibility]}
            </p>
            {opened.spoiler ? (
              <details>
                <summary>展开剧透内容</summary>
                <NoteBody note={opened} />
              </details>
            ) : (
              <NoteBody note={opened} />
            )}
            <MemoryRevisions userID={userID} id={opened.id} />
            <div className="memory-actions">
              <Button
                className="button secondary"
                onClick={() => {
                  setEditor(opened);
                  setOpened(null);
                }}
              >
                编辑记忆
              </Button>
              <Button
                className="button secondary"
                onClick={() => {
                  setSharing(opened.id);
                  setOpened(null);
                }}
              >
                分享
              </Button>
              <Button
                className="text-button danger-text"
                onClick={() => setConfirm(true)}
              >
                移出记忆库
              </Button>
            </div>
            {confirm && (
              <div className="memory-confirm">
                <p>
                  移出后不会出现在收藏与分享中，历史修订仍保留在完整备份里。原件可在「图片管理」单独删除。
                </p>
                <Button
                  className="button danger"
                  disabled={remove.isPending}
                  onClick={() => remove.mutate(opened)}
                >
                  确认移出
                </Button>
                <Button
                  className="button quiet"
                  onClick={() => setConfirm(false)}
                >
                  保留记忆
                </Button>
              </div>
            )}
            {remove.error && <Problem error={remove.error} />}
          </div>
        </Dialog>
      )}
      {sharing && (
        <ShareMemory kind="note" id={sharing} onClose={() => setSharing("")} />
      )}
    </section>
  );
}
function NoteBody({ note }: { note: Note }) {
  return (
    <>
      <p className="memory-prose">{note.body}</p>
      {note.anchor.quote && <blockquote>{note.anchor.quote}</blockquote>}
      {note.anchor.scene && <p className="muted">{note.anchor.scene}</p>}
      {note.anchor.timestamp_seconds !== null && (
        <p>
          定位：{Math.floor(note.anchor.timestamp_seconds / 60)}:
          {String(note.anchor.timestamp_seconds % 60).padStart(2, "0")}
        </p>
      )}
      <div className="memory-reading-photos">
        {note.media_ids.map((id) => (
          <MemoryImage key={id} id={id} />
        ))}
      </div>
    </>
  );
}
