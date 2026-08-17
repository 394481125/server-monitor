"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const logic = require("../../monitor/static/app_logic.js");

test("escapes untrusted HTML and formats metric values", () => {
  assert.equal(logic.escapeHtml('<script a="1">&'), "&lt;script a=&quot;1&quot;&gt;&amp;");
  assert.equal(logic.formatBytes(1536), "1.5 KiB");
  assert.equal(logic.formatBytes(null), "未知");
  assert.equal(logic.formatPercentage(12.34), "12.3%");
  assert.equal(logic.formatDuration(90061), "1 天 1 小时");
  assert.equal(logic.formatShortDuration(61), "1分1秒");
});

test("dashboard filters require every selected tag and the chosen GPU user", () => {
  const card = {search:"gpu-node-01 10.0.0.1 production", status:"online", tags:["production", "cuda"], gpuUsers:["alice"]};
  assert.equal(logic.dashboardMatches({search:"gpu-node", status:"online", tags:["production", "cuda"], gpu_user:"alice"}, card), true);
  assert.equal(logic.dashboardMatches({search:"", status:"online", tags:["missing"], gpu_user:""}, card), false);
  assert.equal(logic.dashboardMatches({search:"", status:"", tags:[], gpu_user:"bob"}, card), false);
});

test("normalizes FP8, INT8, training and TP8 benchmark results", () => {
  const normalized = logic.normalizeBenchmark({
    mode:"multi", gpu_count:8,
    matrix:[
      {precision:"fp8_e4m3", unit:"TFLOPS", aggregate:1200, per_gpu:[{device:0, tops:150}]},
      {precision:"int8", unit:"TOPS", aggregate:2400, per_gpu:[{device:0, tops:300}]},
    ],
    training:{model:"resnet50", dataset:"cifar10", it_per_sec:20, images_per_sec:2560, avg_loss:1.2, avg_accuracy:0.6},
    parallelism:{tp_degree:8, tp8_ready:true},
  });
  assert.equal(normalized.matrix[0].precision, "fp8_e4m3");
  assert.equal(normalized.matrix[1].unit, "TOPS");
  assert.equal(normalized.training.model, "resnet50");
  assert.equal(normalized.training.dataset, "cifar10");
  assert.equal(normalized.tpDegree, 8);
  assert.equal(normalized.tp8Ready, true);
});
