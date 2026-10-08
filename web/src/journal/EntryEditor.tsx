import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { useMutation } from "@tanstack/react-query";
import type { FormEvent } from "react";
import { client, errorMessage, result } from "../api/client";
import type { Accent, Entry, Status } from "../api/client";
import { Dialog } from "../components/ui/Dialog";
import { Icon } from "../components/ui/Icon";
import { accentLabels, formatLabels, statusLabels, statuses } from "./labels";

export function EntryEditor({
  entry,
  onClose,
  onSaved,
}: {
  entry: Entry | null;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const save = useMutation({
    mutationFn: async (data: FormData) => {
      const score = Number(data.get("score"));
      const body = {
        title: String(data.get("title")).trim(),
        original_title: String(data.get("original_title")).trim(),
        format: String(data.get("format")) as Entry["format"],
        status: String(data.get("status")) as Status,
        airing_state: String(data.get("airing_state")) as Entry["airing_state"],
        total_episodes: Number(data.get("total_episodes")),
        notes: String(data.get("notes")).trim(),
        tags: String(data.get("tags"))
          .split(/[,，]/)
          .map((tag) => tag.trim())
          .filter(Boolean),
        accent: String(data.get("accent")) as Accent,
        visibility: String(data.get("visibility")) as Entry["visibility"],
        details: {
          studio: String(data.get("studio")).trim(),
          airing_period: String(data.get("airing_period")).trim(),
          description: String(data.get("description")).trim(),
          reference_url: String(data.get("reference_url")).trim(),
        },
      };
      return entry
        ? result(
            client.PATCH("/api/v1/entries/{id}", {
              params: { path: { id: entry.id } },
              body: { ...body, score, version: entry.version },
            }),
          )
        : result(
            client.POST("/api/v1/entries", {
              body: { ...body, score: score || null },
            }),
          );
    },
    onSuccess: () =>
      onSaved(entry ? "番剧记录已更新" : "这部看过的番，已经记下了"),
  });
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    save.mutate(new FormData(event.currentTarget));
  }
  return (
    <Dialog
      title={entry ? "编辑番剧记录" : "记下一部看过的番"}
      eyebrow="KEEP YOUR ANIME MEMORIES"
      onClose={onClose}
    >
      <p className="editor-introduction muted">
        {entry
          ? "只修改想补充的内容，其他资料会保留。"
          : "只记得名字也可以。日期、集数和评分，都可以以后再补。"}
      </p>
      <form
        className="editor-form"
        onSubmit={submit}
        onInvalidCapture={(event) => {
          const details = (event.target as HTMLElement).closest("details");
          if (details) details.open = true;
        }}
      >
        <fieldset disabled={save.isPending}>
          <label>
            番剧名称 <span className="required">*</span>
            <Input
              name="title"
              placeholder="例如：夏目友人帐"
              defaultValue={entry?.title}
              required
              maxLength={160}
              data-initial-focus
            />
          </label>
          <label>
            留一点感想 <span className="optional">选填</span>
            <textarea
              name="notes"
              defaultValue={entry?.notes}
              placeholder="高中时看过，已经不记得哪一天，但还记得那时的心情。"
              maxLength={4000}
              rows={4}
            />
          </label>
          <details className="extra-fields">
            <summary>补充评分、状态与作品资料</summary>
            <p className="field-hint">
              不填写细节也能保存；“看过，细节未记”不会生成观看日期或逐集记录。
            </p>
            <div className="form-row">
              <label>
                观看情况
                <select
                  name="status"
                  aria-label="观看情况"
                  defaultValue={entry?.status || "recorded"}
                >
                  {statuses.filter(value => value !== "planned" || entry?.status === "planned").map((value) => (
                    <option key={value} value={value}>
                      {statusLabels[value]}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                我的评分
                <Input
                  name="score"
                  type="number"
                  min="0"
                  max="10"
                  step="0.1"
                  defaultValue={entry?.score || ""}
                  placeholder="未评分"
                />
                <span className="field-hint">
                  1–10 分；留空或填 0 表示未评分。
                </span>
              </label>
            </div>
            <label>
              标签 <span className="optional">选填</span>
              <Input
                name="tags"
                defaultValue={entry?.tags.join("，")}
                placeholder="治愈，夏天，学生时代"
                maxLength={200}
              />
              <span className="field-hint">用逗号分隔，最多 8 个标签。</span>
            </label>
            <label>
              原名 <span className="optional">选填</span>
              <Input
                name="original_title"
                defaultValue={entry?.original_title}
                maxLength={160}
              />
            </label>
            <div className="form-row">
              <label>
                类型
                <select name="format" defaultValue={entry?.format || "tv"}>
                  {Object.entries(formatLabels).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                总话数
                <Input
                  name="total_episodes"
                  type="number"
                  min="0"
                  max="10000"
                  step="1"
                  defaultValue={entry?.total_episodes || ""}
                  placeholder="不知道可留空"
                />
              </label>
            </div>
            <label>
              作品播出情况
              <select
                name="airing_state"
                defaultValue={entry?.airing_state || "unknown"}
              >
                <option value="unknown">不确定</option>
                <option value="airing">尚未完结</option>
                <option value="finished">已完结</option>
              </select>
            </label>
            <div className="form-row">
              <label>
                制作公司
                <Input
                  name="studio"
                  defaultValue={entry?.details.studio}
                  maxLength={120}
                />
              </label>
              <label>
                播出时期
                <Input
                  name="airing_period"
                  defaultValue={entry?.details.airing_period}
                  maxLength={50}
                />
              </label>
            </div>
            <label>
              作品简介
              <textarea
                name="description"
                defaultValue={entry?.details.description}
                maxLength={8000}
                rows={3}
              />
            </label>
            <label>
              资料链接
              <Input
                name="reference_url"
                type="url"
                defaultValue={entry?.details.reference_url}
                maxLength={1000}
                placeholder="https://…"
              />
            </label>
          </details>
          <details className="extra-fields">
            <summary>可见性与封面颜色</summary>
            <label>
              可见性
              <select
                name="visibility"
                defaultValue={entry?.visibility || "private"}
              >
                <option value="private">私密 · 仅自己</option>
                <option value="unlisted">持链接可见</option>
                <option value="public">公开 · 可列入公开手账</option>
              </select>
            </label>
            <p className="field-hint">
              分享包含作品资料、评分、短评、标签和封面。观看日期与逐次记录始终保密；分享前需在账号设置中开启分享。
            </p>
            <div className="palette-field">
              <span className="field-label">封面颜色</span>
              <div className="palette">
                {Object.entries(accentLabels).map(([value, label]) => (
                  <label
                    className="color-option"
                    data-accent={value}
                    key={value}
                    title={label}
                  >
                    <Input
                      name="accent"
                      type="radio"
                      value={value}
                      defaultChecked={value === (entry?.accent || "violet")}
                    />
                    <span>
                      <Icon name="check" />
                    </span>
                    <span className="sr-only">{label}</span>
                  </label>
                ))}
              </div>
            </div>
          </details>
          {!entry && (
            <p className="field-hint">
              保存后，可在作品详情搜索 Bangumi 资料或上传封面。
            </p>
          )}
          <p className="field-hint">
            {!entry
              ? "新记录默认仅自己可见，可在上方调整。记不清的细节可以一直留空。"
              : "可见性沿用原设置，可在上方调整。"}
          </p>
        </fieldset>
        {save.isError && (
          <p className="error-message" role="alert">
            {errorMessage(save.error)}
          </p>
        )}
        <footer className="dialog-footer">
          <Button type="button" className="button quiet" onClick={onClose}>
            取消
          </Button>
          <Button className="button primary" disabled={save.isPending}>
            {save.isPending ? "正在保存…" : entry ? "保存修改" : "保存记录"}
            <Icon name="check" />
          </Button>
        </footer>
      </form>
    </Dialog>
  );
}
