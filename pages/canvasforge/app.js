const bridge = window.AstrBotPluginPage;

const settingKeys = [
  "model",
  "size",
  "quality",
  "output_format",
  "output_compression",
  "request_timeout_seconds",
  "cooldown_seconds",
  "max_prompt_chars",
  "max_output_mib",
  "max_reference_images",
  "max_total_reference_mib",
  "max_reference_megapixels",
  "max_reference_edge",
  "cache_max_images",
];

const numericSettingKeys = new Set([
  "output_compression",
  "request_timeout_seconds",
  "cooldown_seconds",
  "max_prompt_chars",
  "max_output_mib",
  "max_reference_images",
  "max_total_reference_mib",
  "max_reference_megapixels",
  "max_reference_edge",
  "cache_max_images",
]);

const state = {
  items: [],
  observer: null,
  previewItem: null,
  previewGeneration: 0,
  confirmResolve: null,
  cacheLoadGeneration: 0,
};

function byId(id) {
  return document.getElementById(id);
}

function messageOf(error) {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "请求失败，请稍后重试。";
}

function setInlineMessage(text, tone = "normal") {
  const element = byId("settings-message");
  element.textContent = text;
  element.dataset.tone = tone;
}

function setCacheMessage(text, tone = "normal") {
  const element = byId("cache-message");
  element.textContent = text;
  element.dataset.tone = tone;
  element.hidden = !text;
}

function setButtonBusy(button, busy, busyText) {
  if (busy) {
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.textContent;
    }
    button.textContent = busyText;
  } else if (button.dataset.originalText) {
    button.textContent = button.dataset.originalText;
    delete button.dataset.originalText;
  }
  button.disabled = busy;
}

function applySettings(settings) {
  for (const key of settingKeys) {
    const input = byId(key);
    if (input && Object.hasOwn(settings, key)) {
      input.value = String(settings[key]);
    }
  }
  syncCompressionState();
}

function collectSettings() {
  const values = {};
  for (const key of settingKeys) {
    const input = byId(key);
    values[key] = numericSettingKeys.has(key) ? Number.parseInt(input.value, 10) : input.value.trim();
  }
  return values;
}

function syncCompressionState() {
  const isPng = byId("output_format").value === "png";
  const compression = byId("output_compression");
  compression.disabled = isPng;
  byId("compression-help").textContent = isPng
    ? "PNG 不使用此设置；当前值会保留。"
    : "数值越高，JPEG / WebP 质量越高。";
}

async function loadSettings() {
  setInlineMessage("正在读取…");
  try {
    const settings = await bridge.apiGet("settings");
    applySettings(settings);
    setInlineMessage("");
  } catch (error) {
    setInlineMessage(messageOf(error), "error");
  }
}

async function saveSettings(event) {
  event.preventDefault();
  const form = byId("settings-form");
  if (!form.reportValidity()) {
    return;
  }
  const button = byId("save-settings");
  setButtonBusy(button, true, "保存中…");
  setInlineMessage("");
  try {
    const result = await bridge.apiPost("settings", collectSettings());
    if (result && result.settings) {
      applySettings(result.settings);
    }
    const evicted = Number(result && result.evicted);
    setInlineMessage(evicted > 0 ? `设置已保存，并淘汰 ${evicted} 张旧图。` : "设置已保存。", "success");
    await loadCache();
  } catch (error) {
    setInlineMessage(messageOf(error), "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function activateTab(panelId) {
  for (const button of document.querySelectorAll(".tab")) {
    const selected = button.dataset.tab === panelId;
    button.classList.toggle("is-active", selected);
    button.setAttribute("aria-selected", String(selected));
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.hidden = panel.id !== panelId;
  }
  if (panelId === "cache-panel") {
    loadCache();
  }
}

function humanBytes(value) {
  const bytes = Number(value);
  if (!Number.isFinite(bytes) || bytes < 0) {
    return "未知大小";
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KiB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

function formatTime(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "时间未知";
  }
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function labelValue(label, value) {
  const row = document.createElement("div");
  row.className = "meta-row";
  const key = document.createElement("span");
  key.className = "meta-key";
  key.textContent = label;
  const content = document.createElement("span");
  content.className = "meta-value";
  content.textContent = value || "—";
  row.append(key, content);
  return row;
}

function itemFilename(item) {
  const extension = item.format === "jpeg" ? "jpg" : item.format || "bin";
  return `canvasforge-${item.id}.${extension}`;
}

function createCard(item) {
  const article = document.createElement("article");
  article.className = "cache-card";
  article.dataset.cacheId = item.id;

  const previewButton = document.createElement("button");
  previewButton.className = "image-button";
  previewButton.type = "button";
  previewButton.setAttribute("aria-label", `预览 ${formatTime(item.created_at)} 生成的图片`);
  previewButton.addEventListener("click", () => openPreview(item));

  const image = document.createElement("img");
  image.alt = "CanvasForge 生成图片缩略图";
  image.loading = "lazy";
  image.dataset.cacheId = item.id;
  const placeholder = document.createElement("span");
  placeholder.className = "image-placeholder";
  placeholder.textContent = "载入缩略图";
  previewButton.append(image, placeholder);

  const body = document.createElement("div");
  body.className = "card-body";
  const heading = document.createElement("div");
  heading.className = "card-heading";
  const title = document.createElement("h3");
  title.textContent = item.mode === "edit" ? "引用图编辑" : item.mode === "recovered" ? "恢复的缓存" : "文本生图";
  const time = document.createElement("time");
  time.dateTime = item.created_at || "";
  time.textContent = formatTime(item.created_at);
  heading.append(title, time);

  const metadata = document.createElement("div");
  metadata.className = "metadata";
  const dimensions = item.size || (item.width && item.height ? `${item.width}×${item.height}` : "");
  metadata.append(
    labelValue("模型", item.model),
    labelValue("规格", `${dimensions || "尺寸未知"} · ${String(item.format || "未知").toUpperCase()} · ${humanBytes(item.file_size)}`),
    labelValue("用户", item.user_name ? `${item.user_name} (${item.user_id || "ID 未知"})` : item.user_id),
    labelValue("会话", item.conversation_name || item.conversation_id || item.chat_type),
  );

  const actions = document.createElement("div");
  actions.className = "card-actions";
  const download = document.createElement("button");
  download.className = "button subtle compact";
  download.type = "button";
  download.textContent = "下载";
  download.addEventListener("click", () => downloadItem(item, download));
  const remove = document.createElement("button");
  remove.className = "button danger compact";
  remove.type = "button";
  remove.textContent = "删除";
  remove.addEventListener("click", () => deleteItem(item, remove));
  actions.append(download, remove);

  body.append(heading, metadata, actions);
  article.append(previewButton, body);
  return article;
}

function renderGallery() {
  const gallery = byId("gallery");
  if (state.observer) {
    state.observer.disconnect();
  }
  gallery.replaceChildren();
  byId("empty-cache").hidden = state.items.length !== 0;
  gallery.hidden = state.items.length === 0;

  state.observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (entry.isIntersecting) {
          state.observer.unobserve(entry.target);
          loadThumbnail(entry.target);
        }
      }
    },
    { rootMargin: "240px 0px" },
  );

  for (const item of state.items) {
    const card = createCard(item);
    gallery.append(card);
    const image = card.querySelector("img");
    state.observer.observe(image);
  }
}

async function loadThumbnail(image) {
  const cacheId = image.dataset.cacheId;
  const placeholder = image.nextElementSibling;
  try {
    const result = await bridge.apiGet(`cache/${cacheId}/thumbnail`);
    if (!image.isConnected) {
      return;
    }
    image.addEventListener(
      "load",
      () => {
        image.classList.add("is-loaded");
        if (placeholder) {
          placeholder.hidden = true;
        }
      },
      { once: true },
    );
    image.addEventListener(
      "error",
      () => {
        if (placeholder) {
          placeholder.textContent = "缩略图不可用";
        }
      },
      { once: true },
    );
    image.src = `data:${result.mime_type};base64,${result.base64_data}`;
  } catch (error) {
    if (placeholder) {
      placeholder.textContent = "缩略图不可用";
    }
  }
}

async function loadCache() {
  const generation = ++state.cacheLoadGeneration;
  const refresh = byId("refresh-cache");
  setButtonBusy(refresh, true, "读取中…");
  setCacheMessage("");
  try {
    const result = await bridge.apiGet("cache");
    if (generation !== state.cacheLoadGeneration) {
      return;
    }
    state.items = Array.isArray(result.items) ? result.items : [];
    const limit = Number(result.limit);
    byId("cache-summary").textContent =
      limit === 0
        ? `共 ${state.items.length} 张；当前已停止新增缓存。`
        : `共 ${state.items.length} 张，最多保留 ${Number.isFinite(limit) ? limit : 3} 张。`;
    renderGallery();
  } catch (error) {
    if (generation === state.cacheLoadGeneration) {
      setCacheMessage(messageOf(error), "error");
    }
  } finally {
    if (generation === state.cacheLoadGeneration) {
      setButtonBusy(refresh, false);
    }
  }
}

function invalidateCacheLoads() {
  state.cacheLoadGeneration += 1;
  setButtonBusy(byId("refresh-cache"), false);
}

async function openPreview(item) {
  const dialog = byId("preview-dialog");
  const image = byId("preview-image");
  const loading = byId("preview-loading");
  const generation = ++state.previewGeneration;
  state.previewItem = item;
  byId("preview-title").textContent = `${item.mode === "edit" ? "引用图编辑" : "文本生图"} · ${formatTime(item.created_at)}`;
  image.hidden = true;
  image.removeAttribute("src");
  loading.hidden = false;
  loading.textContent = "正在载入大图预览…";
  dialog.showModal();
  try {
    const result = await bridge.apiGet(`cache/${item.id}/preview`);
    if (
      generation === state.previewGeneration
      && state.previewItem
      && state.previewItem.id === item.id
      && dialog.open
    ) {
      image.src = `data:${result.mime_type};base64,${result.base64_data}`;
      image.hidden = false;
      loading.hidden = true;
    }
  } catch (error) {
    if (
      generation === state.previewGeneration
      && state.previewItem
      && state.previewItem.id === item.id
      && dialog.open
    ) {
      loading.textContent = messageOf(error);
    }
  }
}

function closePreview() {
  const dialog = byId("preview-dialog");
  state.previewGeneration += 1;
  state.previewItem = null;
  byId("preview-image").removeAttribute("src");
  if (dialog.open) {
    dialog.close();
  }
}

function resolveConfirmation(confirmed) {
  const callback = state.confirmResolve;
  state.confirmResolve = null;
  const dialog = byId("confirm-dialog");
  if (dialog.open) {
    dialog.close();
  }
  if (callback) {
    callback(confirmed);
  }
}

function requestConfirmation(message) {
  if (state.confirmResolve) {
    resolveConfirmation(false);
  }
  byId("confirm-message").textContent = message;
  byId("confirm-dialog").showModal();
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
  });
}

async function downloadItem(item, button) {
  setButtonBusy(button, true, "下载中…");
  setCacheMessage("");
  try {
    await bridge.download(`cache/${item.id}/download`, {}, itemFilename(item));
  } catch (error) {
    setCacheMessage(messageOf(error), "error");
  } finally {
    setButtonBusy(button, false);
  }
}

async function deleteItem(item, button) {
  if (!(await requestConfirmation("确定删除这张缓存图片吗？删除后无法恢复。"))) {
    return;
  }
  setButtonBusy(button, true, "删除中…");
  setCacheMessage("");
  try {
    await bridge.apiPost(`cache/${item.id}/delete`, {});
    invalidateCacheLoads();
    if (state.previewItem && state.previewItem.id === item.id) {
      closePreview();
    }
    state.items = state.items.filter((candidate) => candidate.id !== item.id);
    renderGallery();
    byId("cache-summary").textContent = `共 ${state.items.length} 张缓存图片。`;
    setCacheMessage("缓存图片已删除。", "success");
  } catch (error) {
    setCacheMessage(messageOf(error), "error");
    setButtonBusy(button, false);
  }
}

async function clearCache() {
  if (state.items.length === 0) {
    setCacheMessage("当前没有可清空的图片。");
    return;
  }
  if (
    !(await requestConfirmation(
      `确定清空全部 ${state.items.length} 张缓存图片吗？此操作无法恢复。`,
    ))
  ) {
    return;
  }
  const button = byId("clear-cache");
  setButtonBusy(button, true, "清空中…");
  setCacheMessage("");
  try {
    const result = await bridge.apiPost("cache/clear", {});
    invalidateCacheLoads();
    closePreview();
    state.items = [];
    renderGallery();
    byId("cache-summary").textContent = "共 0 张缓存图片。";
    setCacheMessage(`已清空 ${Number(result.removed) || 0} 张图片。`, "success");
  } catch (error) {
    setCacheMessage(messageOf(error), "error");
  } finally {
    setButtonBusy(button, false);
  }
}

function wireEvents() {
  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  }
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("output_format").addEventListener("change", syncCompressionState);
  byId("refresh-cache").addEventListener("click", loadCache);
  byId("clear-cache").addEventListener("click", clearCache);
  byId("close-preview").addEventListener("click", closePreview);
  byId("preview-download").addEventListener("click", () => {
    if (state.previewItem) {
      downloadItem(state.previewItem, byId("preview-download"));
    }
  });
  byId("preview-dialog").addEventListener("click", (event) => {
    if (event.target === byId("preview-dialog")) {
      closePreview();
    }
  });
  byId("confirm-cancel").addEventListener("click", () => {
    resolveConfirmation(false);
  });
  byId("confirm-accept").addEventListener("click", () => {
    resolveConfirmation(true);
  });
  byId("confirm-dialog").addEventListener("cancel", (event) => {
    event.preventDefault();
    resolveConfirmation(false);
  });
  byId("confirm-dialog").addEventListener("click", (event) => {
    if (event.target === byId("confirm-dialog")) {
      resolveConfirmation(false);
    }
  });
}

async function initialize() {
  wireEvents();
  if (!bridge) {
    setInlineMessage("AstrBot Page bridge 不可用。", "error");
    return;
  }
  try {
    await bridge.ready();
    await Promise.all([loadSettings(), loadCache()]);
  } catch (error) {
    setInlineMessage(messageOf(error), "error");
  }
}

initialize();
