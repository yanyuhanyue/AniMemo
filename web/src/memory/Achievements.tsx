import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { client, result } from "../api/client";
import { Button } from "../components/ui/Button";
import { Problem } from "../public/Public";
import { useMemoryRefresh } from "./shared";
import { AchievementBadge } from "../achievements/Badge";
import {
  achievementState,
  achievementStateNames,
  metricNames,
  badgeGrade,
  metricUnits,
  metricDescriptions,
} from "../achievements/presentation";
import "../achievements/achievements.css";
const seriesOrder = new Map(
  [
    "recorded-worlds",
    "memory-pages",
    "memory-shelves",
    "yearly-albums",
    "first-steps",
    "many-worlds",
  ].map((id, index) => [id, index]),
);

export function Achievements({ userID }: { userID: string }) {
  const [series, setSeries] = useState("");
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
  const firstUnseen = unseen[0];
  const items = [...(q.data?.items ?? [])].sort(
    (a, b) =>
      (seriesOrder.get(a.series_id) ?? 100) -
      (seriesOrder.get(b.series_id) ?? 100),
  );
  const seriesOptions = new Map(
    items.map((a) => [a.series_id, a.series_title]),
  );
  const selectedSeries = seriesOptions.has(series) ? series : "";
  const visibleItems = items.filter(
    (a) => !selectedSeries || a.series_id === selectedSeries,
  );
  const earned = items.filter((a) => a.granted).length;
  const toggleShowcase = (unlockID: string) => {
    const ids = selected.map((a) => a.unlock_id);
    showcase.mutate(
      ids.includes(unlockID)
        ? ids.filter((id) => id !== unlockID)
        : [...ids, unlockID],
    );
  };
  return (
    <div className="achievement-library">
      <header className="memory-section-heading">
        <div>
          <p className="eyebrow">A LITTLE KEEPSAKE</p>
          <h2>属于你的徽章</h2>
          <p>为写下的故事留一枚纪念，慢慢收藏就好。</p>
        </div>
        {q.data && (
          <p className="achievement-count">
            已获得 <strong>{earned}</strong> 枚纪念章
          </p>
        )}
      </header>
      {q.error ? (
        <Problem error={q.error} />
      ) : q.isPending ? (
        <p role="status">正在整理徽章…</p>
      ) : (
        <>
          {firstUnseen && (
            <div className="achievement-notice" role="status">
              <AchievementBadge
                imageID={firstUnseen.badge_image_id}
                badge={firstUnseen.badge}
                tier={firstUnseen.tier}
                size="small"
              />
              <div>
                <strong>故事留下了新的纪念</strong>
                <p>
                  有 {unseen.length} 枚新徽章：
                  {unseen.map((a) => a.title).join("、")}
                </p>
              </div>
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
            <header>
              <div>
                <p className="eyebrow">MY KEEPSAKES</p>
                <h3>我的纪念架</h3>
              </div>
              <p>
                {selected.length} / 6 <span>枚展示中</span>
              </p>
            </header>
            <p className="achievement-showcase-help">
              {selected.length
                ? "把喜欢的片刻放在这里。移出展示仍会保留已获得的徽章。"
                : "从下方挑一枚喜欢的徽章，放进你的纪念架。最多展示六枚。"}
            </p>
            <div className="achievement-shelf">
              {selected.map((a) => (
                <div key={a.unlock_id} className="achievement-showcase-item">
                  <AchievementBadge
                    imageID={a.badge_image_id}
                    badge={a.badge}
                    tier={a.tier}
                  />
                  <strong>{a.title}</strong>
                  <Button
                    type="button"
                    className="achievement-shelf-remove"
                    aria-label={`移出展示：${a.title}`}
                    disabled={showcase.isPending}
                    onClick={() => toggleShowcase(a.unlock_id)}
                  >
                    移出展示
                  </Button>
                </div>
              ))}
              {Array.from({ length: 6 - selected.length }, (_, i) => (
                <div
                  className="achievement-shelf-empty"
                  key={`empty-${i}`}
                  aria-hidden="true"
                >
                  <span>
                    {String(selected.length + i + 1).padStart(2, "0")}
                  </span>
                  <small>留给喜欢的纪念</small>
                </div>
              ))}
            </div>
          </section>
          <section aria-labelledby="achievement-catalog-title">
            <header className="achievement-catalog-heading">
              <h3 id="achievement-catalog-title">纪念图鉴</h3>
              <p>每个图案，都对应一段留下的故事。</p>
            </header>
            <div className="achievement-catalog-filter">
              <label>
                纪念系列
                <select
                  aria-label="纪念系列"
                  value={selectedSeries}
                  onChange={(e) => setSeries(e.target.value)}
                >
                  <option value="">全部系列</option>
                  {[...seriesOptions].map(([id, title]) => (
                    <option value={id} key={id}>
                      {title}
                    </option>
                  ))}
                </select>
              </label>
              <p role="status">
                展示 {visibleItems.length} 枚 ·
                不必赶进度，每段回忆都有自己的步调。
              </p>
            </div>
            {selected.length >= 6 && (
              <p className="memory-meta" role="status">
                纪念架已放满六枚。移出一枚后，就可以换上其他徽章。
              </p>
            )}
            {items.length === 0 ? (
              <p className="memory-empty">
                这里还没有成就规则，你仍然可以继续记录作品和回忆。
              </p>
            ) : (
              <div className="achievement-grid">
                {visibleItems.map((a) => {
                  const state = achievementState(a);
                  return (
                    <article
                      className="achievement-card"
                      data-state={state}
                      key={a.id}
                    >
                      <div className="achievement-card-art">
                        <span className="achievement-state" data-state={state}>
                          {!a.active && !a.unlock_id
                            ? "已停用"
                            : achievementStateNames[state]}
                        </span>
                        <AchievementBadge
                          imageID={a.badge_image_id}
                          badge={a.badge}
                          tier={a.tier}
                          state={state}
                          size="large"
                        />
                        <span className="achievement-card-tier">
                          {badgeGrade(a.tier).label} · 第 {a.tier} 级
                        </span>
                      </div>
                      <div className="achievement-card-body">
                        <p className="achievement-series">{a.series_title}</p>
                        <h4>{a.title}</h4>
                        <p className="achievement-description">
                          {a.description}
                        </p>
                        <p className="achievement-condition">
                          {metricNames[a.metric]} · {a.threshold}{" "}
                          {metricUnits[a.metric]}
                        </p>
                        <details className="achievement-rule-help">
                          <summary>计入哪些记录</summary>
                          <p>{metricDescriptions[a.metric]}</p>
                        </details>
                        <div className="achievement-card-footer">
                          {a.unlock_id ? (
                            <p className="achievement-date">
                              {a.granted ? (
                                <>
                                  获得于{" "}
                                  <time dateTime={a.unlocked_at ?? undefined}>
                                    {a.unlocked_at?.slice(0, 10)}
                                  </time>
                                </>
                              ) : (
                                "授予已撤回，解锁历史仍保留"
                              )}
                            </p>
                          ) : (
                            <div className="achievement-progress">
                              <p>
                                <span>已留下的记录</span>
                                <span>
                                  {a.value} / {a.threshold}
                                </span>
                              </p>
                              <progress
                                aria-label={`${a.title}进度`}
                                value={Math.min(a.value, a.threshold)}
                                max={a.threshold}
                              />
                            </div>
                          )}
                          {a.granted && (
                            <Button
                              className={`button ${a.showcase_slot ? "secondary" : "primary"}`}
                              disabled={
                                showcase.isPending ||
                                (!a.showcase_slot && selected.length >= 6)
                              }
                              onClick={() => toggleShowcase(a.unlock_id)}
                            >
                              {a.showcase_slot ? "移出展示" : "放上纪念架"}
                            </Button>
                          )}
                        </div>
                      </div>
                    </article>
                  );
                })}
              </div>
            )}
          </section>
        </>
      )}
      {(showcase.error || acknowledge.error) && (
        <Problem error={showcase.error || acknowledge.error} />
      )}
    </div>
  );
}
