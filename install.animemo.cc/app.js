const sourceDetails = Object.freeze({
  github: {
    label: "GitHub",
    rail: "GitHub transport",
    command: "sudo sh /tmp/animemo-install.sh --source github",
    status: "当前选择：GitHub。不会自动回退到其他运输来源。",
  },
  "official-mirror": {
    label: "AniMemo Official Mirror",
    rail: "Official Mirror transport",
    command: "sudo sh /tmp/animemo-install.sh --source official-mirror",
    status: "当前选择：AniMemo Official Mirror。GitHub Release 仍提供唯一发布权威；镜像失败时不会静默回退。",
  },
  "local-bundle": {
    label: "Portable / Offline Bundle",
    rail: "Portable transport (blocked)",
    command: "BLOCKED_PORTABLE_PUBLICATION_AUTHORITY",
    status: "Portable 布局与 OCI foundation 可资格认证；production install authority 尚未冻结，当前会 fail closed。",
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
