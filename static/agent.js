/* 客服工作台：会话接入 / 收发消息 / 上下文同步 / 常用语
   纯原生 JS，无框架依赖。
   实时通道：WebSocket（/ws/agent）事件驱动列表与消息刷新；
   连接连续失败 3 次（或浏览器不支持 WS）时自动降级为短轮询（列表 5s / 消息 3s），
   后台继续尝试重连，连上后自动切回实时推送并停止轮询。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const listEl = $('session-list');
  const bodyEl = $('conv-body');
  const summaryEl = $('summary');
  const cannedListEl = $('canned-list');
  const cannedManageEl = $('canned-manage');
  const replyEl = $('reply-input');
  const toastEl = $('toast');
  const mask = $('canned-modal');

  const STATUS_LABEL = {
    bot: '机器人接待中', queueing: '排队中', agent: '人工接待中',
    closed: '已结束', timeout: '已超时',
  };
  const RESOLVE_LABEL = { bot: '机器人解决', agent: '人工解决', unresolved: '未解决' };
  const TRIGGER_LABEL = {
    user_request: '用户主动要求', fallback_count: '连续兜底', sentiment: '负面情绪',
    risky_keyword: '高风险词', turn_limit: '轮次超限', clarify_limit: '澄清超限',
  };

  let agentId = Number(localStorage.getItem('cs_agent_id') || 0) || '';
  let scope = 'pending';
  let currentId = '';
  let lastMsgId = 0;
  let agents = [];
  let authEnabled = false;

  /* ---------------------------------------------------------- 工具 */
  function toast(text, isErr = false) {
    toastEl.textContent = text;
    toastEl.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.className = 'toast'), 2200);
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  async function api(path, options = {}) {
    const headers = {};
    if (options.body) headers['Content-Type'] = 'application/json';
    const token = localStorage.getItem('cs_token');
    if (token) headers.Authorization = `Bearer ${token}`;
    if (agentId) headers['X-Agent-Id'] = String(agentId);
    const sep = path.includes('?') ? '&' : '?';
    const url = agentId && !path.includes('agent_id=') ? `${path}${sep}agent_id=${agentId}` : path;
    const res = await fetch(url, { headers, ...options });
    if (res.status === 401) { location.href = '/login'; return null; }
    if (!res.ok) {
      let detail = `请求失败 (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  function fmtTime(s) { return (s || '').slice(5, 16); }

  /* 统一兜底异步错误：任何事件驱动的刷新失败都要提示，而不是静默 unhandled rejection */
  function reportAsync(err) {
    const msg = err && err.message ? err.message : String(err);
    if (String(err && err.name) === 'AbortError') return;
    toast('刷新失败：' + msg, true);
  }

  /* ---------------------------------------------------------- 实时连接状态徽标（动态创建，不改 HTML） */
  const realtimeBadge = document.createElement('span');
  realtimeBadge.id = 'realtime-badge';
  realtimeBadge.className = 'badge gray';
  realtimeBadge.title = 'WebSocket 实时通道状态';
  (function insertBadge() {
    const host = document.querySelector('.topbar-actions');
    if (host) host.insertBefore(realtimeBadge, host.firstChild);
    else document.body.appendChild(realtimeBadge);
  })();

  function setRealtimeBadge(state, text) {
    if (state === 'live') {
      realtimeBadge.className = 'badge ok';
      realtimeBadge.innerHTML = '<span class="dot online"></span>实时 · WebSocket 推送';
    } else if (state === 'degraded') {
      realtimeBadge.className = 'badge gray';
      realtimeBadge.innerHTML = '<span class="dot"></span>实时连接中断，已降级为轮询';
    } else if (state === 'connecting') {
      realtimeBadge.className = 'badge warn';
      realtimeBadge.innerHTML = '<span class="dot busy"></span>连接中…（轮询兜底）';
    } else {
      realtimeBadge.className = 'badge gray';
      realtimeBadge.textContent = '';
    }
    const detail = typeof text === 'string' ? text.trim() : '';
    if (detail) realtimeBadge.title = detail;
  }

  /* ---------------------------------------------------------- 客服身份 */
  /* 当前选中的客服对象（来自坐席列表） */
  function me() { return agents.find((a) => String(a.id) === String(agentId)) || null; }

  function renderAgentOptions() {
    const sel = $('agent-select');
    sel.innerHTML = agents.map((a) => {
      const load = (a.current_load === undefined || a.current_load === null)
        ? '' : `· ${a.current_load}/${a.max_concurrent ?? '?'}`;
      return `<option value="${a.id}">${esc(a.name)}（${esc(a.status_label || a.status)}）${load}</option>`;
    }).join('');
    if (!agentId && agents.length) agentId = agents[0].id;
    if (agentId) sel.value = String(agentId);
    sel.disabled = !!authEnabled;
  }

  async function loadAgents() {
    const data = await api('/api/agent/list');
    agents = data.items || data.agents || [];
    authEnabled = !!data.auth_enabled;
    renderAgentOptions();
    $('me-badge').textContent = authEnabled ? '已登录（角色绑定）' : '演示模式';
    $('me-badge').className = 'badge ' + (authEnabled ? 'ok' : 'gray');
    const me = agents.find((a) => String(a.id) === String(agentId));
    if (me) $('status-select').value = me.status;
  }

  /* 用 WS `sync` 首帧的 agents 快照更新坐席切换器与顶部徽标（避免额外 HTTP 请求） */
  function applyAgentsSnapshot(list) {
    if (!Array.isArray(list) || !list.length) return;
    agents = list;
    renderAgentOptions();
    const me = agents.find((a) => String(a.id) === String(agentId));
    if (me) $('status-select').value = me.status;
    $('me-badge').textContent = authEnabled ? '已登录（角色绑定）' : '演示模式';
    $('me-badge').className = 'badge ' + (authEnabled ? 'ok' : 'gray');
  }

  /* ---------------------------------------------------------- 会话列表 */
  let pendingCount = 0;

  function applyPendingCount(n) {
    if (typeof n !== 'number' || !isFinite(n)) return;
    pendingCount = n;
    const el = $('cnt-pending');
    if (el) el.textContent = String(n);
  }

  async function loadSessions(silent = false) {
    if (!silent) listEl.innerHTML = '<div class="empty">加载中…</div>';
    try {
      const data = await api(`/api/agent/sessions?scope=${scope}`);
      const items = data.sessions || [];
      if (scope === 'pending') applyPendingCount(items.length);
      /* 非「待接入」页签时，单独取一次待接入数量，保证左侧计数实时准确 */
      if (scope !== 'pending') {
        api('/api/agent/sessions?scope=pending')
          .then((p) => applyPendingCount((p.sessions || []).length))
          .catch(() => { /* 计数失败不影响主列表 */ });
      }
      if (!items.length) {
        listEl.innerHTML = '<div class="empty">暂无会话</div>';
        return;
      }
      listEl.innerHTML = items.map((s) => `
        <div class="session-item ${s.id === currentId ? 'active' : ''}" data-id="${s.id}">
          <div class="row1">
            <span>${esc(s.id.slice(0, 8))} · ${esc(s.last_category || '未识别')}</span>
            <span class="badge ${s.status === 'queueing' ? 'warn' : 'gray'}">${esc(STATUS_LABEL[s.status] || s.status)}</span>
          </div>
          <div class="row2">${esc(s.last_message || '（暂无消息）')}</div>
          <div class="row2 small">
            ${esc(fmtTime(s.updated_at))} · ${s.message_count || 0} 条
            ${s.waiting_seconds ? ` · <span class="waiting">已等待 ${s.waiting_seconds}s</span>` : ''}
          </div>
        </div>`).join('');
      listEl.querySelectorAll('.session-item').forEach((el) => {
        el.onclick = () => selectSession(el.dataset.id);
        /* 新会话接入的高亮：行是异步渲染出来的，渲染后再补一次高亮 */
        if (highlightUntil[el.dataset.id] > Date.now()) {
          el.style.outline = '2px solid var(--ok)';
        }
      });
    } catch (err) {
      listEl.innerHTML = `<div class="empty">加载失败：${esc(err.message)}</div>`;
    }
  }

  /* ---------------------------------------------------------- 会话详情 */
  async function selectSession(id) {
    currentId = id;
    lastMsgId = 0;
    document.querySelectorAll('.session-item').forEach((el) =>
      el.classList.toggle('active', el.dataset.id === id));
    bodyEl.innerHTML = '<div class="empty">加载中…</div>';
    await Promise.all([loadMessages(true), loadSummary()]);
    /* 轮询降级时才为该会话开启 3s 消息轮询；实时模式下完全由 WS 事件驱动 */
    startMsgPolling();
  }

  async function loadSummary() {
    if (!currentId) return;
    try {
      const s = await api(`/api/agent/sessions/${currentId}/context`);
      const sess = s.session || {};
      const slots = s.slots || {};
      const slotText = Object.keys(slots).length
        ? Object.entries(slots).map(([k, v]) => `<span class="tag slot">${esc(v.label)}</span>`).join('')
        : '<span class="small">无</span>';
      summaryEl.innerHTML = `
        <div class="summary-card">
          <dl class="kv">
            <dt>会话 ID</dt><dd class="mono">${esc(sess.id || '')}</dd>
            <dt>访客</dt><dd class="mono">${esc(sess.visitor_id || '(匿名)')}</dd>
            <dt>状态</dt><dd>${esc(sess.status_label || '')}</dd>
            <dt>轮次</dt><dd>${sess.turn_count || 0} 轮 / 兜底 ${sess.fallback_count || 0} 次</dd>
            <dt>意图</dt><dd>${esc(s.intent || '')}</dd>
            <dt>分类</dt><dd>${esc(s.category || '')}</dd>
            <dt>槽位</dt><dd>${slotText}</dd>
            <dt>转人工</dt><dd>${s.handoff
              ? `${esc(TRIGGER_LABEL[s.handoff.trigger_type] || s.handoff.trigger_type)}<br><span class="small">${esc(s.handoff.trigger_detail || '')}</span>`
              : '<span class="small">无</span>'}</dd>
            <dt>引用知识</dt><dd>${(s.referenced_knowledge || []).length
              ? s.referenced_knowledge.map((k) => `<div class="small">#${k.id} ${esc(k.question)}</div>`).join('')
              : '<span class="small">无</span>'}</dd>
          </dl>
        </div>
        <div class="help">接待建议：先回应客户情绪，再复述已确认的信息（意图 / 槽位），避免让客户重复描述。</div>`;

      const sess2 = s.session || {};
      $('conv-title').textContent = `会话 ${String(currentId).slice(0, 8)} · ${sess2.last_question || '未识别意图'}`;
      $('conv-badges').innerHTML = `
        <span class="badge ${sess2.status === 'queueing' ? 'warn' : (sess2.status === 'agent' ? 'ctx' : 'gray')}">
          ${esc(sess2.status_label || '')}</span>
        ${sess2.handoff_reason ? `<span class="badge warn">触发：${esc(TRIGGER_LABEL[sess2.handoff_reason] || sess2.handoff_reason)}</span>` : ''}
        <span class="badge gray">${sess2.turn_count || 0} 轮</span>
        ${sess2.csat_score ? `<span class="badge ${sess2.csat_score === 1 ? 'ok' : 'warn'}">${sess2.csat_score === 1 ? '好评' : '差评：' + esc(sess2.csat_reason || '')}</span>` : ''}`;

      $('btn-accept').style.display = sess2.status === 'queueing' ? 'inline-block' : 'none';
      $('btn-close-session').style.display = (sess2.status === 'agent' || sess2.status === 'queueing')
        ? 'inline-block' : 'none';
    } catch (err) {
      summaryEl.innerHTML = `<div class="empty">加载失败：${esc(err.message)}</div>`;
    }
  }

  function renderConvMessage(m) {
    if (m.role === 'system') {
      const el = document.createElement('div');
      el.className = 'sys-msg';
      el.textContent = m.content;
      bodyEl.appendChild(el);
      return;
    }
    const wrap = document.createElement('div');
    wrap.className = `msg ${m.role === 'user' ? 'user' : 'bot'}`;
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = m.role === 'user' ? '客' : (m.role === 'agent' ? '我' : 'AI');
    const body = document.createElement('div');
    body.className = 'bubble-wrap';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = m.content;
    body.appendChild(bubble);

    const parts = [];
    if (m.answer_type) parts.push(`<span class="badge gray">${esc(m.answer_type)}</span>`);
    if (m.score) parts.push(`<span class="badge ok">${Number(m.score).toFixed(2)}</span>`);
    if (m.context_used) parts.push('<span class="badge ctx">结合上文</span>');
    if ((m.source_details || []).length) {
      parts.push(`<span class="small">来源：${m.source_details.map((s) =>
        `#${s.id} ${esc(s.question)}`).join('；')}</span>`);
    }
    if (parts.length) {
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.innerHTML = parts.join('');
      body.appendChild(meta);
    }
    wrap.append(avatar, body);
    bodyEl.appendChild(wrap);
  }

  /* 消息增量拉取：in-flight 合并，避免 WS 事件与本地操作叠加出重复请求 */
  let msgLoading = false;
  let msgMore = false;

  async function loadMessages(scroll = false) {
    if (!currentId) return;
    if (msgLoading) { msgMore = true; return; }
    msgLoading = true;
    try {
      const data = await api(`/api/agent/sessions/${currentId}/messages?after=${lastMsgId}`);
      const list = data.messages || [];
      if (list.length) {
        if (bodyEl.querySelector('.empty')) bodyEl.innerHTML = '';
        list.forEach((m) => { renderConvMessage(m); lastMsgId = Math.max(lastMsgId, m.id); });
        bodyEl.scrollTop = bodyEl.scrollHeight;
      } else if (scroll && !bodyEl.children.length) {
        bodyEl.innerHTML = '<div class="empty">暂无消息</div>';
      }
    } catch (err) {
      if (scroll) bodyEl.innerHTML = `<div class="empty">加载失败：${esc(err.message)}</div>`;
      else reportAsync(err);
    } finally {
      msgLoading = false;
      if (msgMore) { msgMore = false; loadMessages().catch(reportAsync); }
    }
  }

  /* ---------------------------------------------------------- 实时通道（WebSocket） */
  const WS_RETRY_BASE = [1000, 2000, 4000, 8000];   // 指数退避：1s/2s/4s/8s
  const WS_RETRY_MAX = 15000;                        // 退避上限 15s
  const WS_DEGRADE_AFTER = 3;                        // 连续 3 次连接失败 → 降级轮询
  const WS_HEARTBEAT_MS = 25000;                     // 与服务端心跳周期（25s）对齐
  const LIST_POLL_MS = 5000;                         // 降级：列表轮询
  const MSG_POLL_MS = 3000;                          // 降级：消息轮询
  const EVENT_DEBOUNCE_MS = 250;                     // 事件刷新防抖 200~300ms

  const WS = {
    enabled: typeof WebSocket === 'function',
    ws: null,
    state: 'idle',          // idle | connecting | live | degraded
    attempt: 0,             // 连续失败次数（连上即归零）
    reconnectTimer: null,
    heartbeatTimer: null,
    degraded: false,
    openedOnce: false,
    cannedSynced: false,    // 常用语只在首个 sync 时同步一次（重连不重复拉取）
    pollTicks: 0,           // 降级轮询触发次数（自测取证用）
    stop: false,
  };
  let listTimer = null;
  let msgTimer = null;
  let listDebounce = null;
  let convDebounce = null;
  const assignedNotice = {};        // 已提示过「新会话接入」的会话，避免重复 toast
  const highlightUntil = {};        // 会话 id → 高亮截止时间戳（行渲染晚于事件时补高亮）

  /* 退避：1s/2s/4s/8s，上限 15s，并叠加 ±30% 抖动（避免多标签页同时重连） */
  function nextRetryDelay(attempt) {
    const base = WS_RETRY_BASE[Math.min(Math.max(attempt, 0), WS_RETRY_BASE.length - 1)];
    const capped = Math.min(base, WS_RETRY_MAX);
    return Math.round(capped * (0.85 + Math.random() * 0.3));
  }

  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/agent`;
  }

  function startHeartbeat() {
    clearInterval(WS.heartbeatTimer);
    WS.heartbeatTimer = setInterval(() => {
      if (WS.ws && WS.ws.readyState === 1) {
        try { WS.ws.send(JSON.stringify({ type: 'ping' })); } catch (e) { /* 断开会触发 onclose */ }
      }
    }, WS_HEARTBEAT_MS);
  }

  function connectRealtime() {
    if (WS.stop) return;
    if (!WS.enabled || typeof WebSocket !== 'function') {
      /* 浏览器不支持 WebSocket → 直接降级 */
      degrade('浏览器不支持 WebSocket，已降级为轮询');
      return;
    }
    if (WS.ws && (WS.ws.readyState === 0 || WS.ws.readyState === 1)) return;
    if (WS.state !== 'live' && WS.state !== 'degraded') setRealtimeBadge('connecting');
    WS.state = 'connecting';
    let sock;
    try {
      sock = new WebSocket(wsUrl());
    } catch (err) {
      wsFailed();
      return;
    }
    WS.ws = sock;
    sock.onopen = () => {
      if (WS.ws !== sock) return;
      WS.attempt = 0;
      WS.openedOnce = true;
      WS.state = 'live';
      setRealtimeBadge('live', 'WebSocket 已连接：' + wsUrl());
      stopPolling();
      /* 重连成功：主动请求一次 sync，并立即刷新列表与当前会话 */
      try { sock.send(JSON.stringify({ type: 'sync' })); } catch (e) { /* ignore */ }
      startHeartbeat();
      refreshNow();
      toast('实时通道已连接');
    };
    sock.onmessage = (ev) => {
      if (WS.ws !== sock) return;
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleRealtimeEvent(msg);
    };
    sock.onerror = () => { /* 统一交给 onclose：onerror 之后必然触发 onclose */ };
    sock.onclose = () => {
      if (WS.ws !== sock) return;
      WS.ws = null;
      clearInterval(WS.heartbeatTimer);
      WS.heartbeatTimer = null;
      if (WS.stop) return;
      if (WS.state === 'live') {
        /* 曾经连上过：属于链路断开，重新从 1s 开始退避（不计入降级失败数） */
        WS.attempt = 0;
        setRealtimeBadge('connecting', '连接已断开，正在重连…');
        WS.state = 'connecting';
        scheduleReconnect();
      } else {
        wsFailed();
      }
    };
  }

  /* 连接失败（从未 open）：累计次数，达到阈值即降级为轮询 */
  function wsFailed() {
    WS.ws = null;
    WS.attempt += 1;
    if (WS.attempt >= WS_DEGRADE_AFTER) {
      degrade(`连续 ${WS.attempt} 次连接失败，已降级为轮询（后台继续重连）`);
    } else {
      setRealtimeBadge('connecting', `连接失败（第 ${WS.attempt} 次），正在重试…`);
      WS.state = 'connecting';
    }
    scheduleReconnect();
  }

  function scheduleReconnect() {
    if (WS.stop) return;
    clearTimeout(WS.reconnectTimer);
    const delay = nextRetryDelay(WS.attempt);
    WS.reconnectTimer = setTimeout(() => {
      WS.reconnectTimer = null;
      connectRealtime();
    }, delay);
  }

  /* 降级：切回 5s 列表轮询 + 3s 消息轮询，并显示灰色徽标。
     注意：后台每次重连失败都会走到这里，必须幂等——否则会把轮询定时器反复重启，
     导致 5s 轮询永远等不到第一次触发。 */
  function degrade(reason) {
    setRealtimeBadge('degraded', reason);
    WS.state = 'degraded';
    if (!WS.degraded) {
      WS.degraded = true;
      startPolling();
      /* 降级瞬间先补一次刷新，不用等第一个 5s 周期 */
      loadSessions(true).catch(reportAsync);
      if (currentId) {
        loadMessages().catch(reportAsync);
        loadSummary().catch(reportAsync);
      }
    }
  }

  function startPolling() {
    stopPolling();
    listTimer = setInterval(() => {
      WS.pollTicks += 1;
      if (document.hidden) return;          // 页面不可见时跳过请求
      loadSessions(true).catch(reportAsync);
    }, LIST_POLL_MS);
    if (currentId) startMsgPolling();
  }

  function stopPolling() {
    if (listTimer) clearInterval(listTimer);
    listTimer = null;
    if (msgTimer) clearInterval(msgTimer);
    msgTimer = null;
  }

  /* 当前会话消息轮询：仅降级模式（WS 正常时不存在任何消息定时器） */
  function startMsgPolling() {
    if (msgTimer) clearInterval(msgTimer);
    if (!WS.degraded || !currentId) return;
    msgTimer = setInterval(() => {
      if (document.hidden) return;
      loadMessages().catch(reportAsync);
      loadSummary().catch(reportAsync);
    }, MSG_POLL_MS);
  }

  /* 事件驱动刷新（带 200~300ms 防抖，一次操作只刷新一次） */
  function scheduleListRefresh() {
    clearTimeout(listDebounce);
    listDebounce = setTimeout(() => {
      listDebounce = null;
      loadSessions(true).catch(reportAsync);
    }, EVENT_DEBOUNCE_MS);
  }

  function scheduleConvRefresh() {
    if (!currentId) return;
    clearTimeout(convDebounce);
    convDebounce = setTimeout(() => {
      convDebounce = null;
      loadMessages().catch(reportAsync);
      loadSummary().catch(reportAsync);
    }, EVENT_DEBOUNCE_MS);
  }

  function refreshNow() {
    loadSessions(true).catch(reportAsync);
    if (currentId) {
      loadMessages().catch(reportAsync);
      loadSummary().catch(reportAsync);
    }
  }

  /* 最近收到的事件（仅保留 30 条，用于自测取证，不参与业务逻辑） */
  const eventLog = [];
  function logEvent(msg) {
    try {
      eventLog.unshift({ at: Date.now(), type: msg.type, session_id: msg.session_id || '',
        assigned: msg.assigned, offline: msg.offline, trigger_type: msg.trigger_type,
        agent_id: msg.agent_id, agent_name: msg.agent_name, status: msg.status });
      if (eventLog.length > 30) eventLog.length = 30;
    } catch (err) { /* 取证失败不影响业务 */ }
  }

  /* 处理服务端主动推送的事件 */
  function handleRealtimeEvent(msg) {
    const type = msg && msg.type;
    if (!type || type === 'ping' || type === 'pong') return;   // 心跳直接忽略
    logEvent(msg);
    if (type === 'sync') {
      /* 首帧 / 重连帧：刷新服务状态、坐席列表、待接入计数，并立即补一次列表与当前会话 */
      applyAgentsSnapshot(msg.agents);
      applyPendingCount(Number((msg.service || {}).queue_count));
      if (!WS.cannedSynced) { WS.cannedSynced = true; loadCanned().catch(reportAsync); }
      refreshNow();
      return;
    }
    if (type === 'message') {
      /* 某会话有新消息 → 刷新列表；正是当前会话则拉增量并刷新摘要 */
      scheduleListRefresh();
      if (msg.session_id && msg.session_id === currentId) scheduleConvRefresh();
      return;
    }
    if (type === 'session_updated') {
      scheduleListRefresh();
      if (msg.session_id && msg.session_id === currentId) scheduleConvRefresh();
      /* 会话被分配给当前客服（handoff 事件常缺 agent_id，这里用 session_updated 的 agent_id 兜底） */
      if (msg.status === 'agent' && msg.agent_id != null
          && String(msg.agent_id) === String(agentId) && msg.session_id) {
        notifyAssigned(msg.session_id, '分配接入');
      }
      return;
    }
    if (type === 'session_created') {
      scheduleListRefresh();
      return;
    }
    if (type === 'handoff') {
      handleHandoffEvent(msg);
      scheduleListRefresh();
      return;
    }
    if (type === 'agent_status_changed') {
      loadAgents().catch(reportAsync);
      scheduleListRefresh();
      return;
    }
  }

  /* 判断某个分配结果是否属于「我」。
     服务端 handoff 事件只带 agent_name（不带 agent_id），而 session_updated 事件带 agent_id，
     所以两条线索都要用上，避免把别人的会话误报成「有新会话接入」。 */
  function isAssignedToMe(agentIdValue, agentName) {
    if (agentIdValue != null && String(agentIdValue) === String(agentId)) return true;
    const mine = me();
    if (agentName && mine && String(agentName) === String(mine.name)) return true;
    return false;
  }

  function handleHandoffEvent(msg) {
    const trigger = TRIGGER_LABEL[msg.trigger_type] || msg.trigger_type || '转人工';
    const shortId = String(msg.session_id || '').slice(0, 8);
    if (msg.offline) {
      /* 非服务时段 / 无人在线：后端已提示访客留言 */
      toast('当前非人工服务时间，已提示访客留言', true);
      return;
    }
    if (msg.assigned) {
      if (isAssignedToMe(msg.agent_id, msg.agent_name)) {
        notifyAssigned(msg.session_id, trigger);
        return;
      }
      toast(`会话 ${shortId} 已分配给「${msg.agent_name || '其他客服'}」（${trigger}）`);
      return;
    }
    toast(`会话 ${shortId} 转人工排队中（${trigger}，第 ${msg.queue_position ?? '—'} 位）`);
  }

  /* 提示「有新会话接入」并高亮列表中的该会话 */
  function notifyAssigned(sessionId, trigger) {
    WS.assignHits = (WS.assignHits || 0) + 1;
    if (assignedNotice[sessionId]) return;      // 同一次分配只提示一次
    assignedNotice[sessionId] = true;
    setTimeout(() => { delete assignedNotice[sessionId]; }, 60000);
    toast(`有新会话接入：${String(sessionId || '').slice(0, 8)}（${trigger}）`);
    highlightUntil[sessionId] = Date.now() + 2600;
    /* 立即刷新一次列表，让该会话先出现在列表里，再做高亮（否则行还没渲染出来） */
    loadSessions(true).catch(reportAsync).then(() => {
      const el = listEl.querySelector(`.session-item[data-id="${sessionId}"]`);
      if (el) {
        el.style.outline = '2px solid var(--ok)';
        setTimeout(() => { el.style.outline = ''; }, 2500);
      }
    });
  }

  /* ---------------------------------------------------------- 操作 */
  async function acceptSession() {
    try {
      const res = await api(`/api/agent/sessions/${currentId}/accept`, { method: 'POST' });
      toast(res.message || '已接入');
      await Promise.all([loadSummary(), loadMessages(), loadSessions(true), loadAgents()]);
    } catch (err) { toast(err.message, true); }
  }

  async function sendReply() {
    const content = replyEl.value.trim();
    if (!content || !currentId) return;
    try {
      await api(`/api/agent/sessions/${currentId}/reply`, {
        method: 'POST', body: JSON.stringify({ content }),
      });
      replyEl.value = '';
      await Promise.all([loadMessages(), loadSessions(true), loadAgents()]);
    } catch (err) { toast(err.message, true); }
  }

  async function closeSession() {
    if (!currentId) return;
    if (!confirm('确认结束该会话？结束后会按口径自动归因（机器人解决 / 人工解决 / 未解决）。')) return;
    try {
      const res = await api(`/api/agent/sessions/${currentId}/close`, { method: 'POST' });
      toast(`会话已结束，归因：${RESOLVE_LABEL[res.resolved_by] || res.resolved_by}`);
      await Promise.all([loadSummary(), loadMessages(), loadSessions(true), loadAgents()]);
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------------------------------------------------- 常用语 */
  async function loadCanned() {
    try {
      const data = await api('/api/canned-responses');
      const items = data.items || [];
      cannedListEl.innerHTML = items.length
        ? items.map((c) => `<button class="chip" data-id="${c.id}" title="${esc(c.content)}">${esc(c.title)}</button>`).join('')
        : '<span class="small">暂无常用语，可在右侧新增</span>';
      cannedListEl.querySelectorAll('.chip').forEach((el) => {
        el.onclick = () => {
          const item = items.find((c) => String(c.id) === el.dataset.id);
          if (!item) return;
          replyEl.value = item.content;
          replyEl.focus();
          api(`/api/canned-responses/${item.id}/use`, { method: 'POST' }).catch(() => {});
        };
      });
      cannedManageEl.innerHTML = items.length
        ? items.map((c) => `
            <div class="session-item">
              <div class="row1"><span>${esc(c.title)}</span>
                <span class="small">${c.scope === 'team' ? '团队' : '个人'} · 用 ${c.use_count} 次</span></div>
              <div class="row2">${esc(c.content.slice(0, 40))}</div>
              <div style="margin-top:6px;">
                <button class="btn sm" data-fill="${c.id}">填入</button>
                <button class="btn sm danger" data-del="${c.id}">删除</button>
              </div>
            </div>`).join('')
        : '<div class="small">暂无</div>';
      cannedManageEl.querySelectorAll('[data-fill]').forEach((el) => {
        el.onclick = () => {
          const item = items.find((c) => String(c.id) === el.dataset.fill);
          if (item) { replyEl.value = item.content; replyEl.focus(); }
        };
      });
      cannedManageEl.querySelectorAll('[data-del]').forEach((el) => {
        el.onclick = async () => {
          if (!confirm('确认删除该常用语？')) return;
          await api(`/api/canned-responses/${el.dataset.del}`, { method: 'DELETE' });
          toast('已删除');
          loadCanned();
        };
      });
    } catch (err) {
      cannedListEl.innerHTML = `<span class="small">加载失败：${esc(err.message)}</span>`;
    }
  }

  /* ---------------------------------------------------------- 事件 */
  $('scope-tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    scope = tab.dataset.scope;
    document.querySelectorAll('#scope-tabs .tab').forEach((t) => t.classList.toggle('active', t === tab));
    loadSessions();
  });

  $('btn-refresh').onclick = () => { loadSessions(); loadAgents().catch(reportAsync); };
  $('btn-accept').onclick = acceptSession;
  $('btn-close-session').onclick = closeSession;
  $('btn-reply').onclick = sendReply;

  replyEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendReply(); }
  });

  $('agent-select').onchange = async (e) => {
    agentId = Number(e.target.value);
    localStorage.setItem('cs_agent_id', String(agentId));
    try {
      await loadAgents();
      await loadCanned();
      await loadSessions();
      if (currentId) { await loadSummary(); }
    } catch (err) { reportAsync(err); }
  };

  $('status-select').onchange = async (e) => {
    try {
      await api('/api/agent/status', { method: 'PUT', body: JSON.stringify({ status: e.target.value }) });
      toast('状态已更新');
      loadAgents().catch(reportAsync);
    } catch (err) { toast(err.message, true); }
  };

  $('btn-canned-add').onclick = () => {
    $('cm-title').value = '';
    $('cm-content').value = '';
    mask.classList.add('show');
  };
  $('cm-cancel').onclick = () => mask.classList.remove('show');
  mask.addEventListener('click', (e) => { if (e.target === mask) mask.classList.remove('show'); });
  $('cm-save').onclick = async () => {
    const title = $('cm-title').value.trim();
    const content = $('cm-content').value.trim();
    if (!title || !content) { toast('标题和内容不能为空', true); return; }
    try {
      await api('/api/canned-responses', {
        method: 'POST',
        body: JSON.stringify({ title, content, scope: $('cm-scope').value }),
      });
      mask.classList.remove('show');
      toast('已新增');
      loadCanned();
    } catch (err) { toast(err.message, true); }
  };

  /* 页面不可见：降级轮询跳过请求（定时器内部判断 document.hidden）；
     恢复可见时立即补一次刷新 */
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    loadSessions(true).catch(reportAsync);
    if (currentId) {
      loadMessages().catch(reportAsync);
      loadSummary().catch(reportAsync);
    }
    if (WS.state !== 'live' && !WS.reconnectTimer) scheduleReconnect();
  });

  /* 关闭页面时释放连接与定时器 */
  window.addEventListener('beforeunload', () => {
    WS.stop = true;
    clearTimeout(WS.reconnectTimer);
    clearInterval(WS.heartbeatTimer);
    clearTimeout(listDebounce);
    clearTimeout(convDebounce);
    stopPolling();
    try { if (WS.ws) WS.ws.close(); } catch (e) { /* ignore */ }
  });

  /* ---------------------------------------------------------- 初始化 */
  (async function init() {
    try {
      await loadAgents();
      await loadCanned();
      await loadSessions();
      setRealtimeBadge('connecting');
      connectRealtime();     // 建立实时通道；不再无条件启动轮询
    } catch (err) {
      toast('初始化失败：' + err.message, true);
      listEl.innerHTML = `<div class="empty">初始化失败：${esc(err.message)}</div>`;
      /* 初始化失败也要保证有兜底刷新 */
      degrade('初始化失败，已降级为轮询');
    }
  })();

  /* 供自测/CDP 断言使用：不影响业务逻辑 */
  window.__csRealtime = {
    state: () => WS.state,
    debug: () => ({
      state: WS.state,
      attempt: WS.attempt,
      degraded: WS.degraded,
      listPolling: !!listTimer,
      msgPolling: !!msgTimer,
      pollTicks: WS.pollTicks,
      badge: realtimeBadge.textContent,
      url: wsUrl(),
    }),
    retryDelays: () => [0, 1, 2, 3, 9].map((n) => ({ attempt: n, delay: nextRetryDelay(n) })),
    retryBase: WS_RETRY_BASE.slice(),
    retryMax: WS_RETRY_MAX,
    degradeAfter: WS_DEGRADE_AFTER,
    events: () => eventLog.slice(0, 12),
    assignHits: () => WS.assignHits || 0,
    myAgentId: () => agentId,
    myAgentName: () => (me() || {}).name,
  };
})();
