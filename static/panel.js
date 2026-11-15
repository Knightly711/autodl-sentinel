"use strict";
const el = id => document.getElementById(id);
const labels = {idle:"等待开始", checking:"正在检查", waiting:"等待重试", paused:"已暂停",
  submitting:"正在创建", review:"待核对", stopped:"已停止", success:"已抢到"};
let lastFeedback;
let state, account, lastTask, optionsRevision = 0, pending = false;

async function api(path, body) {
  const opts = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json",
    "X-Grab-Panel":"1"}, body:JSON.stringify(body)};
  const res = await fetch("/api/" + path, {...opts, cache:"no-store"});
  const data = await res.json();
  if (!res.ok) throw Error(typeof data.detail === "string" ? data.detail : t("输入有误，请检查数量、间隔和价格。"));
  return data;
}
function feedback(text, error=false) {
  lastFeedback = {text, error};
  el("feedback").textContent = t(text);
  el("feedback").hidden = false;
  el("feedback").classList.toggle("error", error);
}
function action(id, callback) {
  el(id).addEventListener("click", async event => {
    event.preventDefault();
    if (pending) return;
    pending = true;
    el(id).disabled = true;
    try { await callback(); }
    catch (error) { feedback(t(error.message), true); }
    finally { pending = false; el(id).disabled = false; await poll(); }
  });
}
function select(id, items, hint, value="") {
  el(id).replaceChildren(new Option(hint,""), ...items.map(i => new Option(t(i.label),i.id)));
  el(id).value = value;
}
function selectedModels() {
  return [...el("models").querySelectorAll("input:checked")].map(input => input.value);
}
function showModels(names, selected=[]) {
  el("models").replaceChildren(...names.map(name => {
    const label = document.createElement("label");
    const check = document.createElement("input");
    check.type = "checkbox";
    check.value = name;
    check.checked = selected.includes(name);
    label.append(check, document.createTextNode(name));
    return label;
  }));
}
function syncMode() {
  const clone = el("mode").value === "clone";
  el("sourceWrap").hidden = !clone;
  el("copyWrap").hidden = !clone;
  el("cpuWrap").hidden = !clone;
  el("coresWrap").hidden = !clone;
  el("source").disabled = !clone;
  el("source").required = clone;
  el("cpuName").disabled = !clone;
  el("cpuCores").disabled = !clone;
  el("cloneRegionNote").hidden = !clone;
  el("image").disabled = clone;
  el("image").required = !clone;
  el("image").closest("label").hidden = clone;
}
function syncModeButtons() {
  const mode = el("mode").value;
  document.querySelectorAll("[data-mode]").forEach(button => {
    const active = button.dataset.mode === mode;
    button.setAttribute("aria-pressed", active ? "true" : "false");
  });
}
function showSources(items, value="") {
  select("source", items, t("请选择源实例"), value);
}
function showCpuOptions(models=[], cores=[]) {
  el("cpuNames").replaceChildren(...models.map(value => new Option(value, value)));
  el("cpuCoreOptions").replaceChildren(...cores.map(value => new Option(String(value), String(value))));
}
function csvValues(value) {
  return String(value || "").split(",").map(item => item.trim()).filter(Boolean);
}
async function images() {
  const revision = ++optionsRevision;
  const gpus = selectedModels();
  const clone = el("mode").value === "clone";
  const previous = el("image").value;
  if (!gpus.length) {
    showCpuOptions();
    if (!clone) select("image", [], t("请先选择显卡"));
    return;
  }
  if (!clone) select("image", [], t("正在读取镜像"));
  const query = new URLSearchParams(gpus.map(name => ["gpu",name]));
  const result = await api("options?" + query);
  if (revision !== optionsRevision) return;
  if (result.account !== account) throw Error(t("账号发生变化，请重新读取选项"));
  showCpuOptions(result.cpu_models || [], result.cpu_cores || []);
  if (clone) return;
  select("image", result.images, t("请选择镜像"), result.images.some(i => i.id === previous) ? previous : "");
}
el("gpu").onchange = () => images().catch(e => feedback(t(e.message),true));
el("mode").onchange = () => { syncMode(); syncModeButtons(); images().catch(e => feedback(t(e.message), true)); };
document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
  el("mode").value = button.dataset.mode;
  el("mode").dispatchEvent(new Event("change"));
}));
action("login", async () => { await api("login", {}); feedback(t("在专用窗口登录后，点击「读取选项」。")); });
action("load", async () => {
  const [result, sourceResult] = await Promise.all([api("options"), api("instances")]);
  account = result.account;
  ++optionsRevision;
  showModels(result.gpus);
  showSources(sourceResult.instances);
  showCpuOptions(result.cpu_models || [], result.cpu_cores || []);
  syncMode();
  syncModeButtons();
  select("image", [], t("请先选择显卡"));
  feedback(t("已读取当前账号的显卡型号。"));
});
function config() {
  const clone = el("mode").value === "clone";
  const gpu = selectedModels();
  if (!gpu.length) throw Error(t("请选择至少一种候选显卡"));
  if (!el("form").reportValidity()) throw Error(t("请填写完整配置"));
  return {account,gpu,image:clone ? null : el("image").value,
    clone_instance:clone ? el("source").value : null, copy_data_disk:clone && el("copyData").checked,
    cpu_names:clone ? csvValues(el("cpuName").value) : [],
    cpu_cores:clone ? csvValues(el("cpuCores").value).map(Number).filter(Number.isInteger) : [],
    count:el("count").valueAsNumber,
    interval:el("interval").valueAsNumber,limit:el("limit").value === "" ? null : el("limit").valueAsNumber,
    disk:el("disk").value === "" ? null : el("disk").valueAsNumber};
}
action("preview", async () => {
  const {match} = await api("preview", config());
  feedback(match ? t("匹配到 {gpu}，主机 {machine}，CPU {cpu_name} / {cpu_per_gpu} 核，整机报价 ¥{price}/小时。", match) : t("当前没有符合条件的主机。"));
});
action("start", async () => { await api("start", config()); feedback(t("任务已启动。")); });
el("form").onsubmit = event => {event.preventDefault(); if (!el("start").disabled) el("start").click();};
action("save", async () => {
  if (!el("interval").reportValidity()) return;
  await api("interval", {interval:el("interval").valueAsNumber});
  feedback(t("重试间隔已保存。"));
});
action("stop", async () => { await api("stop", {}); feedback(t("任务已停止。")); });
action("resume", async () => { await api("resume", {}); });
action("resolve", async () => {
  const name = prompt(t("请在「我的实例」核实没有创建本次实例，再输入完整订单标记结束任务。"));
  if (name) await api("resolve", {name});
});
function render(data) {
  const live = !["idle","success","stopped"].includes(data.phase);
  if (live && lastTask !== data.name) {
    const cfg = data.settings;
    const selected = Array.isArray(cfg.gpu) ? cfg.gpu : [cfg.gpu];
    const available = [...el("models").querySelectorAll("input")].map(input => input.value);
    showModels([...new Set([...available, ...selected])], selected);
    if (el("image").value !== cfg.image) select("image", [{id:cfg.image,label:cfg.image}], "", cfg.image);
    account = cfg.account;
    el("interval").value = cfg.interval;
    el("count").value = cfg.count;
    el("limit").value = cfg.limit ?? "";
    el("disk").value = cfg.disk ?? "";
    el("mode").value = cfg.clone_instance ? "clone" : "create";
    if (cfg.clone_instance && ![...el("source").options].some(option => option.value === cfg.clone_instance))
      showSources([{id:cfg.clone_instance,label:cfg.clone_instance}], cfg.clone_instance);
    else if (cfg.clone_instance) el("source").value = cfg.clone_instance;
    el("copyData").checked = !!cfg.copy_data_disk;
    el("cpuName").value = (cfg.cpu_names || []).join(", ");
    el("cpuCores").value = (cfg.cpu_cores || []).join(", ");
    syncMode();
    syncModeButtons();
  }
  lastTask = data.name;
  el("taskPanel").dataset.phase = data.phase;
  for (const id of ["gpu","mode","source","copyData","cpuName","cpuCores","image","count","limit","disk","load","preview","start"]) el(id).disabled = live || pending;
  if (data.browser_mode === "readonly" || data.browser_mode === "offline") {
    el("start").disabled = true;
    el("connection").textContent = data.browser_mode === "readonly" ? t("浏览器扩展已连接 · 只读") : t("浏览器扩展已断开");
  } else {
    el("connection").textContent = t("本地控制台");
  }
  el("phase").textContent = t(labels[data.phase]);
  const captions = {idle:"准备就绪，等待开始",checking:"正在扫描可用主机",waiting:"等待下一轮检查",paused:"任务已暂停",submitting:"正在提交订单",review:"需要核对订单",stopped:"任务已停止",success:"实例已创建"};
  el("monitorCaption").textContent = t(captions[data.phase] || captions.idle);
  el("monitorMode").textContent = ["checking","waiting","submitting"].includes(data.phase) ? "ACTIVE" : data.phase === "review" ? "REVIEW" : "STANDBY";
  el("attempts").textContent = t("{count} 次", {count:data.attempts});
  el("intervalDisplay").innerHTML = `${Number(data.settings?.interval ?? 0)} <small>s</small>`;
  el("message").textContent = t(data.message);
  const showName = ["submitting","review","success"].includes(data.phase);
  el("orderLabel").hidden = !showName;
  el("order").textContent = showName ? data.name : "";
  el("idLabel").hidden = !data.instance;
  el("instance").textContent = data.instance || "";
  el("save").hidden = !["checking","waiting","paused"].includes(data.phase);
  el("stop").hidden = el("save").hidden;
  el("resume").hidden = !["paused","review"].includes(data.phase);
  el("resume").textContent = data.phase === "review" ? t("再次核对") : t("继续任务");
  el("resolve").hidden = data.phase !== "review";
  el("due").textContent = data.due ? t("{time} 检查", {time:new Date(data.due*1000).toLocaleTimeString(i18n.language)}) : t("尚未安排");
  const selected = data.settings && (Array.isArray(data.settings.gpu) ? data.settings.gpu : [data.settings.gpu]);
  el("summaryGpu").textContent = selected?.filter(Boolean).join(" / ") || t("尚未选择");
  el("summaryMode").textContent = data.settings?.clone_instance ? t("克隆已有实例") : t("从镜像创建");
  const cpus = [...(data.settings?.cpu_names || []), ...(data.settings?.cpu_cores || []).map(value => `${value} 核`)];
  el("summaryCpu").textContent = cpus.length ? cpus.join(" · ") : t("不限");
  el("summaryDisk").textContent = data.settings?.disk ? `${data.settings.disk} GB` : t("不限");
  state = data;
  tick();
}
function tick() {
  el("timer").textContent = state?.due ? Math.max(0, state.due - Date.now()/1000).toFixed(1) + " s" : "—";
}
async function poll() {
  try { render(await api("task")); }
  catch (error) { feedback(t("无法连接本地服务，请检查启动窗口。"), true); }
}
async function refresh() { await poll(); setTimeout(refresh, 1000); }
setInterval(tick, 250);
i18n.ready.then(refresh);

window.addEventListener("languagechange", () => {
  if (state) render(state);
  if (lastFeedback) feedback(lastFeedback.text, lastFeedback.error);
  for (const option of el("image").options) option.textContent = t(option.textContent);
});
