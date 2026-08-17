"use strict";

(function expose(root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (root) root.ServerMonitorLogic = api;
})(typeof globalThis !== "undefined" ? globalThis : this, () => {
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  }

  function formatBytes(value, suffix = "") {
    if (value == null || Number.isNaN(Number(value))) return "未知";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let index = 0;
    let current = Number(value);
    while (Math.abs(current) >= 1024 && index < units.length - 1) { current /= 1024; index += 1; }
    return `${current.toFixed(index ? 1 : 0)} ${units[index]}${suffix}`;
  }

  function formatPercentage(value, waiting = "未知") {
    return value == null ? waiting : `${Number(value).toFixed(1)}%`;
  }

  function formatDuration(seconds) {
    if (seconds == null) return "未知";
    const total = Math.max(0, Math.floor(Number(seconds)));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    return days ? `${days} 天 ${hours} 小时` : hours ? `${hours} 小时 ${minutes} 分钟` : `${minutes} 分钟`;
  }

  function formatShortDuration(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    const days = Math.floor(total / 86400);
    const hours = Math.floor((total % 86400) / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    if (days) return `${days}天${hours}小时`;
    if (hours) return `${hours}小时${minutes}分`;
    if (minutes) return `${minutes}分${total % 60}秒`;
    return `${total}秒`;
  }

  function dashboardMatches(filters, card) {
    const search = String(filters?.search || "");
    const status = String(filters?.status || "");
    const selectedTags = Array.isArray(filters?.tags) ? filters.tags : [];
    const gpuUser = String(filters?.gpu_user || "");
    const tags = Array.isArray(card?.tags) ? card.tags : [];
    const gpuUsers = Array.isArray(card?.gpuUsers) ? card.gpuUsers : [];
    return (
      (!search || String(card?.search || "").includes(search)) &&
      (!status || card?.status === status) &&
      (!selectedTags.length || selectedTags.every((tag) => tags.includes(tag))) &&
      (!gpuUser || gpuUsers.includes(gpuUser))
    );
  }

  function normalizeBenchmark(result = {}) {
    const training = result.training || {};
    return {
      mode: result.mode === "multi" ? "multi" : "single",
      gpuCount: Number(result.gpu_count || 0),
      matrix: (result.matrix || []).map((item) => ({
        precision: String(item.precision || "unknown"),
        unit: String(item.unit || "TFLOPS"),
        aggregate: item.aggregate ?? null,
        perGpu: (item.per_gpu || []).map((gpu) => ({device: gpu.device, value: gpu.tops})),
      })),
      training: {
        model: training.model || null,
        dataset: training.dataset || "synthetic",
        iterationsPerSecond: training.it_per_sec ?? null,
        imagesPerSecond: training.images_per_sec ?? null,
        loss: training.avg_loss ?? null,
        accuracy: training.avg_accuracy ?? null,
      },
      tpDegree: Number(result.parallelism?.tp_degree ?? result.gpu_count ?? 0),
      tp8Ready: Boolean(result.parallelism?.tp8_ready),
    };
  }

  return {
    escapeHtml,
    formatBytes,
    formatPercentage,
    formatDuration,
    formatShortDuration,
    dashboardMatches,
    normalizeBenchmark,
  };
});
