// 好奇心 · 复古科幻观察站
"use strict";

var bridge = window.AstrBotPluginView || window.AstrBotPluginPage;

// ---------------------------------------------------------------- 桥接
function noBridge() {
  var e = new Error("面板桥未注入，请从 AstrBot 面板打开本页");
  e.__noBridge = true;
  return e;
}

function call(fn, a, b) {
  return new Promise(function (resolve, reject) {
    if (!bridge) return reject(noBridge());
    var p;
    try {
      p = fn.call(bridge, a, b);
    } catch (err) {
      return reject(err);
    }
    Promise.resolve(p).then(resolve, reject);
  });
}

function apiGet(path, params) {
  return call(bridge && bridge.apiGet, path, params || {});
}

function apiPost(path, body) {
  return call(bridge && bridge.apiPost, path, body || {});
}

// ---------------------------------------------------------------- 小工具
function $(id) {
  return document.getElementById(id);
}

function setText(el, txt) {
  if (el) el.textContent = txt;
}

function fail(err) {
  if (err && err.__noBridge) return "面板接口未接通，刷新页面或重新登录面板";
  var m = (err && err.message) || String(err);
  if (/HTTP 401|Unauthorized/i.test(m)) return "面板授权失效，重新登录一下面板";
  if (/HTTP 403/.test(m)) return "被面板拒了（403）";
  if (/HTTP 5\d\d/.test(m)) return "后端出错了：" + m;
  if (/超时|timeout/i.test(m)) return "等太久没回应，接口卡住了";
  return m;
}

function fmtTime(ts) {
  if (!ts) return "";
  var d = new Date(Number(ts) * 1000);
  if (isNaN(d.getTime())) return "";
  function p(n) {
    return (n < 10 ? "0" : "") + n;
  }
  return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) +
    " " + p(d.getHours()) + ":" + p(d.getMinutes());
}

function fmtAgo(ts) {
  var s = Math.floor(Date.now() / 1000 - Number(ts));
  if (isNaN(s) || s < 0) return "";
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return Math.floor(s / 86400) + " 天前";
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function safeUrl(u) {
  if (!u) return "";
  try {
    var p = new URL(u);
    return (p.protocol === "http:" || p.protocol === "https:") ? u : "";
  } catch (e) {
    return "";
  }
}

function siteOf(u) {
  try {
    return new URL(u).hostname.replace(/^www\./, "");
  } catch (e) {
    return "";
  }
}

// ---------------------------------------------------------------- 主题
function applyTheme(dark) {
  document.documentElement.setAttribute("data-theme", dark ? "deep" : "void");
  document.body.classList.toggle("deep", !!dark);
}

// ---------------------------------------------------------------- 数据
var state = {
  tab: "console",
  findOffset: 0,
  findLimit: 40,
  findKeyword: "",
  findItems: [],
};

// ---------------------------------------------------------------- 标签切换
function showTab(name) {
  state.tab = name;
  var tabs = document.querySelectorAll(".tab");
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].classList.toggle("active", tabs[i].getAttribute("data-tab") === name);
  }
  var panels = { console: "panel-console", finds: "panel-finds", logs: "panel-logs" };
  for (var k in panels) {
    var el = $(panels[k]);
    if (el) el.classList.toggle("active", k === name);
  }
  if (name === "finds") refreshFinds(true);
  if (name === "logs") refreshLogs();
}

// ---------------------------------------------------------------- 状态条
function statusHtml(s) {
  var items = [
    ["状态", s.enabled ? "在跑" : "停了"],
    ["节奏", s.interval_hours + " 小时/轮"],
    ["联网", s.search_engine === "qianfan" ? "千帆" : "未开"],
    ["取几条", String(s.qianfan_top_k)],
    ["时间范围", s.qianfan_recency],
    ["总结模型", s.provider_id || "—"],
    ["讲给", s.target_umo || "未设置"],
    ["让讲门限", s.deliver_threshold + " 条"],
    ["安静时段", s.quiet_hours && s.quiet_hours.length === 2 ? s.quiet_hours[0] + ":00 - " + s.quiet_hours[1] + ":00" : "—"],
  ];
  var out = "";
  for (var i = 0; i < items.length; i++) {
    out += '<span class="kv"><i>' + esc(items[i][0]) + '</i><b>' + esc(items[i][1]) + "</b></span>";
  }
  return out;
}

function loadStatus() {
  return apiGet("page/status").then(
    function (s) {
      $("statusKv").innerHTML = statusHtml(s);
      setText($("statusLine"), "观察站在线");
      $("beacon").className = "beacon on";
      // 顶部大表盘
      setText($("gReady"), String(s.ready_count || 0));
      setText($("gTotal"), String(s.total || 0));
      setText($("gDelivered"), String(s.delivered || 0));
      setText($("gRuns"), String(s.run_count || 0));
      setText($("gLastRun"), s.last_run ? fmtAgo(s.last_run) : "还没扫过");
      setText($("gLastTopic"), s.last_topic || "—");
      var pct = s.deliver_threshold > 0
        ? Math.min(100, Math.round(((s.ready_count || 0) / s.deliver_threshold) * 100))
        : 0;
      var bar = $("gBar");
      if (bar) {
        bar.style.width = pct + "%";
        bar.className = "charge-bar" + (pct >= 100 ? " full" : "");
      }
      setText($("gBarLabel"),
        (s.ready_count || 0) + " / " + s.deliver_threshold + " 条" +
        (pct >= 100 ? " · 够料了，下轮就开口" : " · 还差 " + Math.max(0, s.deliver_threshold - (s.ready_count || 0)) + " 条"));
      setText($("pillQueue"), "待讲 " + (s.ready_count || 0) + " 条");
      return s;
    },
    function (err) {
      setText($("statusLine"), "离线");
      $("beacon").className = "beacon";
      $("statusKv").innerHTML = '<span class="kv err">' + esc(fail(err)) + "</span>";
      throw err;
    }
  );
}

// ---------------------------------------------------------------- 任务简报
function briefHtml(s) {
  var out = "";
  var topics = s.topics || [];
  out += '<div class="brief-block"><div class="brief-label">扫描方向</div>';
  if (!topics.length) {
    out += '<div class="brief-empty">还没设方向</div>';
  } else {
    out += '<div class="chips">';
    for (var i = 0; i < topics.length; i++) {
      out += '<span class="chip">' + esc(topics[i]) + "</span>";
    }
    out += "</div>";
  }
  out += "</div>";

  out += '<div class="brief-block"><div class="brief-label">口吻设定</div>';
  out += '<blockquote class="persona">' + esc(s.persona_hint || "（没写，走默认）") + "</blockquote></div>";

  out += '<div class="brief-block"><div class="brief-label">参数</div><div class="kv-grid">';
  var rows = [
    ["每轮最多入账", (s.max_items_per_run || 0) + " 条"],
    ["搜索超时", (s.timeout_seconds || 0) + " 秒"],
    ["千帆 key", s.has_key ? "已配" : "没配（会退回问模型）"],
  ];
  for (var j = 0; j < rows.length; j++) {
    out += '<div class="kv-cell"><i>' + esc(rows[j][0]) + "</i><b>" + esc(rows[j][1]) + "</b></div>";
  }
  out += "</div></div>";
  return out;
}

function loadBrief() {
  return apiGet("page/status").then(function (s) {
    $("briefBody").innerHTML = briefHtml(s);
  });
}

// ---------------------------------------------------------------- 藏品库
function findsHtml(data) {
  var items = data.items || [];
  state.findItems = items;
  if (!items.length) {
    return '<div class="empty">' +
      (state.findKeyword ? "没翻到匹配「" + esc(state.findKeyword) + "」的东西" : "库里还空着，等等下一次扫描") +
      "</div>";
  }
  var out = "";
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    var score = Math.max(0, Math.min(10, Number(it.score || 0)));
    var url = safeUrl(it.url);
    out += '<article class="find' + (it.delivered ? " told" : "") + '">';
    out += '<div class="find-head"><h3 class="find-title">' + esc(it.title) + "</h3>";
    out += '<span class="find-score" title="有趣程度 ' + score + '/10">' + score + "</span></div>";
    out += '<div class="find-meta"><span>' + esc(fmtTime(it.created_at)) + "</span>";
    if (it.topic) out += "<span>" + esc(it.topic) + "</span>";
    if (url) out += "<span>" + esc(siteOf(url)) + "</span>";
    out += "<span>" + (it.delivered ? "已讲" : "待讲") + "</span></div>";
    if (it.gist) out += '<p class="find-gist">' + esc(it.gist) + "</p>";
    if (it.why) out += '<p class="find-why"><i>为什么值得讲</i>' + esc(it.why) + "</p>";
    if (url) {
      out += '<a class="find-link" href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(url) + "</a>";
    }
    out += "</article>";
  }
  return out;
}

function refreshFinds(reset) {
  if (reset) {
    state.findOffset = 0;
    state.findItems = [];
    $("findsList").innerHTML = '<div class="empty">读取中…</div>';
  }
  return apiGet("page/finds", {
    q: state.findKeyword,
    limit: state.findLimit,
    offset: state.findOffset,
  }).then(
    function (data) {
      $("findsList").innerHTML = findsHtml(data);
      setText($("findsCount"), (data.total || 0) + " 条存档");
      var hasMore = state.findOffset + (data.items || []).length < (data.total || 0);
      $("findsMore").hidden = !hasMore;
      return data;
    },
    function (err) {
      $("findsList").innerHTML = '<div class="empty err">' + esc(fail(err)) + "</div>";
      throw err;
    }
  );
}

function loadMoreFinds() {
  state.findOffset += state.findLimit;
  return refreshFinds(false);
}

// ---------------------------------------------------------------- 航行日志
function logsHtml(data) {
  var items = data.items || [];
  if (!items.length) return '<div class="empty">还没有扫描记录</div>';
  var out = '<table class="log-table"><thead><tr>' +
    "<th>时间</th><th>方向</th><th>结果</th><th>取回</th><th>新增</th><th>耗时</th>" +
    "</tr></thead><tbody>";
  for (var i = 0; i < items.length; i++) {
    var r = items[i];
    var ok = Number(r.ok) === 1;
    out += "<tr>";
    out += '<td class="mono">' + esc(fmtTime(r.at)) + "</td>";
    out += "<td>" + esc(r.topic || "—") + "</td>";
    out += '<td><span class="tag ' + (ok ? "tag-ok" : "tag-bad") + '">' + (ok ? "成功" : "失败") + "</span></td>";
    out += '<td class="mono">' + esc(r.got || 0) + "</td>";
    out += '<td class="mono">' + esc(r.fresh || 0) + "</td>";
    out += '<td class="mono">' + esc((r.elapsed || 0).toFixed ? (r.elapsed || 0).toFixed(2) + "s" : (r.elapsed || 0) + "s") + "</td>";
    out += "</tr>";
    if (!ok && r.note) {
      out += '<tr><td colspan="6" class="log-note">' + esc(r.note) + "</td></tr>";
    }
  }
  out += "</tbody></table>";
  return out;
}

function refreshLogs() {
  $("logsBody").innerHTML = '<div class="empty">读取中…</div>';
  return apiGet("page/runs", { limit: 60 }).then(
    function (data) {
      $("logsBody").innerHTML = logsHtml(data);
      return data;
    },
    function (err) {
      $("logsBody").innerHTML = '<div class="empty err">' + esc(fail(err)) + "</div>";
      throw err;
    }
  );
}

// ---------------------------------------------------------------- 手动操作
function withBusy(btn, label, task) {
  if (!btn) return Promise.resolve();
  var old = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  return Promise.resolve()
    .then(task)
    .then(function (v) {
      btn.disabled = false;
      btn.textContent = old;
      return v;
    }, function (e) {
      btn.disabled = false;
      btn.textContent = old;
      throw e;
    });
}

function doScan() {
  var topic = ($("topicInput").value || "").trim();
  var btn = $("btnScan");
  setText($("scanResult"), "扫描中，通常 5~15 秒…");
  return withBusy(btn, "扫描中…", function () {
    return apiPost("page/run", { topic: topic });
  }).then(
    function (r) {
      var msg = "取回 " + (r.got || 0) + " 条，新增 " + (r.fresh || 0) + " 条";
      if (r.elapsed) msg += "，用时 " + Number(r.elapsed).toFixed(2) + "s";
      setText($("scanResult"), msg + (r.note ? "（" + r.note + "）" : ""));
      return Promise.all([loadStatus(), refreshFinds(true)]);
    },
    function (err) {
      setText($("scanResult"), "扫描失败：" + fail(err));
      throw err;
    }
  );
}

function doTell() {
  var btn = $("btnTell");
  setText($("tellResult"), "正在组织语言…");
  return withBusy(btn, "酝酿中…", function () {
    return apiPost("page/tell", {});
  }).then(
    function (r) {
      var body = (r.text || "").trim();
      $("tellResult").innerHTML = body
        ? '<div class="tell-box">' + esc(body) + "</div>" +
          '<div class="tell-note">' + (r.delivered ? "已发到 " + esc(r.umo || "") : "只生成了文本，没发出去") + "</div>"
        : '<div class="empty">模型没给出内容</div>';
      return loadStatus();
    },
    function (err) {
      setText($("tellResult"), "失败：" + fail(err));
      throw err;
    }
  );
}

// ---------------------------------------------------------------- 启动
function bind() {
  var tabs = document.querySelectorAll(".tab");
  for (var i = 0; i < tabs.length; i++) {
    tabs[i].addEventListener("click", function () {
      showTab(this.getAttribute("data-tab"));
    });
  }

  $("btnScan").addEventListener("click", function () {
    doScan().catch(function () {});
  });
  $("btnTell").addEventListener("click", function () {
    doTell().catch(function () {});
  });
  $("btnRefresh").addEventListener("click", function () {
    loadStatus().catch(function () {});
    if (state.tab === "finds") refreshFinds(true).catch(function () {});
    if (state.tab === "logs") refreshLogs().catch(function () {});
  });
  $("btnReloadFinds").addEventListener("click", function () {
    refreshFinds(true).catch(function () {});
  });
  $("btnReloadLogs").addEventListener("click", function () {
    refreshLogs().catch(function () {});
  });

  var kw = $("findQ");
  var timer = null;
  kw.addEventListener("input", function () {
    if (timer) clearTimeout(timer);
    var v = kw.value;
    timer = setTimeout(function () {
      state.findKeyword = v.trim();
      refreshFinds(true).catch(function () {});
    }, 300);
  });

  $("findsMore").addEventListener("click", function () {
    loadMoreFinds().catch(function () {});
  });
}

function start() {
  if (!bridge) {
    setText($("statusLine"), "未接通");
    $("statusKv").innerHTML = '<span class="kv err">' + esc(noBridge().message) + "</span>";
    bind();
    return;
  }
  var readyP = typeof bridge.ready === "function" ? bridge.ready() : Promise.resolve(null);
  readyP
    .then(function (ctx) {
      applyTheme(!!(ctx && ctx.isDark));
      if (typeof bridge.onContext === "function") {
        bridge.onContext(function (next) {
          applyTheme(!!(next && next.isDark));
        });
      }
    }, function () {
      applyTheme(false);
    })
    .then(function () {
      bind();
      return Promise.all([loadStatus(), loadBrief()]);
    })
    .catch(function () {});
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start);
} else {
  start();
}
