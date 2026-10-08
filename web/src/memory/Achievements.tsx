import { useMutation, useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import { Button } from "../components/ui/Button";
import { Problem } from "../public/Public";
import { useMemoryRefresh } from "./shared";
export const badgeMarks = {
  spark: "✦",
  moon: "☾",
  orbit: "◎",
  flower: "✿",
  book: "▤",
};
export const metricNames = {
  watch_records: "观看记录",
  distinct_anime: "有观看记录的作品",
  completed_anime: "已看完作品",
  watched_episodes: "记录中的观看话数",
  memory_notes: "独立记忆",
};
export function Achievements({ userID }: { userID: string }) {
  const refresh = useMemoryRefresh(userID);
  const q = useQuery({
    queryKey: ["memory", userID, "achievements"],
    queryFn: ({ signal }) =>
      result(client.GET("/api/v1/memory/achievements", { signal })),
    refetchInterval: 10000,
  });
  const showcase = useMutation({
    mutationFn: (unlock_ids: string[]) =>
      result(
        client.PUT("/api/v1/memory/achievements/showcase", {
          body: { unlock_ids },
        }),
      ),
    onSuccess: refresh,
  });
  const acknowledge = useMutation({
    mutationFn: () =>
      result(client.POST("/api/v1/memory/achievements/acknowledge")),
    onSuccess: refresh,
  });
  const selected =
    q.data?.items
      .filter((a) => a.showcase_slot > 0 && a.granted)
      .sort((a, b) => a.showcase_slot - b.showcase_slot) ?? [];
  const unseen = q.data?.items.filter((a) => a.granted && !a.notified) ?? [];
  return (
    <>
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">SMALL STEPS, LASTING STORIES</p>
          <h2>属于你的徽章</h2>
          <p>每一步都算数。没有排行榜，也不用赶进度。</p>
        </div>
      </header>
      {unseen.length > 0 && (
        <div className="achievement-notice" role="status">
          <span>
            有 {unseen.length} 枚新徽章：{unseen.map((a) => a.title).join("、")}
          </span>
          <Button
            className="button secondary"
            disabled={acknowledge.isPending}
            onClick={() => acknowledge.mutate()}
          >
            收下这份纪念
          </Button>
        </div>
      )}
      <section className="achievement-showcase" aria-label="我的徽章展示">
        <h3>
          我的纪念架 <small>{selected.length} / 6</small>
        </h3>
        {selected.length ? (
          <div>
            {selected.map((a) => (
              <Button
                key={a.unlock_id}
                className="achievement-showcase-item"
                aria-label={`移出展示：${a.title}`}
                disabled={showcase.isPending}
                onClick={() =>
                  showcase.mutate(
                    selected
                      .filter((v) => v.unlock_id !== a.unlock_id)
                      .map((v) => v.unlock_id),
                  )
                }
              >
                <span aria-hidden="true">{badgeMarks[a.badge]}</span>
                {a.title}
              </Button>
            ))}
          </div>
        ) : (
          <p>获得徽章后，可以挑选最多六枚放在这里。</p>
        )}
      </section>
      {q.error ? (
        <Problem error={q.error} />
      ) : q.isPending ? (
        <p role="status">正在整理徽章…</p>
      ) : (
        <div className="achievement-grid">
          {q.data.items.map((a) => (
            <article
              className={`achievement-card ${a.granted ? "is-unlocked" : ""}`}
              key={a.id}
            >
              <span className="achievement-mark" aria-hidden="true">
                {badgeMarks[a.badge]}
              </span>
              <p className="memory-meta">
                {a.series_title} · 第 {a.tier} 级
              </p>
              <h3>{a.title}</h3>
              <p>{a.description}</p>
              <p className="memory-meta">
                {a.unlock_id
                  ? a.granted
                    ? `获得于 ${a.unlocked_at?.slice(0, 10)}`
                    : "已撤回授予，解锁历史保留"
                  : `${metricNames[a.metric]}：${a.value} / ${a.threshold}`}
              </p>
              {!a.unlock_id && (
                <progress
                  aria-label={`${a.title}进度`}
                  value={Math.min(a.value, a.threshold)}
                  max={a.threshold}
                />
              )}
              {a.granted && (
                <Button
                  className="button secondary"
                  disabled={
                    showcase.isPending ||
                    (!a.showcase_slot && selected.length >= 6)
                  }
                  onClick={() =>
                    showcase.mutate(
                      a.showcase_slot
                        ? selected
                            .filter((v) => v.unlock_id !== a.unlock_id)
                            .map((v) => v.unlock_id)
                        : [...selected.map((v) => v.unlock_id), a.unlock_id],
                    )
                  }
                >
                  {a.showcase_slot ? "移出展示" : "放上纪念架"}
                </Button>
              )}
            </article>
          ))}
        </div>
      )}
      {(showcase.error || acknowledge.error) && (
        <Problem error={showcase.error || acknowledge.error} />
      )}
    </>
  );
}
