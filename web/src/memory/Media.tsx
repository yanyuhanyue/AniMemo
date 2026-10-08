import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import { Button } from "../components/ui/Button";
import { Dialog } from "../components/ui/Dialog";
import { Pager, Problem } from "../public/Public";
import { MemoryImage, useMemoryRefresh } from "./shared";
export function MemoryMediaManager({ userID }: { userID: string }) {
  const [page, setPage] = useState(1);
  const [remove, setRemove] = useState("");
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "media", page],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/media", {
          signal,
          params: { query: { page } },
        }),
      ),
  });
  const del = useMutation({
    mutationFn: () =>
      result(
        client.DELETE("/api/v1/memory/media/{id}", {
          params: { path: { id: remove } },
        }),
      ),
    onSuccess: async () => {
      setRemove("");
      await refresh();
    },
  });
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">KEEP WHAT MATTERS</p>
          <h2>记忆图片</h2>
          <p>原图属于你的记忆库；删除会同步影响札记、年度册与分享。</p>
        </div>
      </header>
      {q.isPending ? (
        <p role="status">读取图片…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : (
        <>
          <p className="muted">
            已用 {(q.data.used_bytes / 1024 / 1024).toFixed(1)} / 100
            MiB。图片在编写札记或瞬间时上传。
          </p>
          <div className="memory-gallery">
            {q.data.items
              .filter((m) => m.state === "ready")
              .map((m) => (
                <article className="memory-note-card" key={m.id}>
                  <MemoryImage id={m.id} small />
                  <p>
                    {m.width} × {m.height} · {(m.byte_size / 1024).toFixed(0)}{" "}
                    KiB
                  </p>
                  <Button
                    className="text-button danger-text"
                    onClick={() => setRemove(m.id)}
                  >
                    删除原件
                  </Button>
                </article>
              ))}
          </div>
          <Pager
            page={page}
            total={q.data.total}
            size={q.data.page_size}
            onChange={setPage}
          />
        </>
      )}
      {remove && (
        <Dialog title="删除图片原件" onClose={() => setRemove("")}>
          <div className="memory-form">
            <p>
              这张图片的原图和缩略图会删除，所有记忆及分享立即失去图片访问，文字仍然保留。仅从某页移出图片请回到记忆编辑。
            </p>
            <div className="memory-actions">
              <Button
                className="button secondary"
                onClick={() => setRemove("")}
              >
                保留图片
              </Button>
              <Button
                className="button danger"
                disabled={del.isPending}
                onClick={() => del.mutate()}
              >
                确认删除原件
              </Button>
            </div>
            {del.error && <Problem error={del.error} />}
          </div>
        </Dialog>
      )}
    </>
  );
}
