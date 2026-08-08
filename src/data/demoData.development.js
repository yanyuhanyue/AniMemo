import { animeRecords } from "./anime.js";
import { featuredColumns } from "./featuredColumns.js";
import { demoUniverseOwners, getDemoUniverseOwner as findDemoUniverseOwner } from "./universe.js";

export const demoEnabled = true;
export const demoAnimeRecords = animeRecords;
export const demoFeaturedColumns = featuredColumns;
export { demoUniverseOwners };

export function getDemoUniverseOwner(publicSlug) {
  return findDemoUniverseOwner(publicSlug);
}

export function getDemoAuthMessage(mode) {
  return mode === "register"
    ? "本地演示：注册信息已校验，连接后端后会发送激活邮件。"
    : "本地演示：重置请求已记录。";
}
