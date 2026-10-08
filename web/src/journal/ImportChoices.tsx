import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Pager, Problem } from "../public/Public";
import { watchDate } from "./labels";
type Choice = components["schemas"]["ImportChoice"];
export type ImportSelection = components["schemas"]["ImportSelection"];
export function ImportChoices({
  userID,
  choices,
  onChange,
}: {
  userID: string;
  choices: Choice[];
  onChange: (selection: ImportSelection[]) => void;
}) {
  const [page, setPage] = useState(1);
  const [selection, setSelection] = useState<ImportSelection[]>(() =>
    choices
      .filter((c) => c.default_selected)
      .map((c) => ({
        index: c.index,
        records: c.history.map((_, i) => i),
        target_id: "",
        target_version: 0,
      })),
  );
  useEffect(() => onChange(selection), [selection, onChange]);
  function update(index: number, value: ImportSelection | null) {
    setSelection((prev) =>
      value
        ? [...prev.filter((v) => v.index !== index), value].sort(
            (a, b) => a.index - b.index,
          )
        : prev.filter((v) => v.index !== index),
    );
  }
  return (
    <section className="import-choices">
      <h4>
        逐项核对 · 已选择 {selection.length} / {choices.length} 部
      </h4>
      <p className="muted">
        可取消单条记录，或明确选择已有作品追加。追加保留原评分、文字和封面；日期、精度、话数、刷次与笔记全部相同的记录会跳过。预览本身不写入手账。
      </p>
      {choices.slice((page - 1) * 10, page * 10).map((c) => (
        <ImportChoiceRow
          key={c.index}
          userID={userID}
          choice={c}
          value={selection.find((v) => v.index === c.index)}
          onChange={(v) => update(c.index, v)}
        />
      ))}
      <Pager page={page} size={10} total={choices.length} onChange={setPage} />
    </section>
  );
}
function ImportChoiceRow({
  userID,
  choice: c,
  value,
  onChange,
}: {
  userID: string;
  choice: Choice;
  value: ImportSelection | undefined;
  onChange: (v: ImportSelection | null) => void;
}) {
  const [search, setSearch] = useState(c.title);
  const [page, setPage] = useState(1);
  const q = useQuery({
    queryKey: ["journal", userID, "import-match", search],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/entries", {
          signal,
          params: { query: { search, page_size: 50 } },
        }),
      ),
  });
  const targets = [...c.candidates];
  for (const e of q.data?.items ?? [])
    if (!targets.some((t) => t.id === e.id)) targets.push(e);
  const updateRecords = (records: number[]) =>
    value && onChange({ ...value, records });
  return (
    <article className="import-choice">
      <h4>{c.title}</h4>
      <label>
        查找可追加的已有作品
        <Input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </label>
      <label>
        本项处理方式
        <select
          aria-label="本项处理方式"
          value={value ? value.target_id || "new" : "skip"}
          onChange={(e) => {
            const v = e.target.value;
            if (v === "skip") {
              onChange(null);
              return;
            }
            const target = targets.find((t) => t.id === v);
            onChange({
              index: c.index,
              records: value?.records ?? c.history.map((_, i) => i),
              target_id: target?.id ?? "",
              target_version: target?.version ?? 0,
            });
          }}
        >
          <option value="skip">跳过这一项</option>
          {c.default_selected && <option value="new">建立新作品</option>}
          {targets.map((t) => (
            <option key={t.id} value={t.id}>
              追加至：{t.title}（版本 {t.version}）
            </option>
          ))}
        </select>
      </label>
      {!c.default_selected && (
        <p className="muted">
          检测到重名或重复资料来源，默认跳过；确认是同一作品后再选择追加。
        </p>
      )}
      {q.error && <Problem error={q.error} />}
      {c.history.length > 0 && (
        <details>
          <summary>
            核对 {c.history.length} 条观看记录 · 已选{" "}
            {value?.records.length ?? 0} 条
          </summary>
          {value && (
            <div className="memory-actions">
              <Button
                type="button"
                className="button quiet"
                onClick={() => updateRecords(c.history.map((_, i) => i))}
              >
                选择本项全部记录
              </Button>
              <Button
                type="button"
                className="button quiet"
                onClick={() => updateRecords([])}
              >
                清空本项选择
              </Button>
            </div>
          )}
          <ul className="import-records">
            {c.history.slice((page - 1) * 30, page * 30).map((r, at) => {
              const i = (page - 1) * 30 + at;
              return (
                <li key={i}>
                  <label>
                    <Input
                      type="checkbox"
                      disabled={!value}
                      checked={value?.records.includes(i) ?? false}
                      onChange={(e) =>
                        updateRecords(
                          e.target.checked
                            ? [...(value?.records ?? []), i].sort(
                                (a, b) => a - b,
                              )
                            : (value?.records ?? []).filter((v) => v !== i),
                        )
                      }
                    />
                    {watchDate(r.watched_on, r.time_precision)} · 第{" "}
                    {r.episode_from}–{r.episode_to} 话 · 第 {r.rewatch} 刷
                  </label>
                  {r.source_line > 0 && (
                    <small>
                      来源：{r.source_filename} · 第 {r.source_line} 行
                    </small>
                  )}
                  {r.note && <p>{r.note}</p>}
                </li>
              );
            })}
          </ul>
          <Pager
            page={page}
            size={30}
            total={c.history.length}
            onChange={setPage}
          />
        </details>
      )}
    </article>
  );
}
