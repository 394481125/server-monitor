"use strict";

const state = {
  user: null,
  csrf: null,
  page: "dashboard",
  timer: null,
  alertTimer: null,
  platformTimer: null,
  lastAlertId: null,
  alertNotifiedKeys: new Set(),
  openGpuDetail: null,
  refreshMs: 5000,
  timeZone: "Asia/Shanghai",
  dashboardFilters: {search: "", status: "", tags: [], gpu_user: ""},
  hostsCache: [],
  detailHostId: null,
  history: null,
  fileHostId: null,
  filePath: "/",
  developmentHostId: null,
  developmentRoots: {},
  scanSettings: {scan_timeout_seconds:60, scan_max_depth:8, scan_result_limit:100, scan_minimum_mib:1024, environment_inventory_timeout:60},
  scanSettingsPromise: null,
  scanSettingsLoaded: false,
  stressTimers: new Set(),
};

const pages = {
  dashboard: ["集群概览", "全部受管服务器的实时缓存状态"],
  hosts: ["主机管理", "管理 SSH 连接、采集策略与运维权限"],
  files: ["文件管理", "通过 SSH 安全浏览和管理服务器文件"],
  jobs: ["调度记录", "GPU 自动调度的提交动作和执行结果"],
  alerts: ["告警事件", "查看当前告警和已恢复事件"],
  logs: ["审计日志", "追踪用户操作及远端状态变更"],
  settings: ["系统设置", "配置采集、告警、调度、安全和数据策略"],
  environments: ["开发环境", "盘点 GPU 软件栈，管理受约束的 Python 环境与 APT 方案"],
  permissions: ["权限与界面", "管理员授权，用户调整个人页面显示"],
};

const pagePermissions = {dashboard:"page.dashboard", hosts:"page.hosts", files:"page.files", jobs:"page.jobs", alerts:"page.alerts", logs:"page.logs", settings:"page.settings", environments:"page.environments"};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const {escapeHtml:esc, formatBytes:fmtBytes, formatPercentage:percentage, formatDuration:duration, formatShortDuration:shortDuration, dashboardMatches, normalizeBenchmark} = window.ServerMonitorLogic;
const same = (left, right) => JSON.stringify(left) === JSON.stringify(right);
const isAdmin = () => state.user?.role === "admin";
const can = (permission) => isAdmin() || Boolean(state.user?.permissions?.includes(permission));
const canShowPage = (page) => page === "permissions" || (can(pagePermissions[page]) && (isAdmin() || state.user?.visible_pages?.includes(pagePermissions[page])));

function applyNavigationPermissions() {
  $$("#main-nav button").forEach((button) => { button.hidden = !canShowPage(button.dataset.page); });
}

function fmtTime(value) {
  if (!value) return "从未";
  try {
    return new Intl.DateTimeFormat("zh-CN", {dateStyle:"short", timeStyle:"medium", timeZone:state.timeZone}).format(new Date(value));
  } catch (_) {
    return new Date(value).toLocaleString("zh-CN");
  }
}

async function api(path, options = {}) {
  const headers = {Accept: "application/json", ...(options.headers || {})};
  if (options.body && typeof FormData !== "undefined" && options.body instanceof FormData) {
    // Browser FormData sets its own multipart boundary and content type.
  } else if (options.body && typeof options.body !== "string") {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.body);
  }
  if (state.csrf && options.method && !["GET", "HEAD"].includes(options.method)) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(path, {...options, headers});
  const type = response.headers.get("content-type") || "";
  const result = type.includes("json") ? await response.json() : await response.text();
  if (!response.ok) {
    const error = new Error(result?.error || `请求失败 (${response.status})`);
    error.status = response.status;
    error.details = result;
    error.requiresElevation = Boolean(result?.requires_elevation);
    error.mustChange = Boolean(result?.must_change_password);
    throw error;
  }
  return result;
}

function startOperationProgress(label, {target = null, timeoutSeconds = null} = {}) {
  const node = document.createElement("section");
  node.className = "operation-progress";
  node.innerHTML = `<div class="operation-progress-head"><strong>${esc(label)}</strong><span data-operation-elapsed>0 秒</span></div><progress max="100" aria-label="${esc(label)}"></progress><div class="operation-progress-detail" data-operation-detail></div>`;
  const host = target || $("#operation-progress-region");
  if (target) {
    target.className = "scan-result-host";
    target.hidden = false;
    target.replaceChildren(node);
  } else {
    host.append(node);
  }
  const meter = $("progress", node);
  const elapsedNode = $("[data-operation-elapsed]", node);
  const detailNode = $("[data-operation-detail]", node);
  const started = Date.now();
  let mode = timeoutSeconds ? "timed" : "indeterminate";
  let manualDetail = "";
  if (!timeoutSeconds) meter.removeAttribute("value");
  const tick = () => {
    const elapsed = Math.max(0, Math.floor((Date.now() - started) / 1000));
    elapsedNode.textContent = `已运行 ${elapsed} 秒`;
    if (mode === "timed") {
      meter.value = Math.min(99, Math.round(elapsed / timeoutSeconds * 100));
      detailNode.textContent = elapsed < timeoutSeconds ? `时间预算 ${elapsed} / ${timeoutSeconds} 秒` : `已到 ${timeoutSeconds} 秒预算，正在等待远端结束`;
    } else if (mode === "indeterminate") {
      detailNode.textContent = manualDetail || "任务正在运行，等待远端返回";
    } else if (mode === "determinate" && manualDetail) {
      detailNode.textContent = manualDetail;
    }
  };
  tick();
  const timer = setInterval(tick, 500);
  return {
    setDeterminate(value, maximum, detail = "") {
      mode = "determinate";
      manualDetail = detail;
      meter.value = maximum > 0 ? Math.max(0, Math.min(100, value / maximum * 100)) : 0;
      tick();
    },
    setIndeterminate(detail = "") {
      mode = "indeterminate";
      manualDetail = detail;
      meter.removeAttribute("value");
      tick();
    },
    stop() {
      clearInterval(timer);
      node.remove();
      if (target && !target.childElementCount) target.hidden = true;
    },
  };
}

async function withOperationProgress(label, action, options = {}) {
  const progress = startOperationProgress(label, options);
  try { return await action(progress); }
  finally { progress.stop(); }
}

function uploadApi(path, form, progress, processingDetail = "内容已送达平台，正在通过 SSH 写入远端服务器") {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", path);
    xhr.setRequestHeader("Accept", "application/json");
    if (state.csrf) xhr.setRequestHeader("X-CSRF-Token", state.csrf);
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable) progress.setDeterminate(event.loaded, event.total, `${fmtBytes(event.loaded)} / ${fmtBytes(event.total)} 已传输到平台`);
    });
    xhr.upload.addEventListener("load", () => progress.setIndeterminate(processingDetail));
    xhr.addEventListener("load", () => {
      const type = xhr.getResponseHeader("content-type") || "";
      let result = xhr.responseText;
      if (type.includes("json")) {
        try { result = JSON.parse(xhr.responseText); } catch (_) { result = {}; }
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else {
        const error = new Error(result?.error || `请求失败 (${xhr.status})`);
        error.status = xhr.status;
        error.details = result;
        reject(error);
      }
    });
    xhr.addEventListener("error", () => reject(new Error("上传连接中断")));
    xhr.send(form);
  });
}

async function getScanSettings(force = false) {
  if (!can("storage.scan")) return state.scanSettings;
  if (!force && state.scanSettingsLoaded) return state.scanSettings;
  if (!force && state.scanSettingsPromise) return state.scanSettingsPromise;
  state.scanSettingsPromise = api("/api/scan-settings").then((result) => {
    state.scanSettings = {...state.scanSettings, ...(result.settings || {})};
    state.scanSettingsLoaded = true;
    return state.scanSettings;
  }).catch(() => state.scanSettings).finally(() => { state.scanSettingsPromise = null; });
  return state.scanSettingsPromise;
}

async function pollPlatformStatus() {
  if (!state.user || $("#app-view").hidden) return;
  try {
    const result = await api("/api/platform-status");
    $("[data-platform-service]").textContent = result.background_running ? "平台正常" : "后台任务异常";
    $("[data-platform-led]").classList.toggle("offline", !result.background_running);
    $(".platform-state").classList.toggle("is-offline", !result.background_running);
    $("[data-platform-host]").textContent = `本机 ${result.hostname}`;
    $("[data-platform-uptime]").textContent = `开机 ${result.uptime_seconds == null ? "-" : shortDuration(result.uptime_seconds)}`;
    $("[data-platform-load]").textContent = `负载 ${result.load_one ?? "-"}`;
    $("[data-platform-memory]").textContent = `内存 ${percentage(result.memory_usage_percent)}`;
    $("[data-platform-disk]").textContent = `数据盘 ${percentage(result.disk_usage_percent)}`;
    $("[data-platform-database]").textContent = `数据库 ${fmtBytes(result.database_bytes)}`;
    $("[data-platform-hosts]").textContent = `受管 ${result.reachable_hosts}/${result.managed_hosts}`;
    $("[data-platform-service]").title = `平台进程已运行 ${shortDuration(result.application_uptime_seconds)}`;
    $("[data-platform-memory]").title = `${fmtBytes(result.memory_used_bytes)} / ${fmtBytes(result.memory_total_bytes)}`;
    $("[data-platform-disk]").title = `${fmtBytes(result.disk_used_bytes)} / ${fmtBytes(result.disk_total_bytes)}`;
  } catch (_) {
    $("[data-platform-service]").textContent = "状态暂不可用";
    $("[data-platform-led]").classList.add("offline");
    $(".platform-state").classList.add("is-offline");
  }
}

function toast(message, type = "success") {
  const element = document.createElement("div");
  element.className = `toast${type === "error" ? " error" : type === "warning" ? " warning" : ""}`;
  element.textContent = message;
  $("#toast-region").append(element);
  setTimeout(() => element.remove(), 4300);
}

function clearRuntime() {
  clearInterval(state.timer);
  clearInterval(state.alertTimer);
  clearInterval(state.platformTimer);
  state.timer = null;
  state.alertTimer = null;
  state.platformTimer = null;
  state.stressTimers.forEach((timer) => clearInterval(timer));
  state.stressTimers.clear();
}

function showLogin() {
  clearRuntime();
  $("#app-view").hidden = true;
  $("#login-view").hidden = false;
  document.body.classList.remove("nav-open");
  state.user = null;
  state.csrf = null;
  state.detailHostId = null;
}

async function initialize() {
  try {
    const result = await api("/api/auth/me");
    acceptUser(result.user);
  } catch (_) {
    showLogin();
  }
}

function acceptUser(user) {
  clearRuntime();
  state.user = user;
  state.csrf = user.csrf_token;
  document.body.dataset.theme = user.theme || "light";
  $("#theme-select").value = user.theme || "light";
  $("#current-user").textContent = `${user.username} · ${isAdmin() ? "管理员" : "普通用户"}`;
  applyNavigationPermissions();
  $("#login-view").hidden = true;
  $("#app-view").hidden = false;
  state.lastAlertId = null;
  state.alertNotifiedKeys.clear();
  const firstPage = ["dashboard", "hosts", "files", "environments", "jobs", "alerts", "logs", "settings", "permissions"].find(canShowPage) || "permissions";
  navigate(firstPage);
  if (can("page.alerts")) {
    pollAlerts();
    state.alertTimer = setInterval(pollAlerts, 5000);
  }
  pollPlatformStatus();
  state.platformTimer = setInterval(pollPlatformStatus, 15000);
  if (can("storage.scan")) getScanSettings();
  if (user.must_change_password) openPasswordDialog(true);
}

async function pollAlerts() {
  try {
    const result = await api("/api/alerts?page_size=20");
    const notificationsEnabled = result.toast_enabled !== false;
    const toastEvents = Object.prototype.hasOwnProperty.call(result, "toast_events") ? new Set(Array.isArray(result.toast_events) ? result.toast_events : []) : null;
    const shouldNotify = (item) => item.notification_allowed !== false && (!toastEvents || toastEvents.has(item.alert_type));
    const active = notificationsEnabled ? result.items.filter((item) => item.state === "active" && !item.acknowledged_at && !item.cleared_at && shouldNotify(item)) : [];
    const badge = $("#alert-count");
    badge.textContent = String(Math.min(active.length, 99));
    badge.hidden = active.length === 0;
    const newest = Math.max(0, ...result.items.map((item) => item.id));
    result.items.filter((item) => item.state === "recovered").forEach((item) => state.alertNotifiedKeys.delete(item.alert_key || `${item.host_id || "platform"}:${item.alert_type}`));
    if (state.lastAlertId != null) {
      result.items.filter((item) => item.id > state.lastAlertId && item.state === "active" && !item.acknowledged_at && !item.cleared_at && shouldNotify(item)).reverse().forEach((item) => {
        const alertKey = item.alert_key || `${item.host_id || "platform"}:${item.alert_type}`;
        if (!notificationsEnabled || state.alertNotifiedKeys.has(alertKey)) return;
        state.alertNotifiedKeys.add(alertKey);
        toast(item.summary, item.severity === "critical" ? "error" : "warning");
        if ("Notification" in window && Notification.permission === "granted") {
          const notification = new Notification(item.severity === "critical" ? "Server Monitor 严重告警" : "Server Monitor 告警", {
            body: item.summary,
            icon: "/static/icon.svg",
            tag: `server-monitor-alert-${item.id}`,
          });
          notification.onclick = () => { window.focus(); navigate("alerts"); notification.close(); };
        }
      });
    }
    state.lastAlertId = Math.max(state.lastAlertId || 0, newest);
  } catch (error) {
    if (error.status === 401) showLogin();
  }
}

async function updateAlertNotificationSetting(enabled) {
  try {
    return await api("/api/alerts/notification-setting", {method:"PATCH", body:{enabled}});
  } catch (error) {
    // Older deployments may still expose only the general settings endpoint.
    if (error.status !== 404) throw error;
    return api("/api/settings", {method:"PATCH", body:{toast_enabled:enabled}});
  }
}

function setHeader(title, subtitle, parent = null) {
  $("#page-title").textContent = title;
  $("#page-subtitle").textContent = subtitle;
  $("#breadcrumb").innerHTML = parent
    ? `<span>控制台</span><i>/</i><span>${esc(parent)}</span><i>/</i><strong>${esc(title)}</strong>`
    : `<span>控制台</span><i>/</i><strong>${esc(title)}</strong>`;
}

function closeMobileNav() {
  document.body.classList.remove("nav-open");
  $("#sidebar-scrim").hidden = true;
}

function navigate(page) {
  if (!canShowPage(page)) page = "permissions";
  clearInterval(state.timer);
  state.timer = null;
  state.page = page;
  state.detailHostId = null;
  state.history = null;
  $$("#main-nav button").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  setHeader(...pages[page]);
  closeMobileNav();
  renderPage();
}

async function renderPage() {
  const requestedPage = state.page;
  $("#page-content").innerHTML = '<div class="loading">正在读取缓存数据</div>';
  try {
    if (requestedPage === "dashboard") await renderDashboard();
    else if (requestedPage === "hosts") await renderHosts();
    else if (requestedPage === "jobs") await renderJobs();
    else if (requestedPage === "alerts") await renderAlerts();
    else if (requestedPage === "logs") await renderLogs();
    else if (requestedPage === "files") await renderFiles();
    else if (requestedPage === "settings") await renderSettings();
    else if (requestedPage === "environments") await renderDevelopmentPage();
    else if (requestedPage === "permissions") await renderPermissions();
  } catch (error) {
    if (error.status === 401) return showLogin();
    if (state.page === requestedPage) $("#page-content").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`;
  }
}

function statusName(host) {
  if (!host.enabled) return "已禁用";
  return ({
    online:"在线", degraded:"指标降级", gpu_error:"GPU 采集失败", offline:"离线",
    ssh_unreachable:"SSH 网络不通", auth_failed:"SSH 认证失败", collection_timeout:"采集超时",
    command_error:"采集命令失败", busy:"采集繁忙", fingerprint_error:"指纹异常",
    unknown:"等待采集", disabled:"已禁用",
  })[host.status] || "等待采集";
}

function metricBar(label, value, settings, fallback = "未知") {
  const numeric = value == null ? 0 : Math.max(0, Math.min(100, Number(value)));
  const level = value == null ? "" : numeric >= settings.yellow_threshold ? "danger" : numeric >= settings.green_threshold ? "warning" : "";
  return `<div class="metric-line"><header><span>${esc(label)}</span><strong>${percentage(value, fallback)}</strong></header><progress class="bar ${level}" max="100" value="${numeric}" aria-label="${esc(label)} ${percentage(value, fallback)}"></progress></div>`;
}

function finiteNumber(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function gpuCardSummary(gpus, settings) {
  const utilThreshold = finiteNumber(settings.gpu_util_threshold) ?? 10;
  const memoryThreshold = finiteNumber(settings.gpu_memory_threshold) ?? 10;
  const summary = {total: gpus.length, busy: 0, idle: 0, unknown: 0, devices: []};
  for (const gpu of gpus) {
    const utilization = finiteNumber(gpu.utilization_percent);
    const memory = finiteNumber(gpu.memory_percent);
    const hasProcesses = Array.isArray(gpu.processes) && gpu.processes.length > 0;
    const busy = hasProcesses || (utilization != null && utilization >= utilThreshold) || (memory != null && memory >= memoryThreshold);
    const state = busy ? "busy" : utilization != null && memory != null ? "idle" : "unknown";
    summary[state] += 1;
    summary.devices.push({
      index: gpu.index ?? "?",
      state,
      name: gpu.name || "未知型号",
      utilization,
      memory,
      memoryUsedMib: gpu.memory_used_mib,
      memoryTotalMib: gpu.memory_total_mib,
      processes: Array.isArray(gpu.processes) ? gpu.processes : [],
    });
  }
  return summary;
}

function gpuDetailMarkup(hostId, gpu, position) {
  const detailKey = `${hostId}:${position}`;
  const detailId = `gpu-detail-${hostId}-${position}`;
  const expanded = state.openGpuDetail === detailKey;
  const processRows = gpu.processes.length ? gpu.processes.map((process) => `<div class="gpu-card-process">
      <div><strong>PID ${esc(process.pid ?? "?")}${process.pid_exists === false ? "（已不存在）" : ""}</strong><span>${esc(process.user || "unknown")} · ${esc(process.memory_mib ?? "?")} MiB</span></div>
      <dl><div><dt>工作目录</dt><dd class="mono">${esc(process.cwd || "无权限/不可用")}</dd></div><div><dt>命令</dt><dd class="mono">${esc(process.command || process.name || "未知")}</dd></div></dl>
    </div>`).join("") : '<div class="card-section-empty">暂无可归属的 GPU 计算进程</div>';
  return {
    button: `<button type="button" class="gpu-device ${gpu.state}" data-gpu-detail-toggle="${esc(detailKey)}" aria-controls="${esc(detailId)}" aria-expanded="${expanded}">GPU ${esc(gpu.index)}</button>`,
    panel: `<div class="gpu-card-detail" id="${esc(detailId)}" data-gpu-detail="${esc(detailKey)}" ${expanded ? "" : "hidden"}><header><div><strong>GPU ${esc(gpu.index)} · ${esc(gpu.name)}</strong><span>利用率 ${percentage(gpu.utilization)} · 显存 ${esc(gpu.memoryUsedMib ?? "?")} / ${esc(gpu.memoryTotalMib ?? "?")} MiB（${percentage(gpu.memory)}）</span></div><span>${gpu.processes.length} 个进程</span></header>${processRows}</div>`,
  };
}

function physicalDisks(data) {
  if (Array.isArray(data.block_devices) && data.block_devices.length) return data.block_devices;
  const physicalName = /^(?:(?:sd|vd|xvd|hd)[a-z]+|nvme\d+n\d+|mmcblk\d+|md\d+)$/;
  return (data.disks_io || []).filter((disk) => physicalName.test(String(disk.name || "")));
}

function storageUsageRows(filesystems, settings) {
  if (!filesystems.length) return '<div class="card-section-empty">等待存储采样</div>';
  return `<div class="storage-list">${filesystems.map((filesystem) => {
    const usage = finiteNumber(filesystem.usage_percent);
    const numeric = usage == null ? 0 : Math.max(0, Math.min(100, usage));
    const level = usage == null ? "" : numeric >= settings.yellow_threshold ? "danger" : numeric >= settings.green_threshold ? "warning" : "";
    const mountpoint = filesystem.mountpoint || filesystem.filesystem || "未命名挂载点";
    const capacity = filesystem.used == null || filesystem.total == null ? "容量未知" : `${fmtBytes(filesystem.used)} / ${fmtBytes(filesystem.total)}`;
    return `<div class="storage-row"><header><span title="${esc(`${filesystem.filesystem || ""} · ${mountpoint}`)}">${esc(mountpoint)}</span><strong>${percentage(usage)}</strong></header><progress class="bar ${level}" max="100" value="${numeric}" aria-label="${esc(`${mountpoint} ${percentage(usage)}`)}"></progress><small>${esc(capacity)}</small></div>`;
  }).join("")}</div>`;
}

function hostCard(item, settings) {
  const host = item.host;
  const data = item.latest?.data || {};
  const gpus = data.gpus || [];
  const filesystems = data.filesystems || [];
  const gpuSummary = gpuCardSummary(gpus, settings);
  const disks = physicalDisks(data);
  const diskCapacity = disks.reduce((total, disk) => total + (finiteNumber(disk.size) || 0), 0);
  const cpuCores = finiteNumber(data.cpu?.logical_cores);
  const memory = data.memory || {};
  const gpuUsers = [...new Set(gpus.flatMap((gpu) => (gpu.processes || []).map((process) => String(process.user || "unknown"))))];
  const gpuFallback = data.tools?.["nvidia-smi"] === "未安装" ? "未安装" : "无 GPU";
  const gpuDetails = gpuSummary.devices.map((gpu, position) => gpuDetailMarkup(host.id, gpu, position));
  return `<article class="host-card" data-host-status="${esc(host.status || "unknown")}" data-host-search="${esc([host.name, host.address, host.is_local ? "本机" : "", ...(host.tags || [])].join(" ").toLowerCase())}" data-host-tags="${esc(JSON.stringify(host.tags || []))}" data-gpu-users="${esc(JSON.stringify(gpuUsers))}">
    <div class="host-head"><div><div class="host-title-line"><h3>${esc(host.name)}</h3>${host.is_local ? '<span class="local-badge">本机</span>' : ""}</div><p title="${esc(`${host.address}:${host.port} · ${host.username}`)}">${esc(host.address)}:${host.port} · ${esc(host.username)}</p></div><span class="status ${esc(host.status || "unknown")}" title="${esc(host.last_error || statusName(host))}">${statusName(host)}</span></div>
    <div class="tag-line">${(host.tags || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join("") || '<span class="hint">无标签</span>'}</div>
    <div class="resource-summary" aria-label="${esc(`${host.name} 资源概览`)}">
      <div class="resource-stat"><span>CPU</span><strong>${cpuCores == null ? "待采样" : `${cpuCores} 核`}</strong><small>${percentage(data.cpu?.usage_percent, "-")}</small></div>
      <div class="resource-stat"><span>内存</span><strong>${memory.total == null ? "待采样" : fmtBytes(memory.total)}</strong><small>${percentage(memory.usage_percent, "-")}</small></div>
      <div class="resource-stat"><span>GPU</span><strong>${gpus.length ? `${gpus.length} 张` : gpuFallback}</strong><small>${gpus.length ? `${gpuSummary.busy} 使用 / ${gpuSummary.idle} 空闲` : "-"}</small></div>
      <div class="resource-stat"><span>硬盘</span><strong>${disks.length ? `${disks.length} 块` : "待采样"}</strong><small>${diskCapacity ? fmtBytes(diskCapacity) : `${filesystems.length || 0} 挂载点`}</small></div>
    </div>
    <div class="metrics">${metricBar(`CPU 使用率${cpuCores == null ? "" : ` · ${cpuCores} 逻辑核`}`, data.cpu?.usage_percent, settings, "待采样")}${metricBar(`内存 · ${memory.used == null || memory.total == null ? "容量待采样" : `${fmtBytes(memory.used)} / ${fmtBytes(memory.total)}`}`, memory.usage_percent, settings, "待采样")}</div>
    <section class="card-section gpu-overview"><header><span>GPU 状态</span><strong>${gpus.length ? `${gpuSummary.busy} 使用中 · ${gpuSummary.idle} 空闲${gpuSummary.unknown ? ` · ${gpuSummary.unknown} 未知` : ""}` : gpuFallback}</strong></header>${gpus.length ? `<div class="gpu-device-list">${gpuDetails.map((item) => item.button).join("")}</div><div class="gpu-detail-list">${gpuDetails.map((item) => item.panel).join("")}</div>` : '<div class="card-section-empty">NVIDIA 指标将在采样后显示</div>'}</section>
    <section class="card-section storage-overview"><header><span>存储容量</span><strong>${disks.length ? `${disks.length} 块盘 · ${filesystems.length} 挂载点` : `${filesystems.length || 0} 个挂载点`}</strong></header>${storageUsageRows(filesystems, settings)}</section>
    <div class="host-foot"><span>最后成功 ${fmtTime(host.last_success_at)}</span><span>${can("host.refresh") && host.enabled ? `<button data-refresh="${host.id}" title="提交一次采集任务">刷新</button>` : ""}<button data-detail="${host.id}">详情</button></span></div>
  </article>`;
}

function applyDashboardFilters() {
  $$(".host-card").forEach((card) => {
    card.hidden = !dashboardMatches(state.dashboardFilters, {
      search: card.dataset.hostSearch,
      status: card.dataset.hostStatus,
      tags: JSON.parse(card.dataset.hostTags || "[]"),
      gpuUsers: JSON.parse(card.dataset.gpuUsers || "[]"),
    });
  });
}

async function saveDashboardView() {
  const name = prompt("快捷视图名称", "");
  if (!name?.trim()) return;
  await api("/api/saved-views", {method:"POST", body:{page:"dashboard", name:name.trim(), filters:state.dashboardFilters}});
  toast("快捷视图已保存");
  renderDashboard();
}

async function deleteDashboardView(views) {
  const viewId = Number($("#dashboard-saved-view")?.value);
  const view = views.find((item) => item.id === viewId);
  if (!view || !confirm(`确认删除快捷视图“${view.name}”？`)) return;
  await api(`/api/saved-views/${view.id}`, {method:"DELETE", body:{}});
  toast("快捷视图已删除");
  renderDashboard();
}

function startDashboardTimer() {
  clearInterval(state.timer);
  if (state.page === "dashboard") state.timer = setInterval(() => { if (!document.hidden) renderDashboard(true); }, state.refreshMs);
}

async function renderDashboard(backgroundRefresh = false) {
  const [result, viewsResult] = await Promise.all([api("/api/dashboard"), api("/api/saved-views?page=dashboard")]);
  if (state.page !== "dashboard") return;
  state.refreshMs = Math.max(3000, Number(result.settings.frontend_refresh_interval || 5) * 1000);
  if (result.settings.timezone) state.timeZone = result.settings.timezone;
  const items = result.items;
  const views = viewsResult.items || [];
  const allTags = [...new Set(items.flatMap((item) => item.host.tags || []))].sort((left, right) => left.localeCompare(right, "zh-CN"));
  const attentionStatuses = ["degraded", "gpu_error", "busy", "fingerprint_error", "offline", "ssh_unreachable", "auth_failed", "collection_timeout", "command_error"];
  const offlineStatuses = ["offline", "ssh_unreachable", "auth_failed", "collection_timeout", "command_error"];
  const online = items.filter((item) => item.host.status === "online").length;
  const warning = items.filter((item) => attentionStatuses.includes(item.host.status)).length;
  const offline = items.filter((item) => offlineStatuses.includes(item.host.status)).length;
  $("#page-content").innerHTML = `<div class="dashboard-local-overview">
      <div><strong>本机概览</strong><span>${result.local_configured ? "直接读取监控服务所在机器的系统指标" : "尚未配置本机概览"}</span></div>
      ${switchControl("show_local_overview", "显示本机", result.show_local_overview)}
    </div>
    ${result.show_local_overview && !result.local_configured ? `<div class="notice-panel local-overview-setup"><div><strong>需要配置本机概览</strong><span>${can("host.manage") ? "配置后会直接读取本机指标，并保留主机详情与运维功能。" : "请联系管理员添加并标记本机概览。"}</span></div>${can("host.manage") ? '<button id="configure-local-host" class="primary">配置本机</button>' : ""}</div>` : ""}
    <div class="stats-band">
      <button class="stat" data-summary-status=""><small>受管主机</small><strong>${items.length}</strong></button>
      <button class="stat success" data-summary-status="online"><small>在线</small><strong>${online}</strong></button>
      <button class="stat warning" data-summary-status="attention"><small>需要关注</small><strong>${warning}</strong></button>
      <button class="stat danger-tone" data-summary-status="failed"><small>连接或采集失败</small><strong>${offline}</strong></button>
    </div>
    <div class="dashboard-filter-band">
      <div class="toolbar-search"><input id="dashboard-search" placeholder="搜索名称、地址或标签" value="${esc(state.dashboardFilters.search)}"></div>
      <label>状态<select id="dashboard-status"><option value="">全部状态</option><option value="online">在线</option><option value="degraded">指标降级</option><option value="gpu_error">GPU 采集失败</option><option value="ssh_unreachable">SSH 网络不通</option><option value="auth_failed">SSH 认证失败</option><option value="collection_timeout">采集超时</option><option value="command_error">采集命令失败</option><option value="busy">采集繁忙</option><option value="offline">连续采集失败</option><option value="fingerprint_error">指纹异常</option><option value="unknown">等待采集</option></select></label>
      <label>标签（多选）<select id="dashboard-tags" multiple size="2">${allTags.map((tag) => `<option value="${esc(tag)}" ${state.dashboardFilters.tags.includes(tag) ? "selected" : ""}>${esc(tag)}</option>`).join("")}</select></label>
      <label>GPU 用户<select id="dashboard-gpu-user"><option value="">全部用户</option>${(result.gpu_users || []).map((item) => `<option value="${esc(item.username)}" ${state.dashboardFilters.gpu_user === item.username ? "selected" : ""}>${esc(item.username)} · ${item.gpu_count} 卡</option>`).join("")}</select></label>
      <label>快捷视图<select id="dashboard-saved-view"><option value="">选择视图</option>${views.map((view) => `<option value="${view.id}">${esc(view.name)}</option>`).join("")}</select></label>
      <div class="toolbar-group"><button id="save-dashboard-view" title="保存当前筛选条件">保存视图</button><button id="delete-dashboard-view" class="danger-quiet" ${views.length ? "" : "disabled"}>删除视图</button>${can("host.manage") ? '<button id="add-host" class="primary"><span aria-hidden="true">+</span> 添加主机</button>' : ""}</div>
    </div>
    ${(result.gpu_users || []).length ? `<details class="gpu-user-summary"><summary><strong>GPU 用户占用</strong><span>${result.gpu_users.length} 个用户 · ${result.gpu_users.reduce((total, item) => total + Number(item.process_count || 0), 0)} 个进程 · 点击查看汇总</span></summary><div class="gpu-user-summary-body">${textTable(["Linux 用户","占用 GPU","进程数","显存合计","涉及主机"], result.gpu_users.map((item) => [item.username,`${item.gpu_count} 张`,item.process_count,`${Number(item.memory_mib).toFixed(1)} MiB`,item.hosts.join("、")]))}</div></details>` : ""}
    ${items.length ? `<div class="host-grid">${items.map((item) => hostCard(item, result.settings)).join("")}</div>` : '<div class="empty"><div><strong>尚未添加主机</strong>添加第一台 Linux 服务器后，采集状态会显示在这里。</div></div>'}`;
  $("#dashboard-status").value = state.dashboardFilters.status;
  bindHostLinks();
  $("#add-host")?.addEventListener("click", () => showHostForm());
  $("#configure-local-host")?.addEventListener("click", () => showHostForm(null, {name:"本机", address:"127.0.0.1", port:22, is_local:true}));
  $('[name="show_local_overview"]')?.addEventListener("change", async (event) => {
    const input = event.target;
    input.disabled = true;
    try {
      await api("/api/profile/local-overview", {method:"PATCH", body:{enabled:input.checked}});
      toast(input.checked ? "本机概览已显示" : "本机概览已隐藏");
      renderDashboard();
    } catch (error) {
      input.checked = !input.checked;
      input.disabled = false;
      toast(error.message, "error");
    }
  });
  $("#dashboard-search")?.addEventListener("input", (event) => { state.dashboardFilters.search = event.target.value.trim().toLowerCase(); applyDashboardFilters(); });
  $("#dashboard-status")?.addEventListener("change", (event) => { state.dashboardFilters.status = event.target.value; applyDashboardFilters(); });
  $("#dashboard-tags")?.addEventListener("change", (event) => { state.dashboardFilters.tags = [...event.target.selectedOptions].map((option) => option.value); applyDashboardFilters(); });
  $("#dashboard-gpu-user")?.addEventListener("change", (event) => { state.dashboardFilters.gpu_user = event.target.value; applyDashboardFilters(); });
  $("#dashboard-saved-view")?.addEventListener("change", (event) => {
    const view = views.find((item) => item.id === Number(event.target.value));
    if (!view) return;
    state.dashboardFilters = {search:"", status:"", tags:[], gpu_user:"", ...view.filters};
    renderDashboard();
  });
  $("#save-dashboard-view")?.addEventListener("click", () => saveDashboardView().catch((error) => toast(error.message, "error")));
  $("#delete-dashboard-view")?.addEventListener("click", () => deleteDashboardView(views).catch((error) => toast(error.message, "error")));
  $$('[data-gpu-detail-toggle]').forEach((button) => button.addEventListener("click", () => {
    const detailKey = button.dataset.gpuDetailToggle;
    state.openGpuDetail = state.openGpuDetail === detailKey ? null : detailKey;
    $$('[data-gpu-detail-toggle]').forEach((item) => item.setAttribute("aria-expanded", String(item.dataset.gpuDetailToggle === state.openGpuDetail)));
    $$('[data-gpu-detail]').forEach((panel) => { panel.hidden = panel.dataset.gpuDetail !== state.openGpuDetail; });
  }));
  $$('[data-summary-status]').forEach((button) => button.addEventListener("click", () => {
    const selected = button.dataset.summaryStatus;
    state.dashboardFilters.status = ["attention", "failed"].includes(selected) ? "" : selected;
    $("#dashboard-status").value = state.dashboardFilters.status;
    if (selected === "attention") {
      applyDashboardFilters();
      $$(".host-card").forEach((card) => { card.hidden ||= !attentionStatuses.includes(card.dataset.hostStatus); });
    } else if (selected === "failed") {
      applyDashboardFilters();
      $$(".host-card").forEach((card) => { card.hidden ||= !offlineStatuses.includes(card.dataset.hostStatus); });
    } else applyDashboardFilters();
  }));
  applyDashboardFilters();
  const indicator = $("#live-indicator");
  indicator.innerHTML = `<i></i>更新于 ${esc(new Date().toLocaleTimeString("zh-CN", {hour12:false}))}`;
  startDashboardTimer();
  if (!backgroundRefresh) setHeader(...pages.dashboard);
}

function bindHostLinks(root = document) {
  $$('[data-detail]', root).forEach((button) => { button.onclick = () => renderHostDetail(Number(button.dataset.detail)); });
  $$('[data-refresh]', root).forEach((button) => { button.onclick = () => refreshHost(Number(button.dataset.refresh), button); });
}

async function refreshHost(hostId, button) {
  const original = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "提交中"; }
  try {
    const result = await api(`/api/hosts/${hostId}/refresh`, {method:"POST", body:{}});
    toast(result.task_id === "busy" ? "该主机已有采集任务，未重复提交" : `采集任务已提交：${result.task_id}` , result.task_id === "busy" ? "warning" : "success");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    if (button) { button.disabled = false; button.textContent = original; }
  }
}

function hostTableRows(items) {
  return items.map((host) => `<tr data-host-id="${host.id}" data-status="${esc(host.status || "unknown")}" data-search="${esc([host.name, host.address, host.is_local ? "本机" : "", ...(host.tags || [])].join(" ").toLowerCase())}" data-updated="${esc(host.runtime_updated_at || host.updated_at || "")}">
    ${can("host.manage") ? `<td class="table-checkbox"><input type="checkbox" data-host-select="${host.id}" aria-label="选择 ${esc(host.name)}"></td>` : ""}
    <td><div class="host-title-line"><strong>${esc(host.name)}</strong>${host.is_local ? '<span class="local-badge">本机</span>' : ""}</div><div class="hint">${esc(host.username)}</div></td><td class="mono">${esc(host.address)}:${host.port}</td>
    <td><span class="status ${esc(host.status || "unknown")}" title="${esc(host.last_error || statusName(host))}">${statusName(host)}</span></td><td>${(host.tags || []).map((tag) => `<span class="tag">${esc(tag)}</span>`).join("") || "-"}</td>
    <td>${fmtTime(host.last_success_at)}</td><td class="nowrap"><button class="text-button" data-detail="${host.id}">详情</button>${can("host.manage") ? `<button class="text-button" data-edit-host="${host.id}">编辑</button><button class="text-button" data-toggle-host="${host.id}" data-enabled="${host.enabled}">${host.enabled ? "禁用" : "启用"}</button>` : ""}</td>
  </tr>`).join("");
}

async function renderHosts() {
  const result = await api("/api/hosts");
  if (state.page !== "hosts") return;
  state.hostsCache = result.items;
  $("#page-content").innerHTML = `<div class="toolbar"><div class="toolbar-group"><div class="toolbar-search"><input id="host-search" placeholder="搜索名称、地址或标签"></div><select id="host-status"><option value="">全部状态</option><option value="online">在线</option><option value="degraded">指标降级</option><option value="gpu_error">GPU 采集失败</option><option value="ssh_unreachable">SSH 网络不通</option><option value="auth_failed">SSH 认证失败</option><option value="collection_timeout">采集超时</option><option value="command_error">采集命令失败</option><option value="busy">采集繁忙</option><option value="offline">连续采集失败</option><option value="fingerprint_error">指纹异常</option><option value="unknown">等待采集</option></select><select id="host-sort"><option value="name">按名称排序</option><option value="status">按状态排序</option><option value="updated">按更新时间排序</option><option value="tags">按标签排序</option></select></div><div class="toolbar-group"><a class="button-link" href="/api/snapshots/current">当前快照 JSON</a>${can("host.manage") ? '<a class="button-link" href="/api/hosts/import-template?format=csv">CSV 模板</a><a class="button-link" href="/api/hosts/import-template?format=json">JSON 模板</a><a class="button-link" href="/api/hosts/export?format=csv">导出 CSV</a><a class="button-link" href="/api/hosts/export?format=json">导出 JSON</a><a class="button-link" href="/api/hardware-assets/export">资产 CSV</a><button id="import-hosts">导入主机</button><button id="show-fingerprints">指纹复核</button><button id="batch-tags" disabled>批量标签</button><button id="batch-test-hosts" disabled>重测 SSH</button><button id="add-host" class="primary"><span aria-hidden="true">+</span> 添加主机</button>' : ""}</div></div>
    ${result.items.length ? `<div class="table-wrap"><table id="hosts-table"><thead><tr>${can("host.manage") ? '<th class="table-checkbox"><input id="select-all-hosts" type="checkbox" aria-label="全选主机"></th>' : ""}<th>主机</th><th>SSH 地址</th><th>状态</th><th>标签</th><th>最后成功</th><th>操作</th></tr></thead><tbody>${hostTableRows(result.items)}</tbody></table></div>` : '<div class="empty"><div><strong>尚未纳管主机</strong>添加主机后可以统一查看状态并执行受控运维操作。</div></div>'}<input id="host-import-input" type="file" accept=".json,.csv,application/json,text/csv" hidden>`;
  bindHostLinks();
  $("#add-host")?.addEventListener("click", () => showHostForm());
  $$('[data-edit-host]').forEach((button) => { button.onclick = () => showHostForm(state.hostsCache.find((host) => host.id === Number(button.dataset.editHost))); });
  $$('[data-toggle-host]').forEach((button) => { button.onclick = () => toggleHost(Number(button.dataset.toggleHost), button.dataset.enabled === "true"); });
  const filter = () => {
    const query = $("#host-search").value.trim().toLowerCase();
    const status = $("#host-status").value;
    $$("#hosts-table tbody tr").forEach((row) => { row.hidden = Boolean((query && !row.dataset.search.includes(query)) || (status && row.dataset.status !== status)); });
  };
  $("#host-search")?.addEventListener("input", filter);
  $("#host-status")?.addEventListener("change", filter);
  $("#host-sort")?.addEventListener("change", sortHostRows);
  $("#select-all-hosts")?.addEventListener("change", (event) => { $$('[data-host-select]').forEach((input) => { input.checked = event.target.checked; }); updateBatchState(); });
  $$('[data-host-select]').forEach((input) => input.addEventListener("change", updateBatchState));
  $("#batch-tags")?.addEventListener("click", showBatchTags);
  $("#batch-test-hosts")?.addEventListener("click", showBatchTest);
  $("#show-fingerprints")?.addEventListener("click", showFingerprintReview);
  $("#import-hosts")?.addEventListener("click", () => $("#host-import-input").click());
  $("#host-import-input")?.addEventListener("change", importHosts);
}

function updateBatchState() {
  const disabled = !$$('[data-host-select]:checked').length;
  [$("#batch-tags"), $("#batch-test-hosts")].forEach((button) => { if (button) button.disabled = disabled; });
}

function sortHostRows(event) {
  const tbody = $("#hosts-table tbody");
  if (!tbody) return;
  const key = event.target.value;
  const order = {fingerprint_error:0, auth_failed:1, ssh_unreachable:2, collection_timeout:3, command_error:4, offline:5, gpu_error:6, busy:7, degraded:8, unknown:9, online:10, disabled:11};
  [...tbody.rows].sort((left, right) => {
    if (key === "status") return (order[left.dataset.status] ?? 9) - (order[right.dataset.status] ?? 9);
    if (key === "updated") return right.dataset.updated.localeCompare(left.dataset.updated);
    if (key === "tags") return left.cells[can("host.manage") ? 4 : 3].textContent.localeCompare(right.cells[can("host.manage") ? 4 : 3].textContent, "zh-CN");
    return left.dataset.search.localeCompare(right.dataset.search, "zh-CN");
  }).forEach((row) => tbody.append(row));
}

async function toggleHost(hostId, enabled) {
  try {
    if (!confirm(`确认${enabled ? "禁用" : "启用"}该主机的定时采集？`)) return;
    await api(`/api/hosts/${hostId}`, {method:"PATCH", body:{enabled:!enabled}});
    toast(`主机采集已${enabled ? "禁用" : "启用"}`);
    renderHosts();
  } catch (error) { toast(error.message, "error"); }
}

function createDialog(content, className = "") {
  const dialog = document.createElement("dialog");
  dialog.className = className;
  dialog.innerHTML = content;
  document.body.append(dialog);
  const remove = () => { if (dialog.isConnected) dialog.remove(); };
  dialog.addEventListener("close", remove, {once:true});
  dialog.addEventListener("cancel", (event) => { event.preventDefault(); dialog.close("cancel"); });
  dialog.showModal();
  return dialog;
}

function showBatchTags() {
  const ids = $$('[data-host-select]:checked').map((input) => Number(input.dataset.hostSelect));
  if (!ids.length) return;
  const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon">#</span><div><h2>批量修改标签</h2><p>将对已选择的 ${ids.length} 台主机分别执行并返回结果。</p></div></div><label>添加标签<input name="add" placeholder="例如：GPU, 生产"></label><label>移除标签<input name="remove" placeholder="例如：测试, 临时"></label><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button class="primary" type="submit">应用标签</button></div></form>`);
  $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    const split = (value) => value.split(",").map((item) => item.trim()).filter(Boolean);
    try {
      const result = await api("/api/hosts/batch-tags", {method:"POST", body:{host_ids:ids, add:split(event.target.add.value), remove:split(event.target.remove.value)}});
      const failed = result.results.filter((item) => !item.success);
      toast(failed.length ? `${result.results.length - failed.length} 台成功，${failed.length} 台失败` : `${result.results.length} 台主机标签已更新`, failed.length ? "warning" : "success");
      dialog.close("done");
      renderHosts();
    } catch (error) { $(".form-error", dialog).textContent = error.message; }
  };
}

function batchResultDialog(title, results, render) {
  const dialog = createDialog(`<div><div class="dialog-heading"><span class="dialog-icon">✓</span><div><h2>${esc(title)}</h2><p>共 ${results.length} 项</p></div></div><div class="table-wrap"><table><thead><tr><th>主机</th><th>结果</th><th>说明</th></tr></thead><tbody>${results.map(render).join("")}</tbody></table></div><div class="dialog-actions"><button type="button" data-close>关闭</button></div></div>`, "wide-dialog");
  $("[data-close]", dialog).onclick = () => dialog.close("done");
}

async function importHosts(event) {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (!file) return;
  try {
    await ensureElevated();
    const form = new FormData();
    form.append("file", file);
    const result = await withOperationProgress(
      "正在导入主机清单",
      (progress) => uploadApi("/api/hosts/import", form, progress, "内容已送达平台，正在校验并导入主机"),
    );
    batchResultDialog("主机导入结果", result.results, (item) => `<tr><td>${esc(item.name)}</td><td><span class="status ${item.success ? "online" : "offline"}">${item.success ? "已纳管" : "失败"}</span></td><td>${esc(item.success ? `主机 ID ${item.host_id}` : item.error)}</td></tr>`);
    toast(`导入完成：成功 ${result.success_count} 台，失败 ${result.failure_count} 台`, result.failure_count ? "warning" : "success");
    renderHosts();
  } catch (error) { toast(error.message, "error"); }
}

async function showBatchTest() {
  const ids = $$('[data-host-select]:checked').map((input) => Number(input.dataset.hostSelect));
  if (!ids.length) return;
  try {
    await ensureElevated();
    const result = await withOperationProgress(
      `正在重测 ${ids.length} 台主机的 SSH`,
      () => api("/api/hosts/batch-test", {method:"POST", body:{host_ids:ids}}),
    );
    const label = {ok:"正常",fingerprint_mismatch:"指纹不一致",physical_identity_changed:"物理身份变化",duplicate:"已纳管重复",failed:"连接失败"};
    batchResultDialog("批量 SSH 重测", result.results, (item) => `<tr><td>${esc(item.name || state.hostsCache.find((host) => host.id === Number(item.host_id))?.name || item.host_id)}</td><td><span class="status ${item.status === "ok" ? "online" : "offline"}">${esc(label[item.status] || item.status)}</span></td><td>${esc(item.error || item.duplicate?.name || (item.status === "ok" ? item.identity?.hostname || "SSH 身份已验证" : "需要逐台处理"))}</td></tr>`);
    toast(`重测完成：正常 ${result.ok_count} 台，需处理 ${result.attention_count} 台`, result.attention_count ? "warning" : "success");
  } catch (error) { toast(error.message, "error"); }
}

async function showFingerprintReview() {
  try {
    const result = await api("/api/hosts/fingerprints");
    const items = result.items || [];
    const dialog = createDialog(`<div><div class="dialog-heading"><span class="dialog-icon">SSH</span><div><h2>SSH 主机指纹复核</h2><p>指纹不会直接清空；必须重新连接并提交本次实际观察到的精确指纹。</p></div></div><div class="table-wrap"><table><thead><tr><th class="table-checkbox"></th><th>主机</th><th>当前指纹</th><th>状态 / 最近原因</th></tr></thead><tbody>${items.map((item) => `<tr><td class="table-checkbox"><input type="checkbox" data-fingerprint-host="${item.host_id}" ${item.status === "fingerprint_error" ? "checked" : ""}></td><td><strong>${esc(item.name)}</strong><div class="hint mono">${esc(item.address)}</div></td><td class="mono">${esc(item.fingerprint || "未记录")}</td><td><span class="status ${esc(item.status || "unknown")}">${statusName({status:item.status, enabled:true})}</span><div class="hint" title="${esc(item.last_error || "")}">${esc(item.last_error || "-")}</div></td></tr>`).join("")}</tbody></table></div><div data-fingerprint-results></div><div class="notice-panel">批量确认只处理明确返回 fingerprint_mismatch 的主机。若 machine-id 同时变化，平台会拒绝批量沿用历史，需要逐台确认物理节点替换。</div><div class="dialog-actions"><button type="button" data-close>关闭</button><button type="button" data-fingerprint-retest>重测所选</button><button type="button" class="danger" data-fingerprint-confirm disabled>确认所选变化</button></div></div>`, "wide-dialog");
    const pending = new Map();
    $("[data-close]", dialog).onclick = () => dialog.close("done");
    $("[data-fingerprint-retest]", dialog).onclick = async () => {
      const ids = $$('[data-fingerprint-host]:checked', dialog).map((input) => Number(input.dataset.fingerprintHost));
      if (!ids.length) return toast("请先选择主机", "warning");
      try {
        await ensureElevated();
        const tested = await withOperationProgress(`正在重测 ${ids.length} 台主机的 SSH 指纹`, () => api("/api/hosts/batch-test", {method:"POST", body:{host_ids:ids}}));
        pending.clear();
        for (const item of tested.results) if (item.status === "fingerprint_mismatch" && item.observed) pending.set(Number(item.host_id), item);
        const labels = {ok:"正常", fingerprint_mismatch:"指纹变化", physical_identity_changed:"物理身份变化", duplicate:"身份重复", failed:"连接失败"};
        $("[data-fingerprint-results]", dialog).innerHTML = `<div class="table-wrap fingerprint-results"><table><thead><tr><th>确认</th><th>主机</th><th>重测结果</th><th>观察值</th></tr></thead><tbody>${tested.results.map((item) => `<tr><td class="table-checkbox">${pending.has(Number(item.host_id)) ? `<input type="checkbox" data-fingerprint-confirm-item="${item.host_id}" checked>` : "-"}</td><td>${esc(item.name || item.host_id)}</td><td>${esc(labels[item.status] || item.status)}<div class="hint">${esc(item.error || item.duplicate?.name || "")}</div></td><td class="mono">${esc(item.observed || item.identity?.fingerprint || "-")}</td></tr>`).join("")}</tbody></table></div>`;
        $("[data-fingerprint-confirm]", dialog).disabled = pending.size === 0;
      } catch (error) { toast(error.message, "error"); }
    };
    $("[data-fingerprint-confirm]", dialog).onclick = async () => {
      const selected = $$('[data-fingerprint-confirm-item]:checked', dialog).map((input) => pending.get(Number(input.dataset.fingerprintConfirmItem))).filter(Boolean);
      if (!selected.length) return toast("没有可确认的指纹变化", "warning");
      if (!confirm(`将重新连接并确认 ${selected.length} 台主机本次实际返回的 SSH 指纹。确认继续？`)) return;
      try {
        await ensureElevated();
        const confirmed = await withOperationProgress(`正在确认 ${selected.length} 台主机指纹`, () => api("/api/hosts/fingerprints/confirm", {method:"POST", body:{items:selected.map((item) => ({host_id:Number(item.host_id), observed:item.observed}))}}));
        toast(`指纹确认完成：成功 ${confirmed.success_count} 台，失败 ${confirmed.failure_count} 台`, confirmed.failure_count ? "warning" : "success");
        dialog.close("done");
        renderHosts();
      } catch (error) { toast(error.message, "error"); }
    };
  } catch (error) { toast(error.message, "error"); }
}

function showMountThresholds(host, filesystems, rules, defaults) {
  const byMountpoint = Object.fromEntries(rules.map((item) => [item.mountpoint, item]));
  const known = [...filesystems, ...rules.filter((rule) => !filesystems.some((filesystem) => filesystem.mountpoint === rule.mountpoint)).map((rule) => ({mountpoint:rule.mountpoint}))];
  const rows = known.map((filesystem) => {
    const rule = byMountpoint[filesystem.mountpoint] || {};
    return `<tr data-mount-rule="${esc(filesystem.mountpoint)}"><td class="mono">${esc(filesystem.mountpoint)}</td><td>${percentage(filesystem.usage_percent)}</td><td>${percentage(filesystem.inode_usage_percent)}</td><td><input name="usage_threshold" type="number" min="1" max="100" step="0.1" value="${esc(rule.usage_threshold ?? "")}" placeholder="${esc(defaults.filesystem_usage)}"></td><td><input name="inode_threshold" type="number" min="1" max="100" step="0.1" value="${esc(rule.inode_threshold ?? "")}" placeholder="${esc(defaults.filesystem_inode)}"></td></tr>`;
  }).join("") || '<tr><td colspan="5">尚无文件系统采样，可添加自定义挂载路径。</td></tr>';
  const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon">%</span><div><h2>挂载点告警覆盖</h2><p>${esc(host.name)} · 留空表示继承系统阈值</p></div></div><div class="table-wrap"><table><thead><tr><th>挂载点</th><th>容量</th><th>inode</th><th>容量阈值</th><th>inode 阈值</th></tr></thead><tbody>${rows}</tbody></table></div><div class="form-grid two mount-threshold-custom"><label>额外挂载点<input name="custom_mountpoint" placeholder="/data"></label><label>容量阈值<input name="custom_usage_threshold" type="number" min="1" max="100" step="0.1" placeholder="${esc(defaults.filesystem_usage)}"></label><label>inode 阈值（可选）<input name="custom_inode_threshold" type="number" min="1" max="100" step="0.1" placeholder="${esc(defaults.filesystem_inode)}"></label></div><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button type="submit" class="primary">保存规则</button></div></form>`, "wide-dialog");
  $("[data-cancel]", dialog).onclick = () => dialog.close("cancel");
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const nextRules = $$('[data-mount-rule]', form).map((row) => ({
      mountpoint: row.dataset.mountRule,
      usage_threshold: $("[name='usage_threshold']", row).value,
      inode_threshold: $("[name='inode_threshold']", row).value,
    })).filter((rule) => rule.usage_threshold || rule.inode_threshold).map((rule) => ({
      ...rule,
      usage_threshold: Number(rule.usage_threshold || defaults.filesystem_usage),
      inode_threshold: rule.inode_threshold ? Number(rule.inode_threshold) : null,
    }));
    if (form.custom_mountpoint.value.trim() || form.custom_usage_threshold.value || form.custom_inode_threshold.value) {
      nextRules.push({
        mountpoint: form.custom_mountpoint.value.trim(),
        usage_threshold: Number(form.custom_usage_threshold.value || defaults.filesystem_usage),
        inode_threshold: form.custom_inode_threshold.value ? Number(form.custom_inode_threshold.value) : null,
      });
    }
    try {
      await api(`/api/hosts/${host.id}/mount-thresholds`, {method:"PUT", body:{rules:nextRules}});
      dialog.close("done");
      toast("挂载点告警规则已保存");
      renderHostDetail(host.id, "storage");
    } catch (error) { $(".form-error", dialog).textContent = error.message; }
  };
}

function switchControl(name, label, checked) {
  return `<label class="switch-label"><input type="checkbox" name="${name}" ${checked ? "checked" : ""}><i aria-hidden="true"></i><span>${esc(label)}</span></label>`;
}

function tagMultiSelectMarkup(tags) {
  const values = tags || [];
  return `<div class="multi-select" data-tag-multi-select><div class="multi-select-control" data-tag-control><div class="multi-select-values" data-tag-values>${values.map((tag) => `<button type="button" class="multi-select-tag" data-tag-remove="${esc(tag)}"><span>${esc(tag)}</span><i aria-hidden="true">×</i></button>`).join("")}</div><input data-tag-query autocomplete="off" placeholder="输入后按回车添加"></div><input name="tags" type="hidden" value="${esc(values.join(","))}"><div class="multi-select-menu" data-tag-menu hidden></div></div>`;
}

function bindTagMultiSelect(form) {
  const root = $("[data-tag-multi-select]", form);
  if (!root) return;
  const hidden = form.tags;
  const input = $("[data-tag-query]", root);
  const valuesRoot = $("[data-tag-values]", root);
  const menu = $("[data-tag-menu]", root);
  const suggestions = [...new Set(state.hostsCache.flatMap((item) => item.tags || []))].sort((left, right) => left.localeCompare(right, "zh-CN"));
  let values = hidden.value.split(",").map((item) => item.trim()).filter(Boolean);
  const sync = () => {
    hidden.value = values.join(",");
    valuesRoot.innerHTML = values.map((tag) => `<button type="button" class="multi-select-tag" data-tag-remove="${esc(tag)}"><span>${esc(tag)}</span><i aria-hidden="true">×</i></button>`).join("");
    const query = input.value.trim().toLowerCase();
    const available = suggestions.filter((tag) => !values.includes(tag) && (!query || tag.toLowerCase().includes(query))).slice(0, 8);
    menu.innerHTML = available.map((tag) => `<button type="button" data-tag-add="${esc(tag)}">${esc(tag)}</button>`).join("") || '<span class="hint">输入新标签后按回车</span>';
    menu.hidden = document.activeElement !== input;
  };
  const add = (raw) => {
    const tag = raw.trim().replace(/^,+|,+$/g, "");
    if (!tag || values.includes(tag)) return;
    values.push(tag);
    input.value = "";
    sync();
  };
  input.addEventListener("focus", sync);
  input.addEventListener("input", sync);
  input.addEventListener("blur", () => setTimeout(() => { menu.hidden = true; }, 120));
  input.addEventListener("keydown", (event) => {
    if (["Enter", ","].includes(event.key)) { event.preventDefault(); add(input.value); }
    else if (event.key === "Backspace" && !input.value && values.length) { values.pop(); sync(); }
  });
  root.addEventListener("click", (event) => {
    const remove = event.target.closest("[data-tag-remove]");
    const addButton = event.target.closest("[data-tag-add]");
    if (remove) { values = values.filter((tag) => tag !== remove.dataset.tagRemove); sync(); input.focus(); }
    if (addButton) { add(addButton.dataset.tagAdd); input.focus(); }
  });
  sync();
}

function hostFormMarkup(host, defaults = {}) {
  const edit = Boolean(host);
  const source = host || defaults;
  const value = (key, fallback = "") => esc(source?.[key] ?? fallback);
  const checked = (key, fallback = true) => source && Object.hasOwn(source, key) ? Boolean(source[key]) : fallback;
  const keyAuth = source?.auth_type === "key";
  const isLocal = Boolean(source?.is_local);
  const connectionSecretConfigured = keyAuth ? host?.private_key_configured : host?.auth_secret_configured;
  const connectionSecretPlaceholder = isLocal ? "本机直采不需要 SSH 凭据" : edit ? (connectionSecretConfigured ? "已配置，留空保持不变" : "未配置，请填写") : "";
  const connectionSecretRequired = !isLocal && (!edit || !connectionSecretConfigured) ? "required" : "";
  return `<form id="host-form"><div class="dialog-heading"><span class="dialog-icon" aria-hidden="true">▦</span><div><h2>${edit ? "编辑主机" : "添加 SSH 主机"}</h2><p>${edit ? "连接信息变更会先重新测试 SSH 身份。" : "测试连通性和物理身份后纳入统一管理。"}</p></div></div>
    <fieldset><legend>基础连接</legend><div class="form-grid two"><label>显示名称<input name="name" value="${value("name")}" maxlength="255" required></label><label>地址<input name="address" value="${value("address")}" required></label><label>SSH 端口<input name="port" type="number" value="${value("port", 22)}" min="1" max="65535" required></label><label>SSH 用户<input name="username" value="${value("username")}" required></label><label>认证方式<select name="auth_type"><option value="password" ${source?.auth_type !== "key" ? "selected" : ""}>密码</option><option value="key" ${source?.auth_type === "key" ? "selected" : ""}>私钥</option></select></label><label data-secret-label>${keyAuth ? "私钥" : "SSH 密码"}<textarea data-secret name="${keyAuth ? "private_key" : "auth_secret"}" ${connectionSecretRequired} placeholder="${connectionSecretPlaceholder}"></textarea></label><label data-vault-key-row ${keyAuth ? "" : "hidden"}>密钥库（可选）<select name="ssh_key_id" data-ssh-key-select><option value="">不使用密钥库</option></select></label><label data-key-passphrase-row ${keyAuth ? "" : "hidden"}>私钥口令（可选）<input data-key-passphrase name="private_key_passphrase" type="password" autocomplete="new-password" placeholder="${host?.private_key_passphrase_configured ? "已配置，留空保持不变" : "未配置"}"></label><label>标签（可多选）${tagMultiSelectMarkup(source?.tags || [])}</label><label>采集超时覆盖（秒）<input name="timeout_seconds" type="number" value="${value("timeout_seconds")}" min="5" max="60" placeholder="使用系统设置"></label><label>机房位置<input name="asset_location" value="${value("asset_location")}" maxlength="255" placeholder="例如 A03 机柜"></label><label>运维负责人<input name="asset_owner" value="${value("asset_owner")}" maxlength="255"></label><label>保修到期<input name="warranty_expires" type="date" value="${value("warranty_expires")}"></label></div><div class="host-local-option">${switchControl("is_local", "作为本机概览", checked("is_local", false))}<span class="hint">本机直接采集，不依赖 SSH 服务；同一时间只能配置一台。</span></div><label>备注<textarea name="notes" maxlength="4000">${value("notes")}</textarea></label></fieldset>
    <fieldset><legend>跳板机连接</legend><div class="form-grid two">${switchControl("jump_enabled", "通过跳板机连接", checked("jump_enabled", false))}<label>跳板机地址<input name="jump_address" value="${value("jump_address")}" placeholder="堡垒机地址"></label><label>端口<input name="jump_port" type="number" min="1" max="65535" value="${value("jump_port", 22)}"></label><label>账号<input name="jump_username" value="${value("jump_username")}"></label><label>认证方式<select name="jump_auth_type"><option value="password">密码</option><option value="key">私钥</option></select></label><label>跳板机密码<input name="jump_auth_secret" type="password" placeholder="已配置时留空"></label><label>跳板机私钥<textarea name="jump_private_key" placeholder="仅在跳板机使用私钥时填写"></textarea></label><label>跳板机私钥口令<input name="jump_private_key_passphrase" type="password" placeholder="可选"></label></div><span class="hint">跳板机凭证只在服务端使用，不会下发浏览器。</span></fieldset>
    <fieldset><legend>远端 sudo 授权</legend><div class="form-grid two"><label>远端 sudo 密码（可选）<input data-sudo-password name="sudo_password" type="password" autocomplete="new-password" placeholder="${host?.sudo_password_configured ? "已配置，留空保持不变" : "未配置"}"><span class="hint">仅在精确 NOPASSWD 授权不可用时用于工具安装。</span></label>${host?.sudo_password_configured ? '<label class="check-label"><input data-clear-sudo-password type="checkbox">移除已保存的远端 sudo 密码</label>' : ""}</div></fieldset>
    <fieldset><legend>功能权限</legend><div class="form-grid two">${switchControl("enabled", "启用定时采集", checked("enabled"))}${switchControl("docker_enabled", "采集 Docker 指标", checked("docker_enabled"))}${switchControl("allow_tmux", "允许 Tmux 管理", checked("allow_tmux"))}${switchControl("allow_terminal", "允许 Web 终端", checked("allow_terminal"))}${switchControl("allow_process", "允许进程操作", checked("allow_process"))}${switchControl("allow_install", "允许工具安装", checked("allow_install"))}${switchControl("allow_stress", "允许压力测试", checked("allow_stress"))}</div></fieldset>
    <fieldset><legend>GPU 调度</legend><div class="form-grid two">${switchControl("scheduler_enabled", "启用主机级调度", checked("scheduler_enabled", false))}<label>执行模式<select name="schedule_mode"><option value="tmux" ${host?.schedule_mode !== "direct" ? "selected" : ""}>Tmux 后台提交</option><option value="direct" ${host?.schedule_mode === "direct" ? "selected" : ""}>直接 Shell</option></select></label><label>空闲时长覆盖（秒）<input name="scheduler_idle_seconds" type="number" min="60" max="86400" value="${value("scheduler_idle_seconds")}" placeholder="使用系统设置"></label><label>计算进程保护<select name="scheduler_process_guard"><option value="inherit" ${host?.scheduler_process_guard == null ? "selected" : ""}>继承系统设置</option><option value="true" ${host?.scheduler_process_guard === true ? "selected" : ""}>启用</option><option value="false" ${host?.scheduler_process_guard === false ? "selected" : ""}>禁用</option></select></label><label>工作目录<input name="schedule_cwd" value="${value("schedule_cwd")}" placeholder="可选"></label><label>Shell<input name="schedule_shell" value="${value("schedule_shell", "/bin/bash")}" required></label></div><label>默认调度命令<textarea name="schedule_command" maxlength="500" placeholder="启用自动调度前必须配置">${value("schedule_command")}</textarea></label><label>环境变量（JSON 对象）<textarea name="schedule_env" placeholder='{"KEY":"VALUE"}'>${esc(JSON.stringify(host?.schedule_env || {}, null, 2))}</textarea></label></fieldset>
    <div class="notice-panel">平台不会回显已保存的 SSH、私钥或 sudo 凭据。平台登录密码只用于危险操作再认证，不会作为远端 sudo 密码使用。</div><div class="form-error" role="alert"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button class="primary" type="submit">${edit ? "测试并保存" : "测试并添加"}</button></div></form>`;
}

function hostPayload(form, host = null) {
  let scheduleEnv;
  try { scheduleEnv = JSON.parse(form.schedule_env.value || "{}"); }
  catch (_) { throw new Error("调度环境变量必须是有效的 JSON 对象"); }
  if (!scheduleEnv || Array.isArray(scheduleEnv) || typeof scheduleEnv !== "object" || Object.values(scheduleEnv).some((value) => typeof value !== "string")) throw new Error("调度环境变量必须是字符串键值对象");
  const guard = form.scheduler_process_guard.value;
  const payload = {
    name: form.name.value.trim(), address: form.address.value.trim(), port:Number(form.port.value), username:form.username.value.trim(), auth_type:form.auth_type.value,
    tags:form.tags.value.split(",").map((item) => item.trim()).filter(Boolean), notes:form.notes.value, timeout_seconds:form.timeout_seconds.value ? Number(form.timeout_seconds.value) : null,
    is_local:form.is_local.checked, enabled:form.enabled.checked, docker_enabled:form.docker_enabled.checked, allow_tmux:form.allow_tmux.checked, allow_terminal:form.allow_terminal.checked, allow_process:form.allow_process.checked, allow_install:form.allow_install.checked, allow_stress:form.allow_stress.checked,
    scheduler_enabled:form.scheduler_enabled.checked, scheduler_idle_seconds:form.scheduler_idle_seconds.value ? Number(form.scheduler_idle_seconds.value) : null, scheduler_process_guard:guard === "inherit" ? null : guard === "true", schedule_command:form.schedule_command.value.trim() || null, schedule_cwd:form.schedule_cwd.value.trim() || null, schedule_shell:form.schedule_shell.value.trim(), schedule_env:scheduleEnv, schedule_mode:form.schedule_mode.value,
    asset_location:form.asset_location.value.trim(), asset_owner:form.asset_owner.value.trim(), warranty_expires:form.warranty_expires.value || null,
    ssh_key_id: form.ssh_key_id.value ? Number(form.ssh_key_id.value) : null, jump_enabled:form.jump_enabled.checked, jump_address:form.jump_address.value.trim() || null, jump_port:form.jump_port.value ? Number(form.jump_port.value) : null, jump_username:form.jump_username.value.trim() || null, jump_auth_type:form.jump_auth_type.value || null,
  };
  const secret = $('[data-secret]', form);
  if (secret.value) payload[secret.name] = secret.value;
  const keyPassphrase = $('[data-key-passphrase]', form);
  if (keyPassphrase.value) payload.private_key_passphrase = keyPassphrase.value;
  if (form.jump_auth_secret.value) payload.jump_auth_secret = form.jump_auth_secret.value;
  if (form.jump_private_key.value) payload.jump_private_key = form.jump_private_key.value;
  if (form.jump_private_key_passphrase.value) payload.jump_private_key_passphrase = form.jump_private_key_passphrase.value;
  const sudoPassword = $('[data-sudo-password]', form);
  if (sudoPassword.value) payload.sudo_password = sudoPassword.value;
  else if ($('[data-clear-sudo-password]', form)?.checked) payload.sudo_password = "";
  if (!host) return payload;
  const changed = {};
  Object.entries(payload).forEach(([key, value]) => {
    const original = host[key] ?? ((key === "tags") ? [] : null);
    if (!same(value, original)) changed[key] = value;
  });
  if (secret.value) changed[secret.name] = secret.value;
  return changed;
}

async function confirmFingerprintChange(host, payload, error) {
  const observed = error.details?.observed;
  if (!observed) throw error;
  const expected = error.details?.expected || host.fingerprint || "已记录指纹";
  if (!confirm(`SSH 主机指纹已变化。\n\n已记录：${expected}\n当前返回：${observed}\n\n仅当你确认目标服务器已重装或已轮换主机密钥时，才确认替换。`)) {
    throw new Error("已保留旧 SSH 指纹。若这是另一台新服务器，请删除旧主机记录后重新添加。");
  }
  const identity = await api(`/api/hosts/${host.id}/test`, {method:"POST", body:{...payload, confirmed_fingerprint:observed}});
  if (identity.duplicate) throw new Error(`新 SSH 身份已由主机“${identity.duplicate.name}”管理；请删除旧记录或编辑该主机。`);
  if (identity.machine_id_changed) {
    if (!confirm("远端 /etc/machine-id 也已变化，这可能是另一台或重装后的服务器。\n\n确认替换会沿用当前主机记录和历史；取消后可删除旧记录并重新添加。\n\n确认沿用当前记录吗？")) {
      throw new Error("已取消物理节点替换。请删除旧主机记录后重新添加这台服务器。");
    }
    payload.confirmed_physical_replacement = true;
  }
  payload.confirmed_fingerprint = observed;
  return identity;
}

async function showHostForm(host = null, defaults = {}) {
  let vaultKeys = [];
  try { vaultKeys = (await api("/api/credentials/ssh-keys")).items || []; } catch (_) { vaultKeys = []; }
  const dialog = createDialog(hostFormMarkup(host, defaults), "wide-dialog");
  const form = $("#host-form", dialog);
  const keySelect = $("[data-ssh-key-select]", form);
  if (keySelect) {
    keySelect.innerHTML = `<option value="">不使用密钥库</option>${vaultKeys.map((key) => `<option value="${key.id}" ${Number(host?.ssh_key_id ?? defaults?.ssh_key_id) === key.id ? "selected" : ""}>${esc(key.name)} · ${esc(key.key_type)}</option>`).join("")}`;
    keySelect.onchange = () => { if (keySelect.value) { $('[data-secret]', form).required = false; $('[data-secret]', form).value = ""; } };
  }
  bindTagMultiSelect(form);
  $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
  form.auth_type.onchange = () => {
    const key = form.auth_type.value === "key";
    const secret = $('[data-secret]', form);
    secret.name = key ? "private_key" : "auth_secret";
    $('[data-secret-label]', form).firstChild.textContent = key ? "私钥" : "SSH 密码";
    const secretConfigured = key ? host?.private_key_configured : host?.auth_secret_configured;
    secret.required = !host || !secretConfigured;
    secret.placeholder = host ? (secretConfigured ? "已配置，留空保持不变" : "未配置，请填写") : "";
    $('[data-key-passphrase-row]', form).hidden = !key;
    $('[data-vault-key-row]', form).hidden = !key;
    if (!key) { form.ssh_key_id.value = ""; }
  };
  form.onsubmit = async (event) => {
    event.preventDefault();
    const submit = $('button[type="submit"]', form);
    const errorNode = $(".form-error", form);
    errorNode.textContent = "";
    try {
      const payload = hostPayload(form, host);
      if (host && !Object.keys(payload).length) { toast("没有需要保存的更改", "warning"); return; }
      submit.disabled = true;
      submit.textContent = "正在验证";
      if (!host) {
        const identity = await api("/api/hosts/test", {method:"POST", body:payload});
        if (identity.duplicate) throw new Error(`该物理主机已存在：${identity.duplicate.name}`);
        const result = await api("/api/hosts", {method:"POST", body:{...payload, identity}});
        toast(`主机 ${result.host.name} 已添加`);
      } else {
        const connectionKeys = new Set(["address", "port", "username", "auth_type", "auth_secret", "private_key", "private_key_passphrase", "ssh_key_id", "jump_enabled", "jump_address", "jump_port", "jump_username", "jump_auth_type", "jump_auth_secret", "jump_private_key"]);
        const needsConnectionTest = Object.keys(payload).some((key) => connectionKeys.has(key));
    const needsElevation = needsConnectionTest || Object.hasOwn(payload, "schedule_command") || ["auth_secret", "private_key", "private_key_passphrase", "sudo_password", "jump_auth_secret", "jump_private_key", "jump_private_key_passphrase"].some((key) => Object.hasOwn(payload, key));
        if (needsElevation) await ensureElevated();
        if (needsConnectionTest) {
          let identity;
          try {
            identity = await api(`/api/hosts/${host.id}/test`, {method:"POST", body:payload});
          } catch (error) {
            if (!error.details?.fingerprint_mismatch) throw error;
            identity = await confirmFingerprintChange(host, payload, error);
          }
          if (identity.duplicate) throw new Error(`该物理主机已存在：${identity.duplicate.name}`);
          payload.identity = identity;
        }
        const result = await api(`/api/hosts/${host.id}`, {method:"PATCH", body:payload});
        toast(`主机 ${result.host.name} 已更新`);
      }
      dialog.close("done");
      if (state.page === "dashboard") renderDashboard();
      else if (state.page === "hosts") renderHosts();
      else if (state.page === "host-detail" && host) renderHostDetail(host.id);
    } catch (error) {
      errorNode.textContent = error.message;
    } finally {
      submit.disabled = false;
      submit.textContent = host ? "测试并保存" : "测试并添加";
    }
  };
}

function textTable(headers, rows, emptyTitle = "暂无数据", emptyCopy = "完成一次成功采集后显示。") {
  if (!rows.length) return `<div class="empty"><div><strong>${esc(emptyTitle)}</strong>${esc(emptyCopy)}</div></div>`;
  return `<div class="table-wrap"><table><thead><tr>${headers.map((header) => `<th>${esc(header)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${row.map((cell) => `<td>${esc(cell)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function capabilityStatus(host, key) {
  return host[key] ? '<span class="status online">已允许</span>' : '<span class="status disabled">未允许</span>';
}

function developmentPythonOptions(stack) {
  const versions = stack?.python_versions || [];
  const unique = new Map(versions.map((item) => [item.command, item]));
  const options = [...unique.values()].map((item) => `<option value="${esc(item.command)}">${esc(item.command)} · ${esc(item.version || "版本未知")}</option>`);
  return options.length ? options.join("") : '<option value="" disabled selected>未检测到 Python 3</option>';
}

function developmentRootFor(host) {
  return state.developmentRoots[host.id] || `/home/${host.username}`;
}

function developmentStackSummary(stack) {
  const tools = stack?.tools || {};
  const tool = (name) => tools[name]?.available ? `<span class="status online">已安装</span><div class="hint">${esc(tools[name].version || tools[name].path || "可用")}</div>` : '<span class="status disabled">未安装</span>';
  const packages = stack?.cuda?.cudnn_packages || [];
  const libraries = stack?.cuda?.cudnn_libraries || [];
  const cudnn = packages.length ? `${packages.slice(0, 3).map((item) => `${item.package} ${item.version}`).join(" / ")}${packages.length > 3 ? ` 等 ${packages.length} 个包` : ""}` : libraries.length ? `${libraries.slice(0, 3).map((item) => item.name).join(" / ")}${libraries.length > 3 ? ` 等 ${libraries.length} 个库` : ""}` : "未检测到";
  const warnings = stack?.warnings || [];
  const recommendation = stack?.gpu?.recommended_driver || stack?.gpu?.recommendation_note || "无推荐结果";
  return `<div class="kv-grid"><div class="kv"><small>发行版</small><strong>${esc(`${stack?.os?.id || "未知"} ${stack?.os?.version || ""}`)}</strong></div><div class="kv"><small>NVIDIA 驱动</small><strong>${esc(stack?.gpu?.driver_version || "未检测到")}</strong><div class="hint">推荐：${esc(recommendation)}</div></div><div class="kv"><small>CUDA nvcc</small><strong>${esc(stack?.cuda?.nvcc_version || "未检测到")}</strong></div><div class="kv"><small>cuDNN</small><strong>${esc(cudnn)}</strong></div><div class="kv"><small>Python 3</small><strong>${(stack?.python_versions || []).length} 个</strong></div><div class="kv"><small>conda / uv</small><strong>${tool("conda")} ${tool("uv")}</strong></div></div>${warnings.length ? `<div class="notice-panel">${warnings.map((warning) => `<div>${esc(warning)}</div>`).join("")}</div>` : ""}`;
}

function developmentOutput(root, title, value, isError = false) {
  const output = $("[data-dev-output]", root);
  if (!output) return;
  output.classList.toggle("error-output", isError);
  output.textContent = `${title}\n${typeof value === "string" ? value : JSON.stringify(value, null, 2)}`;
  output.hidden = false;
}

function gpuBenchmarkMarkup(record) {
  const result = record?.result || record || {};
  const normalized = normalizeBenchmark(result);
  const training = result.training || {};
  const collective = result.collective;
  const matrixRows = normalized.matrix.map((item) => `<tr><td>${esc(item.precision)}</td><td>${esc(item.aggregate ?? "-")} ${esc(item.unit)}</td><td>${item.perGpu.map((gpu) => `GPU ${esc(gpu.device)}: ${esc(gpu.value)} ${esc(item.unit)}`).join(" · ")}</td></tr>`).join("");
  const memoryRows = (result.memory_bandwidth || []).map((item) => `<tr><td>GPU ${esc(item.device)}</td><td>${esc(item.gbps)} GB/s</td><td>${fmtBytes(item.bytes)}</td></tr>`).join("");
  const warnings = result.warnings || [];
  return `<div class="benchmark-result"><div class="kv-grid"><div class="kv"><small>模式 / GPU</small><strong>${normalized.mode === "multi" ? "多卡" : "单卡"} · ${esc(normalized.gpuCount || "-")} 张</strong></div><div class="kv"><small>训练模型</small><strong>${esc(normalized.training.model || "-")}</strong><div class="hint">${esc(normalized.training.dataset)}</div></div><div class="kv"><small>训练吞吐</small><strong>${esc(normalized.training.iterationsPerSecond ?? "-")} it/s</strong><div class="hint">${esc(normalized.training.imagesPerSecond ?? "-")} images/s</div></div><div class="kv"><small>快速 loss / acc</small><strong>${esc(normalized.training.loss ?? "-")} / ${normalized.training.accuracy == null ? "-" : percentage(Number(normalized.training.accuracy) * 100)}</strong></div><div class="kv"><small>NCCL All-Reduce</small><strong>${collective?.available ? `${esc(collective.bus_gbps)} GB/s` : (normalized.mode === "multi" ? "不可用" : "单卡无需测试")}</strong><div class="hint">${collective?.available ? `算法带宽 ${esc(collective.algorithm_gbps)} GB/s` : esc(collective?.reason || "-")}</div></div><div class="kv"><small>Tensor Parallel</small><strong>TP=${esc(normalized.tpDegree || "-")}</strong><div class="hint">${normalized.tp8Ready ? "满足 TP=8 卡数条件" : "不足 8 张卡"}</div></div></div><div class="hint">${esc(training.accuracy_note || "短时结果只用于快速横向比较")}</div>${matrixRows ? `<div class="table-wrap"><table><thead><tr><th>精度</th><th>聚合吞吐</th><th>逐卡结果</th></tr></thead><tbody>${matrixRows}</tbody></table></div>` : '<div class="notice-panel">没有成功完成的矩阵精度项，请查看警告。</div>'}${memoryRows ? `<div class="table-wrap"><table><thead><tr><th>GPU</th><th>显存拷贝带宽</th><th>测试块</th></tr></thead><tbody>${memoryRows}</tbody></table></div>` : ""}${warnings.length ? `<div class="notice-panel">${warnings.map((warning) => `<div>${esc(warning)}</div>`).join("")}</div>` : ""}</div>`;
}

async function loadGpuBenchmarkHistory(host, root) {
  const target = $("[data-gpu-benchmark-history]", root);
  if (!target || !can("diagnostics.view")) return;
  try {
    const history = await api(`/api/hosts/${host.id}/development/gpu-benchmarks?limit=5`);
    target.innerHTML = history.items.length ? history.items.map((item, index) => `<details ${index === 0 ? "open" : ""}><summary>${fmtTime(item.created_at)} · ${item.mode === "multi" ? "多卡" : "单卡"} · ${esc(item.result?.training?.model || "-")} · ${esc(item.gpu_count)} GPU</summary>${gpuBenchmarkMarkup(item)}</details>`).join("") : '<div class="hint">尚无 GPU 快速评估记录</div>';
  } catch (error) {
    target.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`;
  }
}

function scanStatusLabel(result) {
  if (result?.timed_out) return '<span class="status degraded">已超时，返回部分结果</span>';
  if (result?.partial) return '<span class="status degraded">部分结果</span>';
  return '<span class="status online">扫描完成</span>';
}

function scanResultMarkup(result, mode = "large") {
  const title = mode === "usage" ? "目录容量" : "大文件扫描";
  const warning = result?.warning ? `<div class="notice-panel scan-warning">${esc(result.warning)}</div>` : "";
  if (mode === "usage") {
    return `<div class="scan-result"><div class="scan-result-head"><div><strong>${title}</strong><span class="hint mono">${esc(result?.path || "")}</span></div>${scanStatusLabel(result)}</div><div class="scan-stat"><strong>${fmtBytes(result?.bytes)}</strong><span>已统计容量${result?.partial ? "（可能不完整）" : ""}</span></div>${warning}</div>`;
  }
  const items = result?.items || [];
  const rows = items.map((item) => { const mtime = Number(item.mtime); return `<tr><td class="mono">${esc(fmtBytes(item.bytes))}</td><td class="mono">${esc(item.path)}</td><td>${Number.isFinite(mtime) ? esc(fmtTime(mtime * 1000)) : "-"}</td></tr>`; }).join("");
  return `<div class="scan-result"><div class="scan-result-head"><div><strong>${title}</strong><span class="hint mono">${esc(result?.path || "")}</span></div>${scanStatusLabel(result)}</div><div class="scan-meta"><span>阈值 ${esc(fmtBytes(result?.minimum_bytes))}</span><span>深度 ${esc(result?.max_depth ?? "-")}</span><span>超时 ${esc(result?.timeout_seconds ?? "-")} 秒</span><span>返回 ${items.length} 条${result?.truncated ? "（已截断）" : ""}</span></div>${items.length ? `<div class="table-wrap scan-table"><table><thead><tr><th>大小</th><th>路径</th><th>修改时间</th></tr></thead><tbody>${rows}</tbody></table></div>` : '<div class="scan-empty">没有超过阈值的文件</div>'}${warning}</div>`;
}

function scanSelectOptions(values, selected, suffix) {
  const current = Number(selected);
  const options = [...new Set([...values, current])].filter(Number.isFinite).sort((left, right) => left - right);
  return options.map((value) => `<option value="${value}" ${value === current ? "selected" : ""}>${esc(value)} ${esc(suffix)}${value === current ? "（系统默认）" : ""}</option>`).join("");
}

function scanDepthOptions(selected) {
  const current = Number(selected);
  const option = (value) => `<option value="${value}" ${value === current ? "selected" : ""}>${value} 层${value === current ? "（系统默认）" : ""}</option>`;
  const custom = [1, 3, 5, 8, 12].includes(current) ? "" : `<optgroup label="系统默认">${option(current)}</optgroup>`;
  return `<optgroup label="快速">${option(1)}${option(3)}${option(5)}</optgroup><optgroup label="完整">${option(8)}${option(12)}</optgroup>${custom}`;
}

function scanParams(root) {
  const path = $("[data-dev-scan-path]", root).value.trim();
  const minimumMiB = Number($("[data-dev-scan-min]", root).value);
  return {
    path,
    minimumBytes: Math.round(minimumMiB * 1024 * 1024),
    limit: Number($("[data-dev-scan-limit]", root).value),
    maxDepth: Number($("[data-dev-scan-depth]", root).value),
    timeoutSeconds: Number($("[data-dev-scan-timeout]", root).value),
  };
}

function bindSystemPlanFields(form) {
  const update = () => {
    const kind = form.kind.value;
    $$('[data-system-field]', form).forEach((field) => {
      const visible = (field.dataset.systemField || "").split(",").includes(kind);
      field.hidden = !visible;
      field.querySelectorAll("input, select").forEach((input) => { input.disabled = !visible; });
    });
  };
  form.kind.addEventListener("change", update);
  update();
}

async function renderDevelopmentPanel(host, root) {
  if (!root) return;
  root.innerHTML = '<div class="loading">正在盘点远端 GPU 软件栈</div>';
  let stack = {};
  try {
    const scanSettingsRequest = can("storage.scan") ? getScanSettings() : Promise.resolve(state.scanSettings);
    if (can("development.view")) stack = (await withOperationProgress("正在盘点 GPU 软件栈", () => api(`/api/hosts/${host.id}/development/stack`))).stack || {};
    const scanSettings = await scanSettingsRequest;
    const scanRoot = developmentRootFor(host);
    root.innerHTML = `<section class="section"><div class="section-title"><div><h3>GPU 软件栈现状</h3><p class="hint">当前目标：${esc(host.username)}@${esc(host.address)} · 驱动、CUDA、cuDNN 只生成可审阅方案，不直接修改系统。</p></div><div class="toolbar-group">${can("development.view") ? '<button data-dev-refresh>刷新盘点</button>' : ""}${can("diagnostics.view") ? '<button data-dev-gpu>GPU 健康自检</button>' : ""}</div></div>${developmentStackSummary(stack)}<pre class="diagnostic-output" data-dev-output hidden></pre></section>
      ${can("diagnostics.view") ? `<section class="section"><div class="section-title"><div><h3>GPU 快速评估</h3><p class="hint">覆盖 FP32、TF32、FP16、BF16、可用时的 FP8 E4M3/E5M2 和 INT8；多卡额外测试 DataParallel 与 NCCL。</p></div></div><form data-gpu-benchmark-form><div class="form-grid two"><label>模式<select name="mode"><option value="single">单卡</option><option value="multi">多卡（最多 8 张）</option></select></label><label>训练模型<select name="model"><option value="resnet18">ResNet-18</option><option value="resnet34">ResNet-34</option><option value="resnet50">ResNet-50</option><option value="mobilenet_v3_small">MobileNetV3-Small</option><option value="vit_tiny_patch16_224">ViT-Tiny / 224（timm）</option></select></label><label>数据集<select name="dataset"><option value="synthetic">GPU 合成数据（纯吞吐）</option><option value="fake_cifar10">FakeData-CIFAR10（轻量链路）</option><option value="cifar10">真实 CIFAR-10</option></select></label><label>单项时间预算<select name="duration_seconds"><option value="3">3 秒</option><option value="5" selected>5 秒</option><option value="10">10 秒</option><option value="20">20 秒</option><option value="30">30 秒</option></select></label><label>PyTorch Python<input name="python" value="python3" list="gpu-python-options-${host.id}" placeholder="python3 或 /opt/conda/envs/torch/bin/python"><datalist id="gpu-python-options-${host.id}">${(stack.python_versions || []).map((item) => `<option value="${esc(item.path || item.command)}"></option>`).join("")}</datalist></label><label data-gpu-download hidden><span>数据准备</span><span class="switch-control"><input type="checkbox" name="download_dataset"><span>允许首次下载 CIFAR-10</span></span></label></div><div class="action-strip">${can("gpu.benchmark") && host.allow_stress ? '<button type="submit" class="primary">开始评估</button>' : '<span class="hint">运行需要“GPU 快速评估”权限，且主机必须允许压力任务。</span>'}</div><div class="form-error" data-gpu-benchmark-error></div></form><div data-gpu-benchmark-history><div class="loading">正在读取评估历史</div></div></section>` : ""}
      ${can("development.view") ? `<section class="section"><div class="section-title"><div><h3>虚拟环境</h3><p class="hint">支持 venv、conda、uv；网页执行仅对创建和依赖安装开放，删除环境只生成脚本。</p></div><button data-dev-inventory>刷新环境列表</button></div><div class="toolbar-group"><label class="compact-field">扫描根目录<input data-dev-root value="${esc(scanRoot)}" placeholder="例如 /home/ops/projects"></label></div><div class="table-wrap"><table><thead><tr><th>后端</th><th>路径</th><th>Python / 主要包</th><th>操作</th></tr></thead><tbody data-dev-environments><tr><td colspan="4" class="hint">点击刷新环境列表</td></tr></tbody></table></div><form data-dev-environment-form class="settings-section"><h4>创建或管理环境</h4><div class="form-grid two"><label>后端<select name="backend"><option value="venv" ${stack.python_versions?.length ? "" : "disabled"}>venv（Python 自带）${stack.python_versions?.length ? "" : "（未检测到 Python 3）"}</option><option value="conda" ${stack.tools?.conda?.available ? "" : "disabled"}>conda${stack.tools?.conda?.available ? "" : "（未安装）"}</option><option value="uv" ${stack.tools?.uv?.available ? "" : "disabled"}>uv${stack.tools?.uv?.available ? "" : "（未安装）"}</option></select></label><label>操作<select name="action"><option value="create">创建环境</option><option value="install">安装依赖</option><option value="remove">删除环境（仅脚本）</option></select></label><label>目标路径<input name="path" value="${esc(`${scanRoot.replace(/\/$/, "")}/.venv`)}" required></label><label>Python 版本<select name="python">${developmentPythonOptions(stack)}<option value="">conda 默认</option></select></label><label>PyTorch 预设<select name="pytorch"><option value="none">不安装</option><option value="cpu">CPU</option><option value="cu118">CUDA 11.8</option><option value="cu121">CUDA 12.1</option><option value="cu124">CUDA 12.4</option></select></label><label>额外依赖（空格或逗号分隔）<input name="packages" placeholder="numpy pandas==2.2"></label></div><div class="action-strip">${can("development.plan") ? '<button type="button" data-dev-environment-plan>仅生成脚本</button>' : '<span class="hint">当前账号没有生成环境方案的权限</span>'}${can("development.execute") && host.allow_install ? '<button type="button" class="primary" data-dev-environment-execute>复核后网页执行</button>' : ""}</div><div class="form-error" data-dev-env-error></div></form>${can("development.plan") && host.allow_install ? '<form data-dev-conda-yaml-form class="settings-section"><h4>conda YAML 导入重建</h4><div class="form-grid two"><label>YAML 文件<input name="file" type="file" accept=".yml,.yaml,text/yaml,text/x-yaml" required></label><label>目标环境路径<input name="path" value="/home/' + esc(host.username) + '/conda-env" required></label></div><div class="action-strip"><button type="button" data-dev-conda-plan>仅生成脚本</button>' + (can("development.execute") ? '<button type="button" class="primary" data-dev-conda-execute>复核后网页执行</button>' : "") + '</div><div class="form-error" data-dev-conda-error></div></form>' : ""}</section>` : '<div class="notice-panel">当前账号只有 GPU 自检权限，开发环境盘点需管理员授权。</div>'}
      ${can("development.plan") && host.allow_install ? `<section class="section"><div class="section-title"><div><h3>GPU 驱动、CUDA、cuDNN 与 APT 方案</h3><p class="hint">先选方案类型，再填写对应参数；页面只生成可复核脚本，不直接执行系统级安装。</p></div></div><form data-dev-system-form><div class="form-grid two"><label>方案类型<select name="kind"><optgroup label="GPU 软件栈"><option value="gpu-driver">NVIDIA 驱动（推荐）</option><option value="cuda">CUDA Toolkit</option><option value="cudnn">cuDNN</option></optgroup><optgroup label="开发工具"><option value="uv-install">安装 uv</option><option value="conda-install">安装 Miniconda</option></optgroup><optgroup label="系统包管理"><option value="apt">APT 常用操作</option></optgroup></select></label><label data-system-field="gpu-driver">驱动包（推荐值）<input name="package" value="${esc(stack.gpu?.recommended_driver || "")}" placeholder="由 ubuntu-drivers 提供"></label><label data-system-field="cuda">CUDA 版本<select name="cuda_version"><option value="11.8">11.8</option><option value="12.1">12.1</option><option value="12.4">12.4</option></select></label><label data-system-field="cudnn">cuDNN 版本<select name="cudnn_version"><option value="9-cuda12">9 / CUDA 12</option><option value="8">8</option></select></label><label data-system-field="apt">APT 操作<select name="apt_action"><option value="update">update</option><option value="upgrade">upgrade</option><option value="autofix">autofix</option><option value="install">install</option><option value="remove">remove</option><option value="purge">purge</option></select></label><label data-system-field="apt">APT 包名<input name="apt_package" placeholder="例如 build-essential"></label></div><button class="primary" type="submit">生成方案脚本</button><div class="form-error" data-dev-system-error></div></form></section>` : ""}
      ${can("development.view") ? `<section class="section"><div class="section-title"><h3>APT 已安装包</h3><form data-dev-apt-search class="toolbar-group"><input name="search" placeholder="按包名筛选"><button>查询</button></form></div><div class="table-wrap"><table><thead><tr><th>包</th><th>版本</th><th>状态</th></tr></thead><tbody data-dev-packages><tr><td colspan="3" class="hint">输入条件查询，最多返回 200 项</td></tr></tbody></table></div></section>` : ""}
      ${can("storage.scan") ? `<section class="section scan-workbench"><div class="section-title"><div><h3>目录容量与大文件扫描</h3><p class="hint">限制跨文件系统、扫描深度和运行时限；超时会保留已发现的部分结果。默认值可在“系统设置 / 扫描与长任务”调整。</p></div></div><div class="scan-options"><label class="scan-path-field">目录<input data-dev-scan-path value="${esc(scanRoot)}"></label><label>最小大小<select data-dev-scan-min>${scanSelectOptions([64, 256, 1024, 4096, 10240], scanSettings.scan_minimum_mib, "MiB")}</select></label><label>扫描深度<select data-dev-scan-depth>${scanDepthOptions(scanSettings.scan_max_depth)}</select></label><label>超时<select data-dev-scan-timeout>${scanSelectOptions([10, 30, 60, 120], scanSettings.scan_timeout_seconds, "秒")}</select></label><label>返回条数<select data-dev-scan-limit>${scanSelectOptions([50, 100, 200], scanSettings.scan_result_limit, "条")}</select></label><div class="split-button"><button type="button" class="primary" data-dev-large>扫描大文件</button><button type="button" class="primary split-toggle" aria-expanded="false" aria-label="展开扫描操作">⌄</button><div class="split-menu-panel" hidden><button type="button" data-dev-usage>统计目录容量</button></div></div></div><div class="scan-result-host" data-dev-scan-output hidden></div></section>` : ""}
    `;
    $("[data-dev-refresh]", root)?.addEventListener("click", () => renderDevelopmentPanel(host, root));
    $("[data-dev-gpu]", root)?.addEventListener("click", async () => { try { developmentOutput(root, "GPU 健康自检", (await withOperationProgress("正在执行 GPU 健康自检", () => api(`/api/hosts/${host.id}/development/gpu-diagnostics`))).diagnostics); } catch (error) { developmentOutput(root, "GPU 自检失败", error.message, true); } });
    const benchmarkForm = $("[data-gpu-benchmark-form]", root);
    if (benchmarkForm) {
      const datasetSelect = benchmarkForm.elements.dataset;
      const updateDatasetControls = () => { $("[data-gpu-download]", benchmarkForm).hidden = datasetSelect.value !== "cifar10"; };
      datasetSelect.addEventListener("change", updateDatasetControls);
      updateDatasetControls();
      benchmarkForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.target;
        const errorNode = $("[data-gpu-benchmark-error]", form);
        errorNode.textContent = "";
        try {
          await ensureElevated();
          if (!confirm(`确认在 ${host.name} 上运行 GPU 快速评估？评估期间会产生明显 GPU 负载。`)) return;
          const payload = {
            mode: form.mode.value, model: form.model.value, dataset: form.elements.dataset.value,
            duration_seconds: Number(form.duration_seconds.value), python: form.python.value.trim(),
            download_dataset: form.download_dataset.checked,
          };
          await withOperationProgress("正在运行 GPU 快速评估", () => api(`/api/hosts/${host.id}/development/gpu-benchmarks`, {method:"POST", body:payload}));
          toast("GPU 快速评估完成");
          await loadGpuBenchmarkHistory(host, root);
        } catch (error) { errorNode.textContent = error.message; }
      });
    }
    loadGpuBenchmarkHistory(host, root);
    $("[data-dev-inventory]", root)?.addEventListener("click", async () => { try { const path = $("[data-dev-root]", root).value.trim(); state.developmentRoots[host.id] = path; const result = await withOperationProgress("正在盘点虚拟环境", () => api(`/api/hosts/${host.id}/development/environments?root=${encodeURIComponent(path)}`), {timeoutSeconds:scanSettings.environment_inventory_timeout}); $("[data-dev-environments]", root).innerHTML = result.items.length ? result.items.map((item) => `<tr><td>${esc(item.backend)}</td><td class="mono">${esc(item.path)}</td><td>${esc(item.python || "未知")}${item.packages?.length ? `<div class="hint">${esc(item.packages.slice(0, 8).map((pkg) => `${pkg.name} ${pkg.version}`).join(" · "))}${item.packages.length > 8 ? " · …" : ""}</div>` : ""}</td><td><div class="toolbar-group">${item.backend === "conda" ? `<button class="text-button" data-conda-export="${esc(item.path)}">导出 yml</button>` : ""}${can("development.plan") ? `<button class="text-button" data-env-backup="${esc(item.path)}" data-env-backend="${item.backend === "conda" ? "conda" : "venv"}">备份脚本</button>` : ""}</div></td></tr>`).join("") : '<tr><td colspan="4" class="hint">未发现 pyvenv.cfg 或 conda 环境</td></tr>'; $$('[data-conda-export]', root).forEach((button) => { button.onclick = async () => { try { const content = await withOperationProgress("正在导出 conda YAML", () => api(`/api/hosts/${host.id}/development/conda-export?path=${encodeURIComponent(button.dataset.condaExport)}`)); const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob([content], {type:"text/yaml;charset=utf-8"})); link.download = "environment.yml"; link.click(); URL.revokeObjectURL(link.href); } catch (error) { developmentOutput(root, "导出 conda YAML 失败", error.message, true); } }; }); $$('[data-env-backup]', root).forEach((button) => { button.onclick = async () => { try { const result = await withOperationProgress("正在生成环境备份脚本", () => api(`/api/hosts/${host.id}/development/environment-backup-plan`, {method:"POST", body:{backend:button.dataset.envBackend, path:button.dataset.envBackup}})); developmentOutput(root, "环境备份脚本", result.plan.script); } catch (error) { developmentOutput(root, "生成环境备份脚本失败", error.message, true); } }; }); } catch (error) { developmentOutput(root, "环境盘点失败", error.message, true); } });
    const environmentPayload = (form) => ({backend:form.backend.value, action:form.action.value, path:form.path.value.trim(), python:form.python.value, pytorch:form.pytorch.value, packages:form.packages.value});
    $("[data-dev-environment-form] [name='backend']", root)?.addEventListener("change", (event) => { const python = $("[data-dev-environment-form] [name='python']", root); if (!python) return; if (event.target.value === "conda") python.value = ""; else if (!python.value) python.selectedIndex = 0; });
    const submitEnvironment = async (mode) => { const form = $("[data-dev-environment-form]", root); const payload = environmentPayload(form); const safePayload = {...payload, confirmed_path:payload.action !== "create"}; const errorNode = $("[data-dev-env-error]", root); errorNode.textContent = ""; try { if (mode === "execute") { await ensureElevated(); if (!confirm(`确认通过 SSH 在 ${host.name} 执行 ${payload.action} 方案？远端输出将在完成后返回，页面不会执行删除环境。`)) return; } const result = await withOperationProgress(mode === "execute" ? "正在执行环境方案" : "正在生成环境方案", () => api(`/api/hosts/${host.id}/development/environment-${mode === "execute" ? "execute" : "plan"}`, {method:"POST", body:safePayload})); if (mode === "execute") { developmentOutput(root, result.ok ? "网页执行完成" : "网页执行失败", `${result.stdout || ""}\n${result.stderr || ""}`, !result.ok); } else developmentOutput(root, "环境方案脚本", result.plan.script); } catch (error) { errorNode.textContent = error.message; } };
    $("[data-dev-environment-plan]", root)?.addEventListener("click", () => submitEnvironment("plan"));
    $("[data-dev-environment-execute]", root)?.addEventListener("click", () => submitEnvironment("execute"));
    const submitCondaYaml = async (mode) => { const form = $("[data-dev-conda-yaml-form]", root); if (!form) return; const errorNode = $("[data-dev-conda-error]", root); errorNode.textContent = ""; try { const file = form.file.files[0]; if (!file) throw new Error("请选择 conda YAML 文件"); if (file.size > 512 * 1024) throw new Error("conda YAML 不能超过 512 KiB"); const payload = {path:form.path.value.trim(), yaml:await file.text()}; if (mode === "execute") { await ensureElevated(); if (!confirm(`确认通过 SSH 在 ${host.name} 重建 conda 环境？已有同路径环境不会被自动删除。`)) return; } const result = await withOperationProgress(mode === "execute" ? "正在重建 conda 环境" : "正在生成 conda 重建方案", () => api(`/api/hosts/${host.id}/development/conda-yaml-${mode === "execute" ? "execute" : "plan"}`, {method:"POST", body:payload})); if (mode === "execute") developmentOutput(root, result.ok ? "conda YAML 重建完成" : "conda YAML 重建失败", `${result.stdout || ""}\n${result.stderr || ""}`, !result.ok); else developmentOutput(root, "conda YAML 重建脚本", result.plan.script); } catch (error) { errorNode.textContent = error.message; } };
    $("[data-dev-conda-plan]", root)?.addEventListener("click", () => submitCondaYaml("plan"));
    $("[data-dev-conda-execute]", root)?.addEventListener("click", () => submitCondaYaml("execute"));
    const systemForm = $("[data-dev-system-form]", root);
    if (systemForm) {
      bindSystemPlanFields(systemForm);
      systemForm.addEventListener("submit", async (event) => {
        event.preventDefault();
        const form = event.target;
        const kind = form.kind.value;
        const payload = {kind};
        if (kind === "gpu-driver") payload.package = form.package.value.trim();
        if (kind === "cuda") payload.version = form.cuda_version.value;
        if (kind === "cudnn") payload.version = form.cudnn_version.value;
        if (kind === "apt") { payload.action = form.apt_action.value; payload.package = form.apt_package.value.trim(); }
        try {
          const result = await api(`/api/hosts/${host.id}/development/system-plan`, {method:"POST", body:payload});
          developmentOutput(root, result.plan.title, result.plan.script);
        } catch (error) { $("[data-dev-system-error]", root).textContent = error.message; }
      });
    }
    $("[data-dev-apt-search]", root)?.addEventListener("submit", async (event) => { event.preventDefault(); try { const result = await withOperationProgress("正在查询 APT 软件包", () => api(`/api/hosts/${host.id}/development/apt-packages?search=${encodeURIComponent(event.target.search.value)}`)); $("[data-dev-packages]", root).innerHTML = result.items.length ? result.items.map((item) => `<tr><td class="mono">${esc(item.package)}</td><td>${esc(item.version)}</td><td>${esc(item.status)}</td></tr>`).join("") : '<tr><td colspan="3" class="hint">没有匹配包</td></tr>'; } catch (error) { developmentOutput(root, "APT 查询失败", error.message, true); } });
    $(".split-toggle", root)?.addEventListener("click", (event) => {
      const menu = $(".split-menu-panel", root);
      const open = menu.hidden;
      menu.hidden = !open;
      event.currentTarget.setAttribute("aria-expanded", String(open));
    });
    root.addEventListener("click", (event) => {
      if (event.target.closest(".split-button")) return;
      const menu = $(".split-menu-panel", root);
      if (menu) menu.hidden = true;
      $(".split-toggle", root)?.setAttribute("aria-expanded", "false");
    });
    $("[data-dev-usage]", root)?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const originalText = button.textContent;
      try {
        button.disabled = true;
        button.textContent = "统计中";
        const params = scanParams(root);
        const query = new URLSearchParams({path:params.path, timeout_seconds:String(params.timeoutSeconds)});
        const output = $("[data-dev-scan-output]", root);
        const result = await withOperationProgress(
          "正在统计目录容量",
          () => api(`/api/hosts/${host.id}/files/usage?${query}`),
          {target:output, timeoutSeconds:params.timeoutSeconds},
        );
        output.innerHTML = scanResultMarkup(result, "usage");
        output.hidden = false;
        $(".split-menu-panel", root).hidden = true;
        $(".split-toggle", root).setAttribute("aria-expanded", "false");
      } catch (error) {
        const output = $("[data-dev-scan-output]", root);
        output.innerHTML = `<div class="error-panel">目录容量统计失败：${esc(error.message)}</div>`;
        output.hidden = false;
      } finally { button.disabled = false; button.textContent = originalText; }
    });
    $("[data-dev-large]", root)?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const originalText = button.textContent;
      try {
        button.disabled = true;
        button.textContent = "扫描中";
        const params = scanParams(root);
        const query = new URLSearchParams({path:params.path, minimum_bytes:String(params.minimumBytes), limit:String(params.limit), max_depth:String(params.maxDepth), timeout_seconds:String(params.timeoutSeconds)});
        const output = $("[data-dev-scan-output]", root);
        const result = await withOperationProgress(
          "正在扫描大文件",
          () => api(`/api/hosts/${host.id}/files/large-files?${query}`),
          {target:output, timeoutSeconds:params.timeoutSeconds},
        );
        output.innerHTML = scanResultMarkup(result, "large");
        output.hidden = false;
      } catch (error) {
        const output = $("[data-dev-scan-output]", root);
        output.innerHTML = `<div class="error-panel">大文件扫描失败：${esc(error.message)}</div>`;
        output.hidden = false;
      } finally { button.disabled = false; button.textContent = originalText; }
    });
  } catch (error) {
    root.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`;
  }
}

async function renderDevelopmentPage() {
  const hostsResult = await api("/api/development/hosts");
  if (state.page !== "environments") return;
  if (!state.developmentHostId && hostsResult.items.length) state.developmentHostId = hostsResult.items[0].id;
  const host = hostsResult.items.find((item) => item.id === state.developmentHostId) || hostsResult.items[0] || null;
  if (host && host.id !== state.developmentHostId) state.developmentHostId = host.id;
  setHeader("开发环境", "GPU 软件栈、Python 环境、磁盘扫描和审阅方案", "资源与运维");
  $("#page-content").innerHTML = host ? `<div class="toolbar"><div class="toolbar-group"><label class="compact-field">目标主机<select id="development-host-select">${hostsResult.items.map((item) => `<option value="${item.id}" ${item.id === host.id ? "selected" : ""}>${esc(item.name)} · ${esc(item.address)}</option>`).join("")}</select></label></div></div><div id="development-page-panel"></div>` : '<div class="empty"><div><strong>没有可用主机</strong>请先纳管一台服务器。</div></div>';
  $("#development-host-select")?.addEventListener("change", (event) => { state.developmentHostId = Number(event.currentTarget.value); renderDevelopmentPage(); });
  if (host) renderDevelopmentPanel(host, $("#development-page-panel"));
}

async function renderHostDetail(hostId, activeTab = "overview") {
  clearInterval(state.timer);
  state.timer = null;
  state.page = "host-detail";
  state.detailHostId = hostId;
  state.history = null;
  $$("#main-nav button").forEach((button) => button.classList.toggle("active", button.dataset.page === "hosts"));
  $("#page-content").innerHTML = '<div class="loading">正在读取主机详情</div>';
  try {
    const result = await api(`/api/hosts/${hostId}`);
    if (state.page !== "host-detail" || state.detailHostId !== hostId) return;
    const host = result.host;
    const data = result.latest?.data || {};
    const gpuRuntime = Object.fromEntries((result.gpu_runtime || []).map((item) => [item.gpu_uuid, item]));
    const mountRules = Object.fromEntries((result.mount_thresholds || []).map((item) => [item.mountpoint, item]));
    const thresholds = result.thresholds || {filesystem_usage:85, filesystem_inode:85};
    setHeader(host.name, `${host.address}:${host.port} · ${statusName(host)}`, "主机管理");
    $("#page-content").innerHTML = `<div class="detail-head"><div class="resource-identity"><span class="resource-icon">${esc(host.name.slice(0,2).toUpperCase())}</span><div><h2>${esc(host.name)} <span class="status ${esc(host.status || "unknown")}" title="${esc(host.last_error || statusName(host))}">${statusName(host)}</span></h2><p>${esc(host.username)}@${esc(host.address)}:${host.port} · ${esc((host.tags || []).join(" / ") || "无标签")}</p></div></div><div class="toolbar-group"><button id="back-hosts">返回列表</button>${can("host.manage") ? '<button id="edit-current-host" class="primary">编辑主机</button>' : ""}</div></div>
      <div class="kv-grid"><div class="kv"><small>CPU 利用率</small><strong>${percentage(data.cpu?.usage_percent, "等待下一采样")}</strong></div><div class="kv"><small>CPU iowait</small><strong>${percentage(data.cpu?.iowait_percent)}</strong></div><div class="kv"><small>内存利用率</small><strong>${percentage(data.memory?.usage_percent)}</strong></div><div class="kv"><small>Swap 使用率</small><strong>${percentage(data.memory?.swap_usage_percent)}<span class="hint"> ${fmtBytes(data.memory?.swap_used)} / ${fmtBytes(data.memory?.swap_total)}</span></strong></div><div class="kv"><small>负载 1 / 5 / 15</small><strong>${data.load ? `${esc(data.load.one)} / ${esc(data.load.five)} / ${esc(data.load.fifteen)}` : "未知"}</strong></div><div class="kv"><small>上下文切换 / 中断</small><strong>${data.system_activity?.per_second?.ctxt == null ? "未知" : `${data.system_activity.per_second.ctxt}/s · ${data.system_activity.per_second.intr ?? "-"}/s`}</strong></div><div class="kv"><small>系统运行时间</small><strong>${duration(data.uptime_seconds)}</strong></div><div class="kv"><small>资产位置 / 负责人</small><strong>${esc(host.asset_location || "未录入")} / ${esc(host.asset_owner || "未录入")}</strong></div><div class="kv"><small>保修到期</small><strong>${esc(host.warranty_expires || "未录入")}</strong></div></div>
      <div class="resource-actions"><div class="action-strip">${host.enabled && can("host.refresh") ? '<button data-operation="refresh">刷新采集</button>' : ""}${can("diagnostics.view") ? '<button data-operation="inspection" class="primary">一键只读巡检</button><button data-operation="services">系统服务</button><button data-operation="network">网络诊断</button>' : ""}${host.allow_tmux && can("tmux.view") ? '<button data-operation="tmux">Tmux</button>' : ""}${host.allow_process && can("process.view") ? '<button data-operation="processes">进程</button>' : ""}${can("tools.view") ? '<button data-operation="tools">工具</button>' : ""}${host.allow_stress && can("stress.manage") ? '<button data-operation="stress">压力测试</button>' : ""}${host.allow_terminal && can("terminal.open") ? '<button data-operation="terminal">Web 终端</button>' : ""}${can("host.manage") ? '<button data-operation="key-push">推送公钥</button><button data-operation="delete" class="danger-quiet">删除主机</button>' : ""}</div></div>
      <div id="operation-workbench" class="operation-workbench" hidden></div>
      <div class="tabs" role="tablist"><button data-detail-tab="overview" class="active">运行概览</button><button data-detail-tab="storage">存储与网络</button><button data-detail-tab="gpu">NVIDIA GPU</button>${can("development.view") || can("development.plan") || can("diagnostics.view") ? '<button data-detail-tab="development">开发环境</button>' : ""}<button data-detail-tab="containers">Docker</button><button data-detail-tab="capabilities">能力与错误</button></div>
      <div class="tab-panel" data-tab-panel="overview">
        <section class="section"><div class="section-title"><h3>历史趋势</h3><div class="toolbar-group" data-history-range><button data-history-hours="1" class="primary">1 小时</button><button data-history-hours="3">3 小时</button><button data-history-hours="6">6 小时</button><button data-history-hours="24">24 小时</button><button data-history-hours="168" ${result.retention_days < 7 ? "disabled" : ""}>7 天</button><button data-history-hours="720" ${result.retention_days < 30 ? "disabled" : ""}>30 天</button></div></div><canvas id="history-chart" class="history-chart" width="1000" height="240"></canvas></section>
        <section class="section"><h3>内存、Swap、限制与温度</h3><div class="kv-grid"><div class="kv"><small>已用 / 总内存</small><strong>${fmtBytes(data.memory?.used)} / ${fmtBytes(data.memory?.total)}</strong></div><div class="kv"><small>Swap 已用 / 总量</small><strong>${fmtBytes(data.memory?.swap_used)} / ${fmtBytes(data.memory?.swap_total)}</strong></div><div class="kv"><small>打开文件限制</small><strong>${data.limits?.open_files_soft ?? "未知"} / ${data.limits?.open_files_hard ?? "未知"}</strong></div><div class="kv"><small>进程数限制</small><strong>${data.limits?.processes_soft ?? "未知"} / ${data.limits?.processes_hard ?? "未知"}</strong></div><div class="kv"><small>CPU 温度</small><strong>${data.cpu_temperature_c == null ? "不支持" : `${data.cpu_temperature_c} C`}</strong></div><div class="kv"><small>采集耗时</small><strong>${data.duration_seconds == null ? "未知" : `${data.duration_seconds} 秒`}</strong></div></div><div class="notice-panel">内核 ${esc(data.software?.kernel || "未知")} · Python ${esc(data.software?.python3 || "未知")} · CUDA ${esc(data.software?.cuda || "未知")} · Docker ${esc(data.software?.docker || "未知")}</div></section>
        <section class="section"><div class="section-title"><h3>硬件资产档案</h3><a class="button-link" href="/api/hardware-assets/export">导出全部资产 CSV</a></div><div class="kv-grid"><div class="kv"><small>CPU 型号</small><strong>${esc(data.hardware?.cpu_model || "等待采集")}</strong></div><div class="kv"><small>物理内存</small><strong>${fmtBytes(data.hardware?.memory_total_bytes)}</strong></div><div class="kv"><small>主板</small><strong>${esc(data.hardware?.motherboard || "等待采集")}</strong></div><div class="kv"><small>PCIe 设备</small><strong>${data.hardware?.pci_devices?.length ?? 0} 项</strong></div></div>${textTable(["PCI 地址","设备"], (data.hardware?.pci_devices || []).map((device) => [device.bus,device.description]), "暂无 PCIe 档案", "远端安装 lspci 后可自动采集插槽设备信息。")}</section>
      </div>
      <div class="tab-panel" data-tab-panel="storage" hidden>
        <section class="section"><div class="section-title"><h3>文件系统</h3>${can("host.manage") ? '<button id="mount-thresholds-button">挂载点告警覆盖</button>' : ""}</div>${textTable(["挂载点","文件系统","容量使用率","inode 使用率","已用 / 总量","告警阈值"], (data.filesystems || []).map((disk) => { const rule = mountRules[disk.mountpoint]; return [disk.mountpoint,disk.filesystem,percentage(disk.usage_percent),percentage(disk.inode_usage_percent),`${fmtBytes(disk.used)} / ${fmtBytes(disk.total)}`,`${rule ? `${rule.usage_threshold}% / ${rule.inode_threshold ?? thresholds.filesystem_inode}%` : `${thresholds.filesystem_usage}% / ${thresholds.filesystem_inode}%`}（容量 / inode）`]; }))}</section>
        <section class="section"><h3>物理磁盘 IO</h3>${textTable(["设备","读速率","写速率","读 IOPS","写 IOPS","忙碌率"], (data.disks_io || []).map((disk) => [disk.name,fmtBytes(disk.read_bytes_rate,"/s"),fmtBytes(disk.write_bytes_rate,"/s"),disk.read_ops_rate ?? "未知",disk.write_ops_rate ?? "未知",percentage(disk.busy_percent)]))}</section>
        <section class="section"><h3>SMART 健康</h3>${textTable(["设备","健康状态","温度","原因"], (data.smart || []).map((disk) => [disk.device,disk.health,disk.temperature_c == null ? "未知" : `${disk.temperature_c} C`,disk.reason || "-"]))}</section>
        <section class="section"><h3>网络接口</h3>${textTable(["接口","接收速率","发送速率","接收错误 / 丢弃","发送错误 / 丢弃"], (data.network || []).map((item) => [item.name,fmtBytes(item.rx_rate,"/s"),fmtBytes(item.tx_rate,"/s"),`${item.rx_errors} / ${item.rx_dropped}`,`${item.tx_errors} / ${item.tx_dropped}`]))}</section>
        <section class="section"><h3>TCP 与监听端口</h3><div class="kv-grid"><div class="kv"><small>TCP 套接字</small><strong>${data.tcp?.total ?? "未知"}</strong></div><div class="kv"><small>ESTABLISHED</small><strong>${data.tcp?.established ?? "未知"}</strong></div><div class="kv"><small>TIME_WAIT</small><strong>${data.tcp?.time_wait ?? "未知"}</strong></div><div class="kv"><small>监听端口</small><strong>${data.listening_port_count ?? (data.listening_ports ? data.listening_ports.length : "未知")}</strong></div></div>${textTable(["协议","端口","监听地址"], (data.listening_ports || []).map((item) => [item.protocol.toUpperCase(), item.port, item.address]), "暂无监听端口", "ss 未安装或尚未采样；列表最多显示 256 项")}</section>
      </div>
      <div class="tab-panel" data-tab-panel="gpu" hidden>
        <section class="section"><div class="section-title"><h3>GPU 设备与健康</h3><span class="inline-note">功耗、P-State、风扇、ECC、PCIe、节流和 XID 均来自只读 nvidia-smi 查询</span></div>${gpuTable(host, data.gpus || [], gpuRuntime)}</section>
      </div>
      ${can("development.view") || can("development.plan") || can("diagnostics.view") ? '<div class="tab-panel" data-tab-panel="development" hidden><div id="development-panel" class="development-panel"></div></div>' : ""}
      <div class="tab-panel" data-tab-panel="containers" hidden>
        <section class="section"><div class="toolbar"><h3>Docker 容器</h3><div class="toolbar-group"><div class="toolbar-search"><input id="docker-filter" placeholder="按名称或镜像筛选"></div><select id="docker-status"><option value="">全部状态</option><option value="running">运行中</option><option value="exited">已退出</option></select>${host.docker_enabled ? '<button id="docker-inventory-button">刷新镜像 / Volume</button>' : ""}</div></div>${host.docker_enabled ? `<div id="docker-table">${dockerTable(data.docker || [])}</div><div id="docker-inventory" class="docker-inventory"></div>` : '<div class="notice-panel">该主机已关闭 Docker 指标采集，远端采集命令不会访问 Docker。</div>'}</section>
      </div>
      <div class="tab-panel" data-tab-panel="capabilities" hidden>
        <section class="section"><h3>运维权限</h3><div class="kv-grid"><div class="kv"><small>Tmux 管理</small><strong>${capabilityStatus(host,"allow_tmux")}</strong></div><div class="kv"><small>Web 终端</small><strong>${capabilityStatus(host,"allow_terminal")}</strong></div><div class="kv"><small>进程操作</small><strong>${capabilityStatus(host,"allow_process")}</strong></div><div class="kv"><small>安装 / 压测</small><strong>${host.allow_install || host.allow_stress ? '<span class="status online">部分或全部允许</span>' : '<span class="status disabled">未允许</span>'}</strong></div></div></section>
        <section class="section"><h3>工具检测</h3>${textTable(["工具","采集状态"], Object.entries(data.tools || {}).map(([name,status]) => [name,status]), "等待首次采集", "成功采集后显示远端工具能力。")}</section>
        ${Object.keys(data.optional_errors || {}).length ? `<section class="section"><h3>可选指标错误</h3>${textTable(["能力","最近错误"], Object.entries(data.optional_errors))}</section>` : ""}
        ${host.last_error ? `<section class="section"><div class="error-panel">最近采集错误：${esc(host.last_error)}</div></section>` : ""}
      </div>`;
    $("#back-hosts").onclick = () => navigate("hosts");
    $("#edit-current-host")?.addEventListener("click", () => showHostForm(host));
    $$('[data-operation]').forEach((button) => { button.onclick = () => hostOperation(host, button.dataset.operation); });
    $$('[data-detail-tab]').forEach((button) => { button.onclick = () => { activateDetailTab(button.dataset.detailTab); if (button.dataset.detailTab === "development") renderDevelopmentPanel(host, $("#development-panel")); }; });
    $$('[data-gpu-config]').forEach((button) => { button.onclick = () => showGpuConfig(host, button.dataset.gpuConfig); });
    $$('[data-history-hours]').forEach((button) => { button.onclick = () => selectHistoryRange(hostId, Number(button.dataset.historyHours), button); });
    $("#mount-thresholds-button")?.addEventListener("click", () => showMountThresholds(host, data.filesystems || [], result.mount_thresholds || [], thresholds));
    if (host.docker_enabled) {
      const filterDocker = () => {
        const query = $("#docker-filter").value.trim().toLowerCase();
        const status = $("#docker-status").value.toLowerCase();
        $$("#docker-table tbody tr").forEach((row) => { row.hidden = Boolean((query && !row.dataset.search.includes(query)) || (status && !row.dataset.status.includes(status))); });
      };
      $("#docker-filter")?.addEventListener("input", filterDocker);
      $("#docker-status")?.addEventListener("change", filterDocker);
      $("#docker-inventory-button")?.addEventListener("click", () => renderDockerInventory(host).catch((error) => { $("#docker-inventory").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; }));
      $$('[data-docker-logs]').forEach((button) => { button.onclick = async () => {
        try {
          const keyword = prompt("容器日志关键词（可留空）", "") ?? "";
          const query = new URLSearchParams({container:button.dataset.dockerLogs, lines:"100", keyword});
          const result = await withOperationProgress("正在读取容器日志", () => api(`/api/hosts/${host.id}/docker/logs?${query}`));
          const dialog = createDialog(`<div class="dialog-heading"><span class="dialog-icon">D</span><div><h2>容器日志</h2><p>${esc(button.dataset.dockerLogs)}</p></div></div><pre class="snapshot">${esc(result.lines.join("\n") || "没有匹配日志")}</pre><div class="dialog-actions"><button data-close>关闭</button></div>`, "wide-dialog");
          $('[data-close]', dialog).onclick = () => dialog.close("done");
        } catch (error) { toast(error.message, "error"); }
      }; });
    }
    activateDetailTab(activeTab);
    if (activeTab === "development") renderDevelopmentPanel(host, $("#development-panel"));
    loadHistory(hostId, 1);
  } catch (error) {
    if (error.status === 401) return showLogin();
    $("#page-content").innerHTML = `<div class="error-panel">${esc(error.message)}</div>`;
  }
}

function activateDetailTab(tab) {
  $$('[data-detail-tab]').forEach((button) => button.classList.toggle("active", button.dataset.detailTab === tab));
  $$('[data-tab-panel]').forEach((panel) => { panel.hidden = panel.dataset.tabPanel !== tab; });
}

function gpuState(runtime) {
  if (!runtime) return "未配置";
  const names = {disabled:"已禁用",unknown:"等待采样",busy:"使用中",idle_timing:"空闲计时",pending:"待执行",running:"执行中",retry_wait:"等待重试",cooldown:"冷却中",frozen:"已冻结"};
  let detail = names[runtime.state] || runtime.state;
  if (runtime.state === "idle_timing") detail += ` · 已空闲 ${Math.floor(runtime.idle_seconds_accum || 0)} 秒`;
  if (runtime.last_error && ["unknown", "frozen"].includes(runtime.state)) detail += ` · ${runtime.last_error}`;
  return detail;
}

function gpuTable(host, gpus, runtime) {
  if (!gpus.length) return `<div class="empty"><div><strong>${esc(host.enabled ? "未检测到 NVIDIA GPU" : "主机采集已禁用")}</strong>${esc(host.enabled ? "请检查 nvidia-smi 安装状态和采集权限。" : "启用采集后才能获取 GPU 指标。")}</div></div>`;
  return `<div class="table-wrap"><table><thead><tr><th>设备</th><th>型号</th><th>利用率 / 显存</th><th>温度 / 功耗</th><th>风扇 / P-State</th><th>ECC</th><th>PCIe</th><th>节流 / XID</th><th>进程</th><th>调度状态</th>${can("gpu.manage") ? "<th>操作</th>" : ""}</tr></thead><tbody>${gpus.map((gpu) => {
    const pcie = gpu.pcie_gen == null ? "未知" : `Gen${gpu.pcie_gen} / x${gpu.pcie_width ?? "?"}`;
    const pcieTitle = gpu.pcie_gen_max == null ? "" : `最大 Gen${gpu.pcie_gen_max} / x${gpu.pcie_width_max ?? "?"}`;
    const processRows = (gpu.processes || []).map((process) => `<div class="gpu-process-row"><strong>${esc(process.user || "unknown")} · PID ${esc(process.pid)}${process.pid_exists === false ? "（PID 不存在）" : ""}</strong><span>显存 ${esc(process.memory_mib ?? "?")} MiB</span><small title="${esc(process.cwd || "无权限/不可用")}">目录：${esc(process.cwd || "无权限/不可用")}</small><small title="${esc(process.command || process.name || "未知")}">命令：${esc(process.command || process.name || "未知")}</small></div>`).join("");
    const processes = (gpu.processes || []).map((process) => `${process.user || "unknown"} · PID ${process.pid}${process.pid_exists === false ? "（PID 不存在）" : ""} · ${process.memory_mib ?? "?"} MiB · ${process.cwd || "无权限/不可用"} · ${process.command || process.name || "未知"}`).join("\n");
    const xid = (gpu.xid_errors || []).map((event) => `XID ${event.code}`).join(", ");
    const residual = gpu.residual_memory_suspected ? `<span class="status degraded" title="${esc(processes || "nvidia-smi 未返回可归属进程")}">疑似残留 ${esc(gpu.residual_memory_mib)} MiB</span>` : "";
    const clock = gpu.clock_current_mhz == null ? "时钟未知" : `${gpu.clock_current_mhz} MHz`;
    return `<tr><td><strong>GPU ${esc(gpu.index)}</strong><div class="mono">${esc(gpu.uuid)}</div></td><td>${esc(gpu.name)}<div class="hint">驱动 ${esc(gpu.driver)} · ${esc(gpu.compute_mode || "默认模式")}</div></td><td>${percentage(gpu.utilization_percent)}<div class="hint">${percentage(gpu.memory_percent)} · ${esc(gpu.memory_used_mib)} / ${esc(gpu.memory_total_mib)} MiB</div>${residual}</td><td>${gpu.temperature_c == null ? "不支持" : `${gpu.temperature_c} C`} / ${gpu.power_w == null ? "不支持" : `${gpu.power_w} W`}<div class="hint">上限 ${gpu.power_limit_w == null ? "未知" : `${gpu.power_limit_w} W`}</div></td><td>${gpu.fan_percent == null ? "不支持" : `${gpu.fan_percent}%`} / ${esc(gpu.pstate || "未知")}<div class="hint">${esc(clock)} · 应用 ${esc(gpu.clock_application_mhz ?? "-")} / 默认 ${esc(gpu.clock_default_application_mhz ?? "-")}</div></td><td>${gpu.ecc_corrected ?? "-"} / ${gpu.ecc_uncorrected ?? "-"}<div class="hint">可纠正 / 不可纠正</div></td><td title="${esc(pcieTitle)}" class="${gpu.pcie_degraded ? "text-danger" : ""}">${esc(pcie)}${gpu.pcie_degraded ? " · 降级" : ""}</td><td>${gpu.throttle_active ? `<span class="status degraded" title="${esc((gpu.throttle_reasons || []).join("、"))}">节流</span>` : "正常"}<div class="hint" title="${esc((gpu.xid_errors || []).map((event) => event.message).join("\n"))}">${esc(xid || "无 XID")}</div></td><td title="${esc(processes)}">${gpu.processes?.length || 0} 个<div class="hint">${esc([...new Set((gpu.processes || []).map((process) => process.user || "unknown"))].join("、"))}</div>${processRows ? `<details class="gpu-process-details"><summary>查看进程</summary>${processRows}</details>` : ""}</td><td>${esc(gpuState(runtime[gpu.uuid]))}</td>${can("gpu.manage") ? `<td><button data-gpu-config="${esc(gpu.uuid)}">配置</button></td>` : ""}</tr>`;
  }).join("")}</tbody></table></div>`;
}

function dockerTable(items) {
  if (!items.length) return '<div class="empty"><div><strong>没有容器数据</strong>Docker 未运行、无权限或当前没有容器。</div></div>';
  return `<div class="table-wrap"><table><thead><tr><th>名称 / 镜像</th><th>状态</th><th>CPU / 内存</th><th>网络 / Block IO</th><th>GPU 映射</th><th>资源限制</th><th>挂载</th><th>日志</th></tr></thead><tbody>${items.map((item) => {
    const limits = item.resource_limits || {};
    const gpu = item.gpu_requests?.length ? `${item.gpu_requests.length} 项` : "无";
    const mounts = item.mounts?.length ? item.mounts.map((mount) => `${mount.source || "?"} → ${mount.destination || "?"}${mount.rw ? " (rw)" : " (ro)"}`).join("\n") : "无";
    return `<tr data-search="${esc([item.Names || item.Name,item.Image,item.ID].join(" ").toLowerCase())}" data-status="${esc((item.State || item.Status || "").toLowerCase())}"><td><strong>${esc(item.Names || item.Name)}</strong><div class="hint mono">${esc(item.Image || "")}</div></td><td>${esc(item.State || item.Status)}</td><td>${percentage(item.cpu_percent)} / ${percentage(item.memory_percent)}</td><td>${esc(item.network_io || "未知")}<div class="hint">${esc(item.block_io || "未知")}</div></td><td>${esc(gpu)}</td><td title="${esc(JSON.stringify(limits))}">${limits.memory_bytes ? fmtBytes(limits.memory_bytes) : "未限制"}<div class="hint">PIDs ${esc(limits.pids_limit ?? "未限制")}</div></td><td title="${esc(mounts)}">${item.mounts?.length || 0} 个</td><td><button data-docker-logs="${esc(item.ID || item.Names || item.Name)}">查看</button></td></tr>`;
  }).join("")}</tbody></table></div>`;
}

async function renderDockerInventory(host) {
  const root = $("#docker-inventory");
  if (!root) return;
  const result = await withOperationProgress("正在读取 Docker 镜像和 Volume", () => api(`/api/hosts/${host.id}/docker/inventory`), {target:root});
  if (!result.available) {
    root.innerHTML = `<div class="error-panel">${esc(result.error || "Docker 不可用")}</div>`;
    return;
  }
  const info = result.info || {};
  root.innerHTML = `<section class="section"><h3>Docker 系统</h3><div class="kv-grid"><div class="kv"><small>Server 版本</small><strong>${esc(info.ServerVersion || "未知")}</strong></div><div class="kv"><small>存储驱动</small><strong>${esc(info.Driver || "未知")}</strong></div><div class="kv"><small>Cgroup</small><strong>${esc(info.CgroupVersion || info.CgroupDriver || "未知")}</strong></div><div class="kv"><small>Docker Root</small><strong>${esc(info.DockerRootDir || "未知")}</strong></div></div></section><section class="section"><h3>本地镜像</h3>${textTable(["仓库","Tag","ID","大小","创建"], (result.images || []).map((item) => [item.Repository || item.repository,item.Tag || item.tag,item.ID || item.ID,item.Size || item.size,item.CreatedSince || item.CreatedAt || "-"]))}</section><section class="section"><h3>Volume</h3>${textTable(["名称","驱动","范围"], (result.volumes || []).map((item) => [item.Name || item.name,item.Driver || item.driver,item.Scope || item.scope || "local"]))}</section><section class="section"><h3>Compose 项目</h3>${textTable(["名称","状态","配置文件"], (result.compose || []).map((item) => [item.Name || item.name,item.Status || item.status,item.ConfigFiles || item.configFiles || "-"]))}</section>`;
}

function selectHistoryRange(hostId, hours, button) {
  $$('[data-history-hours]').forEach((item) => item.classList.toggle("primary", item === button));
  loadHistory(hostId, hours);
}

async function loadHistory(hostId, hours) {
  const canvas = $("#history-chart");
  if (!canvas) return;
  const end = new Date();
  const start = new Date(end.getTime() - hours * 3600 * 1000);
  try {
    const [cpu, memory] = await Promise.all([
      api(`/api/hosts/${hostId}/history?metric=cpu_usage&start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`),
      api(`/api/hosts/${hostId}/history?metric=memory_usage&start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`),
    ]);
    state.history = {canvas, series:[{name:"CPU",color:getCss("--primary"),items:cpu.items},{name:"内存",color:getCss("--warning"),items:memory.items}], kind:cpu.kind};
    drawHistory(state.history);
  } catch (error) { toast(error.message, "error"); }
}

function getCss(name) { return getComputedStyle(document.body).getPropertyValue(name).trim(); }

function drawHistory(chart) {
  const {canvas, series, kind} = chart;
  if (!canvas?.isConnected) return;
  const ratio = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(280, Math.floor(rect.width * ratio));
  canvas.height = Math.floor(240 * ratio);
  const ctx = canvas.getContext("2d");
  const width = canvas.width;
  const height = canvas.height;
  const left = 38 * ratio;
  const right = 14 * ratio;
  const top = 28 * ratio;
  const bottom = 32 * ratio;
  ctx.clearRect(0, 0, width, height);
  ctx.font = `${11 * ratio}px sans-serif`;
  ctx.fillStyle = getCss("--muted");
  [0,25,50,75,100].forEach((value) => {
    const y = height - bottom - value / 100 * (height - top - bottom);
    ctx.fillText(`${value}`, 5 * ratio, y + 4 * ratio);
    ctx.strokeStyle = getCss("--line");
    ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width - right, y); ctx.stroke();
  });
  const timestamps = series.flatMap((line) => line.items).map((item) => new Date(item.ts).getTime()).filter(Number.isFinite);
  if (!timestamps.length) { ctx.fillText("该范围暂无历史数据", Math.max(left, width / 2 - 58 * ratio), height / 2); return; }
  const min = Math.min(...timestamps);
  const max = Math.max(...timestamps);
  const gapLimit = ({raw:30,mid:180,long:900,adaptive:900}[kind] || 30) * 1000;
  series.forEach((line, index) => {
    ctx.strokeStyle = line.color;
    ctx.lineWidth = 2 * ratio;
    ctx.beginPath();
    let previous = null;
    line.items.forEach((item) => {
      const time = new Date(item.ts).getTime();
      const x = left + (time - min) / Math.max(1, max - min) * (width - left - right);
      const y = height - bottom - Math.max(0, Math.min(100, Number(item.value))) / 100 * (height - top - bottom);
      if (previous == null || time - previous > gapLimit) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      previous = time;
    });
    ctx.stroke();
    ctx.fillStyle = line.color;
    ctx.fillText(line.name, width - (100 - index * 48) * ratio, 17 * ratio);
  });
  ctx.fillStyle = getCss("--muted");
  ctx.fillText(fmtTime(new Date(min).toISOString()), left, height - 8 * ratio);
  const endText = fmtTime(new Date(max).toISOString());
  ctx.fillText(endText, Math.max(left, width - right - ctx.measureText(endText).width), height - 8 * ratio);
}

async function showGpuConfig(host, uuid) {
  try {
    const result = await api(`/api/hosts/${host.id}/gpu/${encodeURIComponent(uuid)}`);
    const config = result.config || {};
    const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon">G</span><div><h2>GPU 调度配置</h2><p class="mono">${esc(uuid)}</p></div></div><div class="form-grid two">${switchControl("enabled", "启用单卡调度", Boolean(config.enabled))}<label>空闲判定<select name="idle_mode"><option value="">继承系统设置</option><option value="both" ${config.idle_mode === "both" ? "selected" : ""}>利用率和显存</option><option value="util" ${config.idle_mode === "util" ? "selected" : ""}>仅利用率</option><option value="memory" ${config.idle_mode === "memory" ? "selected" : ""}>仅显存</option></select></label><label>利用率阈值（%）<input name="util_threshold" type="number" min="0" max="100" value="${esc(config.util_threshold ?? "")}" placeholder="继承"></label><label>显存阈值（%）<input name="memory_threshold" type="number" min="0" max="100" value="${esc(config.memory_threshold ?? "")}" placeholder="继承"></label><label>计算进程保护<select name="process_guard"><option value="">继承</option><option value="true" ${config.process_guard === true ? "selected" : ""}>启用</option><option value="false" ${config.process_guard === false ? "selected" : ""}>禁用</option></select></label><label>执行模式<select name="mode_override"><option value="">继承主机</option><option value="tmux" ${config.mode_override === "tmux" ? "selected" : ""}>Tmux 后台</option><option value="direct" ${config.mode_override === "direct" ? "selected" : ""}>直接 Shell</option></select></label><label>工作目录覆盖<input name="cwd_override" value="${esc(config.cwd_override || "")}" placeholder="继承主机"></label><label>Shell 覆盖<input name="shell_override" value="${esc(config.shell_override || "")}" placeholder="继承主机"></label></div><label>单卡命令覆盖<textarea name="command_override" maxlength="500" placeholder="留空继承主机默认命令">${esc(config.command_override || "")}</textarea></label><label>环境变量覆盖（JSON 对象，留空继承）<textarea name="env_override" placeholder='{"KEY":"VALUE"}'>${config.env_override ? esc(JSON.stringify(config.env_override, null, 2)) : ""}</textarea></label><div class="notice-panel">修改配置会清零该 GPU 的空闲计时。自动调度仍受全局开关和主机级开关控制。</div><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button class="primary" type="submit">保存配置</button></div></form>`, "wide-dialog");
    $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
    $("form", dialog).onsubmit = async (event) => {
      event.preventDefault();
      const form = event.target;
      try {
        let env = null;
        if (form.env_override.value.trim()) env = JSON.parse(form.env_override.value);
        const payload = {enabled:form.enabled.checked, idle_mode:form.idle_mode.value || null, util_threshold:form.util_threshold.value === "" ? null : Number(form.util_threshold.value), memory_threshold:form.memory_threshold.value === "" ? null : Number(form.memory_threshold.value), process_guard:form.process_guard.value === "" ? null : form.process_guard.value === "true", mode_override:form.mode_override.value || null, command_override:form.command_override.value.trim() || null, cwd_override:form.cwd_override.value.trim() || null, shell_override:form.shell_override.value.trim() || null, env_override:env};
        await ensureElevated();
        await api(`/api/hosts/${host.id}/gpu/${encodeURIComponent(uuid)}`, {method:"PATCH", body:payload});
        toast("GPU 调度配置已保存，空闲计时已重置");
        dialog.close("done");
        renderHostDetail(host.id, "gpu");
      } catch (error) { $(".form-error", form).textContent = error.message; }
    };
  } catch (error) { toast(error.message, "error"); }
}

async function hostOperation(host, operation) {
  const workbench = $("#operation-workbench");
  try {
    if (operation === "refresh") return refreshHost(host.id, $('[data-operation="refresh"]'));
    if (operation === "delete") {
      await ensureElevated();
      if (!confirm(`确认软删除主机 ${host.name}？远端已有会话和任务不会被终止。`)) return;
      await api(`/api/hosts/${host.id}`, {method:"DELETE", body:{}});
      toast("主机已删除");
      return navigate("hosts");
    }
    if (operation === "terminal") return openTerminal(host);
    if (operation === "key-push") return showKeyPush(host);
    if (operation === "stress") return showStressDialog(host);
    workbench.hidden = false;
    workbench.innerHTML = '<div class="loading">正在读取远端状态</div>';
    if (operation === "inspection") await renderHealthInspection(host, workbench);
    else if (operation === "services") await renderSystemServices(host, workbench);
    else if (operation === "network") await renderNetworkDiagnostic(host, workbench);
    else if (operation === "tmux") await renderTmux(host, workbench);
    else if (operation === "processes") await renderProcesses(host, workbench);
    else if (operation === "tools") await renderTools(host, workbench);
    workbench.scrollIntoView({behavior:"smooth", block:"nearest"});
  } catch (error) {
    workbench.hidden = false;
    workbench.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`;
  }
}

async function showKeyPush(host) {
  try {
    const keys = (await api("/api/credentials/ssh-keys")).items || [];
    if (!keys.length) return toast("请先在系统设置的 SSH 密钥库中保存公钥", "warning");
    const dialog = createDialog(`<form><div class="dialog-heading"><div><h2>推送平台公钥</h2><p>${esc(host.name)} · 幂等写入 ~/.ssh/authorized_keys</p></div></div><label>选择密钥<select name="ssh_key_id">${keys.map((item) => `<option value="${item.id}">${esc(item.name)} · ${esc(item.key_type)}</option>`).join("")}</select></label><label>模式<select name="mode"><option value="script">仅输出脚本</option><option value="remote">远程 SSH 执行</option></select></label><pre data-push-output class="file-preview-content"></pre><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button type="submit" class="primary">生成</button></div></form>`, "wide-dialog");
    $("[data-cancel]", dialog).onclick = () => dialog.close("cancel");
    $("form", dialog).onsubmit = async (event) => { event.preventDefault(); try { const form = event.target; const result = await api(`/api/hosts/${host.id}/key-push`, {method:"POST", body:{ssh_key_id:Number(form.ssh_key_id.value), mode:form.mode.value}}); $("[data-push-output]", dialog).textContent = result.script || `${result.stdout || ""}\n${result.stderr || ""}`; } catch (error) { $(".form-error", dialog).textContent = error.message; } };
  } catch (error) { toast(error.message, "error"); }
}

async function renderHealthInspection(host, root) {
  const result = await withOperationProgress(
    `正在巡检 ${host.name}`,
    () => api(`/api/hosts/${host.id}/health-inspection`, {method:"POST", body:{}}),
  );
  const status = {passed:"通过", warning:"警告", unavailable:"不可用"};
  root.hidden = false;
  root.innerHTML = `<section class="section"><div class="toolbar"><div><h3>主机只读巡检报告</h3><div class="hint">${fmtTime(result.inspected_at)} · 通过 ${result.passed} / 警告 ${result.warnings} / 不可用 ${result.unavailable}</div></div><button data-close-workbench>关闭</button></div><div class="inspection-grid">${result.checks.map((item) => `<article class="inspection-item ${esc(item.status)}"><header><strong>${esc(item.title)}</strong><span class="status ${item.status === "passed" ? "online" : item.status === "warning" ? "degraded" : "unknown"}">${status[item.status] || item.status}</span></header><p>${esc(item.summary)}</p>${item.details ? `<details><summary>查看原始结果</summary><pre class="snapshot">${esc(item.details)}</pre></details>` : ""}</article>`).join("")}</div><div class="notice-panel">巡检仅执行只读命令：NVIDIA、inode、SMART、dmesg 摘要、nouveau、Secure Boot、nvidia-persistenced、内核模块、内核版本、NFS 和 NTP，不运行 badblocks。</div></section>`;
  $("[data-close-workbench]", root).onclick = () => { root.hidden = true; root.innerHTML = ""; };
}

async function renderSystemServices(host, root) {
  const result = await withOperationProgress("正在读取 systemd 服务", () => api(`/api/hosts/${host.id}/system-services`), {target:root});
  root.innerHTML = `<section class="section"><div class="toolbar"><div><h3>关键 systemd 服务</h3><div class="hint">只读查看状态和日志；重启仅生成脚本，不由网页执行。</div></div><button data-close-workbench>关闭</button></div>${result.items.length ? `<div class="table-wrap"><table><thead><tr><th>服务</th><th>加载</th><th>运行状态</th><th>开机状态</th><th>操作</th></tr></thead><tbody>${result.items.map((item) => `<tr><td class="mono">${esc(item.unit)}</td><td>${esc(item.load)}</td><td><span class="status ${item.active === "active" ? "online" : item.load === "not-found" ? "disabled" : "degraded"}">${esc(item.active)} / ${esc(item.sub)}</span></td><td>${esc(item.enabled)}</td><td><button data-service-logs="${esc(item.unit)}">日志</button><button data-service-plan="${esc(item.unit)}">重启脚本</button></td></tr>`).join("")}</tbody></table></div><pre class="diagnostic-output" data-service-output hidden></pre>` : '<div class="notice-panel">远端未安装 systemctl，或没有可读取的服务状态。</div>'}</section>`;
  $('[data-close-workbench]', root).onclick = () => { root.hidden = true; root.innerHTML = ""; };
  $$('[data-service-logs]', root).forEach((button) => { button.onclick = async () => {
    try {
      const keyword = prompt("日志关键词（可留空）", "") ?? "";
      const query = new URLSearchParams({unit:button.dataset.serviceLogs, lines:"100", keyword});
      const logs = await withOperationProgress("正在读取服务日志", () => api(`/api/hosts/${host.id}/system-services/logs?${query}`), {target:root});
      const output = $('[data-service-output]', root);
      output.textContent = logs.lines.join("\n") || "没有匹配日志";
      output.hidden = false;
    } catch (error) { toast(error.message, "error"); }
  }; });
  $$('[data-service-plan]', root).forEach((button) => { button.onclick = async () => {
    try {
      const query = new URLSearchParams({unit:button.dataset.servicePlan});
      const plan = await api(`/api/hosts/${host.id}/system-services/restart-plan?${query}`);
      const output = $('[data-service-output]', root);
      output.textContent = plan.script;
      output.hidden = false;
    } catch (error) { toast(error.message, "error"); }
  }; });
}

async function renderNetworkDiagnostic(host, root) {
  root.innerHTML = `<section class="section"><div class="toolbar"><div><h3>单目标网络诊断</h3><div class="hint">命令从当前远端主机发起，仅允许一次 ping 或单端口连接测试，不支持网段扫描。</div></div><button data-close-workbench>关闭</button></div><form data-network-form><div class="form-grid two"><label>方式<select name="mode"><option value="ping">Ping</option><option value="port">TCP 端口</option></select></label><label>目标 IP 或主机名<input name="target" placeholder="例如 10.0.0.8" required></label><label data-network-port hidden>端口<input name="port" type="number" min="1" max="65535" value="22"></label></div><button class="primary" type="submit">开始诊断</button><div class="form-error"></div></form><pre class="diagnostic-output" data-network-output hidden></pre></section>`;
  $('[data-close-workbench]', root).onclick = () => { root.hidden = true; root.innerHTML = ""; };
  const form = $('[data-network-form]', root);
  form.mode.onchange = () => { $('[data-network-port]', form).hidden = form.mode.value !== "port"; };
  form.onsubmit = async (event) => {
    event.preventDefault();
    try {
      const payload = {mode:form.mode.value, target:form.target.value.trim(), port:form.mode.value === "port" ? Number(form.port.value) : null};
      const result = await withOperationProgress("正在执行网络诊断", () => api(`/api/hosts/${host.id}/network-diagnostic`, {method:"POST", body:payload}), {target:root});
      const output = $('[data-network-output]', root);
      output.textContent = `${result.success ? "连通" : "失败"}\n${result.output || "无命令输出"}`;
      output.hidden = false;
    } catch (error) { $('.form-error', form).textContent = error.message; }
  };
}

async function renderTmux(host, root) {
  const result = await api(`/api/hosts/${host.id}/tmux`);
  root.innerHTML = `<section class="section"><div class="toolbar"><h3>Tmux 会话</h3><div class="toolbar-group"><button data-close-workbench>关闭</button><button data-reload-tmux>刷新</button>${can("tmux.manage") ? '<button data-create-tmux class="primary">新建会话</button>' : ""}</div></div>${result.items.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>窗口数</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${result.items.map((item) => `<tr><td class="mono">${esc(item.name)}</td><td>${esc(item.windows)}</td><td>${fmtTime(Number(item.created) * 1000)}</td><td class="nowrap"><button class="text-button" data-tmux-snapshot="${esc(item.name)}">快照</button>${can("tmux.manage") ? `<button class="text-button" data-tmux-rename="${esc(item.name)}">重命名</button>` : ""}<button class="text-button" data-tmux-attach="${esc(item.name)}">附着</button>${can("tmux.manage") ? `<button class="text-button danger-quiet" data-tmux-delete="${esc(item.name)}">删除</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty"><div><strong>没有 Tmux 会话</strong>刷新远端状态后重试。</div></div>'}</section>`;
  $('[data-close-workbench]', root).onclick = () => { root.hidden = true; root.innerHTML = ""; };
  $('[data-reload-tmux]', root).onclick = () => renderTmux(host, root).catch((error) => { root.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; });
  $('[data-create-tmux]', root)?.addEventListener("click", () => showTmuxNameDialog(host, null, () => renderTmux(host, root)));
  $$('[data-tmux-snapshot]', root).forEach((button) => { button.onclick = async () => {
    try {
      const snapshot = await api(`/api/hosts/${host.id}/tmux/${encodeURIComponent(button.dataset.tmuxSnapshot)}/snapshot`);
      const dialog = createDialog(`<div class="dialog-heading"><span class="dialog-icon">T</span><div><h2>Tmux 快照</h2><p>${esc(button.dataset.tmuxSnapshot)}</p></div></div><pre class="snapshot">${esc(snapshot.snapshot || "快照为空")}</pre><div class="dialog-actions"><button data-close>关闭</button></div>`, "wide-dialog");
      $('[data-close]', dialog).onclick = () => dialog.close("done");
    } catch (error) { toast(error.message, "error"); }
  }; });
  $$('[data-tmux-rename]', root).forEach((button) => { button.onclick = () => showTmuxNameDialog(host, button.dataset.tmuxRename, () => renderTmux(host, root)); });
  $$('[data-tmux-attach]', root).forEach((button) => { button.onclick = () => openTerminal(host, button.dataset.tmuxAttach); });
  $$('[data-tmux-delete]', root).forEach((button) => { button.onclick = async () => {
    try {
      await ensureElevated();
      if (!confirm(`确认删除 Tmux 会话 ${button.dataset.tmuxDelete}？`)) return;
      await api(`/api/hosts/${host.id}/tmux/${encodeURIComponent(button.dataset.tmuxDelete)}`, {method:"DELETE", body:{}});
      toast("Tmux 会话已删除");
      renderTmux(host, root);
    } catch (error) { toast(error.message, "error"); }
  }; });
}

function showTmuxNameDialog(host, oldName, done) {
  const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon">T</span><div><h2>${oldName ? "重命名会话" : "新建 Tmux 会话"}</h2><p>${esc(host.name)}</p></div></div><label>会话名称<input name="name" value="${esc(oldName || "")}" maxlength="100" pattern="[A-Za-z0-9_.-]+" required><span class="hint">使用字母、数字、点、下划线或连字符。</span></label><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button type="submit" class="primary">确认</button></div></form>`);
  $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    try {
      const name = event.target.name.value.trim();
      if (oldName) await api(`/api/hosts/${host.id}/tmux/${encodeURIComponent(oldName)}`, {method:"PATCH", body:{name}});
      else await api(`/api/hosts/${host.id}/tmux`, {method:"POST", body:{name}});
      toast(oldName ? "Tmux 会话已重命名" : "Tmux 会话已创建");
      dialog.close("done");
      await done();
    } catch (error) { $(".form-error", dialog).textContent = error.message; }
  };
}

async function renderProcesses(host, root, hideKernel = true) {
  const result = await api(`/api/hosts/${host.id}/processes?hide_kernel=${hideKernel ? "1" : "0"}`);
  root.innerHTML = `<section class="section"><div class="toolbar"><h3>远端进程 <span class="hint">${result.items.length} 项</span></h3><div class="toolbar-group"><div class="toolbar-search"><input data-process-search placeholder="搜索 PID、用户、目录或命令"></div><label class="check-label"><input data-hide-kernel type="checkbox" ${hideKernel ? "checked" : ""}>隐藏内核线程</label><button data-close-workbench>关闭</button><button data-reload-processes>刷新</button></div></div><div class="notice-panel">显示 RSS、Swap 和累计磁盘读写；缩进表示当前返回结果中的父子进程关系。Swap 和 IO 可能因权限不可用。</div><div class="table-wrap"><table><thead><tr><th>进程树</th><th>用户</th><th>状态</th><th>CPU</th><th>RSS</th><th>Swap</th><th>累计读 / 写</th><th>启动时间</th><th>工作目录</th><th>命令</th><th>操作</th></tr></thead><tbody>${result.items.slice(0,500).map((item) => `<tr data-process-search-value="${esc([item.pid,item.ppid,item.user,item.cwd || "",item.command].join(" ").toLowerCase())}"><td><span style="padding-left:${Math.min(10, item.tree_depth || 0) * 12}px">${item.tree_depth ? "↳ " : ""}${item.pid}</span><div class="hint">PPID ${item.ppid}</div></td><td>${esc(item.user)}</td><td>${item.zombie ? '<span class="status degraded">僵尸</span>' : esc(item.state)}</td><td>${item.cpu}%</td><td>${fmtBytes(item.rss_bytes)}</td><td>${fmtBytes(item.swap_bytes)}</td><td>${fmtBytes(item.read_bytes)} / ${fmtBytes(item.write_bytes)}</td><td>${esc(item.started)}</td><td class="process-cwd mono" title="${esc(item.cwd || "无权限/不可用")}">${item.cwd ? esc(item.cwd) : '<span class="hint">无权限/不可用</span>'}</td><td class="mono">${esc(item.command)}</td><td>${can("process.terminate") ? `<button class="danger-quiet" data-kill-pid="${item.pid}" data-started="${esc(item.started)}">SIGTERM</button>` : "-"}</td></tr>`).join("")}</tbody></table></div>${result.items.length > 500 ? '<div class="notice-panel">为保持浏览器响应速度，当前只展示前 500 项。</div>' : ""}</section>`;
  $('[data-close-workbench]', root).onclick = () => { root.hidden = true; root.innerHTML = ""; };
  $('[data-reload-processes]', root).onclick = () => renderProcesses(host, root, $('[data-hide-kernel]', root).checked).catch((error) => { root.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; });
  $('[data-hide-kernel]', root).onchange = (event) => renderProcesses(host, root, event.target.checked).catch((error) => { root.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; });
  $('[data-process-search]', root).oninput = (event) => { const query = event.target.value.trim().toLowerCase(); $$('[data-process-search-value]', root).forEach((row) => { row.hidden = Boolean(query && !row.dataset.processSearchValue.includes(query)); }); };
  $$('[data-kill-pid]', root).forEach((button) => { button.onclick = async () => {
    try {
      await ensureElevated();
      if (!confirm(`向 PID ${button.dataset.killPid} 发送 SIGTERM？平台会重新核对启动时间，避免终止复用 PID。`)) return;
      await api(`/api/hosts/${host.id}/processes/${button.dataset.killPid}/terminate`, {method:"POST", body:{started:button.dataset.started, signal:"TERM"}});
      toast("SIGTERM 已发送");
      renderProcesses(host, root, hideKernel);
    } catch (error) { toast(error.message, "error"); }
  }; });
}

async function renderTools(host, root) {
  const result = await api(`/api/hosts/${host.id}/tools`);
  root.innerHTML = `<section class="section"><div class="toolbar"><h3>工具能力</h3><div class="toolbar-group"><button data-close-workbench>关闭</button><button data-reload-tools>重新检测</button></div></div><div class="table-wrap"><table><thead><tr><th>工具</th><th>状态</th><th>说明</th><th>操作</th></tr></thead><tbody>${Object.entries(result.tools).map(([name,status]) => `<tr><td class="mono">${esc(name)}</td><td><span class="status ${status === "available" ? "online" : "disabled"}">${status === "available" ? "可用" : "未安装"}</span></td><td>${toolDescription(name)}</td><td>${can("tools.install") && host.allow_install && status !== "available" && name !== "nvidia-smi" ? `<button data-install-tool="${esc(name)}">安装</button>` : "-"}</td></tr>`).join("")}</tbody></table></div></section>`;
  $('[data-close-workbench]', root).onclick = () => { root.hidden = true; root.innerHTML = ""; };
  $('[data-reload-tools]', root).onclick = () => renderTools(host, root).catch((error) => { root.innerHTML = `<div class="error-panel">${esc(error.message)}</div>`; });
  $$('[data-install-tool]', root).forEach((button) => { button.onclick = async () => {
    try {
      const plan = await api(`/api/hosts/${host.id}/tools/${encodeURIComponent(button.dataset.installTool)}/install-plan`);
      await ensureElevated();
      const sudoNotice = plan.sudo_password_configured ? "将优先使用精确 NOPASSWD 授权；若远端仍要求密码，则通过标准输入使用已保存的远端 sudo 密码。" : "当前未保存远端 sudo 密码；该操作必须由精确 NOPASSWD 授权执行。";
      if (!confirm(`将在 ${host.name} 执行：\n\n${plan.command}\n\n${sudoNotice}\n\n确认继续？`)) return;
      button.disabled = true;
      button.textContent = "安装中";
      await withOperationProgress(
        `正在安装 ${button.dataset.installTool}`,
        () => api(`/api/hosts/${host.id}/tools/${encodeURIComponent(button.dataset.installTool)}/install`, {method:"POST", body:{}}),
      );
      toast("安装命令已完成并验证工具可用");
      renderTools(host, root);
    } catch (error) { button.disabled = false; button.textContent = "安装"; toast(error.message, "error"); }
  }; });
}

function toolDescription(name) {
  return esc(({tmux:"会话管理与后台任务",htop:"交互式进程查看",ncdu:"交互式磁盘分析",nvtop:"交互式 GPU 查看",sysstat:"iostat/mpstat/sar 统计",iotop:"交互式进程 IO",smartmontools:"smartctl 磁盘健康",ethtool:"网卡协商和错误",iproute2:"ip/ss 网络信息",lsof:"文件和端口句柄",jq:"JSON 解析",git:"代码版本管理",rsync:"数据同步",unzip:"解压工具","build-essential":"编译工具链",cmake:"构建工具",btop:"交互式系统查看",iperf3:"网络吞吐测试",tree:"目录树查看",vim:"文本编辑器",smartctl:"物理磁盘健康",sensors:"CPU 温度",stress_ng:"压力测试","stress-ng":"压力测试","nvidia-smi":"NVIDIA GPU 指标",docker:"容器指标"})[name] || "远端可选能力");
}

function showStressDialog(host) {
  const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon warning-icon">!</span><div><h2>启动压力测试</h2><p>${esc(host.name)} · 仅支持 CPU 和内存</p></div></div><div class="form-grid two"><label>CPU 工作线程<input name="cpu_workers" type="number" min="0" max="256" value="1" required></label><label>内存工作线程<input name="memory_workers" type="number" min="0" max="256" value="0" required></label><label>任务总内存比例（%）<input name="memory_percent" type="number" min="0" max="80" value="0" required></label><label>持续时间（分钟）<input name="duration_minutes" type="number" min="1" max="30" value="1" required></label></div><div class="notice-panel">压力测试会显著增加目标主机负载。内存比例是整个任务的总上限，多 worker 会自动折算。</div><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button class="danger" type="submit">验证并启动</button></div></form>`);
  $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    const form = event.target;
    const payload = {cpu_workers:Number(form.cpu_workers.value), memory_workers:Number(form.memory_workers.value), memory_percent:Number(form.memory_percent.value), duration_minutes:Number(form.duration_minutes.value)};
    try {
      if (payload.cpu_workers + payload.memory_workers === 0) throw new Error("CPU 和内存工作线程不能同时为 0");
      if (!payload.memory_workers && payload.memory_percent) throw new Error("未启用内存 worker 时，内存比例必须为 0");
      await ensureElevated();
      if (!confirm(`确认在 ${host.name} 启动 ${payload.duration_minutes} 分钟压力测试？\nCPU worker: ${payload.cpu_workers}\n内存 worker: ${payload.memory_workers}\n总内存比例: ${payload.memory_percent}%`)) return;
      const result = await api(`/api/hosts/${host.id}/stress`, {method:"POST", body:payload});
      dialog.close("done");
      toast(`压力测试已启动：${result.task_id}`);
      showStressStatus(host, result.task_id, payload.duration_minutes * 60);
    } catch (error) { $(".form-error", form).textContent = error.message; }
  };
}

function showStressStatus(host, taskId, durationSeconds) {
  const root = $("#operation-workbench");
  root.hidden = false;
  root.innerHTML = `<section class="console-panel"><div class="toolbar"><h3>压力测试 <span class="mono">${esc(taskId.slice(0,8))}</span></h3><button data-stop-stress class="danger">停止测试</button></div><div class="kv-grid"><div class="kv"><small>任务状态</small><strong data-stress-state>运行中</strong></div><div class="kv"><small>已运行</small><strong data-stress-elapsed>0 秒</strong></div><div class="kv"><small>预计剩余</small><strong data-stress-remaining>${durationSeconds} 秒</strong></div><div class="kv"><small>任务 ID</small><strong class="mono">${esc(taskId)}</strong></div></div><div class="metric-line"><header><span>计划时长</span><strong data-stress-percent>0.0%</strong></header><progress class="bar" data-stress-progress max="100" value="0" aria-label="压力测试计划时长进度"></progress></div></section>`;
  const started = Date.now();
  const updateElapsed = () => {
    const elapsed = Math.min(durationSeconds, Math.floor((Date.now() - started) / 1000));
    $("[data-stress-elapsed]", root).textContent = `${elapsed} 秒`;
    $("[data-stress-remaining]", root).textContent = `${Math.max(0, durationSeconds - elapsed)} 秒`;
    const percent = durationSeconds ? elapsed / durationSeconds * 100 : 0;
    $("[data-stress-progress]", root).value = percent;
    $("[data-stress-percent]", root).textContent = `${percent.toFixed(1)}%`;
  };
  const finish = () => {
    clearInterval(pollTimer);
    clearInterval(visualTimer);
    state.stressTimers.delete(pollTimer);
    state.stressTimers.delete(visualTimer);
    $("[data-stop-stress]", root).disabled = true;
  };
  const poll = async () => {
    try {
      const result = await api(`/api/hosts/${host.id}/stress/${taskId}`);
      $('[data-stress-state]', root).textContent = result.task.state;
      updateElapsed();
      if (result.task.state !== "running") finish();
    } catch (error) { finish(); toast(error.message, "error"); }
  };
  const pollTimer = setInterval(poll, 3000);
  const visualTimer = setInterval(updateElapsed, 1000);
  state.stressTimers.add(pollTimer);
  state.stressTimers.add(visualTimer);
  updateElapsed();
  poll();
  $('[data-stop-stress]', root).onclick = async () => {
    try {
      await api(`/api/hosts/${host.id}/stress/${taskId}/stop`, {method:"POST", body:{}});
      finish();
      $('[data-stress-state]', root).textContent = "已停止";
      toast("压力测试已停止");
    } catch (error) { toast(error.message, "error"); }
  };
}

function elevationIsValid() {
  const until = state.user?.elevated_until;
  return Boolean(until && new Date(until).getTime() > Date.now() + 1000);
}

function ensureElevated() {
  if (elevationIsValid()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const dialog = $("#elevate-dialog");
    const form = $("#elevate-form");
    let settled = false;
    const finish = (error = null) => {
      if (settled) return;
      settled = true;
      form.onsubmit = null;
      dialog.oncancel = null;
      $("#elevate-cancel").onclick = null;
      if (error) reject(error); else resolve();
    };
    $(".form-error", form).textContent = "";
    form.reset();
    dialog.showModal();
    $("#elevate-cancel").onclick = () => { dialog.close("cancel"); finish(new Error("操作已取消")); };
    dialog.oncancel = (event) => { event.preventDefault(); dialog.close("cancel"); finish(new Error("操作已取消")); };
    form.onsubmit = async (event) => {
      event.preventDefault();
      try {
        const result = await api("/api/auth/elevate", {method:"POST", body:{password:form.password.value}});
        state.user.elevated_until = result.elevated_until;
        dialog.close("done");
        form.reset();
        finish();
      } catch (error) { $(".form-error", form).textContent = error.message; }
    };
  });
}

async function openTerminal(host, tmuxName = null) {
  try {
    await ensureElevated();
  } catch (error) {
    return toast(error.message, "warning");
  }
  const dialog = createDialog(`<div class="terminal-toolbar"><div><h2>${tmuxName ? `Tmux: ${esc(tmuxName)}` : `Web 终端 · ${esc(host.name)}`}</h2><span class="terminal-status" data-terminal-status>正在连接 ${esc(host.username)}@${esc(host.address)}</span></div><div class="toolbar-group"><select data-terminal-favorites aria-label="快捷命令"><option value="">快捷命令</option></select><button type="button" data-terminal-send-favorite>发送</button><button type="button" data-terminal-save-selection>收藏选中文本</button><button data-terminal-close>断开</button></div></div><div class="terminal-screen" role="application" aria-label="远程终端输出和输入区域"></div>`, "terminal-dialog");
  const terminalHost = $(".terminal-screen", dialog);
  const status = $('[data-terminal-status]', dialog);
  if (!window.Terminal || !window.FitAddon?.FitAddon) {
    status.textContent = "终端组件加载失败，请刷新页面后重试";
    terminalHost.textContent = "[终端组件不可用]";
    return;
  }
  const styleNonce = document.querySelector('meta[name="csp-style-nonce"]')?.content;
  const terminalDocument = styleNonce ? new Proxy(document, {
    get(target, property) {
      if (property === "createElement") return (name, options) => {
        const element = target.createElement(name, options);
        if (name === "style") element.setAttribute("nonce", styleNonce);
        return element;
      };
      const value = Reflect.get(target, property, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  }) : document;
  const terminal = new window.Terminal({
    cursorBlink: true,
    convertEol: false,
    scrollback: 10000,
    fontSize: 13,
    lineHeight: 1.25,
    fontFamily: '"Cascadia Mono", "Noto Sans Mono CJK SC", "Noto Sans Mono", Consolas, monospace',
    documentOverride: terminalDocument,
    theme: {background:"#0a1018", foreground:"#d9f1e3", cursor:"#f5f7fa", selectionBackground:"#315b82"},
  });
  const fitAddon = new window.FitAddon.FitAddon();
  terminal.loadAddon(fitAddon);
  terminal.open(terminalHost);
  const favoriteSelect = $("[data-terminal-favorites]", dialog);
  try {
    const favoriteResult = await api(`/api/hosts/${host.id}/command-favorites`);
    (favoriteResult.items || []).forEach((item) => { const option = document.createElement("option"); option.value = item.command; option.textContent = item.name; favoriteSelect.append(option); });
  } catch (_) { /* favorites are optional for an interactive session */ }
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const socketPath = tmuxName ? `/ws/tmux/${host.id}/${encodeURIComponent(tmuxName)}` : `/ws/terminal/${host.id}`;
  const socket = new WebSocket(`${scheme}://${location.host}${socketPath}`);
  let cleaned = false;
  const send = (data) => { if (socket.readyState === WebSocket.OPEN && data) socket.send(JSON.stringify({type:"input", data})); };
  const resize = () => {
    if (!terminalHost.isConnected) return;
    fitAddon.fit();
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:"resize", cols:terminal.cols, rows:terminal.rows}));
  };
  const observer = new ResizeObserver(resize);
  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    observer.disconnect();
    terminal.dispose();
    if ([WebSocket.CONNECTING, WebSocket.OPEN].includes(socket.readyState)) socket.close();
  };
  socket.onopen = () => { status.textContent = "已连接 · 输入会直接发送到远端 Shell"; resize(); terminal.focus(); };
  socket.onmessage = (event) => {
    if (typeof event.data === "string") terminal.write(event.data);
    else if (event.data instanceof Blob) event.data.text().then((text) => terminal.write(text));
  };
  socket.onerror = () => { status.textContent = "连接异常"; };
  socket.onclose = (event) => {
    status.textContent = `连接已断开${event.reason ? ` · ${event.reason}` : ""}`;
    if (!cleaned) terminal.writeln("\r\n\x1b[90m[连接已断开]\x1b[0m");
  };
  terminal.onData(send);
  $("[data-terminal-send-favorite]", dialog).onclick = () => { if (favoriteSelect.value) send(`${favoriteSelect.value}\r`); terminal.focus(); };
  $("[data-terminal-save-selection]", dialog).onclick = async () => {
    const selected = terminal.getSelection().replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, "").trim().slice(0, 4000);
    if (!selected) return toast("请先在终端中选择文本", "warning");
    const name = window.prompt("快捷命令名称", selected.slice(0, 40));
    if (!name) return;
    try { await api(`/api/hosts/${host.id}/command-favorites`, {method:"POST", body:{name, command:selected}}); toast("快捷命令已保存"); } catch (error) { toast(error.message, "error"); }
  };
  observer.observe(terminalHost);
  requestAnimationFrame(resize);
  $('[data-terminal-close]', dialog).onclick = () => { cleanup(); dialog.close("done"); };
  dialog.addEventListener("close", cleanup, {once:true});
  dialog.addEventListener("cancel", cleanup, {once:true});
}

function pageControls(result, onPage) {
  if (!result.pages || result.pages <= 1) return "";
  return `<div class="pagination"><span>共 ${result.total} 条 · 第 ${result.page} / ${result.pages} 页</span><button data-page-prev ${result.page <= 1 ? "disabled" : ""}>上一页</button><button data-page-next ${result.page >= result.pages ? "disabled" : ""}>下一页</button></div>`;
}

function bindPageControls(root, result, onPage) {
  $('[data-page-prev]', root)?.addEventListener("click", () => onPage(result.page - 1));
  $('[data-page-next]', root)?.addEventListener("click", () => onPage(result.page + 1));
}

async function renderJobs(page = 1, filters = null) {
  const current = filters || {search:"", state:"", mode:""};
  const query = new URLSearchParams({page:String(page), page_size:"50"});
  Object.entries(current).forEach(([key,value]) => { if (value) query.set(key, value); });
  const result = await api(`/api/schedule-jobs?${query}`);
  if (state.page !== "jobs") return;
  $("#page-content").innerHTML = `<form id="job-filters" class="toolbar"><div class="toolbar-group"><div class="toolbar-search"><input name="search" placeholder="搜索任务、命令或 GPU" value="${esc(current.search)}"></div><select name="state"><option value="">全部状态</option><option value="running" ${current.state === "running" ? "selected" : ""}>执行中</option><option value="success" ${current.state === "success" ? "selected" : ""}>成功</option><option value="retry_wait" ${current.state === "retry_wait" ? "selected" : ""}>等待重试</option><option value="frozen" ${current.state === "frozen" ? "selected" : ""}>已冻结</option><option value="failed" ${current.state === "failed" ? "selected" : ""}>失败</option></select><select name="mode"><option value="">全部模式</option><option value="tmux" ${current.mode === "tmux" ? "selected" : ""}>Tmux</option><option value="direct" ${current.mode === "direct" ? "selected" : ""}>直接 Shell</option></select><button type="submit">筛选</button></div>${can("jobs.export") ? '<a href="/api/schedule-jobs/export" class="button-link">导出 CSV</a>' : ""}</form>
    ${result.items.length ? `<div class="table-wrap"><table id="jobs-table"><thead><tr><th>任务 ID</th><th>主机</th><th>GPU</th><th>模式</th><th>状态</th><th>尝试</th><th>开始 / 结束</th><th>结果</th></tr></thead><tbody>${result.items.map((job) => `<tr data-search="${esc([job.id,job.host_id,job.gpu_uuid,job.command_summary].join(" ").toLowerCase())}" data-state="${esc(job.state)}" data-mode="${esc(job.mode)}"><td class="mono">${esc(job.id)}</td><td>${esc(job.host_id ?? "已删除")}</td><td class="mono">${esc(job.gpu_uuid)}</td><td>${job.mode === "tmux" ? "Tmux 提交" : "直接 Shell"}</td><td>${esc(job.state)}</td><td>${esc(job.attempt)}</td><td>${fmtTime(job.started_at)}<div class="hint">${job.finished_at ? fmtTime(job.finished_at) : "未结束"}</div></td><td>${esc(job.error || (job.exit_code == null ? "-" : `退出码 ${job.exit_code}`))}${job.stdout_truncated || job.stderr_truncated ? '<div class="hint">输出已截断</div>' : ""}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty"><div><strong>暂无调度记录</strong>GPU 自动调度发生后会在这里记录提交或执行结果。</div></div>'}${pageControls(result)}`;
  $("#job-filters").onsubmit = (event) => { event.preventDefault(); renderJobs(1, Object.fromEntries(new FormData(event.target).entries())); };
  bindPageControls($("#page-content"), result, (nextPage) => renderJobs(nextPage, current));
}

const alertNames = {host_offline:"主机离线",host_online:"主机恢复",ssh_fingerprint_changed:"SSH 指纹异常",temperature_high:"温度过高",temperature_recovered:"温度恢复",filesystem_usage_high:"磁盘容量过高",filesystem_usage_recovered:"磁盘容量恢复",filesystem_inode_high:"inode 使用率过高",filesystem_inode_recovered:"inode 使用率恢复",swap_usage_high:"Swap 使用率过高",swap_usage_recovered:"Swap 使用率恢复",gpu_schedule_success:"GPU 调度成功",gpu_schedule_failed:"GPU 调度失败",gpu_schedule_frozen:"GPU 调度冻结",gpu_power_high:"GPU 功耗过高",gpu_fan_low:"GPU 风扇异常",gpu_ecc_error:"GPU ECC 错误",gpu_xid_error:"GPU XID 错误",gpu_pcie_degraded:"GPU PCIe 降级",gpu_throttling:"GPU 节流",gpu_residual_memory:"疑似 GPU 残留显存",gpu_idle:"GPU 变为空闲",gpu_busy:"GPU 变为占用",backup_failed:"备份失败"};


const notificationEventLabels = {host_offline:"主机离线",temperature_high:"温度过高",filesystem_usage_high:"磁盘容量过高",filesystem_inode_high:"inode 使用率过高",swap_usage_high:"Swap 使用率过高",gpu_power_high:"GPU 功耗过高",gpu_fan_low:"GPU 风扇异常",gpu_ecc_error:"GPU ECC 错误",gpu_xid_error:"GPU XID 错误",gpu_pcie_degraded:"GPU PCIe 降级",gpu_throttling:"GPU 节流",gpu_residual_memory:"GPU 残留显存",gpu_idle:"GPU 变为空闲",gpu_busy:"GPU 变为占用",gpu_schedule_success:"GPU 调度成功",gpu_schedule_failed:"GPU 调度失败",gpu_schedule_frozen:"GPU 调度冻结",backup_failed:"备份失败"};
const notificationEventGroups = [
  {title:"主机与资源", keys:["host_offline","temperature_high","filesystem_usage_high","filesystem_inode_high","swap_usage_high","backup_failed"]},
  {title:"GPU 健康", keys:["gpu_power_high","gpu_fan_low","gpu_ecc_error","gpu_xid_error","gpu_pcie_degraded","gpu_throttling","gpu_residual_memory"]},
  {title:"GPU 状态与调度", keys:["gpu_idle","gpu_busy","gpu_schedule_success","gpu_schedule_failed","gpu_schedule_frozen"]},
];
const notificationEventKeys = notificationEventGroups.flatMap((group) => group.keys);

function localDateTimeValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
}

async function renderAlerts(page = 1, filters = null) {
  const current = filters || {search:"",host_id:"",alert_type:"",state:"",severity:"",start:"",end:"",include_cleared:""};
  const query = new URLSearchParams({page:String(page), page_size:"50"});
  Object.entries(current).forEach(([key,value]) => { if (value !== "" && value != null) query.set(key, value); });
  const [result, faults] = await Promise.all([api(`/api/alerts?${query}`), api("/api/faults")]);
  if (state.page !== "alerts") return;
  const exportQuery = new URLSearchParams(query); exportQuery.delete("page"); exportQuery.delete("page_size");
  $("#page-content").innerHTML = `<section class="section fault-summary"><div class="section-title"><h3>故障主机聚合</h3><strong>${faults.total} 台需处理</strong></div>${faults.items.length ? `<div class="table-wrap"><table><thead><tr><th>主机</th><th>状态</th><th>活动问题</th><th>操作</th></tr></thead><tbody>${faults.items.map((item) => `<tr><td><strong>${esc(item.host.name)}</strong><div class="hint">${esc(item.host.address)}</div></td><td><span class="status ${esc(item.host.status || "unknown")}">${statusName(item.host)}</span></td><td>${item.issues.map((issue) => esc(alertNames[issue.alert_type] || issue.summary)).join(" / ")}</td><td><button class="text-button" data-detail="${item.host.id}">查看主机</button></td></tr>`).join("")}</tbody></table></div>` : '<div class="notice-panel">当前没有离线、指纹异常、采集降级或活动资源告警主机。</div>'}</section>
    <div class="alert-notification-control"><div><strong>告警提醒 · ${result.toast_enabled ? "当前已开启" : "当前已关闭"}</strong><span>关闭后隐藏顶部红点，并停止网页弹窗和外部通知；告警历史仍会保留。</span></div>${can("alerts.manage") ? `<button type="button" data-alert-notification-toggle role="switch" aria-checked="${Boolean(result.toast_enabled)}" class="${result.toast_enabled ? "danger-quiet" : ""}">${result.toast_enabled ? "关闭告警提醒" : "开启告警提醒"}</button>` : `<span class="status ${result.toast_enabled ? "online" : "disabled"}">${result.toast_enabled ? "已开启" : "已关闭"}</span>`}</div>
    <form id="alert-filters" class="toolbar"><div class="toolbar-group"><div class="toolbar-search"><input name="search" placeholder="搜索事件、主机或摘要" value="${esc(current.search)}"></div><input name="host_id" type="number" min="1" placeholder="主机 ID" value="${esc(current.host_id)}"><input name="alert_type" placeholder="事件类型" value="${esc(current.alert_type)}"><select name="state"><option value="">全部状态</option><option value="active" ${current.state === "active" ? "selected" : ""}>活动中</option><option value="recovered" ${current.state === "recovered" ? "selected" : ""}>已恢复</option></select><select name="severity"><option value="">全部级别</option><option value="critical" ${current.severity === "critical" ? "selected" : ""}>严重</option><option value="warning" ${current.severity === "warning" ? "selected" : ""}>警告</option><option value="info" ${current.severity === "info" ? "selected" : ""}>信息</option></select><div class="date-range-picker" role="group" aria-label="告警时间范围"><label>开始<input name="start" type="datetime-local" value="${esc(localDateTimeValue(current.start))}"></label><span aria-hidden="true">至</span><label>结束<input name="end" type="datetime-local" value="${esc(localDateTimeValue(current.end))}"></label></div><label class="check-label"><input name="include_cleared" type="checkbox" value="1" ${current.include_cleared === "1" ? "checked" : ""}>包含已清理</label><button type="submit">筛选</button></div><div class="toolbar-group">${can("alerts.manage") ? `<button type="button" data-alert-bulk-ack ${result.total ? "" : "disabled"}>一键忽略当前结果</button><button type="button" class="danger-quiet" data-alert-bulk-clear ${result.total ? "" : "disabled"}>一键清理当前结果</button>` : ""}<a class="button-link" href="/api/alerts/export?${exportQuery}">导出 CSV</a></div></form>
    ${result.items.length ? `<div class="table-wrap"><table id="alerts-table"><thead><tr><th>发生时间</th><th>事件</th><th>主机</th><th>级别</th><th>状态</th><th>摘要</th><th>恢复时间</th><th>操作</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${fmtTime(item.created_at)}</td><td>${esc(alertNames[item.alert_type] || item.alert_type)}</td><td>${esc(item.host_name || item.host_id || "平台")}${item.host_name ? `<div class="hint">ID ${item.host_id}</div>` : ""}</td><td>${esc(({critical:"严重",warning:"警告",info:"信息"})[item.severity] || item.severity)}</td><td><span class="status ${item.cleared_at ? "disabled" : item.state === "active" ? (item.severity === "critical" ? "offline" : "degraded") : "online"}">${item.cleared_at ? "已清理" : item.acknowledged_at ? "已忽略提示" : item.state === "active" ? "活动中" : "已恢复"}</span></td><td>${esc(item.summary)}</td><td>${fmtTime(item.recovered_at)}</td><td class="nowrap">${can("alerts.manage") && !item.acknowledged_at && !item.cleared_at ? `<button class="text-button" data-alert-ack="${item.id}">忽略提示</button>` : ""}${can("alerts.manage") && !item.cleared_at ? `<button class="text-button danger-quiet" data-alert-clear="${item.id}">清理</button>` : "-"}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty"><div><strong>没有符合条件的告警</strong>调整筛选条件后重试。</div></div>'}${pageControls(result)}`;
  bindHostLinks($("#page-content"));
  $("#alert-filters").onsubmit = (event) => {
    event.preventDefault();
    const next = Object.fromEntries(new FormData(event.target).entries());
    for (const key of ["start", "end"]) next[key] = next[key] ? new Date(next[key]).toISOString() : "";
    renderAlerts(1, next);
  };
  bindPageControls($("#page-content"), result, (nextPage) => renderAlerts(nextPage, current));
  $("[data-alert-notification-toggle]")?.addEventListener("click", async (event) => {
    const button = event.currentTarget;
    const enabled = button.getAttribute("aria-checked") !== "true";
    try {
      button.disabled = true;
      await updateAlertNotificationSetting(enabled);
      toast(enabled ? "告警提醒已开启" : "告警提醒已关闭");
      await pollAlerts();
      renderAlerts(page, current);
    } catch (error) { button.disabled = false; toast(error.message, "error"); }
  });
  $("[data-alert-bulk-ack]")?.addEventListener("click", async () => {
    try {
      if (!confirm(`确认忽略当前筛选结果中的未处理告警提示？单次最多处理 1000 条。`)) return;
      const response = await api("/api/alerts/bulk-acknowledge", {method:"POST", body:{filters:current}});
      toast(`已忽略 ${response.count} 条告警提示`);
      await pollAlerts();
      renderAlerts(1, current);
    } catch (error) { toast(error.message, "error"); }
  });
  $("[data-alert-bulk-clear]")?.addEventListener("click", async () => {
    try {
      if (!confirm(`确认软清理当前筛选结果中的告警？单次最多处理 1000 条，审计历史仍会保留。`)) return;
      const response = await api("/api/alerts/bulk-clear", {method:"POST", body:{filters:current}});
      toast(`已软清理 ${response.count} 条告警`);
      await pollAlerts();
      renderAlerts(1, current);
    } catch (error) { toast(error.message, "error"); }
  });
  $$('[data-alert-ack]').forEach((button) => { button.onclick = async () => { try { await api(`/api/alerts/${button.dataset.alertAck}/acknowledge`, {method:"POST", body:{}}); toast("已忽略该告警的后续页面提示"); await pollAlerts(); renderAlerts(page, current); } catch (error) { toast(error.message, "error"); } }; });
  $$('[data-alert-clear]').forEach((button) => { button.onclick = async () => { try { if (!confirm("确认从默认告警列表软清理该事件？审计历史仍会保留。")) return; await api(`/api/alerts/${button.dataset.alertClear}`, {method:"DELETE", body:{}}); toast("告警已软清理"); await pollAlerts(); renderAlerts(page, current); } catch (error) { toast(error.message, "error"); } }; });
}

function auditChangesMarkup(changes) {
  const entries = Object.entries(changes || {});
  if (!entries.length) return "-";
  return `<details class="audit-diff"><summary>${entries.length} 项变更</summary>${entries.map(([key, value]) => `<div><strong>${esc(key)}</strong><del>${esc(JSON.stringify(value?.before ?? null))}</del><ins>${esc(JSON.stringify(value?.after ?? null))}</ins></div>`).join("")}</details>`;
}

async function renderLogs(page = 1, filters = null) {
  const current = filters || {username:"", action:"", success:"", search:""};
  const query = new URLSearchParams({page:String(page), page_size:"50"});
  Object.entries(current).forEach(([key,value]) => { if (value !== "") query.set(key, value); });
  const result = await api(`/api/logs?${query}`);
  if (state.page !== "logs") return;
  $("#page-content").innerHTML = `<form id="log-filters" class="toolbar"><div class="toolbar-group"><div class="toolbar-search"><input name="search" placeholder="搜索操作、对象或摘要" value="${esc(current.search)}"></div><input name="username" placeholder="用户名" value="${esc(current.username)}"><input name="action" placeholder="操作类型" value="${esc(current.action)}"><select name="success"><option value="">全部结果</option><option value="1" ${current.success === "1" ? "selected" : ""}>成功</option><option value="0" ${current.success === "0" ? "selected" : ""}>失败</option></select><button type="submit">筛选</button></div>${can("logs.export") ? '<a href="/api/logs/export" class="button-link">导出 CSV</a>' : ""}</form>
    ${result.items.length ? `<div class="table-wrap"><table><thead><tr><th>时间</th><th>用户 / 来源</th><th>操作</th><th>对象</th><th>结果</th><th>摘要</th><th>修改前后</th><th>请求 ID</th></tr></thead><tbody>${result.items.map((item) => `<tr><td>${fmtTime(item.ts)}</td><td>${esc(item.username || "系统")}<div class="hint">${esc(item.source_ip || "-")}</div></td><td class="mono">${esc(item.action)}</td><td>${esc(`${item.target_type || "-"} ${item.target_id || ""}`)}</td><td><span class="status ${item.success ? "online" : "offline"}">${item.success ? "成功" : "失败"}</span></td><td>${esc(item.error || item.summary)}</td><td>${auditChangesMarkup(item.changes)}</td><td class="mono">${esc(item.request_id || "-")}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty"><div><strong>没有符合条件的审计日志</strong>调整筛选条件后重试。</div></div>'}${pageControls(result)}`;
  $("#log-filters").onsubmit = (event) => {
    event.preventDefault();
    const next = Object.fromEntries(new FormData(event.target).entries());
    renderLogs(1, next);
  };
  bindPageControls($("#page-content"), result, (nextPage) => renderLogs(nextPage, current));
}

const settingGroups = {
  collection: {title:"采集与 SSH", copy:"控制采集节奏、并发资源和远端连接行为。", keys:["collection_interval","frontend_refresh_interval","ssh_concurrency","queue_limit","interactive_ssh_limit","ssh_connect_timeout","collection_timeout","collection_retries","retry_interval","install_timeout","ssh_reuse","ssh_idle_close"]},
  storage: {title:"数据与扫描", copy:"控制指标分层保留、数据库自动清理、目录扫描和长任务的默认范围。", keys:["metric_raw_retention_minutes","metric_mid_retention_hours","metric_retention_days","collection_task_retention_minutes","cleanup_interval_minutes","log_retention_days","aggregation_mid_seconds","aggregation_long_seconds","scan_timeout_seconds","scan_max_depth","scan_result_limit","scan_minimum_mib","environment_inventory_timeout","gpu_benchmark_timeout"]},
  alerts: {title:"告警策略", copy:"按资源类别集中配置阈值、采样回差和每类 GPU 健康告警开关。", keys:["green_threshold","yellow_threshold","filesystem_usage_threshold","filesystem_inode_threshold","swap_usage_threshold","cpu_temp_threshold","gpu_temp_threshold","disk_temp_threshold","gpu_power_alert_enabled","gpu_power_threshold_percent","gpu_fan_alert_enabled","gpu_fan_min_percent","gpu_fan_alert_temperature","gpu_ecc_alert_enabled","gpu_ecc_corrected_threshold","gpu_xid_alert_enabled","gpu_pcie_alert_enabled","gpu_throttle_alert_enabled","gpu_residual_alert_enabled","alert_samples","alert_hysteresis","alert_repeat_minutes"]},
  gpu: {title:"GPU 调度", copy:"全局调度规则；主机默认命令在主机编辑页配置。", keys:["gpu_scheduler_enabled","gpu_idle_mode","gpu_util_threshold","gpu_memory_threshold","gpu_idle_seconds","gpu_process_guard","gpu_cooldown_seconds","gpu_max_attempts","gpu_retry_seconds","gpu_freeze_seconds","gpu_submit_timeout","gpu_direct_timeout"]},
  security: {title:"安全、备份与区域", copy:"登录保护、终端会话、备份保留和时间区域设置。", keys:["login_fail_limit","login_window_minutes","login_lock_minutes","session_idle_minutes","terminal_idle_seconds","backup_time","backup_dir","backup_keep","schedule_output_limit","timezone"]},
  notifications: {title:"通知", copy:"控制网页提醒和 Apprise 外部通知。", keys:["toast_enabled","apprise_enabled"]},
};

const settingLabels = {
  collection_interval:"采集间隔（秒）",frontend_refresh_interval:"前端刷新（秒）",ssh_concurrency:"常规 SSH 并发",queue_limit:"任务队列上限",interactive_ssh_limit:"交互 SSH 并发",ssh_connect_timeout:"SSH 连接超时（秒）",collection_timeout:"采集总超时（秒）",collection_retries:"采集重试次数",retry_interval:"重试间隔（秒）",install_timeout:"工具安装超时（秒）",ssh_reuse:"启用 SSH 连接复用",ssh_idle_close:"复用连接空闲关闭（秒）",metric_raw_retention_minutes:"原始指标保留（分钟）",metric_mid_retention_hours:"中期聚合保留（小时）",metric_retention_days:"长期指标保留（天）",collection_task_retention_minutes:"采集任务摘要保留（分钟）",cleanup_interval_minutes:"数据库清理间隔（分钟）",log_retention_days:"审计与通知日志保留（天）",aggregation_mid_seconds:"中期聚合粒度（秒）",aggregation_long_seconds:"长期聚合粒度（秒）",
  scan_timeout_seconds:"目录扫描超时（秒）",scan_max_depth:"大文件扫描深度",scan_result_limit:"大文件返回条数",scan_minimum_mib:"大文件默认阈值（MiB）",environment_inventory_timeout:"环境盘点超时（秒）",gpu_benchmark_timeout:"GPU 评估总超时（秒）",
  green_threshold:"绿色上限（%）",yellow_threshold:"黄色上限（%）",filesystem_usage_threshold:"文件系统容量阈值（%）",filesystem_inode_threshold:"文件系统 inode 阈值（%）",swap_usage_threshold:"Swap 使用率阈值（%）",cpu_temp_threshold:"CPU 温度阈值（C）",gpu_temp_threshold:"GPU 温度阈值（C）",disk_temp_threshold:"磁盘温度阈值（C）",gpu_power_alert_enabled:"启用 GPU 功耗过高告警",gpu_power_threshold_percent:"GPU 功耗上限比例（%）",gpu_fan_alert_enabled:"启用 GPU 风扇异常告警",gpu_fan_min_percent:"GPU 最低风扇转速（%）",gpu_fan_alert_temperature:"风扇告警温度门槛（C）",gpu_ecc_alert_enabled:"启用 GPU ECC 错误告警",gpu_ecc_corrected_threshold:"可纠正 ECC 告警累计值",gpu_xid_alert_enabled:"启用 GPU XID 错误告警",gpu_pcie_alert_enabled:"启用 GPU PCIe 降级告警",gpu_throttle_alert_enabled:"启用 GPU 节流告警",gpu_residual_alert_enabled:"启用 GPU 残留显存告警",alert_samples:"连续样本数",alert_hysteresis:"告警恢复回差",alert_repeat_minutes:"重复提醒间隔（分钟）",
  gpu_scheduler_enabled:"全局 GPU 自动调度",gpu_idle_mode:"空闲判定模式",gpu_util_threshold:"GPU 利用率阈值（%）",gpu_memory_threshold:"显存阈值（%）",gpu_idle_seconds:"默认空闲时长（秒）",gpu_process_guard:"默认计算进程保护",gpu_cooldown_seconds:"冷却时间（秒）",gpu_max_attempts:"最大尝试次数",gpu_retry_seconds:"重试间隔（秒）",gpu_freeze_seconds:"冻结时长（秒）",gpu_submit_timeout:"Tmux 提交超时（秒）",gpu_direct_timeout:"直接 Shell 超时（秒）",
  login_fail_limit:"登录失败上限",login_window_minutes:"登录统计窗口（分钟）",login_lock_minutes:"登录暂停时间（分钟）",session_idle_minutes:"会话闲置超时（分钟）",terminal_idle_seconds:"终端闲置超时（秒）",backup_time:"自动备份时间",backup_dir:"备份目录",backup_keep:"备份保留份数",log_retention_days:"日志保留天数",schedule_output_limit:"单流输出上限（字节）",timezone:"显示时区",toast_enabled:"网页告警提醒",apprise_enabled:"启用 Apprise 通知",
};

const numberRules = {
  collection_interval:[5,60],frontend_refresh_interval:[3,30],ssh_concurrency:[1,30],queue_limit:[10,200],interactive_ssh_limit:[1,10],ssh_connect_timeout:[3,30],collection_timeout:[5,60],collection_retries:[0,3],retry_interval:[1,30],install_timeout:[30,600],ssh_idle_close:[10,600],metric_raw_retention_minutes:[5,360],metric_mid_retention_hours:[1,168],metric_retention_days:[1,30],collection_task_retention_minutes:[15,1440],cleanup_interval_minutes:[1,1440],aggregation_mid_seconds:[30,120],aggregation_long_seconds:[120,600],green_threshold:[0,99],yellow_threshold:[1,100],filesystem_usage_threshold:[1,100],filesystem_inode_threshold:[1,100],swap_usage_threshold:[1,100],cpu_temp_threshold:[0,150],gpu_temp_threshold:[0,150],gpu_power_threshold_percent:[50,100],gpu_fan_min_percent:[0,100],gpu_fan_alert_temperature:[0,120],gpu_ecc_corrected_threshold:[1,1000000],disk_temp_threshold:[0,150],alert_samples:[1,10],alert_hysteresis:[0,20],alert_repeat_minutes:[0,1440],gpu_util_threshold:[0,100],gpu_memory_threshold:[0,100],gpu_idle_seconds:[60,86400],gpu_cooldown_seconds:[0,3600],gpu_max_attempts:[1,5],gpu_retry_seconds:[5,3600],gpu_freeze_seconds:[60,86400],gpu_submit_timeout:[10,120],gpu_direct_timeout:[30,600],login_fail_limit:[1,20],login_window_minutes:[1,60],login_lock_minutes:[1,60],session_idle_minutes:[5,240],terminal_idle_seconds:[30,1800],backup_keep:[1,10],log_retention_days:[7,180],schedule_output_limit:[65536,5242880],
  scan_timeout_seconds:[10,120],scan_max_depth:[1,12],scan_result_limit:[1,200],scan_minimum_mib:[1,10240],environment_inventory_timeout:[10,120],gpu_benchmark_timeout:[60,600],
};

function settingInput(key, value) {
  if (typeof value === "boolean") return switchControl(key, settingLabels[key] || key, value);
  if (key === "gpu_idle_mode") return `<label>${settingLabels[key]}<select name="${key}"><option value="both" ${value === "both" ? "selected" : ""}>利用率和显存</option><option value="util" ${value === "util" ? "selected" : ""}>仅利用率</option><option value="memory" ${value === "memory" ? "selected" : ""}>仅显存</option></select></label>`;
  if (key === "timezone") return `<label>${settingLabels[key]}<select name="${key}"><option value="Asia/Shanghai" ${value === "Asia/Shanghai" ? "selected" : ""}>Asia/Shanghai</option><option value="UTC" ${value === "UTC" ? "selected" : ""}>UTC</option></select></label>`;
  const type = key === "backup_time" ? "time" : numberRules[key] ? "number" : "text";
  const range = numberRules[key] ? `min="${numberRules[key][0]}" max="${numberRules[key][1]}"` : "";
  return `<label>${esc(settingLabels[key] || key)}<input name="${key}" type="${type}" value="${esc(value)}" ${range} required></label>`;
}

const alertSettingGroups = [
  {title:"平台与资源阈值", keys:["green_threshold","yellow_threshold","filesystem_usage_threshold","filesystem_inode_threshold","swap_usage_threshold","cpu_temp_threshold","gpu_temp_threshold","disk_temp_threshold"]},
  {title:"GPU 健康告警", keys:["gpu_power_alert_enabled","gpu_power_threshold_percent","gpu_fan_alert_enabled","gpu_fan_min_percent","gpu_fan_alert_temperature","gpu_ecc_alert_enabled","gpu_ecc_corrected_threshold","gpu_xid_alert_enabled","gpu_pcie_alert_enabled","gpu_throttle_alert_enabled","gpu_residual_alert_enabled"]},
  {title:"告警稳定性", keys:["alert_samples","alert_hysteresis","alert_repeat_minutes"]},
];

function settingsFields(id, keys, values) {
  if (id !== "alerts") return `<div class="form-grid">${keys.map((key) => settingInput(key, values[key])).join("")}</div>`;
  return alertSettingGroups.map((group) => `<fieldset class="settings-subgroup"><legend>${group.title}</legend><div class="form-grid">${group.keys.map((key) => settingInput(key, values[key])).join("")}</div></fieldset>`).join("");
}

function notificationEventGroupMarkup(values, configKey, inputName, title) {
  const selected = new Set(Array.isArray(values[configKey]) ? values[configKey] : notificationEventKeys);
  return `<div class="notification-events"><strong>${title}</strong><div class="notification-event-groups">${notificationEventGroups.map((group) => `<fieldset class="notification-event-group"><legend>${group.title}</legend><div class="notification-event-grid">${group.keys.map((key) => `<label class="check-label"><input type="checkbox" name="${inputName}" value="${key}" ${selected.has(key) ? "checked" : ""}>${notificationEventLabels[key]}</label>`).join("")}</div></fieldset>`).join("")}</div></div>`;
}

function notificationHostMarkup(items) {
  const rows = items.map((item) => `<tr><td><strong>${esc(item.name)}</strong><div class="hint">${esc(item.address)}</div></td><td class="table-checkbox"><input type="checkbox" data-host-notification-enabled="${item.host_id}" ${item.enabled ? "checked" : ""}></td><td class="table-checkbox"><input type="checkbox" data-host-notification-toast="${item.host_id}" ${item.toast_enabled ? "checked" : ""} ${item.enabled ? "" : "disabled"}></td><td class="table-checkbox"><input type="checkbox" data-host-notification-apprise="${item.host_id}" ${item.apprise_enabled ? "checked" : ""} ${item.enabled ? "" : "disabled"}></td><td><button type="button" class="text-button" data-host-notification-events="${item.host_id}">事件范围</button></td></tr>`).join("");
  return `<div class="notification-events"><strong>通知主机</strong><p class="hint">关闭某台主机只停止网页提醒和 Apprise 发送，告警仍会产生并保留在告警历史中。</p><div class="table-wrap"><table><thead><tr><th>主机</th><th>启用</th><th>网页</th><th>Apprise</th><th>事件</th></tr></thead><tbody>${rows || '<tr><td colspan="5">尚未录入主机</td></tr>'}</tbody></table></div></div>`;
}

function notificationSectionMarkup(values, notificationHosts = []) {
  const urls = Array.isArray(values.apprise_urls) ? values.apprise_urls : [];
  const rows = urls.map((url, index) => `<div class="apprise-url-row" data-apprise-row data-configured-index="${index}"><input data-apprise-url placeholder="${esc(url)}" aria-label="通知 URL ${index + 1}"><button type="button" class="icon-button" data-apprise-test title="测试此通知 URL" aria-label="测试此通知 URL">▷</button><button type="button" class="icon-button danger-quiet" data-apprise-remove title="删除此通知 URL" aria-label="删除此通知 URL">×</button></div>`).join("");
  const unavailable = values.apprise_available === false ? '<div class="notice-panel">当前运行环境未安装 Apprise，保存 URL 后才能发送外部通知。</div>' : "";
  return `${unavailable}<div class="notification-url-panel"><div class="notification-url-heading"><div><strong>通知 URL 列表</strong><p>使用 Apprise 通知 URL，向几乎任何服务发送通知！请阅读 <a href="https://github.com/caronc/apprise/wiki" target="_blank" rel="noreferrer">通知服务 Wiki</a> 以了解重要配置说明。</p></div><button type="button" id="apprise-test-all" ${urls.length ? "" : "disabled"}>▷ 测试全部</button></div><div id="apprise-url-list">${rows}</div><button type="button" id="apprise-add-url">＋ 添加 URL</button><span class="hint">例如：ntfy://shengziran。每条 URL 使用一个通知目标，凭据会在服务器端加密保存。</span></div>${notificationHostMarkup(notificationHosts)}${notificationEventGroupMarkup(values, "toast_events", "toast_event", "网页告警")}${notificationEventGroupMarkup(values, "apprise_events", "apprise_event", "发送这些告警")}<div class="action-strip"><button type="button" id="desktop-notification-button">启用浏览器桌面通知</button><span class="hint" id="desktop-notification-status">${"Notification" in window ? `当前权限：${Notification.permission}` : "当前浏览器不支持系统通知"}</span></div>`;
}

async function renderSettings() {
  const [result, users, vault, notificationHosts] = await Promise.all([api("/api/settings"), isAdmin() ? api("/api/users") : Promise.resolve({items:[]}), can("host.manage") ? api("/api/credentials/ssh-keys") : Promise.resolve({items:[]}), can("alerts.manage") ? api("/api/notifications/hosts") : Promise.resolve({items:[]})]);
  if (state.page !== "settings") return;
  const values = result.settings;
  $("#page-content").innerHTML = `<div class="settings-layout"><nav class="settings-nav">${Object.entries(settingGroups).map(([id,group], index) => `<button data-setting-target="${id}" class="${index === 0 ? "active" : ""}">${group.title}</button>`).join("")}${can("host.manage") ? '<button data-setting-target="credentials">SSH 密钥库</button>' : ""}${isAdmin() ? '<button data-setting-target="users">用户管理</button>' : ""}</nav><div><form id="settings-form">${Object.entries(settingGroups).map(([id,group]) => `<section class="settings-section" id="setting-${id}"><h2>${group.title}</h2><p>${group.copy}</p>${settingsFields(id, group.keys, values)}${id === "notifications" ? notificationSectionMarkup(values, notificationHosts.items || []) : ""}</section>`).join("")}<section class="settings-section"><div class="toolbar"><div><strong>数据库 ${fmtBytes(values.database_total_bytes)}</strong><div class="hint">主库 ${fmtBytes(values.database_size_bytes)}，WAL ${fmtBytes(values.wal_size_bytes)}，当前磁盘可用 ${fmtBytes(values.disk_free_bytes)}。清理周期可在“数据与扫描”中设置；压缩会额外执行 VACUUM。相对备份目录以数据目录为基准；同盘备份不能防止磁盘故障。</div></div><div class="toolbar-group">${can("backup.create") ? '<button id="backup-button" type="button">立即备份</button>' : ""}${isAdmin() ? '<button id="compact-database-button" type="button" class="danger-quiet">清理并压缩</button>' : ""}${can("settings.manage") ? '<button class="primary" type="submit">保存设置</button>' : ''}</div></div>${can("settings.manage") ? '' : '<div class="notice-panel">当前账号可查看系统设置，但没有修改权限。</div>'}<div class="form-error"></div></section></form>${can("host.manage") ? `<section class="settings-section" id="setting-credentials"><div class="toolbar"><div><h2>全局 SSH 私钥库</h2><p>私钥在服务端加密保存；主机编辑时可直接选择名称。删除被主机引用的密钥会被拒绝。</p></div><button type="button" id="generate-vault-key">生成新密钥</button></div><div class="table-wrap"><table><thead><tr><th>名称</th><th>类型</th><th>指纹</th><th>操作</th></tr></thead><tbody>${(vault.items || []).map((item) => `<tr><td>${esc(item.name)}</td><td>${esc(item.key_type)}</td><td class="mono">${esc(item.fingerprint)}</td><td><button type="button" class="text-button danger-quiet" data-delete-vault-key="${item.id}" data-delete-vault-key-name="${esc(item.name)}" title="删除密钥 ${esc(item.name)}">删除密钥</button></td></tr>`).join("") || '<tr><td colspan="4">尚未保存密钥</td></tr>'}</tbody></table></div><form id="vault-key-form" class="action-strip"><input name="name" placeholder="密钥名称" maxlength="128" required><textarea name="private_key" placeholder="粘贴 RSA 或 ed25519 私钥" required></textarea><input name="passphrase" type="password" placeholder="passphrase（可选）"><label class="file-picker">加载私钥文件<input name="private_key_file" data-vault-file type="file" accept=".pem,.key,text/plain,application/octet-stream"></label><button class="primary">保存密钥</button></form><div class="hint">已有密钥可选择文件加载；文件内容只提交到服务端并加密保存，不会在服务器保留上传文件。删除密钥前必须先解除所有主机引用。</div><div class="form-error" data-vault-error></div></section>` : ""}${isAdmin() ? `<section class="settings-section" id="setting-users"><div class="toolbar"><div><h2>用户管理</h2><p>角色或状态变更会立即使目标用户的现有会话失效。</p></div></div>${usersTable(users.items)}<form id="create-user-form" class="action-strip"><input name="username" placeholder="新用户名" maxlength="64" required><input name="password" type="password" placeholder="一次性密码（10～128 位）" minlength="10" maxlength="128" required><select name="role"><option value="viewer">普通用户</option><option value="admin">管理员</option></select><button class="primary">创建用户</button></form></section>` : ""}</div></div>`;
  bindSettings(values, notificationHosts.items || []);
  $("#vault-key-form")?.addEventListener("submit", async (event) => { event.preventDefault(); const form = event.target; try { await ensureElevated(); await api("/api/credentials/ssh-keys", {method:"POST", body:{name:form.name.value, private_key:form.private_key.value, passphrase:form.passphrase.value || null}}); toast("SSH 密钥已保存"); renderSettings(); } catch (error) { $("[data-vault-error]").textContent = error.message; } });
  $("[data-vault-file]")?.addEventListener("change", async (event) => { const file = event.target.files?.[0]; if (!file) return; try { if (file.size > 128 * 1024) throw new Error("私钥文件不能超过 128 KiB"); const form = $("#vault-key-form"); form.private_key.value = await file.text(); if (!form.name.value) form.name.value = file.name.replace(/\.(pem|key)$/i, ""); $("[data-vault-error]").textContent = "已加载文件内容，请确认名称和口令后保存"; } catch (error) { $("[data-vault-error]").textContent = error.message; event.target.value = ""; } });
  $("#generate-vault-key")?.addEventListener("click", () => { const dialog = createDialog(`<form><div class="dialog-heading"><div><h2>生成 SSH 私钥</h2><p>密钥在服务端生成并加密保存，私钥不会回显到浏览器。</p></div></div><div class="form-grid two"><label>密钥名称<input name="name" maxlength="128" required></label><label>类型<select name="key_type"><option value="ed25519">ed25519</option><option value="rsa">RSA（3072 位）</option></select></label><label>passphrase（可选）<input name="passphrase" type="password" autocomplete="new-password"></label></div><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button class="primary" type="submit">生成并保存</button></div></form>`, "wide-dialog"); $("[data-cancel]", dialog).onclick = () => dialog.close("cancel"); $("form", dialog).onsubmit = async (event) => { event.preventDefault(); const form = event.target; try { await ensureElevated(); await api("/api/credentials/ssh-keys/generate", {method:"POST", body:{name:form.name.value, key_type:form.key_type.value, passphrase:form.passphrase.value || null}}); toast("SSH 密钥已生成并保存"); dialog.close("done"); renderSettings(); } catch (error) { $(".form-error", dialog).textContent = error.message; } }; });
  $$('[data-delete-vault-key]').forEach((button) => { button.onclick = async () => { const name = button.dataset.deleteVaultKeyName || "该密钥"; if (!confirm(`确认删除 SSH 密钥“${name}”？删除后不能恢复；如果仍被主机引用，系统会拒绝删除。`)) return; try { await ensureElevated(); await api(`/api/credentials/ssh-keys/${button.dataset.deleteVaultKey}`, {method:"DELETE", body:{}}); toast(`SSH 密钥“${name}”已删除`); renderSettings(); } catch (error) { toast(error.message, "error"); } }; });
  if (isAdmin()) bindUsers(users.items);
}

function usersTable(users) {
  return `<div class="table-wrap"><table><thead><tr><th>用户名</th><th>角色</th><th>状态</th><th>首次改密</th><th>创建时间</th><th>操作</th></tr></thead><tbody>${users.map((user) => { const self = user.id === state.user.id; return `<tr><td><strong>${esc(user.username)}</strong>${self ? ' <span class="tag">当前账户</span>' : ""}</td><td><select data-user-role="${user.id}" ${self ? "disabled" : ""}><option value="viewer" ${user.role === "viewer" ? "selected" : ""}>普通用户</option><option value="admin" ${user.role === "admin" ? "selected" : ""}>管理员</option></select></td><td>${switchControl(`active_${user.id}`, user.active ? "已启用" : "已禁用", user.active).replace("name=", `data-user-active="${user.id}" ${self ? "data-disabled=true " : ""}name=`)}</td><td>${user.must_change_password ? "是" : "否"}</td><td>${fmtTime(user.created_at)}</td><td class="nowrap"><button class="text-button" data-reset-user="${user.id}" data-username="${esc(user.username)}" ${self ? "disabled" : ""}>重置密码</button><button class="text-button danger-quiet" data-delete-user="${user.id}" data-username="${esc(user.username)}" ${self ? "disabled" : ""}>删除</button></td></tr>`; }).join("")}</tbody></table></div>`;
}

function groupedCatalog(catalog) {
  const grouped = new Map();
  for (const item of catalog) {
    if (!grouped.has(item.group)) grouped.set(item.group, []);
    grouped.get(item.group).push(item);
  }
  return [...grouped.entries()];
}

function permissionMatrix(users, catalog) {
  return `<div class="table-wrap"><table><thead><tr><th>用户</th><th>角色</th>${catalog.map((item) => `<th title="${esc(item.description)}">${esc(item.label)}</th>`).join("")}</tr></thead><tbody>${users.map((user) => `<tr><td><strong>${esc(user.username)}</strong></td><td>${user.role === "admin" ? "管理员" : "普通用户"}</td>${catalog.map((item) => user.role === "admin" ? '<td>全部</td>' : `<td class="table-checkbox"><input type="checkbox" data-permission-user="${user.id}" data-permission-key="${esc(item.key)}" ${user.granted.includes(item.key) ? "checked" : ""}></td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
}

function permissionPreferenceGroups(catalog, granted, visible) {
  return groupedCatalog(catalog.filter((item) => item.kind === "page" && granted.includes(item.key))).map(([group, items]) => `<section class="settings-section"><h2>${esc(group)}</h2><p>只控制页面显示，不会提升实际权限。</p><div class="permission-grid">${items.map((item) => `<label class="check-label"><input type="checkbox" data-visible-page="${esc(item.key)}" ${visible.includes(item.key) ? "checked" : ""}>${esc(item.label)}<span class="hint">${esc(item.description)}</span></label>`).join("")}</div></section>`).join("");
}

async function renderPermissions() {
  if (state.page !== "permissions") return;
  if (isAdmin()) {
    const result = await api("/api/permissions");
    const catalogs = groupedCatalog(result.catalog);
    $("#page-content").innerHTML = `<div class="settings-layout"><nav class="settings-nav">${catalogs.map(([group], index) => `<button data-permission-target="${esc(group)}" class="${index === 0 ? "active" : ""}">${esc(group)}</button>`).join("")}</nav><div>${catalogs.map(([group, items]) => `<section class="settings-section" id="permission-${esc(group)}"><div class="toolbar"><div><h2>${esc(group)}</h2><p>打勾表示授予该用户对应页面或功能。</p></div></div>${permissionMatrix(result.users, items)}</section>`).join("")}</div></div>`;
    $$('[data-permission-target]').forEach((button) => { button.onclick = () => { $$('[data-permission-target]').forEach((item) => item.classList.toggle("active", item === button)); $(`#permission-${button.dataset.permissionTarget}`)?.scrollIntoView({behavior:"smooth", block:"start"}); }; });
    const pending = new Map();
    $$('[data-permission-user]').forEach((input) => { input.onchange = async () => {
      const userId = Number(input.dataset.permissionUser);
      const rows = $$(`[data-permission-user="${userId}"]`);
      const permissions = rows.filter((node) => node.checked).map((node) => node.dataset.permissionKey);
      if (pending.get(userId)) return;
      pending.set(userId, true);
      try {
        await ensureElevated();
        await api(`/api/users/${userId}/permissions`, {method:"PUT", body:{permissions}});
        toast("用户权限已更新，目标用户需重新登录");
      } catch (error) {
        input.checked = !input.checked;
        toast(error.message, "error");
      } finally {
        pending.delete(userId);
      }
    }; });
    return;
  }
  const result = await api("/api/profile/permissions");
  $("#page-content").innerHTML = `<div class="settings-layout"><nav class="settings-nav"><button class="active">页面显示</button></nav><div>${permissionPreferenceGroups(result.catalog, result.granted, result.visible_pages)}<section class="settings-section"><div class="toolbar"><div><h2>已获授权功能</h2><p>以下功能由管理员授予，是否显示页面由你自己控制。</p></div><button id="save-visibility" class="primary" type="button">保存显示偏好</button></div><div class="tag-line">${result.granted.map((key) => `<span class="tag">${esc(key)}</span>`).join("")}</div></section></div></div>`;
  $("#save-visibility").onclick = async () => {
    try {
      const visiblePages = $$('[data-visible-page]').filter((node) => node.checked).map((node) => node.dataset.visiblePage);
      const response = await api("/api/profile/permissions", {method:"PATCH", body:{visible_pages:visiblePages}});
      state.user.visible_pages = response.visible_pages;
      applyNavigationPermissions();
      toast("页面显示偏好已保存");
    } catch (error) { toast(error.message, "error"); }
  };
}

function fileActionButton(label, action, permission, classes = "") {
  return can(permission) ? `<button type="button" data-file-action="${action}" class="${classes}">${label}</button>` : "";
}

function fileRows(items) {
  return items.map((item) => {
    const isDirectory = item.type === "directory";
    const canOpen = isDirectory || (item.type === "file" && can("files.download"));
    const name = canOpen ? `<button class="text-button" data-file-open="${esc(item.path)}">${esc(item.name)}</button>` : `<span>${esc(item.name)}</span>`;
    const download = item.type === "symlink" ? "" : fileActionButton("下载", "download", "files.download").replace("data-file-action=\"download\"", `data-file-download=\"${esc(item.path)}\"`);
    const copy = item.type === "symlink" ? "" : fileActionButton("复制", "copy", "files.manage", "text-button").replace("data-file-action=\"copy\"", `data-file-copy=\"${esc(item.path)}\"`);
    const preview = item.type === "file" && can("files.download") ? `<button type="button" class="text-button" data-file-preview="${esc(item.path)}">预览</button>` : "";
    return `<tr data-file-path="${esc(item.path)}" data-file-type="${esc(item.type)}"><td>${isDirectory ? "目录" : item.type === "symlink" ? "链接" : "文件"}</td><td>${name}</td><td>${isDirectory ? "-" : fmtBytes(item.size)}</td><td>${item.modified_at ? fmtTime(new Date(item.modified_at * 1000).toISOString()) : "未知"}</td><td class="mono">${esc(item.mode)}</td><td class="mono">${item.uid ?? "-"}:${item.gid ?? "-"}</td><td class="nowrap">${preview}${download}${copy}${fileActionButton("重命名", "rename", "files.manage", "text-button").replace("data-file-action=\"rename\"", `data-file-rename=\"${esc(item.path)}\"`)}${fileActionButton("删除", "delete", "files.delete", "text-button danger-quiet").replace("data-file-action=\"delete\"", `data-file-delete=\"${esc(item.path)}\"`)}</td></tr>`;
  }).join("");
}

async function renderFiles() {
  if (!can("files.browse")) {
    if (state.page === "files") $("#page-content").innerHTML = '<div class="notice-panel">当前账号尚未获得“浏览文件”权限，请联系管理员在权限与界面页授权。</div>';
    return;
  }
  const [hostsResult, listing, scanSettings] = await Promise.all([
    api("/api/file-manager/hosts"),
    state.fileHostId ? api(`/api/hosts/${state.fileHostId}/files?path=${encodeURIComponent(state.filePath)}`) : Promise.resolve(null),
    can("storage.scan") ? getScanSettings() : Promise.resolve(state.scanSettings),
  ]);
  if (state.page !== "files") return;
  if (!state.fileHostId && hostsResult.items.length) state.fileHostId = hostsResult.items[0].id;
  const currentHost = hostsResult.items.find((item) => item.id === state.fileHostId) || hostsResult.items[0] || null;
  if (currentHost && currentHost.id !== state.fileHostId) {
    state.fileHostId = currentHost.id;
    state.filePath = "/";
    return renderFiles();
  }
  const activeListing = currentHost ? (listing || await api(`/api/hosts/${currentHost.id}/files?path=${encodeURIComponent(state.filePath)}`)) : {path:"/", parent:null, items:[]};
  let directoryFavorites = [];
  if (currentHost) { try { directoryFavorites = (await api(`/api/hosts/${currentHost.id}/directory-favorites`)).items || []; } catch (_) {} }
  $("#page-content").innerHTML = `<div class="toolbar"><div class="toolbar-group file-manager-toolbar"><select id="file-host-select">${hostsResult.items.map((host) => `<option value="${host.id}" ${host.id === currentHost?.id ? "selected" : ""}>${esc(host.name)} · ${esc(host.address)}</option>`).join("")}</select><select id="file-favorite-select"><option value="">目录收藏</option>${directoryFavorites.map((item) => `<option value="${esc(item.path)}">${esc(item.name)}</option>`).join("")}</select><input id="file-path" value="${esc(activeListing.path)}" aria-label="当前路径"><button id="file-go">前往</button><button id="file-favorite-add" type="button">收藏当前目录</button>${activeListing.parent ? '<button id="file-up">上一级</button>' : ""}</div><div class="toolbar-group">${can("storage.scan") ? '<button id="file-directory-usage" type="button">目录容量</button><button id="file-large-scan" type="button">扫描大文件</button>' : ""}${can("files.upload") ? '<button id="file-upload-button" type="button" class="primary">上传文件</button><button id="file-folder-upload-button" type="button">上传文件夹</button>' : ""}${fileActionButton("新建目录", "mkdir", "files.manage")}${fileActionButton("刷新", "refresh", "files.browse")}</div></div>${currentHost ? `<div class="notice-panel">当前主机：${esc(currentHost.username)}@${esc(currentHost.address)}。目录下载会自动打包成 ZIP；大文件不会在网页中加载。</div><div id="file-scan-output" class="scan-result-host" hidden></div><div class="table-wrap"><table><thead><tr><th>类型</th><th>名称</th><th>大小</th><th>修改时间</th><th>权限</th><th>属主:属组</th><th>操作</th></tr></thead><tbody>${fileRows(activeListing.items)}</tbody></table></div>` : '<div class="empty"><div><strong>没有可用主机</strong>先添加并授权至少一台主机。</div></div>'}<input id="file-upload-input" type="file" multiple hidden><input id="file-folder-upload-input" type="file" webkitdirectory multiple hidden>`;
  $("#file-host-select")?.addEventListener("change", (event) => { state.fileHostId = Number(event.target.value); state.filePath = "/"; renderFiles(); });
  $("#file-go")?.addEventListener("click", () => { state.filePath = $("#file-path").value || "/"; renderFiles(); });
  $("#file-favorite-select")?.addEventListener("change", (event) => { if (event.target.value) { state.filePath = event.target.value; renderFiles(); } });
  $("#file-favorite-add")?.addEventListener("click", async () => { const name = window.prompt("收藏名称", activeListing.path); if (!name) return; try { await api(`/api/hosts/${state.fileHostId}/directory-favorites`, {method:"POST", body:{name, path:activeListing.path}}); toast("目录已收藏"); renderFiles(); } catch (error) { toast(error.message, "error"); } });
  $("#file-up")?.addEventListener("click", () => { state.filePath = activeListing.parent || "/"; renderFiles(); });
  const runFileScan = async (button, label, mode) => {
    const output = $("#file-scan-output");
    if (!output) return;
    const timeoutSeconds = Number(scanSettings.scan_timeout_seconds);
    const query = mode === "usage"
      ? new URLSearchParams({path:activeListing.path, timeout_seconds:String(timeoutSeconds)})
      : new URLSearchParams({
        path:activeListing.path,
        minimum_bytes:String(Number(scanSettings.scan_minimum_mib) * 1024 * 1024),
        limit:String(scanSettings.scan_result_limit),
        max_depth:String(scanSettings.scan_max_depth),
        timeout_seconds:String(timeoutSeconds),
      });
    const originalText = button.textContent;
    try {
      button.disabled = true;
      button.textContent = mode === "usage" ? "统计中" : "扫描中";
      const endpoint = mode === "usage" ? "usage" : "large-files";
      const result = await withOperationProgress(
        label,
        () => api(`/api/hosts/${state.fileHostId}/files/${endpoint}?${query}`),
        {target:output, timeoutSeconds},
      );
      output.className = "scan-result-host";
      output.innerHTML = scanResultMarkup(result, mode);
      output.hidden = false;
    } catch (error) {
      output.className = "scan-result-host";
      output.innerHTML = `<div class="error-panel">${esc(label)}失败：${esc(error.message)}</div>`;
      output.hidden = false;
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  };
  $("#file-directory-usage")?.addEventListener("click", (event) => runFileScan(event.currentTarget, "正在统计目录容量", "usage"));
  $("#file-large-scan")?.addEventListener("click", (event) => runFileScan(event.currentTarget, "正在扫描大文件", "large"));
  $$('[data-file-open]').forEach((button) => { button.onclick = () => {
    const row = button.closest("tr");
    if (row?.dataset.fileType === "directory") { state.filePath = button.dataset.fileOpen; renderFiles(); }
    else if (can("files.download")) window.open(`/api/hosts/${state.fileHostId}/files/download?path=${encodeURIComponent(button.dataset.fileOpen)}`, "_blank");
  }; });
  $$('[data-file-preview]').forEach((button) => { button.onclick = async () => { try { const result = await api(`/api/hosts/${state.fileHostId}/files/preview?path=${encodeURIComponent(button.dataset.filePreview)}`); const dialog = createDialog(`<div class="dialog-heading"><div><h2>${esc(button.dataset.filePreview)}</h2><p>${fmtBytes(result.size)} · 只读预览</p></div></div><pre class="file-preview-content">${esc(result.content)}</pre><div class="dialog-actions"><button type="button" data-cancel>关闭</button></div>`, "wide-dialog"); $('[data-cancel]', dialog).onclick = () => dialog.close("done"); } catch (error) { toast(error.message, "warning"); } }; });
  $$('[data-file-download]').forEach((button) => { button.onclick = () => window.open(`/api/hosts/${state.fileHostId}/files/download?path=${encodeURIComponent(button.dataset.fileDownload)}`, "_blank"); });
  $$('[data-file-rename]').forEach((button) => { button.onclick = () => showFilePrompt("重命名或移动", button.dataset.fileRename, async (destination) => { await api(`/api/hosts/${state.fileHostId}/files`, {method:"PATCH", body:{source:button.dataset.fileRename, destination}}); renderFiles(); toast("文件路径已更新"); }); });
  $$('[data-file-copy]').forEach((button) => { button.onclick = () => showFilePrompt("复制到", button.dataset.fileCopy, async (destination) => { await withOperationProgress("正在复制远端文件", () => api(`/api/hosts/${state.fileHostId}/files/copy`, {method:"POST", body:{source:button.dataset.fileCopy, destination}})); renderFiles(); toast("文件已复制"); }); });
  $$('[data-file-delete]').forEach((button) => { button.onclick = async () => {
    try {
      await ensureElevated();
      if (!confirm(`确认删除 ${button.dataset.fileDelete}？`)) return;
      await withOperationProgress("正在删除远端文件", () => api(`/api/hosts/${state.fileHostId}/files`, {method:"DELETE", body:{path:button.dataset.fileDelete}}));
      renderFiles();
      toast("文件已删除");
    } catch (error) { toast(error.message, "error"); }
  }; });
  const uploadFiles = async (event) => {
    try {
      const selectedFiles = [...event.target.files];
      if (!selectedFiles.length) return;
      const form = new FormData();
      form.append("path", activeListing.path);
      selectedFiles.forEach((file) => form.append("files", file, file.webkitRelativePath || file.name));
      await withOperationProgress(
        `正在上传 ${selectedFiles.length} 个文件`,
        (progress) => uploadApi(`/api/hosts/${state.fileHostId}/files/upload`, form, progress),
      );
      toast("上传完成");
      renderFiles();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      event.target.value = "";
    }
  };
  $("#file-upload-button")?.addEventListener("click", () => $("#file-upload-input").click());
  $("#file-folder-upload-button")?.addEventListener("click", () => $("#file-folder-upload-input").click());
  $("#file-upload-input")?.addEventListener("change", uploadFiles);
  $("#file-folder-upload-input")?.addEventListener("change", uploadFiles);
  $("[data-file-action='mkdir']")?.addEventListener("click", () => showFilePrompt("新建目录", `${activeListing.path.replace(/\/$/, "")}/new-folder`, async (destination) => { await api(`/api/hosts/${state.fileHostId}/files/directories`, {method:"POST", body:{path:destination}}); renderFiles(); toast("目录已创建"); }));
  $("[data-file-action='refresh']")?.addEventListener("click", () => renderFiles());
}

function showFilePrompt(title, placeholder, onSubmit) {
  const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon">▤</span><div><h2>${esc(title)}</h2><p>请输入远端绝对路径。</p></div></div><label>目标路径<input name="path" value="${esc(placeholder)}" required></label><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button type="submit" class="primary">确认</button></div></form>`);
  $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    try {
      await onSubmit(event.target.path.value);
      dialog.close("done");
    } catch (error) { $(".form-error", dialog).textContent = error.message; }
  };
}

function bindSettings(values, notificationHosts = []) {
  $$('[data-setting-target]').forEach((button) => { button.onclick = () => {
    $$('[data-setting-target]').forEach((item) => item.classList.toggle("active", item === button));
    $(`#setting-${button.dataset.settingTarget}`)?.scrollIntoView({behavior:"smooth", block:"start"});
  }; });
  if (!can("settings.manage")) $$("#settings-form input, #settings-form select, #settings-form textarea").forEach((node) => { node.disabled = true; });
  $("#desktop-notification-button")?.addEventListener("click", async () => {
    const status = $("#desktop-notification-status");
    if (!("Notification" in window)) return;
    const permission = await Notification.requestPermission();
    if (status) status.textContent = `当前权限：${permission}`;
    toast(permission === "granted" ? "浏览器桌面通知已启用" : "浏览器未授予桌面通知权限", permission === "granted" ? "success" : "warning");
  });
  const addAppriseRow = () => {
    const list = $("#apprise-url-list");
    if (!list) return;
    const row = document.createElement("div");
    row.className = "apprise-url-row";
    row.dataset.appriseRow = "";
    row.innerHTML = '<input data-apprise-url placeholder="ntfy://shengziran" aria-label="新的通知 URL"><button type="button" class="icon-button" data-apprise-test title="测试此通知 URL" aria-label="测试此通知 URL">▷</button><button type="button" class="icon-button danger-quiet" data-apprise-remove title="删除此通知 URL" aria-label="删除此通知 URL">×</button>';
    list.appendChild(row);
    row.querySelector("input")?.focus();
    $("#apprise-test-all")?.removeAttribute("disabled");
  };
  $("#apprise-add-url")?.addEventListener("click", addAppriseRow);
  $("#apprise-url-list")?.addEventListener("click", async (event) => {
    const button = event.target.closest("button");
    const row = button?.closest("[data-apprise-row]");
    if (!button || !row) return;
    if (button.matches("[data-apprise-remove]")) {
      row.remove();
      if (!$$(`[data-apprise-row]`, $("#apprise-url-list")).length) $("#apprise-test-all")?.setAttribute("disabled", "");
      return;
    }
    if (!button.matches("[data-apprise-test]") || !can("settings.manage")) return;
    const input = $("[data-apprise-url]", row);
    const value = input?.value.trim() || (row.dataset.configuredIndex != null ? `configured:${row.dataset.configuredIndex}` : "");
    if (!value) return toast("请先输入通知 URL", "warning");
    try {
      button.disabled = true;
      const result = await api("/api/notifications/test", {method:"POST", body:{url:value}});
      toast(result.success ? "通知测试成功" : "通知测试失败", result.success ? "success" : "error");
    } catch (error) { toast(error.message, "error"); }
    finally { button.disabled = false; }
  });
  $("#apprise-test-all")?.addEventListener("click", async (event) => {
    if (!can("settings.manage")) return;
    try {
      event.currentTarget.disabled = true;
      const urls = $$('[data-apprise-row]', $("#apprise-url-list")).map((row) => {
        const input = $("[data-apprise-url]", row);
        return input?.value.trim() || (row.dataset.configuredIndex != null ? `configured:${row.dataset.configuredIndex}` : "");
      }).filter(Boolean);
      const result = await api("/api/notifications/test", {method:"POST", body:{urls}});
      const failed = result.results.find((item) => !item.success);
      toast(result.success ? `已发送 ${result.results.length} 个通知目标` : (failed?.summary || "通知测试失败"), result.success ? "success" : "error");
    } catch (error) { toast(error.message, "error"); }
    finally { event.currentTarget.disabled = false; }
  });
  $$('[data-host-notification-enabled], [data-host-notification-toast], [data-host-notification-apprise]').forEach((input) => {
    input.addEventListener("change", async () => {
      const hostId = Number(input.dataset.hostNotificationEnabled || input.dataset.hostNotificationToast || input.dataset.hostNotificationApprise);
      const item = notificationHosts.find((entry) => entry.host_id === hostId);
      if (!item) return;
      const enabled = $(`[data-host-notification-enabled="${hostId}"]`);
      const toastInput = $(`[data-host-notification-toast="${hostId}"]`);
      const appriseInput = $(`[data-host-notification-apprise="${hostId}"]`);
      try {
        await api(`/api/hosts/${hostId}/notification-settings`, {method:"PUT", body:{enabled:enabled.checked, toast_enabled:toastInput.checked, apprise_enabled:appriseInput.checked, toast_events:item.toast_events, apprise_events:item.apprise_events}});
        item.enabled = enabled.checked;
        item.toast_enabled = toastInput.checked;
        item.apprise_enabled = appriseInput.checked;
        if (!enabled.checked) { toastInput.disabled = true; appriseInput.disabled = true; }
        else { toastInput.disabled = false; appriseInput.disabled = false; }
        toast("主机告警通知设置已保存");
      } catch (error) { input.checked = !input.checked; toast(error.message, "error"); }
    });
  });
  $$('[data-host-notification-events]').forEach((button) => { button.onclick = () => {
    const hostId = Number(button.dataset.hostNotificationEvents);
    const item = notificationHosts.find((entry) => entry.host_id === hostId);
    if (!item) return;
    const groups = notificationEventGroups.map((group) => `<fieldset class="notification-event-group"><legend>${group.title}</legend><div class="notification-event-grid">${group.keys.map((key) => `<label class="check-label"><input type="checkbox" name="host_toast_event" value="${key}" ${item.toast_events.includes(key) ? "checked" : ""}>网页 ${notificationEventLabels[key]}</label><label class="check-label"><input type="checkbox" name="host_apprise_event" value="${key}" ${item.apprise_events.includes(key) ? "checked" : ""}>外部 ${notificationEventLabels[key]}</label>`).join("")}</div></fieldset>`).join("");
    const dialog = createDialog(`<form><div class="dialog-heading"><div><h2>${esc(item.name)} · 告警事件范围</h2><p>可分别控制网页提醒和 Apprise 外部通知。</p></div></div>${groups}<div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button type="submit" class="primary">保存</button></div></form>`, "wide-dialog");
    $("[data-cancel]", dialog).onclick = () => dialog.close("cancel");
    $("form", dialog).onsubmit = async (event) => { event.preventDefault(); try { item.toast_events = $$('[name="host_toast_event"]', dialog).filter((input) => input.checked).map((input) => input.value); item.apprise_events = $$('[name="host_apprise_event"]', dialog).filter((input) => input.checked).map((input) => input.value); await api(`/api/hosts/${hostId}/notification-settings`, {method:"PUT", body:{enabled:item.enabled, toast_enabled:item.toast_enabled, apprise_enabled:item.apprise_enabled, toast_events:item.toast_events, apprise_events:item.apprise_events}}); toast("主机告警事件范围已保存"); dialog.close("done"); } catch (error) { $(".form-error", dialog).textContent = error.message; } };
  }; });
  $("#settings-form").onsubmit = async (event) => {
    event.preventDefault();
    if (!can("settings.manage")) return;
    const form = event.target;
    const payload = {};
    Object.values(settingGroups).flatMap((group) => group.keys).forEach((key) => {
      const input = form.elements[key];
      payload[key] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
    });
    payload.toast_events = $$('[name="toast_event"]', form).filter((input) => input.checked).map((input) => input.value);
    payload.apprise_events = $$('[name="apprise_event"]', form).filter((input) => input.checked).map((input) => input.value);
    payload.apprise_urls = $$('[data-apprise-row]', form).map((row) => {
      const input = $("[data-apprise-url]", row);
      return input?.value.trim() || (row.dataset.configuredIndex != null ? `configured:${row.dataset.configuredIndex}` : "");
    }).filter(Boolean);
    try {
      const response = await api("/api/settings", {method:"PATCH", body:payload});
      state.timeZone = payload.timezone;
      state.refreshMs = payload.frontend_refresh_interval * 1000;
      state.scanSettings = {...state.scanSettings, ...response.settings};
      state.scanSettingsLoaded = true;
      toast("系统设置已保存");
      renderSettings();
    } catch (error) { $(".form-error", form).textContent = error.message; }
  };
  const backupButton = $("#backup-button");
  if (backupButton) backupButton.onclick = async () => {
    try { const result = await api("/api/backups", {method:"POST", body:{}}); toast(`备份已创建：${result.path}`); }
    catch (error) { toast(error.message, "error"); }
  };
  const compactButton = $("#compact-database-button");
  if (compactButton) compactButton.onclick = async () => {
    try {
      await ensureElevated();
      if (!confirm("将按当前保留策略删除过期指标和任务记录，并压缩 SQLite 文件以释放磁盘空间。此操作无法撤销，确认继续？")) return;
      compactButton.disabled = true;
      compactButton.textContent = "正在压缩";
      const result = await api("/api/maintenance/compact", {method:"POST", body:{}});
      toast(`数据库维护完成，释放 ${fmtBytes(result.reclaimed_bytes)}；删除采集任务 ${result.cleanup.collection_tasks} 条`);
      renderSettings();
    } catch (error) {
      toast(error.message, "error");
    } finally {
      if (compactButton.isConnected) { compactButton.disabled = false; compactButton.textContent = "清理并压缩"; }
    }
  };
  $("#create-user-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await api("/api/users", {method:"POST", body:{username:event.target.username.value, password:event.target.password.value, role:event.target.role.value}});
      toast("用户已创建，首次登录必须修改密码");
      renderSettings();
    } catch (error) { toast(error.message, "error"); }
  });
}

function bindUsers(users) {
  $$('[data-user-role]').forEach((select) => { select.onchange = async () => {
    const original = users.find((user) => user.id === Number(select.dataset.userRole))?.role;
    try { await api(`/api/users/${select.dataset.userRole}`, {method:"PATCH", body:{role:select.value}}); toast("用户角色已更新"); renderSettings(); }
    catch (error) { select.value = original; toast(error.message, "error"); }
  }; });
  $$('[data-user-active]').forEach((input) => {
    if (input.dataset.disabled) input.disabled = true;
    input.onchange = async () => {
      try { await api(`/api/users/${input.dataset.userActive}`, {method:"PATCH", body:{active:input.checked}}); toast(`用户已${input.checked ? "启用" : "禁用"}`); renderSettings(); }
      catch (error) { input.checked = !input.checked; toast(error.message, "error"); }
    };
  });
  $$('[data-reset-user]').forEach((button) => { button.onclick = () => showResetPassword(button.dataset.resetUser, button.dataset.username); });
  $$('[data-delete-user]').forEach((button) => { button.onclick = async () => {
    try {
      await ensureElevated();
      if (!confirm(`确认删除用户 ${button.dataset.username}？`)) return;
      await api(`/api/users/${button.dataset.deleteUser}`, {method:"DELETE", body:{}});
      toast("用户已删除");
      renderSettings();
    } catch (error) { toast(error.message, "error"); }
  }; });
}

function showResetPassword(userId, username) {
  const dialog = createDialog(`<form><div class="dialog-heading"><span class="dialog-icon warning-icon">!</span><div><h2>重置用户密码</h2><p>${esc(username)} 的现有会话会立即失效。</p></div></div><label>一次性新密码<input name="password" type="password" minlength="10" maxlength="128" autocomplete="new-password" required></label><div class="form-error"></div><div class="dialog-actions"><button type="button" data-cancel>取消</button><button type="submit" class="danger">验证并重置</button></div></form>`);
  $('[data-cancel]', dialog).onclick = () => dialog.close("cancel");
  $("form", dialog).onsubmit = async (event) => {
    event.preventDefault();
    try {
      await ensureElevated();
      await api(`/api/users/${userId}/reset-password`, {method:"POST", body:{password:event.target.password.value}});
      toast("密码已重置，用户下次登录必须修改密码");
      dialog.close("done");
      renderSettings();
    } catch (error) { $(".form-error", dialog).textContent = error.message; }
  };
}

function openPasswordDialog(forced = false) {
  const dialog = $("#password-dialog");
  const form = $("#password-form");
  form.dataset.forced = forced ? "true" : "false";
  form.reset();
  $(".form-error", form).textContent = "";
  $("#password-cancel").hidden = forced;
  $("#password-dialog-copy").textContent = forced ? "首次登录或密码重置后必须设置新密码。" : "修改后当前会话会退出，请使用新密码重新登录。";
  dialog.oncancel = (event) => { if (forced) event.preventDefault(); };
  if (!dialog.open) dialog.showModal();
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  $("#login-error").textContent = "";
  const button = $('button[type="submit"]', form);
  button.disabled = true;
  try {
    const result = await api("/api/auth/login", {method:"POST", body:{username:form.username.value, password:form.password.value}});
    form.reset();
    acceptUser(result.user);
  } catch (error) { $("#login-error").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#password-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target;
  try {
    await api("/api/auth/change-password", {method:"POST", body:{current_password:form.current_password.value, new_password:form.new_password.value}});
    $("#password-dialog").close("done");
    toast("密码已修改，请重新登录");
    showLogin();
  } catch (error) { $(".form-error", form).textContent = error.message; }
});

$("#password-cancel").onclick = () => $("#password-dialog").close("cancel");
$("#change-password-button").onclick = () => openPasswordDialog(false);
$("#logout-button").onclick = async () => { try { await api("/api/auth/logout", {method:"POST", body:{}}); } catch (_) { /* Local logout still proceeds. */ } finally { showLogin(); } };
$("#theme-select").onchange = async (event) => {
  const previous = document.body.dataset.theme;
  document.body.dataset.theme = event.target.value;
  if (state.history) { state.history.series[0].color = getCss("--primary"); state.history.series[1].color = getCss("--warning"); drawHistory(state.history); }
  try { await api("/api/profile/theme", {method:"PATCH", body:{theme:event.target.value}}); }
  catch (error) { document.body.dataset.theme = previous; event.target.value = previous; toast(error.message, "error"); }
};
$$("#main-nav button").forEach((button) => { button.onclick = () => navigate(button.dataset.page); });
$("#mobile-menu-button").onclick = () => { document.body.classList.toggle("nav-open"); $("#sidebar-scrim").hidden = !document.body.classList.contains("nav-open"); };
$("#sidebar-scrim").onclick = closeMobileNav;
document.addEventListener("visibilitychange", () => { if (!document.hidden && state.page === "dashboard") renderDashboard(true); });
window.addEventListener("resize", () => { if (state.history) drawHistory(state.history); });

initialize();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/static/service-worker.js").catch(() => {}));
}
