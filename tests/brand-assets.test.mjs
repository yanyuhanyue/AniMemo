import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const featuredHero = readFileSync(new URL("../src/components/featured/FeaturedHero.jsx", import.meta.url), "utf8");
const indexHtml = readFileSync(new URL("../index.html", import.meta.url), "utf8");
const styles = readFileSync(new URL("../src/styles.css", import.meta.url), "utf8");

test("featured hero uses one dedicated artwork instead of the site avatar", () => {
  assert.match(featuredHero, /const FEATURED_HERO_ART = "\/assets\/featured-column\.png"/);
  assert.equal((featuredHero.match(/featured-portrait featured-portrait--/g) || []).length, 1);
  assert.doesNotMatch(featuredHero, /\/assets\/avatar\.png/);
  assert.match(styles, /\.featured-portrait--single \{[^}]*background: transparent;[^}]*\}/);
  assert.doesNotMatch(styles, /\.featured-portrait--single \{[^}]*grayscale/);
  assert.match(styles, /--featured-hero-height: clamp\(400px, 20\.12vw, 420px\)/);
  assert.match(styles, /\.featured-portrait--single \{[^}]*width: min\(400px, 78%\)/);
  assert.match(styles, /\.featured-portrait--single \{[^}]*right: calc\(2% \+ 48px\);[^}]*bottom: -28px/);
  assert.match(styles, /\.featured-hero__burst \{[^}]*z-index: 3;[^}]*right: 296px;[^}]*font-size: 210px/);
  assert.doesNotMatch(styles, /\.featured-hero__burst \{[^}]*clip-path/);
  assert.match(styles, /\.featured-hero__burst svg \{[^}]*animation: featured-burst 10s linear infinite/);
  assert.match(styles, /\.featured-portrait--single:hover \{[^}]*translate\(5px, 5px\)[^}]*rotate\(0\)/);
  assert.match(styles, /\.featured-portrait--single:hover img \{[^}]*transform: scale\(1\.035\)/);
});

test("site icon is independent from the default profile avatar", () => {
  assert.match(indexHtml, /rel="icon"[^>]+href="\/assets\/site-icon\.png"/);
  assert.match(indexHtml, /rel="apple-touch-icon"[^>]+href="\/assets\/site-icon\.png"/);
  assert.doesNotMatch(indexHtml, /rel="icon"[^>]+href="\/assets\/avatar\.png"/);
});
