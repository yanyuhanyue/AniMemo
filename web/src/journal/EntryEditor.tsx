import { Button } from "../components/ui/Button";
import { Input } from "../components/ui/Input";
import { useMutation } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { client, errorMessage, result } from "../api/client";
import type { Accent, Entry, Status } from "../api/client";
import { Dialog } from "../components/ui/Dialog";
import { Icon } from "../components/ui/Icon";
import { accentLabels, formatLabels, statusLabels, statuses } from "./labels";
import type { components } from "../api/schema";
import { SourcePicker, type SourceChoice } from "./SourcePicker";
import { TagField } from "./ManageTools";

type EntryInput = components["schemas"]["CreateEntry"];
function readEntryForm(data: FormData): EntryInput {
  return {
    title: String(data.get("title")).trim(),
    original_title: String(data.get("original_title")).trim(),
    format: String(data.get("format")) as Entry["format"],
    status: String(data.get("status")) as Status,
    airing_state: String(data.get("airing_state")) as Entry["airing_state"],
    total_episodes: Number(data.get("total_episodes")),
    score: Number(data.get("score")) || null,
    notes: String(data.get("notes")).trim(),
    tags: String(data.get("tags")).split(/[,，]/).map(tag => tag.trim()).filter(Boolean),
    accent: String(data.get("accent")) as Accent,
    visibility: String(data.get("visibility")) as Entry["visibility"],
    details: {
      studio: String(data.get("studio")).trim(),
      airing_period: String(data.get("airing_period")).trim(),
      description: String(data.get("description")).trim(),
      reference_url: String(data.get("reference_url")).trim(),
    },
  };
}

export function EntryEditor({
  entry,
  userID,
  onClose,
  onSaved,
}: {
  entry: Entry | null;
  userID: string;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [draft, setDraft] = useState<EntryInput | null>(null);
  const [source, setSource] = useState<SourceChoice | null>(null);
  const [picking, setPicking] = useState(false);
  const [tagText, setTagText] = useState(entry?.tags.join('，') || '');
  const values = draft ?? entry;
  const save = useMutation({
    mutationFn: async (data: FormData) => {
      const body = readEntryForm(data);
      if (!entry && source) return result(client.POST("/api/v1/entries/from-bangumi", {
        body: { subject_id: source.preview.metadata.subject_id, snapshot: source.preview.snapshot, fields: [], cover: source.cover, entry: body },
      }));
      return entry
        ? result(
            client.PATCH("/api/v1/entries/{id}", {
              params: { path: { id: entry.id } },
              body: { ...body, score: body.score ?? 0, version: entry.version },
            }),
          )
        : result(
            client.POST("/api/v1/entries", {
              body,
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
  function openPicker() {
    setDraft(readEntryForm(new FormData(form.current!)));
    setPicking(true);
  }
  function choose(choice: SourceChoice) {
    const next: EntryInput = { ...draft!, details: { ...draft?.details } };
    const metadata = choice.preview.metadata;
    for (const field of ["title", "original_title", "format", "total_episodes"] as const) {
      if (choice.fields.includes(field)) Object.assign(next, { [field]: metadata[field] });
    }
    for (const field of ["studio", "airing_period", "description", "reference_url"] as const) {
      if (choice.fields.includes(field)) Object.assign(next.details!, { [field]: metadata.details[field] });
    }
    setDraft(next);
    setSource(choice);
    setPicking(false);
  }
  const personalFields = <div className="form-row personal-fields">
    <label>我的评分
      <Input name="score" type="number" min="0" max="10" step="0.1" defaultValue={values?.score || ""} placeholder="未评分" data-initial-focus={entry ? true : undefined} />
      <span className="field-hint">1–10 分；留空或填 0 表示未评分。</span>
    </label>
    <label>观看情况
      <select name="status" aria-label="观看情况" defaultValue={values?.status || "recorded"}>
        {statuses.filter(value => value !== "planned" || values?.status === "planned").map(value => <option key={value} value={value}>{statusLabels[value]}</option>)}
      </select>
    </label>
  </div>;
  if (picking) return <SourcePicker entry={null} initialQuery={draft?.title} onClose={() => setPicking(false)} onSaved={onSaved} onPick={choose} />;
  return (
    <Dialog
      title={entry ? "编辑番剧记录" : "加入番剧"}
      onClose={onClose}
      wide
      className={`entry-form-dialog${entry ? " entry-editor" : ""}`}
    >
      <p className="editor-introduction muted">
        {entry
          ? "只修改想补充的内容，其他资料会保留。"
          : "搜索 Bangumi 带入资料，也可以只填名称保存。"}
      </p>
      <form
        ref={form}
        className="editor-form"
        onSubmit={submit}
        onInvalidCapture={(event) => {
          const details = (event.target as HTMLElement).closest("details");
          if (details) details.open = true;
        }}
      >
        <fieldset disabled={save.isPending}>
          {source && <section className="selected-source" aria-label="已选择的 Bangumi 作品">
            {source.cover && <img src={"/api/v1/providers/bangumi/subjects/" + source.preview.metadata.subject_id + "/cover"} alt="" />}
            <div><span className="source-provider-label">已填入 Bangumi 资料</span><strong>{source.preview.metadata.title}</strong><span>{source.cover ? "封面会随记录一起保存" : "不使用 Bangumi 封面"}</span><div className="selected-source-actions"><Button type="button" className="text-button" onClick={openPicker}>更换作品</Button><Button type="button" className="text-button" onClick={() => setSource(null)}>取消绑定</Button></div></div>
          </section>}
          <div className="entry-title-row"><label>
            番剧名称 <span className="required">*</span>
            <Input
              name="title"
              placeholder="例如：夏目友人帐"
              defaultValue={values?.title}
              required
              maxLength={160}
              data-initial-focus={!entry ? true : undefined}
            />
          </label>{!entry && !source && <Button type="button" className="button secondary source-start" onClick={openPicker}><Icon name="search" />搜索 Bangumi</Button>}</div>
          {entry && personalFields}
          <label>
            留一点感想 <span className="optional">选填</span>
            <textarea
              name="notes"
              defaultValue={values?.notes}
              placeholder="记下你的感想，也可以以后再补。"
              maxLength={4000}
              rows={3}
            />
          </label>
          <TagField userID={userID} value={tagText} onChange={setTagText} />
          <details className="extra-fields">
            <summary>{entry ? "作品资料" : "补充评分、状态与作品资料"}</summary>
            {!entry && <p className="field-hint">
              不填写细节也能保存；选择“看过”不会生成观看日期或逐集记录。
            </p>}
            {!entry && personalFields}
            <label>
              原名 <span className="optional">选填</span>
              <Input
                name="original_title"
                defaultValue={values?.original_title}
                maxLength={160}
              />
            </label>
            <div className="form-row">
              <label>
                类型
                <select name="format" defaultValue={values?.format || "tv"}>
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
                  defaultValue={values?.total_episodes || ""}
                  placeholder="不知道可留空"
                />
              </label>
            </div>
            <label>
              作品播出情况
              <select
                name="airing_state"
                defaultValue={values?.airing_state || "unknown"}
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
                  defaultValue={values?.details?.studio}
                  maxLength={120}
                />
              </label>
              <label>
                播出时期
                <Input
                  name="airing_period"
                  defaultValue={values?.details?.airing_period}
                  maxLength={50}
                />
              </label>
            </div>
            <label>
              作品简介
              <textarea
                name="description"
                defaultValue={values?.details?.description}
                maxLength={8000}
                rows={3}
              />
            </label>
            <label>
              资料链接
              <Input
                name="reference_url"
                type="url"
                defaultValue={values?.details?.reference_url}
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
                defaultValue={values?.visibility || "private"}
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
                      defaultChecked={value === (values?.accent || "violet")}
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
          <p className="field-hint">
            {!entry
              ? "默认仅自己可见。没有填写的观看细节会留空。"
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
