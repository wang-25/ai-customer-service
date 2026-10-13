/* ==========================================================================
 * 数据看板（dashboard.js）
 * 纯原生 JS，无框架、无外部 CDN。
 * 数据来源：
 *   GET /api/admin/overview?days=1|7|30   核心指标 / 趋势 / 分布 / 兜底 TOP / 口径说明
 *   GET /api/health                       运行状态
 *   GET /api/admin/sessions?handoff_only=true&limit=10   最近转人工会话
 *   GET /api/admin/agents                 客服负载
 * 注意：containment_rate / handoff_rate / csat.good_rate / csat.collect_rate
 *       后端已按百分数返回（66.7 表示 66.7%），前端不得再乘 100。
 * ========================================================================== */
(() => {
  'use strict';

  /* ==================================================================== 配置 */
  const REFRESH_MS = 15000;                 // 自动刷新间隔（默认 15 秒）
  const DEFAULT_DAYS = 7;                   // 默认近 7 天
  const RANGE_LABEL = { 1: '近 1 天', 7: '近 7 天', 30: '近 30 天' };
  const BAR_MAX_PX = 78;                    // 单段趋势柱最大像素高度（b1/b2 堆叠，需能放进 .trend 高度内）

  // 转人工触发原因中文映射（未知类型原样展示）
  const HANDOFF_REASON_LABEL = {
    user_request: '用户主动要求',
    fallback_count: '连续兜底未解决',
    sentiment: '负面情绪触发',
    risky_keyword: '高风险关键词触发',
    turn_limit: '会话轮次超限',
    clarify_limit: '澄清轮次超限',
    agent_timeout: '客服长时间未回复',
  };

  // 会话归因（resolved_by）中文映射
  const RESOLVED_BY_LABEL = {
    bot: '机器人解决',
    agent: '人工解决',
    unresolved: '未解决',
  };

  // 口径说明标题映射（metric_notes 的 key -> 展示名）
  const NOTE_LABEL = {
    containment_rate: '机器人独立解决率',
    handoff_rate: '转人工率',
    csat_good_rate: '好评率',
    avg_bot_first_reply_seconds: '机器人首次响应',
    avg_queue_seconds: '平均排队时长',
  };

  /* ==================================================================== 状态 */
  const state = {
    days: DEFAULT_DAYS,
    autoRefresh: true,
    timer: null,
    loading: false,
    lastUpdated: null,
  };

  let kpiCoreRefs = null;   // 核心 KPI DOM 引用
  let kpiPerfRefs = null;   // 性能 KPI DOM 引用

  /* ==================================================================== 基础工具 */
  const $ = (id) => document.getElementById(id);

  /** HTML 转义：所有插值内容必须经过它 */
  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** 轻提示 */
  function toast(text, isErr) {
    const el = $('toast');
    if (!el) return;
    el.textContent = String(text);
    el.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.className = 'toast'; }, 2600);
  }

  /** 读取本地 token（AUTH_ENABLED=1 时才有） */
  function getToken() {
    try { return localStorage.getItem('cs_token') || ''; } catch (e) { return ''; }
  }

  /**
   * 统一请求封装：
   * - 自动带上 Authorization: Bearer <token>
   * - 401 跳转登录页
   * - 非 2xx 抛出后端 detail
   */
  async function api(path, options = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
    const token = getToken();
    if (token) headers['Authorization'] = 'Bearer ' + token;

    const res = await fetch(path, Object.assign({}, options, { headers }));

    if (res.status === 401) {
      location.href = '/login';
      throw new Error('未登录，正在跳转登录页');
    }
    if (!res.ok) {
      let detail = '请求失败 (' + res.status + ')';
      try {
        const data = await res.json();
        if (data && data.detail !== undefined && data.detail !== null) detail = data.detail;
      } catch (e) { /* 响应体不是 JSON，保留默认文案 */ }
      if (Array.isArray(detail)) {
        detail = detail.map((d) => (d && d.msg) || JSON.stringify(d)).join('；');
      } else if (typeof detail === 'object') {
        detail = JSON.stringify(detail);
      }
      throw new Error(String(detail));
    }
    return res.json();
  }

  /* ==================================================================== 格式化 */
  /** 数字（digits=0 取整） */
  function num(v, digits = 0) {
    const n = Number(v);
    if (!isFinite(n)) return '0';
    return digits > 0 ? n.toFixed(digits) : String(Math.round(n));
  }

  /** 百分数数值 -> "66.7%"（后端已是百分数，不再乘 100） */
  function fmtPct(v) {
    const n = Number(v);
    return (isFinite(n) ? n : 0).toFixed(1) + '%';
  }

  /** 秒数 -> "x.x 秒"；超过 60 秒 -> "x.x 分钟" */
  function fmtSeconds(v) {
    const n = Number(v);
    if (!isFinite(n) || n <= 0) return '0.0 秒';
    if (n > 60) return (n / 60).toFixed(1) + ' 分钟';
    return n.toFixed(1) + ' 秒';
  }

  /** "2026-10-02 11:57:47" -> "10-02 11:57"（withSeconds 时保留秒） */
  function fmtTime(ts, withSeconds) {
    if (!ts) return '—';
    const s = String(ts);
    const m = s.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?/);
    if (!m) return s;
    const base = m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
    return withSeconds && m[6] ? base + ':' + m[6] : base;
  }

  /** "2026-10-02" -> "10-02" */
  function fmtDay(day) {
    const m = String(day || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
    return m ? m[2] + '-' + m[3] : String(day || '');
  }

  /** "2026-10-02 11:57:47" -> "2026-10-02 11:57"（用于区间展示） */
  function fmtDateTime(ts) {
    if (!ts) return '—';
    const s = String(ts);
    return s.length >= 16 ? s.slice(0, 16) : s;
  }

  /** 本地当前时间 HH:MM:SS */
  function nowClock() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, '0');
    return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
  }

  /** 取口径说明：优先用后端 metric_notes，缺失时用本地兜底文案 */
  function noteOf(data, key, fallback) {
    const notes = (data && data.metric_notes) || {};
    const v = notes[key];
    return (typeof v === 'string' && v.trim()) ? v : fallback;
  }

  /** 机器人独立解决率配色：≥45 绿 / ≥25 橙 / 否则红 */
  function containmentClass(rate) {
    const n = Number(rate) || 0;
    if (n >= 45) return 'good';
    if (n >= 25) return 'warn';
    return 'bad';
  }

  function reasonLabel(t) {
    if (!t) return '未知';
    return HANDOFF_REASON_LABEL[t] || String(t);
  }

  function resolvedByLabel(v) {
    if (!v) return '未归因';
    return RESOLVED_BY_LABEL[v] || String(v);
  }

  /* ==================================================================== KPI 定义
   * value(data) -> 展示文本；unit -> <small> 单位；note(data) -> 口径说明；
   * color(data) -> .good/.warn/.bad
   * ==================================================================== */
  const KPI_CORE_SPEC = [
    {
      key: 'sessions',
      label: '会话总数',
      unit: '个',
      value: (d) => num(d.totals.sessions),
      note: () => '统计区间内创建的全部会话（含进行中、已结束）',
    },
    {
      key: 'containment',
      label: '机器人独立解决率',
      value: (d) => fmtPct(d.resolve.containment_rate),
      color: (d) => containmentClass(d.resolve.containment_rate),
      note: (d) => noteOf(d, 'containment_rate', "机器人独立解决率 = resolved_by='bot' 的会话 ÷ 已完成会话数"),
    },
    {
      key: 'handoff_rate',
      label: '转人工率',
      value: (d) => fmtPct(d.resolve.handoff_rate),
      note: (d) => noteOf(d, 'handoff_rate', '转人工率 = 存在转人工记录的会话 ÷ 期间会话总数'),
    },
    {
      key: 'csat_good',
      label: '好评率',
      value: (d) => fmtPct(d.csat.good_rate),
      note: (d) => noteOf(d, 'csat_good_rate', '好评率 = 好评数 ÷ 参评总数'),
    },
    {
      key: 'csat_collect',
      label: '好评回收率',
      value: (d) => fmtPct(d.csat.collect_rate),
      note: () => '好评回收率 = 参评总数 ÷ 期间会话总数（衡量评价触达效果）',
    },
    {
      key: 'avg_turns',
      label: '平均轮次',
      unit: '轮',
      value: (d) => num(d.totals.avg_turns, 2),
      note: () => '平均轮次 = 期间会话的平均用户发言轮次（AVG(turn_count)）',
    },
  ];

  const KPI_PERF_SPEC = [
    {
      key: 'bot_first_reply',
      label: '机器人首次响应',
      value: (d) => fmtSeconds(d.performance.avg_bot_first_reply_seconds),
      note: (d) => noteOf(d, 'avg_bot_first_reply_seconds', '机器人首次响应 = 首条机器人消息时间 − 会话创建时间'),
    },
    {
      key: 'queue',
      label: '平均排队时长',
      value: (d) => fmtSeconds(d.performance.avg_queue_seconds),
      note: (d) => noteOf(d, 'avg_queue_seconds', '平均排队时长 = 分配时间 − 进入队列时间（仅统计已分配的会话）'),
    },
    {
      key: 'agent_first_reply',
      label: '客服首次响应',
      value: (d) => fmtSeconds(d.performance.avg_agent_first_reply_seconds),
      note: () => '客服首次响应 = 客服首条回复时间 − 分配时间（后端未下发单独口径，按实现推导）',
    },
    {
      key: 'fallback_times',
      label: '兜底次数',
      unit: '次',
      value: (d) => num(d.totals.fallback_times),
      note: () => '兜底次数 = 区间内进入兜底话术的日志条数（fallback_log）',
    },
    {
      key: 'active',
      label: '进行中会话数',
      unit: '个',
      value: (d) => num(d.totals.active),
      note: () => '进行中 = 会话总数 − 已完成会话数',
    },
    {
      key: 'closed',
      label: '已完成会话数',
      unit: '个',
      value: (d) => num(d.totals.closed),
      note: () => "已完成 = status 为 closed / timeout 的会话",
    },
  ];

  /* ==================================================================== KPI 构建与更新 */
  /** 一次性构建 KPI 卡（之后只改文本，自动刷新不重建 DOM，避免闪烁） */
  function buildKpis(containerId, spec) {
    const box = $(containerId);
    const refs = {};
    if (!box) return refs;
    box.innerHTML = '';
    spec.forEach((s) => {
      const card = document.createElement('div');
      card.className = 'kpi';

      const label = document.createElement('div');
      label.className = 'label';
      label.textContent = s.label;

      const value = document.createElement('div');
      value.className = 'value skel';
      value.textContent = '—';

      const note = document.createElement('div');
      note.className = 'note';
      note.textContent = '加载中…';

      card.appendChild(label);
      card.appendChild(value);
      card.appendChild(note);
      box.appendChild(card);

      refs[s.key] = { card, value, note };
    });
    return refs;
  }

  function updateKpis(refs, spec, data) {
    spec.forEach((s) => {
      const r = refs && refs[s.key];
      if (!r) return;

      // 数值
      let text = '—';
      try { text = s.value(data); } catch (e) { text = '—'; }
      r.value.classList.remove('skel');
      r.value.innerHTML = esc(text) + (s.unit ? ' <small>' + esc(s.unit) + '</small>' : '');

      // 配色
      let cls = 'kpi';
      if (typeof s.color === 'function') {
        let c = '';
        try { c = s.color(data) || ''; } catch (e) { c = ''; }
        if (c) cls += ' ' + c;
      }
      r.card.className = cls;

      // 口径说明
      let note = '';
      try { note = (typeof s.note === 'function' ? s.note(data) : s.note) || ''; } catch (e) { note = ''; }
      r.note.textContent = note;
    });
  }

  /* ==================================================================== 顶部：区间与更新时间 */
  function renderRangeLine(range) {
    const textEl = $('range-text');
    const badgeEl = $('updated-badge');
    if (range && range.since && range.until) {
      textEl.textContent = '数据区间 ' + fmtDateTime(range.since) + ' ~ ' + fmtDateTime(range.until);
    } else {
      textEl.textContent = '数据区间未知';
    }
    if (state.lastUpdated) {
      badgeEl.className = 'badge ok';
      badgeEl.textContent = '最后更新 ' + state.lastUpdated;
    }
  }

  /* ==================================================================== 运行状态 */
  function renderHealth(health) {
    const box = $('health-strip');
    if (!health) { box.innerHTML = '<span class="badge gray">状态不可用</span>'; return; }

    const svc = health.service || {};
    const th = health.thresholds || {};
    const online = !!svc.service_online;
    const items = [
      '<span class="badge ' + (online ? 'ok' : 'gray') + '">' +
        (online ? '服务在线' : '服务离线') +
        (svc.service_hours_text ? ' · ' + esc(svc.service_hours_text) : '') + '</span>',
      '<span class="badge ' + (svc.accepting ? 'ok' : 'warn') + '">' +
        (svc.accepting ? '接待中' : '已停止接待') + '</span>',
      '<span class="badge gray">在线客服 ' + esc(num(svc.online_agents)) + '</span>',
      '<span class="badge ' + (Number(svc.queue_count) > 0 ? 'warn' : 'gray') + '">排队 ' + esc(num(svc.queue_count)) + '</span>',
      '<span class="badge gray">知识条目 ' + esc(num(health.knowledge_count)) + '</span>',
      '<span class="badge ' + (health.llm_enabled ? 'ctx' : 'gray') + '">LLM ' + (health.llm_enabled ? '已启用' : '未启用') + '</span>',
      '<span class="badge gray">阈值 ' + esc(String(th.high_confidence !== undefined ? th.high_confidence : '—')) +
        ' / ' + esc(String(th.mid_confidence !== undefined ? th.mid_confidence : '—')) + '</span>',
    ];
    box.innerHTML = items.join('');
  }

  /* ==================================================================== 会话趋势 */
  /** 单条柱高度：0 -> 0，正数最小 2px，按当期最大值等比换算 */
  function barHeight(v, max) {
    const n = Number(v) || 0;
    if (n <= 0) return 0;
    if (!max || max <= 0) return 2;
    return Math.max(2, Math.round((n / max) * BAR_MAX_PX));
  }

  function renderTrend(daily) {
    const box = $('trend-box');
    const sub = $('trend-sub');
    const list = Array.isArray(daily) ? daily : [];

    if (!list.length) {
      box.className = '';
      box.innerHTML = '<div class="empty">暂无趋势数据</div>';
      sub.textContent = '按天统计';
      return;
    }

    // 当期最大值（机器人解决 / 转人工共用同一比例尺）
    const max = list.reduce((m, d) => Math.max(
      m,
      Number(d && d.bot_resolved) || 0,
      Number(d && d.handoff) || 0
    ), 0);

    box.className = 'trend';
    box.innerHTML = list.map((d) => {
      const day = (d && d.day) || '';
      const sessions = Number(d && d.sessions) || 0;
      const bot = Number(d && d.bot_resolved) || 0;
      const handoff = Number(d && d.handoff) || 0;
      const h1 = barHeight(bot, max);
      const h2 = barHeight(handoff, max);
      const tip = day + '：会话 ' + sessions + '，机器人解决 ' + bot + '，转人工 ' + handoff;
      return '<div class="col" title="' + esc(tip) + '">' +
        '<div class="stack">' +
          '<div class="b1" style="height:' + h1 + 'px"></div>' +
          '<div class="b2" style="height:' + h2 + 'px"></div>' +
        '</div>' +
        '<span class="day">' + esc(fmtDay(day)) + '</span>' +
      '</div>';
    }).join('');

    sub.textContent = '按天统计 · 共 ' + list.length + ' 天';
  }

  /* ==================================================================== 横向条形（意图分布 / 转人工原因） */
  function renderBars(boxId, rows, emptyText) {
    const box = $(boxId);
    // rows: [{ name, times }]
    const list = Array.isArray(rows) ? rows.filter((r) => r && r.name !== undefined && r.name !== null) : [];
    if (!list.length) {
      box.innerHTML = '<div class="empty">' + esc(emptyText) + '</div>';
      return;
    }
    const sorted = list.slice().sort((a, b) => (Number(b.times) || 0) - (Number(a.times) || 0));
    const max = sorted.reduce((m, r) => Math.max(m, Number(r.times) || 0), 0);

    box.innerHTML = sorted.map((r) => {
      const times = Number(r.times) || 0;
      let pct = 0;
      if (max > 0) pct = Math.round((times / max) * 100);
      if (times > 0) pct = Math.max(2, pct);   // 正数至少给一点宽度，保证可见
      return '<div class="bar-row">' +
        '<span class="name" title="' + esc(r.name) + '">' + esc(r.name) + '</span>' +
        '<span class="bar"><i style="width:' + pct + '%"></i></span>' +
        '<span class="num">' + esc(num(times)) + '</span>' +
      '</div>';
    }).join('');
  }

  function renderIntent(intentDistribution) {
    const rows = (Array.isArray(intentDistribution) ? intentDistribution : []).map((it) => ({
      name: (it && it.category) ? it.category : '未分类',
      times: (it && it.times) || 0,
    }));
    renderBars('intent-box', rows, '暂无意图命中数据');
  }

  function renderHandoffReasons(handoffReasons) {
    const rows = (Array.isArray(handoffReasons) ? handoffReasons : []).map((it) => ({
      name: reasonLabel(it && it.trigger_type),
      times: (it && it.times) || 0,
    }));
    renderBars('reason-box', rows, '暂无转人工记录');
  }

  /* ==================================================================== TOP 未解决问题 */
  function renderTopUnresolved(items) {
    const box = $('unresolved-box');
    const list = Array.isArray(items) ? items : [];
    if (!list.length) {
      box.innerHTML = '<div class="empty">暂无未解决问题</div>';
      return;
    }
    box.innerHTML =
      '<div class="table-wrap"><table>' +
        '<thead><tr>' +
          '<th>问题</th>' +
          '<th class="right" style="width:100px;">出现次数</th>' +
          '<th style="width:150px;">最近时间</th>' +
        '</tr></thead><tbody>' +
        list.map((it) => (
          '<tr>' +
            '<td class="q col-q">' + esc(it && it.query) + '</td>' +
            '<td class="right">' + esc(num(it && it.times)) + '</td>' +
            '<td class="small nowrap">' + esc(fmtTime(it && it.last_at)) + '</td>' +
          '</tr>'
        )).join('') +
        '</tbody></table></div>';
  }

  /* ==================================================================== 最近转人工会话 */
  function csatCell(score) {
    if (score === 1 || score === '1') return '<span class="badge ok">好评</span>';
    if (score === -1 || score === '-1') return '<span class="badge warn">差评</span>';
    return '<span class="badge gray">未评价</span>';
  }

  function renderRecentHandoffs(items) {
    const box = $('handoff-box');
    const list = Array.isArray(items) ? items : [];
    if (!list.length) {
      box.innerHTML = '<div class="empty">暂无转人工会话</div>';
      return;
    }
    box.innerHTML =
      '<div class="table-wrap"><table>' +
        '<thead><tr>' +
          '<th style="width:150px;">会话 ID</th>' +
          '<th style="width:140px;">时间</th>' +
          '<th>最后意图</th>' +
          '<th style="width:150px;">触发原因</th>' +
          '<th style="width:110px;">归因</th>' +
          '<th style="width:90px;">满意度</th>' +
        '</tr></thead><tbody>' +
        list.map((s) => (
          '<tr>' +
            '<td class="mono">' + esc(s && s.id) + '</td>' +
            '<td class="small nowrap">' + esc(fmtTime(s && s.created_at)) + '</td>' +
            '<td>' +
              ((s && s.last_category) ? '<span class="tag">' + esc(s.last_category) + '</span>' : '<span class="muted">—</span>') +
              '<div class="small">' + esc((s && s.last_question) ? String(s.last_question).slice(0, 48) : '—') + '</div>' +
            '</td>' +
            '<td><span class="badge ctx">' + esc(reasonLabel(s && s.handoff_reason)) + '</span></td>' +
            '<td class="small">' + esc(resolvedByLabel(s && s.resolved_by)) + '</td>' +
            '<td>' + csatCell(s && s.csat_score) + '</td>' +
          '</tr>'
        )).join('') +
        '</tbody></table></div>';
  }

  /* ==================================================================== 客服负载 */
  function dotClass(status) {
    if (status === 'online') return 'online';
    if (status === 'busy') return 'busy';
    return 'offline';
  }

  function renderAgents(items) {
    const box = $('agent-box');
    const sub = $('agent-sub');
    const list = Array.isArray(items) ? items : [];
    if (!list.length) {
      box.innerHTML = '<div class="empty">暂无客服账号</div>';
      sub.textContent = '—';
      return;
    }
    box.innerHTML = list.map((a) => {
      const load = num(a && a.current_load) + ' / ' + num(a && a.max_concurrent);
      return '<div class="agent-row flex-between">' +
        '<div>' +
          '<span class="dot ' + dotClass(a && a.status) + '"></span>' +
          '<b>' + esc((a && a.name) || '未命名') + '</b> ' +
          '<span class="small">' + esc((a && a.status_label) || '') + '</span>' +
        '</div>' +
        '<div class="small nowrap">负载 <b>' + esc(load) + '</b> · 进行中 ' + esc(num(a && a.active_sessions)) + '</div>' +
      '</div>';
    }).join('');

    const online = list.filter((a) => a && a.status === 'online').length;
    sub.textContent = '共 ' + list.length + ' 人 · 在线 ' + online + ' 人';
  }

  /* ==================================================================== 页尾口径说明 */
  function renderMetricNotes(metricNotes) {
    const box = $('notes-box');
    const entries = Object.entries(metricNotes || {});
    if (!entries.length) {
      box.innerHTML = '<div class="empty">后端未返回口径说明</div>';
      return;
    }
    box.innerHTML = '<ul class="notes-list">' + entries.map(([k, v]) => (
      '<li>' +
        '<span class="k badge gray">' + esc(NOTE_LABEL[k] || k) + '</span>' +
        '<span>' + esc(v) + '</span>' +
      '</li>'
    )).join('') + '</ul>';
  }

  /* ==================================================================== 主渲染 */
  function renderOverview(data) {
    state.lastUpdated = nowClock();

    // 顶部区间
    renderRangeLine(data.range);
    const sub = $('overview-sub');
    if (sub) sub.textContent = (RANGE_LABEL[state.days] || ('近 ' + state.days + ' 天'));

    // KPI
    updateKpis(kpiCoreRefs, KPI_CORE_SPEC, data);
    updateKpis(kpiPerfRefs, KPI_PERF_SPEC, data);

    // 图表与表格（均对空数组做了兜底）
    renderTrend(data.daily);
    renderIntent(data.intent_distribution);
    renderHandoffReasons(data.handoff_reasons);
    renderTopUnresolved(data.top_unresolved);
    renderMetricNotes(data.metric_notes);
  }

  /* ==================================================================== 数据加载 */
  async function loadAll() {
    if (state.loading) return;
    state.loading = true;

    const btn = $('btn-refresh');
    if (btn) btn.disabled = true;

    const overviewPath = '/api/admin/overview?days=' + encodeURIComponent(state.days);
    const results = await Promise.allSettled([
      api(overviewPath),
      api('/api/health'),
      api('/api/admin/sessions?limit=10&handoff_only=true'),
      api('/api/admin/agents'),
    ]);
    const labels = ['核心指标', '运行状态', '最近转人工会话', '客服负载'];

    // 概览接口是主数据，失败时保留上一次渲染结果，避免页面被清空
    if (results[0].status === 'fulfilled') {
      try {
        renderOverview(results[0].value);
      } catch (e) {
        toast('渲染看板失败：' + (e && e.message ? e.message : e), true);
      }
    }
    if (results[1].status === 'fulfilled') {
      try { renderHealth(results[1].value); } catch (e) { /* 忽略局部渲染错误 */ }
    }
    if (results[2].status === 'fulfilled') {
      try { renderRecentHandoffs((results[2].value || {}).items); } catch (e) { /* 忽略 */ }
    }
    if (results[3].status === 'fulfilled') {
      try { renderAgents((results[3].value || {}).items); } catch (e) { /* 忽略 */ }
    }

    const failed = [];
    results.forEach((r, i) => {
      if (r.status === 'rejected') failed.push(labels[i] + '（' + ((r.reason && r.reason.message) || '未知错误') + '）');
    });
    if (failed.length) {
      toast('部分数据加载失败：' + failed.join('；'), true);
    }

    state.loading = false;
    if (btn) btn.disabled = false;
  }

  /* ==================================================================== 自动刷新 */
  function startAutoRefresh() {
    stopAutoRefresh();
    if (!state.autoRefresh) return;
    state.timer = setInterval(() => {
      if (document.hidden) return;   // 页面不可见时跳过，省资源
      loadAll();
    }, REFRESH_MS);
  }

  function stopAutoRefresh() {
    if (state.timer) { clearInterval(state.timer); state.timer = null; }
  }

  /* ==================================================================== 事件绑定 */
  function bindEvents() {
    // 时间范围切换
    const seg = $('range-seg');
    if (seg) {
      seg.addEventListener('click', (e) => {
        const btn = e.target.closest('button[data-days]');
        if (!btn) return;
        const days = Number(btn.dataset.days) || DEFAULT_DAYS;
        if (days === state.days) return;
        state.days = days;
        Array.prototype.forEach.call(seg.querySelectorAll('button'), (b) => {
          b.classList.toggle('active', b === btn);
        });
        // 切换区间时给一次明确的加载反馈（只重置趋势图，KPI 保留旧值避免闪烁）
        const trendBox = $('trend-box');
        if (trendBox) trendBox.innerHTML = '<div class="loading-text">加载中…</div>';
        loadAll();
      });
    }

    // 手动刷新
    const refreshBtn = $('btn-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', () => loadAll());

    // 自动刷新开关（默认开启）
    const autoEl = $('auto-refresh');
    if (autoEl) {
      autoEl.addEventListener('change', () => {
        state.autoRefresh = autoEl.checked;
        if (state.autoRefresh) {
          startAutoRefresh();
          toast('已开启自动刷新（每 ' + (REFRESH_MS / 1000) + ' 秒）');
        } else {
          stopAutoRefresh();
          toast('已关闭自动刷新');
        }
      });
    }

    // 从后台切回来时立即补一次
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && state.autoRefresh) loadAll();
    });
  }

  /* ==================================================================== 初始化 */
  function init() {
    kpiCoreRefs = buildKpis('kpi-core', KPI_CORE_SPEC);
    kpiPerfRefs = buildKpis('kpi-perf', KPI_PERF_SPEC);
    bindEvents();
    startAutoRefresh();
    loadAll();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
