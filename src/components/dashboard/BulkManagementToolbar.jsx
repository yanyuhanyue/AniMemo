import { useState } from "react";
import { Icon } from "../Icon.jsx";

export function BulkManagementToolbar({ active, selectedCount, tags, busy, onToggle, onClear, onApply }) {
  const [operation, setOperation] = useState("status");
  const [value, setValue] = useState("watching");

  const changeOperation = (next) => {
    setOperation(next);
    setValue(next === "status" ? "watching" : next === "visibility" ? "private" : (tags[0] || ""));
  };

  return (
    <section className={`bulk-management${active ? " is-active" : ""}`} aria-label="批量管理">
      <button className="bulk-management__toggle" type="button" onClick={onToggle} aria-pressed={active}><Icon name="table" /> {active ? "退出多选" : "批量管理"}</button>
      {active && <>
        <strong>已选 {selectedCount} 部</strong>
        <label>操作<select value={operation} onChange={(event) => changeOperation(event.target.value)} disabled={busy}>
          <option value="status">修改状态</option><option value="tag-add">添加标签</option><option value="tag-remove">移除标签</option><option value="visibility">修改可见性</option>
        </select></label>
        {operation === "status" ? <label>状态<select value={value} onChange={(event) => setValue(event.target.value)} disabled={busy}><option value="planned">想看</option><option value="watching">在看</option><option value="completed">看过</option><option value="on_hold">搁置</option><option value="dropped">弃番</option></select></label>
          : operation === "visibility" ? <label>可见性<select value={value} onChange={(event) => setValue(event.target.value)} disabled={busy}><option value="private">私人</option><option value="unlisted">链接可见</option><option value="public">公开</option></select></label>
            : <label>标签<input list="bulk-tag-options" value={value} onChange={(event) => setValue(event.target.value)} disabled={busy} placeholder="输入标签" /><datalist id="bulk-tag-options">{tags.map((tag) => <option value={tag} key={tag} />)}</datalist></label>}
        <button type="button" className="is-apply" onClick={() => onApply(operation, value.trim())} disabled={!selectedCount || !value.trim() || busy}><Icon name={busy ? "spinner" : "check"} spin={busy} /> {busy ? "处理中" : "应用"}</button>
        <button type="button" onClick={onClear} disabled={!selectedCount || busy}><Icon name="reset" /> 清空选择</button>
      </>}
    </section>
  );
}
