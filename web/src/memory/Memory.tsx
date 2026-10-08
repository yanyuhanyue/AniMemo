import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { client, result, type User } from "../api/client";
import { Button } from "../components/ui/Button";
import { Icon } from "../components/ui/Icon";
import { PageShell, Problem, PublicCard } from "../public/Public";
import { Notes } from "./Notes";
import { Characters } from "./Characters";
import { Episodes } from "./Episodes";
import { Collections } from "./Collections";
import { Yearlies, YearlyItems } from "./Yearly";
import { MemoryMediaManager } from "./Media";
import { Achievements } from "./Achievements";
import { MemorySearch } from "./Search";
import { AnimeSelect, useAnimeReference } from "./shared";
import { Rating } from "../components/Rating";
import "./memory.css";
const tabs = {
  search: "找回记忆",
  notes: "札记",
  moment: "瞬间",
  characters: "角色",
  episodes: "集数整理",
  collections: "收藏",
  yearly: "年度",
  media: "图片管理",
  achievements: "徽章",
};
type Tab = keyof typeof tabs;
const primaryTabs: Tab[] = [
  "notes",
  "moment",
  "collections",
  "yearly",
  "search",
];
const extraTabs: Tab[] = ["characters", "episodes", "media", "achievements"];
function currentTab(): Tab {
  const hash = location.hash.slice(1);
  return hash in tabs ? (hash as Tab) : "notes";
}
export function MemoryWorkspace({ user }: { user: User }) {
  const [tab, setTab] = useState(currentTab);
  const [anime, setAnime] = useState(() => new URLSearchParams(location.search).get("anime_id") ?? "");
  const [choosingAnime, setChoosingAnime] = useState(false);
  const reference = useAnimeReference(user.id, anime);
  const animeTitle = reference.data?.available ? reference.data.title : undefined;
  const scoped = !!anime && ["notes", "moment", "episodes", "search"].includes(tab);
  const [showTools, setShowTools] = useState(() =>
    extraTabs.includes(currentTab()) && !(anime && currentTab() === "episodes"),
  );
  const character =
    new URLSearchParams(location.search).get("character_id") ?? "";
  useEffect(() => {
    const changed = () => {
      const next = currentTab();
      setTab(next);
      if (extraTabs.includes(next) && !(anime && next === "episodes")) setShowTools(true);
    };
    addEventListener("hashchange", changed);
    return () => removeEventListener("hashchange", changed);
  }, [anime]);
  function changeAnime(id: string) {
    const url = new URL(location.href);
    if (id) url.searchParams.set("anime_id", id); else url.searchParams.delete("anime_id");
    history.replaceState(null, "", url);
    setAnime(id);
    setChoosingAnime(false);
  }
  function navigate(value: Tab) {
    location.hash = value;
    setTab(value);
  }
  return (
    <div className="app-shell memory-shell" data-accent={user.accent}>
      <a href="#memory-main" className="skip-link">
        跳到记忆正文
      </a>
      <header className="site-header">
        <div className="header-inner">
          <a className="brand" href="/">
            <span className="brand-mark">
              <Icon name="play" />
            </span>
            AniMemo<span className="brand-dot">.</span>
          </a>
          <nav className="main-nav" aria-label="主要导航">
            <a href="/">
              <Icon name="book" />
              我的番剧
            </a>
            <a href="/memory" aria-current="page">
              <Icon name="star" />
              记忆库
            </a>
            <a href="/universe">
              <Icon name="sparkle" />
              放映宇宙
            </a>
          </nav>
          <a
            className="memory-account"
            href="/"
            aria-label="返回手账与账号设置"
          >
            {user.display_name}
          </a>
        </div>
      </header>
      <main className="memory-main" id="memory-main">
        <section className="memory-intro">
          <div className="memory-breadcrumb"><a href="/">我的番剧</a><Icon name="chevron" /><span>{scoped ? "作品记忆" : "记忆库"}</span></div>
          <div className="memory-context-heading"><div><h1>{scoped ? animeTitle || (reference.isPending ? "正在读取作品…" : "作品记忆") : "我的记忆库"}</h1><p>{scoped ? "与这部作品有关的文字、片段和观看记忆。" : "把看过的故事，写进自己的记忆。"}</p></div>{scoped && <div className="memory-context-actions"><Button className="button secondary" aria-expanded={choosingAnime} onClick={() => setChoosingAnime(!choosingAnime)}>更换作品</Button><a href={`/memory#${tab}`}>查看全部记忆<Icon name="arrow" /></a></div>}</div>
          {scoped && choosingAnime && <div className="memory-context-picker"><AnimeSelect userID={user.id} value={anime} onChange={changeAnime} required /></div>}
          {reference.error && <Problem error={reference.error} />}
        </section>
        <div className="memory-workspace">
        <aside className="memory-sidebar" aria-label="记忆导航">
        <nav className="memory-tabs" aria-label="记忆分类">
          {(anime ? ["notes", "moment", "episodes", "collections", "yearly", "search"] as Tab[] : primaryTabs).map((key) => (
            <Button
              key={key}
              aria-current={tab === key ? "page" : undefined}
              onClick={() => navigate(key as Tab)}
            >
              <Icon name={key === "notes" ? "book" : key === "moment" ? "sparkle" : key === "episodes" ? "play" : key === "collections" ? "tag" : key === "yearly" ? "clock" : "search"} />
              {tabs[key]}
            </Button>
          ))}
        </nav>
        <details
          className="memory-organize"
          open={showTools}
          onToggle={(e) => setShowTools(e.currentTarget.open)}
        >
          <summary>
            更多整理工具
          </summary>
          <nav className="memory-tabs" aria-label="更多整理工具">
            {extraTabs.filter(key => !anime || key !== "episodes").map((key) => (
              <Button
                key={key}
                aria-current={tab === key ? "page" : undefined}
                onClick={() => navigate(key)}
              >
                {tabs[key]}
              </Button>
            ))}
          </nav>
        </details>
        </aside>
        <div className="memory-content">
        {tab === "search" && (
          <MemorySearch userID={user.id} anime={anime} character={character} />
        )}{" "}
        {tab === "notes" && (
          <Notes key={`notes:${anime}`} userID={user.id} anime={anime} animeTitle={animeTitle} character={character} />
        )}
        {tab === "moment" && (
          <Notes
            key={`moment:${anime}`}
            userID={user.id}
            kind="moment"
            anime={anime}
            animeTitle={animeTitle}
            character={character}
          />
        )}
        {tab === "characters" && <Characters userID={user.id} />}
        {tab === "episodes" && (
          <Episodes key={anime} userID={user.id} anime={anime} onAnimeChange={changeAnime} />
        )}
        {tab === "collections" && <Collections userID={user.id} />}
        {tab === "yearly" && <Yearlies userID={user.id} />}
        {tab === "achievements" && <Achievements userID={user.id} />}{" "}
        {tab === "media" && <MemoryMediaManager userID={user.id} />}
        </div>
        </div>
        <footer className="site-footer">
          <span>AniMemo.</span>
          <p>留住当时的心情，也给以后的自己。</p>
        </footer>
      </main>
    </div>
  );
}
export function Universe({ user }: { user: User }) {
  const [page, setPage] = useState(1);
  const q = useQuery({
    queryKey: ["memory", user.id, "universe", page],
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/entries", {
          signal,
          params: { query: { sort: "score", page, page_size: 12 } },
        }),
      ),
  });
  return (
    <div className="universe-shell">
      <nav aria-label="放映宇宙导航">
        <a href="/">← 我的番剧</a>
        <a href="/memory">翻开记忆库</a>
      </nav>
      <main>
        <p className="eyebrow">YOUR LITTLE ANIME UNIVERSE</p>
        <h1>
          每一个世界，
          <br />
          都曾与你相遇。
        </h1>
        <p className="universe-description">
          {user.display_name} 的私人放映室 · 只对自己可见
        </p>
        {q.error ? (
          <Problem error={q.error} />
        ) : q.isPending ? (
          <p role="status">正在点亮放映室…</p>
        ) : (
          <>
            <div className="universe-grid">
              {q.data.items.map((e, index) => (
                <a
                  className="universe-poster"
                  href={`/memory?anime_id=${e.anime_id}#notes`}
                  key={e.id}
                >
                  {e.cover_revision ? (
                    <img
                      src={`/api/v1/entries/${e.id}/cover/${e.cover_revision}`}
                      alt=""
                      loading="lazy"
                    />
                  ) : (
                    <div className="universe-placeholder">
                      <span aria-hidden="true">✦</span>
                      <span>
                        {String((page - 1) * 12 + index + 1).padStart(2, "0")}
                      </span>
                    </div>
                  )}
                  <div>
                    <p>
                      {e.score !== null && <><Rating score={e.score} /> · </>}
                      {e.original_title || "藏在心里的故事"}
                    </p>
                    <h2>{e.title}</h2>
                    <span>翻开相关记忆 ↗</span>
                  </div>
                </a>
              ))}
            </div>
            {!q.data.items.length && (
              <p>先在手账加入一部番剧，让放映室亮起第一束光。</p>
            )}
            {q.data.total > 12 && (
              <div className="memory-actions">
                <Button
                  className="button secondary"
                  disabled={page === 1}
                  onClick={() => setPage(page - 1)}
                >
                  上一页
                </Button>
                <span>
                  {page} / {Math.ceil(q.data.total / 12)}
                </span>
                <Button
                  className="button secondary"
                  disabled={page * 12 >= q.data.total}
                  onClick={() => setPage(page + 1)}
                >
                  下一页
                </Button>
              </div>
            )}
          </>
        )}
      </main>
    </div>
  );
}
export function SharedMemoryPage({ token }: { token: string }) {
  const q = useQuery({
    queryKey: ["public", "memory", token],
    retry: false,
    queryFn: ({ signal }) =>
      result(
        client.GET("/api/v1/memory/shared/{token}", {
          signal,
          params: { path: { token } },
        }),
      ),
  });
  return (
    <PageShell title={q.data?.title ?? "分享一份记忆"}>
      {q.isPending ? (
        <p role="status">正在翻开分享…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : (
        <div className="memory-reading">
          <p className="memory-prose">{q.data.introduction}</p>
          {q.data.year > 0 && (
            <p className="memory-meta">
              {q.data.year} · 第 {q.data.revision} 版
            </p>
          )}
          <YearlyItems
            items={q.data.items}
            mediaURL={(id) => `/api/v1/memory/shared/${token}/media/${id}`}
          />
          {!q.data.items.length && q.data.kind !== "character" && (
            <p>当前没有可展示的选材，来源可能尚未公开或已撤回。</p>
          )}
        </div>
      )}
    </PageShell>
  );
}
export function SiteHomepage() {
  const q = useQuery({
    queryKey: ["public", "homepage"],
    retry: false,
    queryFn: ({ signal }) => result(client.GET("/api/v1/homepage", { signal })),
  });
  return (
    <PageShell
      title={
        q.data?.owner ? `${q.data.owner.name} 的放映室` : "欢迎来到 AniMemo"
      }
    >
      {q.isPending ? (
        <p role="status">正在读取站点首页…</p>
      ) : q.error ? (
        <Problem error={q.error} />
      ) : q.data.owner ? (
        <>
          <p className="intro-description">{q.data.owner.bio}</p>
          <div className="public-grid">
            {q.data.items.map((item) => (
              <PublicCard key={item.entry.slug} item={item} />
            ))}
          </div>
          <a href={`/u/${q.data.owner.slug}`}>查看完整公开手账 ↗</a>
        </>
      ) : (
        <div className="memory-empty">
          <h2>站点还没有指定公开主人</h2>
          <p>你仍然可以登录自己的手账，或浏览公开目录。</p>
          <a className="button primary" href="/">
            打开我的手账
          </a>
        </div>
      )}
    </PageShell>
  );
}
