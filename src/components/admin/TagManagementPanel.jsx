import { useCallback, useEffect, useState } from "react";
import { api, readableApiError } from "../../lib/api.js";
import { TAG_COLOR_OPTIONS, tagColorStyle } from "../../lib/tagPresets.js";
import { Icon } from "../Icon.jsx";

const EMPTY_TAG = {
  name: "",
  color: "slate",
  is_quick_preset: true,
  sort_order: 0,
};

function TagToggle({ checked, onChange, label }) {
  return (
    <label className="admin-tag-toggle">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span aria-hidden="true"><i /></span>
      <strong>{label}</strong>
    </label>
  );
}

function ColorSelect({ value, onChange, name }) {
  return (
    <label className="admin-tag-color-field">
      <span className="admin-tag-color-swatch" style={tagColorStyle(name, value)} aria-hidden="true" />
      <select value={value} onChange={(event) => onChange(event.target.value)} aria-label={`${name || "新标签"}颜色`}>
        {TAG_COLOR_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
      </select>
    </label>
  );
}

export function TagManagementPanel({ onNotice, onError }) {
  const [tags, setTags] = useState([]);
  const [draft, setDraft] = useState(EMPTY_TAG);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await api.get("staff/tags/");
      setTags(Array.isArray(data?.results) ? data.results : []);
    } catch (error) {
      onError?.(readableApiError(error, "公共标签加载失败。"));
    } finally {
      setLoading(false);
    }
  }, [onError]);

  useEffect(() => {
    load();
  }, [load]);

  const changeTag = (id, field, value) => {
    setTags((current) => current.map((item) => item.id === id ? { ...item, [field]: value } : item));
  };

  const createTag = async (event) => {
    event.preventDefault();
    if (busyId || !draft.name.trim()) return;
    setBusyId("new");
    onError?.("");
    try {
      const { data } = await api.post("staff/tags/", { ...draft, name: draft.name.trim(), sort_order: Number(draft.sort_order) || 0 });
      setTags((current) => [...current, data].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id));
      setDraft(EMPTY_TAG);
      onNotice?.(`已创建公共标签「${data.name}」`);
    } catch (error) {
      onError?.(readableApiError(error, "公共标签创建失败。"));
    } finally {
      setBusyId(null);
    }
  };

  const saveTag = async (item) => {
    if (busyId || !item.name.trim()) return;
    setBusyId(item.id);
    onError?.("");
    try {
      const { data } = await api.patch(`staff/tags/${item.id}/`, {
        name: item.name.trim(),
        color: item.color,
        is_quick_preset: item.is_quick_preset,
        sort_order: Number(item.sort_order) || 0,
      });
      setTags((current) => current.map((tag) => tag.id === item.id ? data : tag).sort((a, b) => a.sort_order - b.sort_order || a.id - b.id));
      onNotice?.(`已保存标签「${data.name}」`);
    } catch (error) {
      onError?.(readableApiError(error, "公共标签保存失败。"));
    } finally {
      setBusyId(null);
    }
  };

  const deleteTag = async (item) => {
    if (busyId || !window.confirm(`确定删除公共标签「${item.name}」吗？\n\n已有番剧记录中的同名标签会保留，只删除后台定义与快捷预设。`)) return;
    setBusyId(item.id);
    onError?.("");
    try {
      await api.delete(`staff/tags/${item.id}/`);
      setTags((current) => current.filter((tag) => tag.id !== item.id));
      onNotice?.(`已删除公共标签「${item.name}」`);
    } catch (error) {
      onError?.(readableApiError(error, "公共标签删除失败。"));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <section className="admin-panel admin-panel--full admin-tag-panel">
      <header>
        <div><span>PUBLIC TAG DIRECTORY</span><h3>公共标签与快捷预设</h3></div>
        <strong>{tags.length} 个定义</strong>
      </header>

      <form className="admin-tag-create" onSubmit={createTag}>
        <label><span>标签名称</span><input value={draft.name} maxLength={40} onChange={(event) => setDraft((current) => ({ ...current, name: event.target.value }))} placeholder="例如：科幻" /></label>
        <label><span>默认颜色</span><ColorSelect value={draft.color} name={draft.name} onChange={(color) => setDraft((current) => ({ ...current, color }))} /></label>
        <label><span>排序</span><input type="number" min="0" max="65535" value={draft.sort_order} onChange={(event) => setDraft((current) => ({ ...current, sort_order: event.target.value }))} /></label>
        <TagToggle checked={draft.is_quick_preset} onChange={(is_quick_preset) => setDraft((current) => ({ ...current, is_quick_preset }))} label="加入快捷预设" />
        <button type="submit" disabled={busyId === "new" || !draft.name.trim()}><Icon name="plus" /> {busyId === "new" ? "正在创建" : "添加标签"}</button>
      </form>

      {loading ? <div className="admin-dashboard-loading"><span /><span /><span /> 正在读取标签</div> : tags.length ? (
        <div className="admin-tag-list">
          <div className="admin-tag-list__head" aria-hidden="true"><span>预览</span><span>名称</span><span>默认颜色</span><span>排序</span><span>快捷预设</span><span>操作</span></div>
          {tags.map((item) => <article className="admin-tag-row" key={item.id}>
            <span className="admin-tag-preview" style={tagColorStyle(item.name, item.color)}>{item.name || "未命名"}</span>
            <input value={item.name} maxLength={40} onChange={(event) => changeTag(item.id, "name", event.target.value)} aria-label={`标签 ${item.name} 名称`} />
            <ColorSelect value={item.color} name={item.name} onChange={(color) => changeTag(item.id, "color", color)} />
            <input type="number" min="0" max="65535" value={item.sort_order} onChange={(event) => changeTag(item.id, "sort_order", event.target.value)} aria-label={`${item.name}排序`} />
            <TagToggle checked={item.is_quick_preset} onChange={(checked) => changeTag(item.id, "is_quick_preset", checked)} label={item.is_quick_preset ? "已加入" : "不加入"} />
            <div className="admin-tag-actions"><button type="button" onClick={() => saveTag(item)} disabled={busyId === item.id}><Icon name="save" /> 保存</button><button type="button" className="is-delete" onClick={() => deleteTag(item)} disabled={busyId === item.id}><Icon name="trash" /> 删除</button></div>
          </article>)}
        </div>
      ) : <div className="admin-empty-state"><Icon name="tags" /><span>还没有公共标签，可以从上方创建。</span></div>}
    </section>
  );
}
