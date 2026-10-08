import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Pager, Problem } from "../public/Public";
import {
  MemoryImage,
  ShareMemory,
  Visibility,
  useMemoryRefresh,
  type Yearly,
} from "./shared";
export function Yearlies({ userID }: { userID: string }) {
  const [editor, setEditor] = useState<Yearly | null | undefined>();
  const [reading, setReading] = useState("");
  const [share, setShare] = useState("");
  const [revision, setRevision] = useState(0);
  const q = useQuery({
    queryKey: ["memory", userID, "yearly"],
    queryFn: ({ signal }) =>
      result(client.GET("/api/v1/memory/yearly", { signal })),
  });
  const detail = useQuery({
    queryKey: ["memory", userID, "yearly", reading],
    enabled: !!reading,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/yearly/{id}", {
          signal,
          params: { path: { id: reading } },
        }),
      ),
  });
  const shown =
    detail.data?.revisions.find((r) => r.revision === revision) ??
    detail.data?.revisions[0];
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">A YEAR, IN YOUR OWN WORDS</p>
          <h2>年度记忆</h2>
          <p>选几页最想留下的回忆，为这一年写一段序言。</p>
        </div>
        <Button className="button primary" onClick={() => setEditor(null)}>
          装订一本年度册
        </Button>
      </header>
      {q.isPending ? (
        <p role="status">正在打开年度记忆…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : (
        <div className="memory-year-grid">
          {q.data.items.map((y) => (
            <article className="memory-year-card" key={y.id}>
              <span className="yearbook-label">ANIMEMO · 年度记忆册</span>
              <span className="memory-year-number">{y.year}</span>
              <h3>{y.title}</h3>
              <p className="memory-excerpt">{y.introduction}</p>
              <Button
                className="button secondary"
                onClick={() => {
                  setReading(y.id);
                  setRevision(0);
                }}
              >
                翻开这一年
              </Button>
            </article>
          ))}
          {!q.data.items.length && (
            <p className="memory-empty">把散落的记忆，慢慢装订成一年。</p>
          )}
        </div>
      )}
      {reading && (
        <Dialog
          title={detail.data?.title ?? "年度记忆"}
          onClose={() => setReading("")}
          wide
          className="yearbook-reader"
        >
          <div className="memory-reading">
            {detail.error ? (
              <Problem error={detail.error} />
            ) : shown && detail.data ? (
              <>
                <p className="memory-meta">
                  {detail.data.year} · {detail.data.timezone} · 截止{" "}
                  {new Date(shown.cutoff).toLocaleString("zh-CN")}
                </p>
                <label>
                  保存的版本
                  <select
                    aria-label="保存的版本"
                    value={shown.revision}
                    onChange={(e) => setRevision(Number(e.target.value))}
                  >
                    {detail.data.revisions.map((r) => (
                      <option key={r.id} value={r.revision}>
                        第 {r.revision} 版 ·{" "}
                        {new Date(r.created_at).toLocaleDateString("zh-CN")}
                      </option>
                    ))}
                  </select>
                </label>
                <h3>{shown.title}</h3>
                <p className="memory-prose">{shown.introduction}</p>
                <div className="memory-year-stats">
                  <span>
                    <strong>{shown.stats.anime}</strong>部作品
                  </span>
                  <span>
                    <strong>{shown.stats.watch_records}</strong>次记录
                  </span>
                  <span>
                    <strong>{shown.stats.notes}</strong>页记忆
                  </span>
                </div>
                <p className="muted">
                  统计按保存的日历年份计算，不确定日期保持原精度；未知年份不计入。此版选材、文案和统计已经冻结。
                </p>
                <YearlyItems items={shown.items} />
                <div className="memory-actions">
                  <Button
                    className="button primary"
                    onClick={() => {
                      setEditor(detail.data!);
                      setReading("");
                    }}
                  >
                    编辑并保存新一版
                  </Button>
                  <Button
                    className="button secondary"
                    onClick={() => {
                      setShare(reading);
                      setReading("");
                    }}
                  >
                    分享年度记忆
                  </Button>
                </div>
              </>
            ) : (
              <p role="status">正在翻开…</p>
            )}
          </div>
        </Dialog>
      )}
      {editor !== undefined && (
        <YearlyEditor
          userID={userID}
          yearly={editor}
          onClose={() => setEditor(undefined)}
        />
      )}{" "}
      {share && (
        <ShareMemory kind="yearly" id={share} onClose={() => setShare("")} />
      )}
    </>
  );
}
function YearlyEditor({
  userID,
  yearly: y,
  onClose,
}: {
  userID: string;
  yearly: Yearly | null;
  onClose: () => void;
}) {
  const [year, setYear] = useState(y?.year ?? new Date().getFullYear());
  const [timezone, setTimezone] = useState(
    y?.timezone ?? Intl.DateTimeFormat().resolvedOptions().timeZone,
  );
  const [title, setTitle] = useState(y?.title ?? "");
  const [introduction, setIntroduction] = useState(y?.introduction ?? "");
  const [visibility, setVisibility] = useState(y?.visibility ?? "private");
  const [selected, setSelected] = useState<string[]>(
    y?.revisions[0]?.items.map((n) => n.note_id) ?? [],
  );
  const [allDates, setAllDates] = useState(false);
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "yearly-picker", year, allDates, search, page],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/notes", {
          signal,
          params: {
            query: { search, page, year: allDates ? undefined : String(year) },
          },
        }),
      ),
  });
  const save = useMutation({
    mutationFn: () => {
      const body: components["schemas"]["YearlyInput"] = {
        version: y?.version ?? 0,
        year,
        timezone,
        title,
        introduction,
        visibility,
        note_ids: selected,
      };
      return y
        ? result(
            client.PUT("/api/v1/memory/yearly/{id}", {
              params: { path: { id: y.id } },
              body,
            }),
          )
        : result(client.POST("/api/v1/memory/yearly", { body }));
    },
    onSuccess: async () => {
      await refresh();
      onClose();
    },
  });
  return (
    <Dialog title="装订年度记忆" onClose={onClose} wide>
      <form
        className="memory-form"
        onSubmit={(e) => {
          e.preventDefault();
          save.mutate();
        }}
      >
        <fieldset disabled={save.isPending}>
          <div className="memory-form-grid">
            <label>
              年份
              <Input
                type="number"
                required
                min={1900}
                max={2100}
                value={year}
                disabled={!!y}
                onChange={(e) => {
                  setYear(Number(e.target.value));
                  setPage(1);
                }}
              />
            </label>
            <label>
              时区
              <Input
                required
                value={timezone}
                disabled={!!y}
                maxLength={80}
                onChange={(e) => setTimezone(e.target.value)}
              />
            </label>
          </div>
          <label>
            年度册名称
            <Input
              data-initial-focus
              value={title}
              required
              maxLength={160}
              placeholder={`${year} · 属于我的故事`}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label>
            写给这一年的序言
            <textarea
              value={introduction}
              rows={5}
              maxLength={10000}
              onChange={(e) => setIntroduction(e.target.value)}
            />
          </label>
          <Visibility value={visibility} onChange={setVisibility} />
          <p className="muted">
            分享只展示装订时及当前都允许分享的来源；之后公开的私人笔记需重新装订一版。删除原件后，年度册里也不会继续展示图片。
          </p>
          <label className="memory-check">
            <Input
              type="checkbox"
              checked={allDates}
              onChange={(e) => {
                setAllDates(e.target.checked);
                setPage(1);
              }}
            />
            显示所有日期的选材（含不记日期的记忆）
          </label>
          <label>
            查找选材
            <Input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
            />
          </label>
          <p>按勾选顺序选材 · 已选 {selected.length} 项</p>
          <div className="memory-picker-results">
            {q.data?.items.map((n) => (
              <label className="memory-check" key={n.id}>
                <Input
                  type="checkbox"
                  checked={selected.includes(n.id)}
                  onChange={(e) =>
                    setSelected((prev) =>
                      e.target.checked
                        ? [...prev, n.id]
                        : prev.filter((id) => id !== n.id),
                    )
                  }
                />
                {n.title} · {n.occurred_on}
              </label>
            ))}
          </div>
          {q.data && (
            <Pager
              page={page}
              total={q.data.total}
              size={q.data.page_size}
              onChange={setPage}
            />
          )}
          <Button
            type="button"
            className="text-button"
            onClick={() => setSelected([])}
          >
            清空选材并重新排列
          </Button>
        </fieldset>
        {(q.error || save.error) && <Problem error={q.error || save.error} />}
        <Button className="button primary" disabled={save.isPending}>
          {y ? "保存为新一版" : "保存这一年的记忆"}
        </Button>
      </form>
    </Dialog>
  );
}
export function YearlyItems({
  items,
  mediaURL,
}: {
  items: components["schemas"]["YearlyItem"][];
  mediaURL?: (id: string) => string;
}) {
  return (
    <div className="memory-year-items">
      {items.map((n, index) => {
        const content = (
          <>
            <p className="memory-prose">{n.body}</p>
            <div className="memory-reading-photos">
              {n.media_ids.map((id) =>
                mediaURL ? (
                  <img
                    key={id}
                    src={mediaURL(id)}
                    alt="记忆图片"
                    loading="lazy"
                  />
                ) : (
                  <MemoryImage key={id} id={id} />
                ),
              )}
            </div>
            {n.unavailable && (
              <p className="muted">原图已删除，保留当时的文字。</p>
            )}
          </>
        );
        return (
          <article key={n.note_id || index}>
            <p className="memory-meta">{n.occurred_on || "不记日期"}</p>
            <h3>{n.title}</h3>
            {n.spoiler ? (
              <details>
                <summary>展开剧透内容</summary>
                {content}
              </details>
            ) : (
              content
            )}
          </article>
        );
      })}
    </div>
  );
}
