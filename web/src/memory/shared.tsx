import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { client, result } from "../api/client";
import type { components } from "../api/schema";
import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { Dialog } from "../components/ui/Dialog";
import { Problem } from "../public/Public";
export type Note = components["schemas"]["MemoryNote"];
export type Character = components["schemas"]["Character"];
export type Episode = components["schemas"]["Episode"];
export type Collection = components["schemas"]["MemoryCollection"];
export type Yearly = components["schemas"]["YearlyMemory"];
export const visibilityLabels = {
  private: "仅自己",
  unlisted: "仅链接可见",
  public: "公开",
};
export function Visibility({
  value,
  onChange,
}: {
  value: keyof typeof visibilityLabels;
  onChange: (v: keyof typeof visibilityLabels) => void;
}) {
  return (
    <label>
      可见性
      <select
        aria-label="可见性"
        value={value}
        onChange={(e) => onChange(e.target.value as typeof value)}
      >
        {Object.entries(visibilityLabels).map(([key, label]) => (
          <option value={key} key={key}>
            {label}
          </option>
        ))}
      </select>
      <small>公开分享仍需在账号设置中开启；关联的私人内容不会自动公开。</small>
    </label>
  );
}
export function useMemoryRefresh(userID: string) {
  const cache = useQueryClient();
  return () => cache.invalidateQueries({ queryKey: ["memory", userID] });
}
export function useAnimeOptions(userID: string, search = "") {
  return useQuery({
    queryKey: ["memory", userID, "anime-options", search],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/entries", {
          signal,
          params: { query: { search, page_size: 100 } },
        }),
      ),
  });
}
export function AnimeSelect({
  userID,
  value,
  onChange,
  required = false,
}: {
  userID: string;
  value: string;
  onChange: (id: string) => void;
  required?: boolean;
}) {
  const [search, setSearch] = useState("");
  const q = useAnimeOptions(userID, search);
  return (
    <div className="memory-resource-picker">
      <label>
        查找关联作品
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="输入作品名称"
        />
      </label>
      <label>
        关联作品
        <select
          aria-label="关联作品"
          value={value}
          required={required}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">{required ? "请选择作品" : "不关联作品"}</option>
          {value && !q.data?.items.some((e) => e.anime_id === value) && (
            <option value={value}>已关联的本地作品</option>
          )}
          {q.data?.items.map((e) => (
            <option key={e.id} value={e.anime_id}>
              {e.title}
            </option>
          ))}
        </select>
      </label>
      {q.error && <Problem error={q.error} />}
    </div>
  );
}
export function MemoryImage({
  id,
  caption = "记忆图片",
  small = false,
}: {
  id: string;
  caption?: string;
  small?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  return failed ? (
    <span className="memory-image-missing">
      原图已删除或暂时无法读取
      <Button
        type="button"
        className="text-button"
        onClick={() => {
          setFailed(false);
          setAttempt((n) => n + 1);
        }}
      >
        重试读取
      </Button>
    </span>
  ) : (
    <img
      className={small ? "memory-thumbnail" : "memory-photo"}
      src={`/api/v1/memory/media/${id}?thumbnail=${small}&retry=${attempt}`}
      alt={caption}
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}
export function ShareMemory({
  kind,
  id,
  onClose,
}: {
  kind: components["schemas"]["MemoryShareInput"]["kind"];
  id: string;
  onClose: () => void;
}) {
  const [days, setDays] = useState(7);
  const share = useMutation({
    mutationFn: (revoke: boolean) =>
      result(
        client.POST("/api/v1/memory/shares", {
          body: { kind, resource_id: id, days, revoke },
        }),
      ),
  });
  return (
    <Dialog title="分享这份记忆" onClose={onClose}>
      <div className="memory-form">
        <p>
          新链接会立即替换旧链接。来源转为私密、删除原图或关闭账号分享后，对应内容不再展示。
        </p>
        <label>
          有效天数
          <Input
            type="number"
            min={1}
            max={90}
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
          />
        </label>
        <div className="memory-actions">
          <Button
            className="button primary"
            disabled={share.isPending}
            onClick={() => share.mutate(false)}
          >
            生成或轮换链接
          </Button>
          <Button
            className="button secondary"
            disabled={share.isPending}
            onClick={() => share.mutate(true)}
          >
            撤销链接
          </Button>
        </div>
        {share.error && <Problem error={share.error} />}{" "}
        {share.data &&
          (share.data.token ? (
            <label>
              分享地址
              <Input
                readOnly
                value={`${location.origin}/memory-share/${share.data.token}`}
                onFocus={(e) => e.currentTarget.select()}
              />
              <a
                href={`/memory-share/${share.data.token}`}
                target="_blank"
                rel="noreferrer"
              >
                打开分享预览 ↗
              </a>
            </label>
          ) : (
            <p role="status">链接已撤销。</p>
          ))}
      </div>
    </Dialog>
  );
}
export function MemoryRevisions({
  userID,
  id,
}: {
  userID: string;
  id: string;
}) {
  const [open, setOpen] = useState(false);
  const q = useQuery({
    queryKey: ["memory", userID, "revisions", id],
    enabled: open,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/revisions/{id}", {
          signal,
          params: { path: { id } },
        }),
      ),
  });
  return (
    <details
      className="memory-revisions"
      onToggle={(e) => setOpen(e.currentTarget.open)}
    >
      <summary>查看记忆修订</summary>
      {q.error ? (
        <Problem error={q.error} />
      ) : (
        q.data?.items.map((r) => (
          <article key={r.id}>
            <time>{new Date(r.recorded_at).toLocaleString("zh-CN")}</time>
            <span>
              {" "}
              ·{" "}
              {(
                {
                  created: "创建",
                  changed: "修改",
                  removed: "移除",
                  merged: "归并",
                  split: "拆分",
                  asserted: "进度快照",
                } as Record<string, string>
              )[r.action] || r.action}
            </span>
            <details>
              <summary>当时的内容</summary>
              <RevisionContents snapshot={r.snapshot} />
            </details>
          </article>
        ))
      )}
    </details>
  );
}

function RevisionContents({
  snapshot: s,
}: {
  snapshot: Record<string, unknown>;
}) {
  const text = (key: string) =>
    typeof s[key] === "string" ? String(s[key]) : "";
  const title = text("title") || text("name");
  const body = text("body") || text("description") || text("notes");
  return (
    <div className="memory-revision-content">
      {title && <h4>{title}</h4>}
      {body && <p className="memory-prose">{body}</p>}
      {text("occurred_on") && <p>记忆日期：{text("occurred_on")}</p>}
      {text("visibility") && (
        <p>
          当时可见性：
          {visibilityLabels[
            text("visibility") as keyof typeof visibilityLabels
          ] ?? text("visibility")}
        </p>
      )}
      {Array.isArray(s.tags) && (
        <p>标签：{s.tags.filter((v) => typeof v === "string").join("、")}</p>
      )}
      {text("redirect_id") && (
        <p>此修订将身份归入另一个本地身份，历史引用仍保留。</p>
      )}
      {!title && !body && (
        <p>已保存本次关系或状态变更。完整记录随个人备份导出。</p>
      )}
    </div>
  );
}
