"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {spawn} = require("node:child_process");

const [baseUrl, initialPassword, changedPassword, screenshotPath = ""] = process.argv.slice(2);
if (!baseUrl || !initialPassword || !changedPassword) {
  throw new Error("usage: node tests/acceptance/browser.js BASE_URL INITIAL_PASSWORD CHANGED_PASSWORD [SCREENSHOT]");
}
if (typeof WebSocket === "undefined") throw new Error("Node.js 22+ is required for the built-in WebSocket client");

const chromeCommand = process.env.CHROME_BIN || "google-chrome";
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "server-monitor-browser-"));
const chrome = spawn(chromeCommand, [
  "--headless=new",
  "--no-sandbox",
  "--disable-dev-shm-usage",
  "--disable-background-networking",
  "--disable-default-apps",
  "--disable-extensions",
  "--no-first-run",
  "--remote-debugging-port=0",
  `--user-data-dir=${profile}`,
  baseUrl,
], {stdio:["ignore", "ignore", "pipe"]});

let chromeStderr = "";
chrome.stderr.on("data", (chunk) => { chromeStderr += chunk.toString("utf8"); });

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function debuggerPort(timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const match = chromeStderr.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)\//);
    if (match) return Number(match[1]);
    if (chrome.exitCode != null) throw new Error(`Chrome exited early (${chrome.exitCode}): ${chromeStderr}`);
    await delay(50);
  }
  throw new Error(`Chrome DevTools endpoint did not start: ${chromeStderr}`);
}

async function pageTarget(port, timeoutMs = 10000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json/list`).then((response) => response.json());
      const target = targets.find((item) => item.type === "page" && item.url.startsWith(baseUrl));
      if (target?.webSocketDebuggerUrl) return target.webSocketDebuggerUrl;
    } catch (_) {
      // Chrome may expose the HTTP endpoint a little after printing its port.
    }
    await delay(50);
  }
  throw new Error("Browser page target was not created");
}

class Protocol {
  constructor(url) {
    this.socket = new WebSocket(url);
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
  }

  async open() {
    await new Promise((resolve, reject) => {
      this.socket.addEventListener("open", resolve, {once:true});
      this.socket.addEventListener("error", () => reject(new Error("DevTools WebSocket connection failed")), {once:true});
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
      } else if (message.method) {
        this.events.push(message);
      }
    });
  }

  send(method, params = {}) {
    const id = this.nextId++;
    this.socket.send(JSON.stringify({id, method, params}));
    return new Promise((resolve, reject) => this.pending.set(id, {resolve, reject}));
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(protocol, expression) {
  const response = await protocol.send("Runtime.evaluate", {expression, awaitPromise:true, returnByValue:true});
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.text || "browser evaluation failed");
  return response.result?.value;
}

async function waitFor(protocol, expression, label, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await evaluate(protocol, `Boolean(${expression})`)) return;
    await delay(80);
  }
  throw new Error(`Timed out waiting for ${label}`);
}

function submitExpression(selector, values) {
  return `(() => { const form = document.querySelector(${JSON.stringify(selector)}); if (!form) return false; ${Object.entries(values).map(([name, value]) => `form.elements[${JSON.stringify(name)}].value=${JSON.stringify(value)};`).join(" ")} form.requestSubmit(); return true; })()`;
}

async function run() {
  const port = await debuggerPort();
  const protocol = new Protocol(await pageTarget(port));
  await protocol.open();
  await protocol.send("Page.enable");
  await protocol.send("Runtime.enable");
  await waitFor(protocol, `document.readyState === "complete" && document.querySelector("#login-form")`, "login page");

  await evaluate(protocol, submitExpression("#login-form", {username:"admin", password:initialPassword}));
  await waitFor(protocol, `document.querySelector("#password-dialog")?.open`, "forced password dialog");
  await evaluate(protocol, submitExpression("#password-form", {current_password:initialPassword, new_password:changedPassword}));
  await waitFor(protocol, `!document.querySelector("#login-view")?.hidden && !document.querySelector("#password-dialog")?.open`, "login after password rotation");

  await evaluate(protocol, submitExpression("#login-form", {username:"admin", password:changedPassword}));
  await waitFor(protocol, `!document.querySelector("#app-view")?.hidden && document.querySelector("#page-title")?.textContent === "集群概览"`, "dashboard");

  await protocol.send("Emulation.setDeviceMetricsOverride", {width:1440, height:1000, deviceScaleFactor:1, mobile:false});
  await evaluate(protocol, `document.querySelector('button[data-page="compute"]').click()`);
  await waitFor(protocol, `document.querySelector("#page-title")?.textContent === "空闲算力" && document.querySelector("#idle-min-memory") && document.querySelector("#idle-apply")`, "idle compute page");
  const computePage = await evaluate(protocol, `({filterCount:document.querySelectorAll(".compute-filter-band input, .compute-filter-band select").length, hasEmptyState:Boolean(document.querySelector(".empty")), viewportOverflow:document.documentElement.scrollWidth > window.innerWidth + 1})`);
  if (computePage.filterCount < 4 || computePage.viewportOverflow) throw new Error(`Idle compute page assertion failed: ${JSON.stringify(computePage)}`);
  await evaluate(protocol, `window.confirm=() => true; document.querySelector('button[data-page="environments"]').click()`);
  await waitFor(protocol, `document.querySelector("#page-title")?.textContent === "开发环境" && document.querySelector("[data-gpu-benchmark-form]")`, "GPU benchmark form");
  await evaluate(protocol, `(() => {
    const form = document.querySelector("[data-gpu-benchmark-form]");
    form.mode.value = "multi";
    form.model.value = "resnet50";
    form.elements.dataset.value = "cifar10";
    form.elements.dataset.dispatchEvent(new Event("change", {bubbles:true}));
    form.duration_seconds.value = "3";
    form.python.value = "python3";
    form.download_dataset.checked = true;
    form.requestSubmit();
  })()`);
  await waitFor(protocol, `document.querySelector("#elevate-dialog")?.open`, "GPU benchmark elevation dialog");
  await evaluate(protocol, submitExpression("#elevate-form", {password:changedPassword}));
  await waitFor(protocol, `document.querySelector("[data-gpu-benchmark-history]")?.textContent.includes("resnet50") && document.querySelector("[data-gpu-benchmark-history]")?.textContent.includes("fp8_e4m3") && document.querySelector("[data-gpu-benchmark-history]")?.textContent.includes("int8") && document.querySelector("[data-gpu-benchmark-history]")?.textContent.includes("TP=8")`, "persisted GPU benchmark result", 20000);
  const gpuPage = await evaluate(protocol, `(() => {
    const text = document.querySelector("[data-gpu-benchmark-history]").textContent;
    const form = document.querySelector("[data-gpu-benchmark-form]");
    return {
      hasCifar10:text.includes("cifar10"),
      hasLoss:text.includes("1.8421"),
      hasAccuracy:text.includes("43.8%"),
      hasNccl:text.includes("367.5 GB/s"),
      downloadControlVisible:!form.querySelector("[data-gpu-download]").hidden,
      viewportOverflow:document.documentElement.scrollWidth > window.innerWidth + 1,
    };
  })()`);
  if (!gpuPage.hasCifar10 || !gpuPage.hasLoss || !gpuPage.hasAccuracy || !gpuPage.hasNccl || !gpuPage.downloadControlVisible || gpuPage.viewportOverflow) {
    throw new Error(`GPU benchmark page assertion failed: ${JSON.stringify(gpuPage)}`);
  }

  if (screenshotPath) {
    const screenshot = await protocol.send("Page.captureScreenshot", {format:"png", captureBeyondViewport:false});
    fs.writeFileSync(screenshotPath, Buffer.from(screenshot.data, "base64"));
  }

  await evaluate(protocol, `document.querySelector('button[data-page="settings"]').click()`);
  await waitFor(protocol, `document.querySelector("#page-title")?.textContent === "系统设置" && document.querySelector("#settings-form")`, "settings page");
  const vaultPage = await evaluate(protocol, `(() => ({
    generateButton: Boolean(document.querySelector("#generate-vault-key")),
    filePicker: Boolean(document.querySelector("[data-vault-file]")),
    privateKeyForm: Boolean(document.querySelector("#vault-key-form")),
  }))()`);
  if (!vaultPage.generateButton || !vaultPage.filePicker || !vaultPage.privateKeyForm) {
    throw new Error(`SSH vault controls missing: ${JSON.stringify(vaultPage)}`);
  }
  const notificationPage = await evaluate(protocol, `(() => {
    const section = document.querySelector("#setting-notifications");
    return {
      separateWebAndApprise: Boolean(section?.textContent.includes("网页告警") && section?.textContent.includes("发送这些告警")),
      hostSelectorCount: section?.querySelectorAll("[data-host-notification-enabled]").length || 0,
      hostEventButtonCount: section?.querySelectorAll("[data-host-notification-events]").length || 0,
    };
  })()`);
  if (!notificationPage.separateWebAndApprise || notificationPage.hostSelectorCount !== notificationPage.hostEventButtonCount) {
    throw new Error(`Notification controls missing: ${JSON.stringify(notificationPage)}`);
  }
  await evaluate(protocol, `(() => { const input = document.querySelector("[data-vault-file]"); const transfer = new DataTransfer(); transfer.items.add(new File(["loaded-from-file"], "existing-key.pem", {type:"text/plain"})); input.files = transfer.files; input.dispatchEvent(new Event("change", {bubbles:true})); return true; })()`);
  await waitFor(protocol, `document.querySelector("#vault-key-form textarea[name=private_key]")?.value === "loaded-from-file"`, "private-key file loaded");
  await evaluate(protocol, `document.querySelector("#generate-vault-key").click()`);
  await waitFor(protocol, `document.querySelector("dialog[open] input[name=name]")`, "SSH key generation dialog");
  await evaluate(protocol, `(() => { const dialog = document.querySelector("dialog[open]"); dialog.querySelector("input[name=name]").value = "browser-generated-key"; dialog.querySelector("select[name=key_type]").value = "ed25519"; dialog.querySelector("input[name=passphrase]").value = "browser-key-pass"; dialog.querySelector("form").requestSubmit(); return true; })()`);
  await waitFor(protocol, `document.querySelector("#elevate-dialog")?.open || document.querySelector("#setting-credentials")?.textContent.includes("browser-generated-key")`, "SSH key generation authorization");
  if (await evaluate(protocol, `Boolean(document.querySelector("#elevate-dialog")?.open)`)) {
    await evaluate(protocol, submitExpression("#elevate-form", {password:changedPassword}));
  }
  await waitFor(protocol, `document.querySelector("#setting-credentials")?.textContent.includes("browser-generated-key")`, "generated SSH key listed");
  await evaluate(protocol, `document.querySelector('[data-delete-vault-key-name="browser-generated-key"]').click()`);
  await waitFor(protocol, `!document.querySelector("#setting-credentials")?.textContent.includes("browser-generated-key")`, "generated SSH key deleted");
  await evaluate(protocol, `document.querySelector('button[data-page="terminal"]').click()`);
  await waitFor(protocol, `document.querySelector("#page-title")?.textContent === "终端工作台" && document.querySelector("#workbench-open")`, "terminal workbench");
  const workbench = await evaluate(protocol, `({hasHostSelector:Boolean(document.querySelector("#workbench-host-select")), x11Notice:document.querySelector("#page-content")?.textContent.includes("X11")})`);
  if (!workbench.hasHostSelector || !workbench.x11Notice) throw new Error(`Terminal workbench assertion failed: ${JSON.stringify(workbench)}`);
  await evaluate(protocol, `document.querySelector("#logout-button").click()`);
  await waitFor(protocol, `!document.querySelector("#login-view")?.hidden && document.querySelector("#app-view")?.hidden`, "logout");

  const exceptions = protocol.events.filter((event) => event.method === "Runtime.exceptionThrown");
  protocol.close();
  if (exceptions.length) throw new Error(`Browser reported ${exceptions.length} uncaught exception(s)`);
  return {login:"passed", password_rotation:"passed", dashboard:"passed", idle_compute:"passed", gpu_benchmark:"passed", settings:"passed", ssh_vault:"passed", terminal_workbench:"passed", logout:"passed"};
}

run().then((result) => {
  process.stdout.write(`${JSON.stringify(result)}\n`);
}).catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n${chromeStderr}\n`);
  process.exitCode = 1;
}).finally(async () => {
  if (chrome.exitCode == null) {
    chrome.kill("SIGTERM");
    await Promise.race([
      new Promise((resolve) => chrome.once("exit", resolve)),
      delay(3000),
    ]);
  }
  fs.rmSync(profile, {recursive:true, force:true, maxRetries:5, retryDelay:100});
});
