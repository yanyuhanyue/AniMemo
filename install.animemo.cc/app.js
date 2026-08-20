const sourceDetails = Object.freeze({
  github: {
    label: "GitHub",
    rail: "GitHub transport",
    command: "gh release verify-asset <EXACT_TAG> ./animemo-stage0/installer-materials.tar --repo yanyuhanyue/AniMemo",
    status: "当前选择：GitHub。不会自动回退到其他运输来源。",
  },
  "official-mirror": {
    label: "AniMemo Official Mirror",
    rail: "Official Mirror transport",
    command: "gh release verify-asset <EXACT_TAG> ./animemo-stage0/installer-materials.tar --repo yanyuhanyue/AniMemo",
    status: "当前选择：AniMemo Official Mirror。GitHub Release 仍提供唯一发布权威；镜像失败时不会静默回退。",
  },
  "local-bundle": {
    label: "Portable / Offline Bundle",
    rail: "Portable transport (blocked)",
    command: "OFFLINE_STAGE0_REQUIRES_OPERATOR_PRETRUST",
    status: "Portable 只运输；没有 independently pretrusted verifier 与 roots 时必须 fail closed。",
  },
});

const runCommand = document.querySelector("#run-command");
const status = document.querySelector("#source-status");
const railName = document.querySelector("#rail-transport-name");
const copyStatus = document.querySelector("#copy-status");

for (const button of document.querySelectorAll("[data-transport]")) {
  button.addEventListener("click", () => {
    const selected = button.dataset.transport;
    const details = sourceDetails[selected];
    if (!details) return;
    for (const candidate of document.querySelectorAll("[data-transport]")) {
      const active = candidate === button;
      candidate.classList.toggle("is-active", active);
      candidate.setAttribute("aria-pressed", String(active));
    }
    runCommand.textContent = details.command;
    status.textContent = details.status;
    railName.textContent = details.rail;
  });
}

for (const button of document.querySelectorAll("[data-copy-target]")) {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    try {
      await navigator.clipboard.writeText(target.textContent.trim());
      copyStatus.textContent = "命令已复制";
    } catch {
      copyStatus.textContent = "无法自动复制，请手动选择命令";
    }
    copyStatus.classList.add("is-visible");
    window.setTimeout(() => copyStatus.classList.remove("is-visible"), 1800);
  });
}
