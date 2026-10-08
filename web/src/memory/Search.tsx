import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import { Input } from "../components/ui/Input";
import { Button } from "../components/ui/Button";
import { Dialog } from "../components/ui/Dialog";
import { Pager, Problem } from "../public/Public";
import { NoteEditor } from "./Notes";
import { MemoryImage, type Note } from "./shared";
const kinds = {
  note: "札记",
  moment: "瞬间",
  anime: "作品",
  character: "角色",
  watch: "观看记录",
  progress: "进度快照",
  collection: "收藏夹",
  yearly: "年度记忆",
};
export function MemorySearch({
  userID,
  anime = "",
  character = "",
}: {
  userID: string;
  anime?: string;
  character?: string;
}) {
  const [search, setSearch] = useState("");
  const [year, setYear] = useState("");
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const [note, setNote] = useState("");
  const q = useQuery({
    queryKey: [
      "memory",
      userID,
      "search",
      search,
      year,
      kind,
      status,
      anime,
      character,
      page,
    ],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/search", {
          signal,
          params: {
            query: {
              search,
              year,
              kind,
              status,
              anime_id: anime,
              character_id: character,
              page,
            },
          },
        }),
      ),
  });
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">FOLLOW THE THREAD OF A MEMORY</p>
          <h2>{anime || character ? "把故事串起来" : "找回一段记忆"}</h2>
          <p>
            作品、札记、角色与观看片段，在同一条时间线上重逢。没有日期的记忆仍然保留。
          </p>
        </div>
      </header>
      <div className="memory-filters">
        <label>
          搜索全部记忆
          <Input
            type="search"
            maxLength={160}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(1);
            }}
            placeholder="标题、正文、别名或标签"
          />
        </label>
        <label>
          内容类型
          <select
            aria-label="内容类型"
            value={kind}
            onChange={(e) => {
              setKind(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部类型</option>
            {Object.entries(kinds).map(([k, v]) => (
              <option value={k} key={k}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label>
          记忆年份
          <Input
            type="number"
            min={1900}
            max={2100}
            value={year}
            onChange={(e) => {
              setYear(e.target.value);
              setPage(1);
            }}
          />
        </label>
        <label>
          作品状态
          <select
            aria-label="作品状态"
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
          >
            <option value="">全部状态</option>
            <option value="recorded">看过，细节未记</option>
            <option value="planned">想看（已有分类）</option>
            <option value="watching">记录过部分观看</option>
            <option value="completed">看完</option>
            <option value="caught_up">当时看到已播部分</option>
            <option value="on_hold">搁置</option>
            <option value="dropped">弃番</option>
          </select>
        </label>
      </div>
      {q.error ? (
        <Problem error={q.error} />
      ) : q.isPending ? (
        <p role="status">正在寻找记忆…</p>
      ) : (
        <>
          <ol className="memory-thread">
            {q.data.items.map((v) => (
              <li key={v.kind + v.id}>
                <div className="memory-thread-date">
                  {v.occurred_on || "不记日期"}
                  <small>{kinds[v.kind as keyof typeof kinds] ?? v.kind}</small>
                </div>
                <article>
                  <h3>
                    {v.kind === "note" || v.kind === "moment" ? (
                      <Button
                        className="memory-title-button"
                        onClick={() => setNote(v.id)}
                      >
                        {v.title}
                      </Button>
                    ) : (
                      <a
                        href={
                          v.kind === "character"
                            ? `/memory?character_id=${v.id}#search`
                            : v.anime_id
                              ? `/memory?anime_id=${v.anime_id}#search`
                              : v.kind === "collection"
                                ? "/memory#collections"
                                : "/memory#yearly"
                        }
                      >
                        {v.title}
                      </a>
                    )}
                  </h3>
                  {v.spoiler ? (
                    <details>
                      <summary>展开剧透摘录</summary>
                      <p className="memory-excerpt">{v.excerpt}</p>
                    </details>
                  ) : (
                    <p className="memory-excerpt">{v.excerpt}</p>
                  )}
                </article>
              </li>
            ))}
          </ol>
          {!q.data.items.length && (
            <p className="memory-empty">
              暂时没有匹配的记忆，试试减少筛选条件。
            </p>
          )}
          <Pager
            page={page}
            total={q.data.total}
            size={q.data.page_size}
            onChange={setPage}
          />
        </>
      )}
      {note && (
        <SearchNote userID={userID} id={note} onClose={() => setNote("")} />
      )}
    </>
  );
}
function SearchNote({
  userID,
  id,
  onClose,
}: {
  userID: string;
  id: string;
  onClose: () => void;
}) {
  const [edit, setEdit] = useState(false);
  const q = useQuery({
    queryKey: ["memory", userID, "note", id],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/notes/{id}", {
          signal,
          params: { path: { id } },
        }),
      ),
  });
  if (edit && q.data)
    return (
      <NoteEditor
        userID={userID}
        note={q.data}
        kind={q.data.kind}
        onClose={onClose}
      />
    );
  return (
    <Dialog title={q.data?.title ?? "正在打开记忆"} onClose={onClose} wide>
      {q.error ? (
        <Problem error={q.error} />
      ) : q.data ? (
        <div className="memory-reading">
          {q.data.spoiler ? (
            <details>
              <summary>展开剧透内容</summary>
              <SearchNoteBody note={q.data} />
            </details>
          ) : (
            <SearchNoteBody note={q.data} />
          )}
          <Button className="button secondary" onClick={() => setEdit(true)}>
            编辑记忆
          </Button>
        </div>
      ) : (
        <p role="status">正在读取…</p>
      )}
    </Dialog>
  );
}
function SearchNoteBody({ note }: { note: Note }) {
  return (
    <>
      <p className="memory-meta">{note.occurred_on || "不记日期"}</p>
      <p className="memory-prose">{note.body}</p>
      {note.anchor.quote && <blockquote>{note.anchor.quote}</blockquote>}
      <div className="memory-reading-photos">
        {note.media_ids.map((id) => (
          <MemoryImage key={id} id={id} />
        ))}
      </div>
    </>
  );
}
