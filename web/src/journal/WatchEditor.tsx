import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { useState } from "react";
import type { FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { client, errorMessage, result } from "../api/client";
import type { Entry, WatchRecord } from "../api/client";
import { Dialog } from "../components/ui/Dialog";
import { Icon } from "../components/ui/Icon";

export function WatchEditor({
  entry,
  record,
  onClose,
  onSaved,
}: {
  entry: Entry;
  record?: WatchRecord;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const [precision, setPrecision] = useState<WatchRecord["time_precision"]>(
    record?.time_precision || "unknown",
  );
  const [requestID] = useState(() => crypto.randomUUID());
  const [confirmDelete, setConfirmDelete] = useState(false);
  const save = useMutation({
    mutationFn: async (data: FormData) => {
      const body = {
        watched_on:
          precision === "unknown" ? "" : String(data.get("watched_on")),
        time_precision: precision,
        episode_from: Number(data.get("episode_from")),
        episode_to: Number(data.get("episode_to")),
        note: String(data.get("note")).trim(),
        request_id: requestID,
        rewatch: Number(data.get("rewatch")),
      };
      if (record) {
        const { request_id: _requestID, ...patch } = body;
        return result(
          client.PATCH("/api/v1/entries/{id}/history/{record}", {
            params: { path: { id: entry.id, record: record.id } },
            body: { ...patch, version: record.version },
          }),
        );
      }
      return result(
        client.POST("/api/v1/entries/{id}/history", {
          params: { path: { id: entry.id } },
          body,
        }),
      );
    },
    onSuccess: () =>
      onSaved(
        record ? "观看记录已更正，进度已重新计算" : "这次观看，已经记下了",
      ),
  });
  const remove = useMutation({
    mutationFn: () =>
      result(
        client.DELETE("/api/v1/entries/{id}/history/{record}", {
          params: {
            path: { id: entry.id, record: record!.id },
            query: { version: record!.version },
          },
        }),
      ),
    onSuccess: () => onSaved("观看记录已删除，进度已重新计算"),
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate(new FormData(event.currentTarget));
  }
  return (
    <Dialog
      title={record ? "编辑观看记录" : "记一次观看"}
      eyebrow="SAVE THIS MOMENT"
      onClose={onClose}
    >
      <div className="watching-title" data-accent={entry.accent}>
        <span className="watching-icon">
          <Icon name="play" />
        </span>
        <div>
          <strong>{entry.title}</strong>
          <span>
            已记录到第 {entry.watched_episodes} 话
            {entry.total_episodes > 0 ? ` / 共 ${entry.total_episodes} 话` : ""}
          </span>
        </div>
      </div>
      <form className="editor-form" onSubmit={submit}>
        <fieldset disabled={save.isPending || remove.isPending}>
          <label>
            日期记得多清楚
            <select
              aria-label="日期记得多清楚"
              value={precision}
              onChange={(e) =>
                setPrecision(e.target.value as WatchRecord["time_precision"])
              }
            >
              <option value="day">记得具体日期</option>
              <option value="month">只记得月份</option>
              <option value="year">只记得年份</option>
              <option value="unknown">已经记不清了</option>
            </select>
          </label>
          {precision !== "unknown" && (
            <label>
              观看日期
              <Input
                key={precision}
                name="watched_on"
                type={
                  precision === "day"
                    ? "date"
                    : precision === "month"
                      ? "month"
                      : "number"
                }
                defaultValue={(record?.watched_on || "").slice(
                  0,
                  precision === "year" ? 4 : precision === "month" ? 7 : 10,
                )}
                required
                data-initial-focus
              />
            </label>
          )}
          <p className="field-hint">
            日期记不清可以留空；下方只填写确实记得的观看范围。
          </p>
          <div className="form-row">
            <label>
              从第几话
              <Input
                name="episode_from"
                type="number"
                min="1"
                max={entry.total_episodes || 10000}
                step="1"
                defaultValue={record?.episode_from ?? ""}
                required
              />
            </label>
            <label>
              看到第几话
              <Input
                name="episode_to"
                type="number"
                min="1"
                max={entry.total_episodes || 10000}
                step="1"
                defaultValue={record?.episode_to ?? ""}
                required
              />
            </label>
          </div>
          <label>
            第几次观看
            <Input
              name="rewatch"
              type="number"
              min="1"
              max="1000"
              step="1"
              defaultValue={record?.rewatch ?? ""}
              required
            />
            <span className="field-hint">1 为首刷，2 为二刷。记不清时，可以只留一份回忆。</span>
          </label>
          <label>
            此刻的感想 <span className="optional">选填</span>
            <textarea
              name="note"
              defaultValue={record?.note}
              placeholder="记下这次观看的感受。集数记不清时，也可以回到作品卡片“留点回忆”。"
              maxLength={2000}
              rows={4}
            />
          </label>
          <p className="form-note">
            {record
              ? "更正或删除后，会根据剩余观看记录重新计算进度。"
              : "进度会更新到看过的最远一话，重看不会减少进度。"}
          </p>
        </fieldset>
        {(save.error || remove.error) && (
          <p className="error-message" role="alert">
            {errorMessage(save.error || remove.error)}
          </p>
        )}
        {record && (
          <div className="history-delete">
            <Button
              type="button"
              className="text-button danger-text"
              disabled={save.isPending || remove.isPending}
              onClick={() => setConfirmDelete(true)}
            >
              删除这条观看记录
            </Button>
            {confirmDelete && (
              <div className="delete-confirm">
                <p>删除后无法撤销，番剧会保留。</p>
                <Button
                  type="button"
                  className="button quiet"
                  onClick={() => setConfirmDelete(false)}
                >
                  保留记录
                </Button>
                <Button
                  type="button"
                  className="button danger"
                  disabled={save.isPending || remove.isPending}
                  onClick={() => remove.mutate()}
                >
                  确认删除观看记录
                </Button>
              </div>
            )}
          </div>
        )}
        <footer className="dialog-footer">
          <Button type="button" className="button quiet" onClick={onClose}>
            取消
          </Button>
          <Button
            className="button primary"
            disabled={save.isPending || remove.isPending}
          >
            {save.isPending
              ? "正在记录…"
              : record
                ? "保存观看修改"
                : "保存这次观看"}
            <Icon name="check" />
          </Button>
        </footer>
      </form>
    </Dialog>
  );
}
