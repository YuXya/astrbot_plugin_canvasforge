const bridge = window.AstrBotPluginPage;

const settingKeys = [
  "model",
  "size",
  "quality",
  "output_format",
  "output_compression",
  "request_timeout_seconds",
  "cooldown_seconds",
  "admin_only_generation",
  "max_prompt_chars",
  "max_output_mib",
  "enable_avatar_references",
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

const booleanSettingKeys = new Set([
  "admin_only_generation",
  "enable_avatar_references",
]);

const activeUpdatePhases = new Set(["accepted", "updating", "verifying"]);
const terminalUpdatePhases = new Set(["succeeded", "failed", "interrupted"]);

const state = {
  items: [],
  observer: null,
  previewItem: null,
  previewGeneration: 0,
  confirmResolve: null,
  cacheLoadGeneration: 0,
  cacheLoaded: false,
  cacheLoading: false,
  cacheStale: false,
  cacheLimit: null,
  settingsLoaded: false,
  settingsLoading: false,
  settingsCacheLimit: null,
  updateCheck: null,
  updateCheckAttempted: false,
  updateChecking: false,
  updateApplying: false,
  updateStatusLoading: false,
  updateActive: false,
  updatePhase: "idle",
  updateTargetVersion: "",
  updateExpectedJobId: "",
  updateLastStatusJobId: "",
  updateSubmissionUnconfirmed: false,
  updateSubmissionBaselineJobId: "",
  updateSubmissionTargetVersion: "",
  updateReloadAvailable: false,
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

function setUpdateMessage(text, tone = "normal") {
  const element = byId("update-status");
  element.textContent = text;
  element.dataset.tone = tone;
}

function setUpdateJobNote(text, tone = "normal") {
  const element = byId("update-job-note");
  element.textContent = text;
  element.dataset.tone = tone;
  element.hidden = !text;
}

function textValue(value, fallback = "") {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function withTimeout(promise, milliseconds, message) {
  let timer = null;
  const timeout = new Promise((_, reject) => {
    timer = window.setTimeout(() => {
      reject(new Error(message));
    }, milliseconds);
  });
  return Promise.race([promise, timeout]).finally(() => {
    if (timer !== null) {
      window.clearTimeout(timer);
    }
  });
}

function normalizeUpdatePayload(result) {
  if (!result || typeof result !== "object") {
    return {};
  }
  if (result.update && typeof result.update === "object") {
    return { ...result, ...result.update };
  }
  if (result.job && typeof result.job === "object") {
    return { ...result, ...result.job };
  }
  if (result.status && typeof result.status === "object") {
    return { ...result, ...result.status };
  }
  return result;
}

function updatePhaseOf(payload) {
  const phase = payload.state ?? payload.phase ?? payload.status;
  return typeof phase === "string" ? phase.trim().toLowerCase() : "idle";
}

function updateCurrentVersion(payload) {
  const version = textValue(
    payload.current_version ?? payload.local_version ?? payload.plugin_version,
  );
  if (version) {
    byId("current-version").textContent = version;
  }
}

function showReleaseDetails(payload) {
  const target = textValue(payload.target_version ?? payload.latest_version);
  if (!target) {
    byId("update-release").hidden = true;
    return;
  }
  byId("target-version").textContent = target;
  const releaseTitle = textValue(payload.release_title ?? payload.title);
  if (releaseTitle || byId("update-release").hidden) {
    byId("release-title").textContent = releaseTitle || "—";
  }
  const publishedAt = textValue(
    payload.published_at ?? payload.release_published_at,
  );
  if (publishedAt || byId("update-release").hidden) {
    byId("release-time").textContent = publishedAt ? formatTime(publishedAt) : "—";
  }
  byId("update-release").hidden = false;
  state.updateTargetVersion = target;
}

function updateCanApply(check) {
  return Boolean(
    check
      && textValue(check.check_id)
      && check.can_apply !== false
      && check.updater_available !== false
      && check.update_supported !== false,
  );
}

function renderUpdateControls() {
  const checkButton = byId("check-update");
  const statusButton = byId("refresh-update-status");
  const applyButton = byId("apply-update");
  const reloadButton = byId("reload-console");
  checkButton.textContent = state.updateChecking
    ? "检查中…"
    : state.updateCheckAttempted
      ? "再次检查"
      : "检查更新";
  checkButton.disabled =
    state.updateChecking
    || state.updateApplying
    || state.updateStatusLoading
    || state.updateActive;
  statusButton.textContent = state.updateStatusLoading
    ? "读取状态中…"
    : "刷新更新状态";
  statusButton.disabled =
    state.updateChecking || state.updateApplying || state.updateStatusLoading;

  const showApplyButton =
    state.updateActive
    || state.updateApplying
    || Boolean(state.updateCheck);
  applyButton.hidden = !showApplyButton;

  if (state.updateApplying) {
    applyButton.textContent = "正在提交更新…";
  } else if (state.updateActive) {
    applyButton.textContent = "更新已受理";
  } else {
    applyButton.textContent = state.updateTargetVersion
      ? `更新到 ${state.updateTargetVersion}`
      : "安装更新";
  }
  applyButton.disabled =
    state.updateApplying
    || state.updateActive
    || !updateCanApply(state.updateCheck);
  reloadButton.hidden = !state.updateReloadAvailable;
}

function updateStatusMessage(phase, payload) {
  const serverMessage = textValue(payload.message);
  if (phase === "accepted") {
    return serverMessage || "更新任务已受理，正在准备…";
  }
  if (phase === "updating") {
    return serverMessage || "正在更新插件，控制台可能会短暂断开…";
  }
  if (phase === "verifying") {
    return serverMessage || "代码已更新，正在验证插件重载结果…";
  }
  return serverMessage;
}

function renderUpdateJob(result) {
  const payload = normalizeUpdatePayload(result);
  const phase = updatePhaseOf(payload);
  const jobId = textValue(payload.job_id);
  if (
    state.updateExpectedJobId
    && jobId
    && state.updateExpectedJobId !== jobId
  ) {
    throw new Error("返回的更新状态不属于当前已受理任务。");
  }
  state.updateLastStatusJobId = jobId;
  updateCurrentVersion(payload);
  if (textValue(payload.target_version ?? payload.latest_version)) {
    showReleaseDetails(payload);
  }
  state.updatePhase = phase;
  state.updateReloadAvailable = phase === "succeeded";

  if (activeUpdatePhases.has(phase)) {
    state.updateActive = true;
    setUpdateJobNote("页面不会自动刷新状态；请稍后再次点击“刷新更新状态”。");
    setUpdateMessage(updateStatusMessage(phase, payload));
    renderUpdateControls();
    return true;
  }

  state.updateActive = false;
  state.updateExpectedJobId = "";
  if (phase === "succeeded") {
    const message = textValue(payload.message, "上次更新已成功完成。");
    state.updateCheck = null;
    setUpdateMessage("更新已完成。", "success");
    setUpdateJobNote(`${message} 请点击“刷新控制台”载入新版本。`, "success");
  } else if (phase === "failed") {
    setUpdateMessage("更新失败。", "error");
    setUpdateJobNote(
      textValue(
        payload.message,
        "上次更新失败；如插件无法正常使用，请从 AstrBot 插件管理按 GitHub 地址重新安装。",
      ),
      "error",
    );
  } else if (phase === "interrupted") {
    setUpdateMessage("更新已中断。", "error");
    setUpdateJobNote(
      textValue(
        payload.message,
        "上次更新被中断，暂时无法确认结果；请重新检查版本。",
      ),
      "error",
    );
  } else if (terminalUpdatePhases.has(phase)) {
    setUpdateJobNote(textValue(payload.message, "更新任务已结束。"));
  } else {
    setUpdateMessage(
      textValue(payload.message, "当前没有进行中的更新任务。"),
    );
    setUpdateJobNote("");
  }
  renderUpdateControls();
  return false;
}

async function refreshUpdateStatus() {
  if (state.updateStatusLoading || state.updateApplying) {
    return;
  }
  state.updateStatusLoading = true;
  setUpdateMessage("正在读取更新状态…");
  renderUpdateControls();
  try {
    const result = await bridge.apiGet("update/status");
    const payload = normalizeUpdatePayload(result);
    if (state.updateSubmissionUnconfirmed) {
      const jobId = textValue(payload.job_id);
      const targetVersion = textValue(payload.target_version);
      const isNewJob =
        Boolean(jobId) && jobId !== state.updateSubmissionBaselineJobId;
      const isExpectedTarget =
        Boolean(targetVersion)
        && targetVersion === state.updateSubmissionTargetVersion;
      if (!isNewJob || !isExpectedTarget) {
        updateCurrentVersion(payload);
        setUpdateMessage("暂时没有确认到本次更新任务。");
        setUpdateJobNote(
          "状态接口返回的是上一次记录或其他任务；请稍后再次手动刷新，暂勿重复提交更新。",
          "error",
        );
        renderUpdateControls();
        return;
      }
      state.updateSubmissionUnconfirmed = false;
      state.updateExpectedJobId = jobId;
      state.updateSubmissionBaselineJobId = "";
      state.updateSubmissionTargetVersion = "";
    }
    renderUpdateJob(payload);
  } catch (error) {
    setUpdateMessage(messageOf(error), "error");
    setUpdateJobNote("未自动重试；请稍后手动刷新更新状态。");
  } finally {
    state.updateStatusLoading = false;
    renderUpdateControls();
  }
}

function checkResultMessage(status, payload) {
  const serverMessage = textValue(payload.message);
  if (serverMessage) {
    return serverMessage;
  }
  if (status === "no_release") {
    return "仓库还没有正式 Release，当前版本不会自动变更。";
  }
  if (status === "up_to_date") {
    return "已是最新版。";
  }
  if (status === "ahead") {
    return "当前版本高于仓库最新正式 Release。";
  }
  if (status === "incompatible") {
    const requiredVersion = textValue(payload.required_astrbot_version);
    return requiredVersion
      ? `发现新版本，但需要 AstrBot ${requiredVersion}。`
      : "发现新版本，但当前 AstrBot 版本不满足更新要求。";
  }
  if (status === "update_available") {
    return "发现可用更新。";
  }
  return "更新检查已完成。";
}

async function checkForUpdates(force = false) {
  if (state.updateChecking || state.updateApplying || state.updateActive) {
    return;
  }
  state.updateChecking = true;
  state.updateCheckAttempted = true;
  state.updateCheck = null;
  state.updateTargetVersion = "";
  byId("update-release").hidden = true;
  setUpdateMessage("正在检查更新…");
  renderUpdateControls();
  try {
    const result = await bridge.apiGet("update/check", force ? { force: 1 } : {});
    const payload = normalizeUpdatePayload(result);
    const status = updatePhaseOf(payload);
    updateCurrentVersion(payload);
    if (status === "update_available") {
      showReleaseDetails(payload);
      const canApply = updateCanApply(payload);
      state.updateCheck = canApply ? payload : null;
      setUpdateMessage(
        canApply
          ? checkResultMessage(status, payload) || "发现可用更新。"
          : textValue(
              payload.unavailable_reason ?? payload.update_message ?? payload.message,
              "发现新版本，但页内更新不可用；请使用 AstrBot 插件管理更新。",
            ),
        canApply ? "success" : "error",
      );
    } else {
      if (
        status === "incompatible"
        && textValue(payload.target_version ?? payload.latest_version)
      ) {
        showReleaseDetails(payload);
      } else {
        byId("update-release").hidden = true;
      }
      setUpdateMessage(
        checkResultMessage(status, payload),
        status === "incompatible" ? "error" : status === "up_to_date" ? "success" : "normal",
      );
    }
  } catch (error) {
    setUpdateMessage(messageOf(error), "error");
  } finally {
    state.updateChecking = false;
    renderUpdateControls();
  }
}

async function applyAvailableUpdate() {
  const check = state.updateCheck;
  if (!updateCanApply(check) || state.updateApplying || state.updateActive) {
    return;
  }
  const currentVersion = byId("current-version").textContent.trim() || "当前版本";
  const targetVersion = state.updateTargetVersion || "新版本";
  const confirmed = await requestConfirmation({
    title: "确认更新 CanvasForge",
    message:
      `即将从 ${currentVersion} 更新到 ${targetVersion}。\n\n`
      + "来源：GitHub · YuXya/astrbot_plugin_canvasforge\n"
      + "更新期间插件会短暂重载，控制台可能暂时断开。\n"
      + "如果更新失败，可能需要从 AstrBot 插件管理按 GitHub 地址重新安装。\n"
      + "请勿同时从 AstrBot 原生更新入口执行更新。",
    acceptText: `更新到 ${targetVersion}`,
    tone: "primary",
  });
  if (!confirmed) {
    return;
  }

  state.updateApplying = true;
  state.updateReloadAvailable = false;
  setUpdateJobNote("");
  setUpdateMessage("正在确认当前更新状态…");
  renderUpdateControls();

  let baselinePayload;
  try {
    baselinePayload = normalizeUpdatePayload(
      await withTimeout(
        bridge.apiGet("update/status"),
        10_000,
        "读取当前更新状态超时。",
      ),
    );
  } catch (error) {
    state.updateApplying = false;
    setUpdateMessage(messageOf(error), "error");
    setUpdateJobNote(
      "尚未提交更新；请先手动刷新更新状态，确认没有其他任务后再试。",
      "error",
    );
    renderUpdateControls();
    return;
  }

  const baselinePhase = updatePhaseOf(baselinePayload);
  if (activeUpdatePhases.has(baselinePhase)) {
    state.updateApplying = false;
    renderUpdateJob(baselinePayload);
    setUpdateJobNote(
      "检测到已有更新任务；请稍后手动刷新状态，不要重复提交。",
    );
    renderUpdateControls();
    return;
  }

  state.updateLastStatusJobId = textValue(baselinePayload.job_id);
  state.updateSubmissionBaselineJobId = state.updateLastStatusJobId;
  state.updateSubmissionTargetVersion = targetVersion;
  state.updateSubmissionUnconfirmed = false;
  setUpdateMessage("正在提交更新任务…");
  renderUpdateControls();

  try {
    const result = await withTimeout(
      bridge.apiPost("update/apply", {
        check_id: check.check_id,
      }),
      15_000,
      "更新提交响应超时。",
    );
    const payload = normalizeUpdatePayload(result);
    if (payload.accepted !== true || !textValue(payload.job_id)) {
      throw new Error("更新任务响应异常，尚未确认任务已受理。");
    }
    state.updateCheck = null;
    state.updateApplying = false;
    state.updateActive = true;
    state.updateExpectedJobId = textValue(payload.job_id);
    state.updateSubmissionBaselineJobId = "";
    state.updateSubmissionTargetVersion = "";
    state.updatePhase = updatePhaseOf(payload) || "accepted";
    if (!activeUpdatePhases.has(state.updatePhase)) {
      state.updatePhase = "accepted";
    }
    state.updateTargetVersion = textValue(
      payload.target_version,
      state.updateTargetVersion,
    );
    setUpdateMessage(updateStatusMessage(state.updatePhase, payload));
    setUpdateJobNote("更新已在后台受理；请稍后手动刷新更新状态。");
    renderUpdateControls();
  } catch (error) {
    state.updateApplying = false;
    state.updateCheck = null;
    state.updateActive = true;
    state.updateExpectedJobId = "";
    state.updateSubmissionUnconfirmed = true;
    state.updatePhase = "accepted";
    setUpdateMessage(messageOf(error), "error");
    setUpdateJobNote(
      "无法自动确认任务是否已受理；请手动刷新更新状态，避免重复提交。",
      "error",
    );
    renderUpdateControls();
  }
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

function renderSettingsControls() {
  byId("save-settings").disabled =
    !state.settingsLoaded || state.settingsLoading;
  byId("reload-settings").disabled = state.settingsLoading;
}

function applySettings(settings) {
  for (const key of settingKeys) {
    const input = byId(key);
    if (input && Object.hasOwn(settings, key)) {
      if (booleanSettingKeys.has(key)) {
        input.checked = settings[key] === true;
      } else {
        input.value = String(settings[key]);
      }
    }
  }
  syncCompressionState();
}

function collectSettings() {
  const values = {};
  for (const key of settingKeys) {
    const input = byId(key);
    if (booleanSettingKeys.has(key)) {
      values[key] = input.checked;
    } else {
      values[key] = numericSettingKeys.has(key) ? Number.parseInt(input.value, 10) : input.value.trim();
    }
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
  if (state.settingsLoading) {
    return;
  }
  state.settingsLoading = true;
  const reload = byId("reload-settings");
  setButtonBusy(reload, true, "读取中…");
  renderSettingsControls();
  setInlineMessage("正在读取…");
  try {
    const settings = await bridge.apiGet("settings");
    applySettings(settings);
    updateCurrentVersion(settings);
    const cacheLimit = Number(settings.cache_max_images);
    state.settingsCacheLimit = Number.isFinite(cacheLimit) ? cacheLimit : null;
    state.settingsLoaded = true;
    setInlineMessage("");
  } catch (error) {
    state.settingsLoaded = false;
    setInlineMessage(messageOf(error), "error");
  } finally {
    state.settingsLoading = false;
    setButtonBusy(reload, false);
    renderSettingsControls();
  }
}

async function saveSettings(event) {
  event.preventDefault();
  if (!state.settingsLoaded || state.settingsLoading) {
    setInlineMessage("请先成功读取设置，再进行保存。", "error");
    return;
  }
  const form = byId("settings-form");
  if (!form.reportValidity()) {
    return;
  }
  const submitted = collectSettings();
  const previousCacheLimit = state.settingsCacheLimit;
  const button = byId("save-settings");
  setButtonBusy(button, true, "保存中…");
  setInlineMessage("");
  try {
    const result = await bridge.apiPost("settings", submitted);
    const savedSettings =
      result && result.settings && typeof result.settings === "object"
        ? result.settings
        : submitted;
    if (result && result.settings) {
      applySettings(result.settings);
    }
    const savedCacheLimit = Number(savedSettings.cache_max_images);
    state.settingsCacheLimit = Number.isFinite(savedCacheLimit)
      ? savedCacheLimit
      : previousCacheLimit;
    const evicted = Number(result && result.evicted);
    setInlineMessage(
      evicted > 0
        ? `设置已保存，并淘汰 ${evicted} 张旧图。`
        : "设置已保存。",
      "success",
    );
    if (
      previousCacheLimit !== null
      && state.settingsCacheLimit !== null
      && previousCacheLimit !== state.settingsCacheLimit
    ) {
      markCacheStale();
    }
  } catch (error) {
    setInlineMessage(messageOf(error), "error");
  } finally {
    setButtonBusy(button, false);
    renderSettingsControls();
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

function cacheModeInfo(value) {
  const mode = String(value || "").trim().toLowerCase();
  if (mode === "generate") {
    return {
      key: "generate",
      label: "文生图",
      detail: "文生图（generations）",
    };
  }
  if (mode === "edit") {
    return {
      key: "edit",
      label: "图生图",
      detail: "图生图（edits）",
    };
  }
  if (mode === "recovered") {
    return {
      key: "unknown",
      label: "模式未知",
      detail: "恢复缓存，未记录生成方式",
    };
  }
  return {
    key: "unknown",
    label: "模式未知",
    detail: "旧缓存未记录生成方式",
  };
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

function renderCacheSummary() {
  const summary = byId("cache-summary");
  if (state.cacheStale) {
    summary.textContent = "缓存设置已变化，当前列表可能已过期；请手动刷新。";
    return;
  }
  if (!state.cacheLoaded) {
    summary.textContent = "尚未读取缓存；生成新图后请点击“刷新缓存”查看最新模式。";
    return;
  }
  summary.textContent =
    state.cacheLimit === 0
      ? `共 ${state.items.length} 张；当前已停止新增缓存。`
      : `共 ${state.items.length} 张，最多保留 ${Number.isFinite(state.cacheLimit) ? state.cacheLimit : 3} 张。`;
}

function renderCacheControls() {
  byId("clear-cache").disabled =
    state.cacheLoading
    || !state.cacheLoaded
    || state.cacheStale
    || state.items.length === 0;
}

function markCacheStale() {
  if (!state.cacheLoaded && !state.cacheStale) {
    return;
  }
  state.cacheLoaded = false;
  state.cacheStale = true;
  if (state.observer) {
    state.observer.disconnect();
  }
  renderCacheSummary();
  renderCacheControls();
  setCacheMessage("缓存列表不会自动重载；请点击“刷新缓存”读取最新内容。");
}

function createCard(item) {
  const article = document.createElement("article");
  article.className = "cache-card";
  article.dataset.cacheId = item.id;
  const modeInfo = cacheModeInfo(item.mode);

  const previewButton = document.createElement("button");
  previewButton.className = "image-button";
  previewButton.type = "button";
  previewButton.setAttribute(
    "aria-label",
    `预览 ${formatTime(item.created_at)} 生成的${modeInfo.label}图片`,
  );
  previewButton.addEventListener("click", () => openPreview(item));

  const image = document.createElement("img");
  image.alt = "CanvasForge 生成图片缩略图";
  image.loading = "lazy";
  image.dataset.cacheId = item.id;
  const placeholder = document.createElement("span");
  placeholder.className = "image-placeholder";
  placeholder.textContent = "载入缩略图";
  const modeBadge = document.createElement("span");
  modeBadge.className = "cache-mode-badge";
  modeBadge.dataset.mode = modeInfo.key;
  modeBadge.textContent = modeInfo.label;
  previewButton.append(image, placeholder, modeBadge);

  const body = document.createElement("div");
  body.className = "card-body";
  const heading = document.createElement("div");
  heading.className = "card-heading";
  const title = document.createElement("h3");
  title.textContent = modeInfo.label;
  const time = document.createElement("time");
  time.dateTime = item.created_at || "";
  time.textContent = formatTime(item.created_at);
  heading.append(title, time);

  const metadata = document.createElement("div");
  metadata.className = "metadata";
  const dimensions = item.size || (item.width && item.height ? `${item.width}×${item.height}` : "");
  metadata.append(
    labelValue("生成方式", modeInfo.detail),
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
  if (state.cacheLoading) {
    return;
  }
  const generation = ++state.cacheLoadGeneration;
  const refresh = byId("refresh-cache");
  state.cacheLoading = true;
  setButtonBusy(refresh, true, "读取中…");
  renderCacheControls();
  setCacheMessage("");
  try {
    const result = await bridge.apiGet("cache");
    if (generation !== state.cacheLoadGeneration) {
      return;
    }
    state.items = Array.isArray(result.items) ? result.items : [];
    const limit = Number(result.limit);
    state.cacheLimit = Number.isFinite(limit) ? limit : null;
    state.cacheLoaded = true;
    state.cacheStale = false;
    renderCacheSummary();
    renderGallery();
  } catch (error) {
    if (generation === state.cacheLoadGeneration) {
      setCacheMessage(messageOf(error), "error");
    }
  } finally {
    if (generation === state.cacheLoadGeneration) {
      state.cacheLoading = false;
      setButtonBusy(refresh, false);
      renderCacheControls();
    }
  }
}

function invalidateCacheLoads() {
  state.cacheLoadGeneration += 1;
  state.cacheLoading = false;
  setButtonBusy(byId("refresh-cache"), false);
  renderCacheControls();
}

async function openPreview(item) {
  const dialog = byId("preview-dialog");
  const image = byId("preview-image");
  const loading = byId("preview-loading");
  const generation = ++state.previewGeneration;
  state.previewItem = item;
  const modeInfo = cacheModeInfo(item.mode);
  byId("preview-title").textContent = `${modeInfo.label} · ${formatTime(item.created_at)}`;
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

function requestConfirmation({
  title = "确认操作",
  message,
  acceptText = "确认",
  tone = "danger",
}) {
  if (state.confirmResolve) {
    resolveConfirmation(false);
  }
  byId("confirm-title").textContent = title;
  byId("confirm-message").textContent = message;
  const accept = byId("confirm-accept");
  accept.textContent = acceptText;
  accept.classList.toggle("primary", tone === "primary");
  accept.classList.toggle("danger", tone !== "primary");
  return new Promise((resolve) => {
    state.confirmResolve = resolve;
    byId("confirm-dialog").showModal();
    window.requestAnimationFrame(() => {
      byId("confirm-cancel").focus();
    });
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
  if (
    !(await requestConfirmation({
      title: "删除缓存图片",
      message: "确定删除这张缓存图片吗？删除后无法恢复。",
      acceptText: "确认删除",
      tone: "danger",
    }))
  ) {
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
    renderCacheSummary();
    renderCacheControls();
    setCacheMessage("缓存图片已删除。", "success");
  } catch (error) {
    setCacheMessage(messageOf(error), "error");
    setButtonBusy(button, false);
  }
}

async function clearCache() {
  if (!state.cacheLoaded || state.cacheStale) {
    setCacheMessage("请先手动刷新缓存列表，再执行清空。", "error");
    return;
  }
  if (state.items.length === 0) {
    setCacheMessage("当前没有可清空的图片。");
    return;
  }
  if (
    !(await requestConfirmation({
      title: "清空图片缓存",
      message: `确定清空全部 ${state.items.length} 张缓存图片吗？此操作无法恢复。`,
      acceptText: "确认清空",
      tone: "danger",
    }))
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
    renderCacheSummary();
    setCacheMessage(`已清空 ${Number(result.removed) || 0} 张图片。`, "success");
  } catch (error) {
    setCacheMessage(messageOf(error), "error");
  } finally {
    setButtonBusy(button, false);
    renderCacheControls();
  }
}

function wireEvents() {
  for (const button of document.querySelectorAll(".tab")) {
    button.addEventListener("click", () => activateTab(button.dataset.tab));
  }
  byId("check-update").addEventListener("click", () => {
    checkForUpdates(true);
  });
  byId("refresh-update-status").addEventListener("click", refreshUpdateStatus);
  byId("apply-update").addEventListener("click", applyAvailableUpdate);
  byId("reload-console").addEventListener("click", () => {
    window.location.reload();
  });
  byId("settings-form").addEventListener("submit", saveSettings);
  byId("reload-settings").addEventListener("click", loadSettings);
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
  renderUpdateControls();
  renderSettingsControls();
  renderCacheSummary();
  renderCacheControls();
  if (!bridge) {
    setInlineMessage("AstrBot Page bridge 不可用。", "error");
    return;
  }
  try {
    await bridge.ready();
    await loadSettings();
  } catch (error) {
    setInlineMessage(messageOf(error), "error");
  }
}

initialize();
