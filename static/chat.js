/* 用户会话页：提问 / 槽位澄清 / 转人工 / 满意度评价 / 答案溯源
   纯原生 JS，无框架依赖。

   消息传输：WebSocket 实时推送（主通道）+ 2.5 秒短轮询（降级通道）
   1. 会话就绪后连接 /ws/chat/<session_id>，服务端首帧下发 sync 全量快照；
   2. 后续 message / status / handoff / csat_request 事件即时更新 UI，页面不再轮询；
   3. 连接失败（浏览器不支持 / 连续 3 次连不上）时自动降级为轮询，
      同时后台继续指数退避重连，连上后自动切回实时推送；
   4. 所有消息（WS 推送、HTTP 增量同步）共用 renderedIds 去重，保证不重不漏。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const messagesEl = $('messages');
  const optListEl = $('opt-list');
  const suggestionsEl = $('suggestions');
  const inputEl = $('input');
  const formEl = $('chat-form');
  const sendBtn = $('btn-send');
  const sessionEl = $('session-id');
  const sessionBadge = $('session-badge');
  const serviceBadge = $('service-badge');
  const toastEl = $('toast');

  const TYPE_LABEL = {
    greeting: '寒暄', smalltalk: '闲聊', ending: '结束语',
    handoff: '转人工', handoff_trigger: '规则触发转人工', handoff_offline: '非服务时间',
    kb_exact: '知识库直接命中', kb_confirm: '命中并确认',
    kb_vector_confirm: '语义匹配并确认',
    kb_context_exact: '结合上文命中', kb_context_confirm: '结合上文确认',
    kb_slot_filled: '结合澄清结果作答', confirm_context: '上文确认',
    clarify_slot: '槽位澄清', fallback: '兜底', fallback_handoff_suggest: '兜底+建议转人工',
    waiting_agent: '人工接待中', queueing: '排队中', agent: '客服回复', system: '系统',
    session_closed: '会话已结束',
  };
  const CSAT_REASONS = ['答案不准确', '没解决我的问题', '回复太慢', '态度问题', '其他'];

  /* ---------------------------------------------------------- 状态变量 */
  let sessionId = localStorage.getItem('cs_session_id') || '';
  let visitorId = localStorage.getItem('cs_visitor_id') || '';
  let lastId = 0;                 // 已渲染的最大消息 id，重连补数据用
  let status = 'bot';
  let busy = false;
  let pendingClarify = null;
  let csatPending = false;
  let csatRated = false;
  let csatShown = false;          // 评价卡是否已在 DOM 中，避免重复插入
  let csatRequested = false;      // 服务端已确认需要评价
  // 向量召回的相似度明细（词面分 / 语义分）：只在 /api/chat 的响应里携带，
  // 消息本身不落库，因此先暂存下来、渲染对应机器回复时再拼进气泡元信息
  let lastMatchInfo = null;

  // 实时通道
  let ws = null;                  // 当前 WebSocket 实例
  let wsSessionId = '';           // 该连接对应的会话（换会话需要重连）
  let wsRetryTimer = null;        // 重连定时器
  let wsFailures = 0;             // 连续连接失败次数（>=3 触发降级）
  let wsManualClose = false;      // 主动关闭（换会话/卸载）时不触发重连
  let reconnectAttempts = 0;      // 已尝试的重连次数，用于指数退避
  let gotFirstSync = false;       // 是否已收到过首帧 sync（决定是否需要清屏全量渲染）
  let syncRequested = false;      // 是否已请求过"重连增量补数据"
  let wsOpenedAt = 0;             // 本次连接建立时间（用于识别"刚连上就断"的闪断）
  let wsDecayTimer = null;        // 连接稳定一段时间后清零失败计数
  let lastQueuePosition = 0;      // 最近一次拿到的排队位次
  let lastQueueFetched = 0;       // 已经为哪个位次补过 HTTP 请求
  let transport = 'ws';           // 'ws' 实时推送 | 'fallback' 轮询降级
  const renderedIds = new Set();  // 已渲染的消息 id —— WS 与 HTTP 共用的唯一去重依据
  const idWithScoreDetail = new Set();  // 已经拼过向量明细的消息 id，避免重复拼
  const renderedNodes = new Map();      // 消息 id → { meta, m }：用于把后到的相似度明细补到已渲染气泡上
  const pendingUser = [];         // 乐观渲染、等待服务端回执认领的用户气泡
  const MAX_WS_FAILURES = 3;      // 连续失败阈值（需求：3 次后降级）

  if (!visitorId) {
    visitorId = 'v-' + Math.random().toString(36).slice(2, 12);
    localStorage.setItem('cs_visitor_id', visitorId);
  }

  /* ---------------------------------------------------------- 工具 */
  function toast(text, isErr = false) {
    toastEl.textContent = text;
    toastEl.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.className = 'toast'), 2400);
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function showTyping(on) {
    let el = $('typing-indicator');
    if (on) {
      if (el) return;
      el = document.createElement('div');
      el.id = 'typing-indicator';
      el.className = 'sys-msg';
      el.textContent = '对方正在输入…';
      messagesEl.appendChild(el);
      scrollBottom();
    } else if (el) {
      el.remove();
    }
  }

  async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json' };
    const token = localStorage.getItem('cs_token');
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(path, { headers, ...options });
    if (!res.ok) {
      let detail = `请求失败 (${res.status})`;
      try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
      throw new Error(detail);
    }
    return res.json();
  }

  /* ---------------------------------------------------------- 渲染 */
  function bubbleFor(role) {
    const wrap = document.createElement('div');
    wrap.className = `msg ${role === 'user' ? 'user' : 'bot'}`;
    const avatar = document.createElement('div');
    avatar.className = 'avatar';
    avatar.textContent = role === 'user' ? '我' : (role === 'agent' ? '客' : 'AI');
    const body = document.createElement('div');
    body.className = 'bubble-wrap';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    body.appendChild(bubble);
    wrap.append(avatar, body);
    return { wrap, bubble, body };
  }

  /* 相似度文案：
     - /api/chat 带回词面分与语义分时显示「相似度 0.76（词面 0.37 / 语义 0.93）」，
       演示时能直接看出是语义（向量）召回把答案拉上来的；
     - 老接口 / 历史消息没有这两个字段时，保持原来的「相似度 0.xx」。 */
  function formatScoreDetail(m, info) {
    if (!m.score) return '';
    const base = `相似度 ${Number(m.score).toFixed(2)}`;
    if (!info) return base;
    const lex = Number(info.lex_score);
    const vec = Number(info.vec_score);
    if (!Number.isFinite(lex) || !Number.isFinite(vec)) return base;
    return `${base}（词面 ${lex.toFixed(2)} / 语义 ${vec.toFixed(2)}）`;
  }

  function metaHTMLOf(m) {
    const parts = [];
    const scoreDetail = (m.score_detail && m.score) ? m.score_detail : null;
    if (scoreDetail && m.id != null) idWithScoreDetail.add(m.id);
    if (m.kb_category) parts.push(`<span class="badge gray">${esc(m.kb_category)}</span>`);
    if (m.score) parts.push(`<span class="badge ok">${esc(formatScoreDetail(m, scoreDetail))}</span>`);
    if (m.context_used) parts.push('<span class="badge ctx">已结合上文</span>');
    if ((m.answer_type || '').startsWith('fallback')) parts.push('<span class="badge warn">兜底策略</span>');
    if (m.answer_type && TYPE_LABEL[m.answer_type]) {
      parts.push(`<span class="badge gray">${esc(TYPE_LABEL[m.answer_type])}</span>`);
    }
    if (m.agent) parts.push(`<span class="badge">${esc(m.agent)}</span>`);
    let html = parts.join('');

    // 答案溯源（PRD M3-3）
    const srcs = (m.source_details || []).filter((s) => s.question);
    if (srcs.length) {
      html += '<div class="src-line">来源：' + srcs.map((s) =>
        `<b>#${s.id}</b> ${esc(s.question)}${s.source_ref && s.source_ref !== '手工录入'
          ? `<span class="small">（${esc(s.source_ref)}）</span>` : ''}`).join('　') + '</div>';
    }
    return html;
  }

  function renderMessage(m) {
    if (m.role === 'system') {
      const el = document.createElement('div');
      el.className = 'sys-msg';
      el.textContent = m.content;
      messagesEl.appendChild(el);
      return;
    }
    const { wrap, bubble, body } = bubbleFor(m.role);
    bubble.textContent = m.content;
    // 向量召回明细：answer_type 可能带 kb_context_ 前缀，比对时统一去掉前缀
    if ((m.role === 'bot') && lastMatchInfo && !m.score_detail) {
      const a = String(m.answer_type || '').replace('kb_context_', '');
      if (a === lastMatchInfo.answer_type) {
        m.score_detail = lastMatchInfo;
        lastMatchInfo = null;
      }
    }
    const metaHTML = metaHTMLOf(m);
    if (metaHTML) {
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.innerHTML = metaHTML;
      body.appendChild(meta);
      if (m.id != null) renderedNodes.set(m.id, { meta, m });
    }
    if (m.answer_type === 'clarify_slot') {
      wrap.dataset.clarify = '1';
    }
    messagesEl.appendChild(wrap);
  }

  function scrollBottom() { messagesEl.scrollTop = messagesEl.scrollHeight; }

  /* ---------------------------------------------------------- 消息去重合并
     WS 推送与 HTTP 增量可能先后到达同一条消息，统一在这里按 id 去重。 */
  function mergeMessages(list) {
    if (!Array.isArray(list) || !list.length) return 0;
    const fresh = [];
    list.forEach((m) => {
      if (!m || m.id == null) return;
      if (renderedIds.has(m.id)) return;   // 已渲染过 → 丢弃，避免重复气泡
      renderedIds.add(m.id);
      lastId = Math.max(lastId, m.id);     // 断线重连补数据的游标
      fresh.push(m);
    });
    if (!fresh.length) return 0;
    fresh.sort((a, b) => a.id - b.id);
    fresh.forEach((m) => {
      // 自己的消息在发送时已经乐观渲染过了：这里用服务端回执「认领」那条气泡，
      // 而不是再建一个新的（否则会出现两个一模一样的气泡）
      if (m.role === 'user') {
        const idx = pendingUser.findIndex((p) => !p.confirmed && p.content === m.content);
        if (idx >= 0) {
          const p = pendingUser.splice(idx, 1)[0];
          p.confirmed = true;
          p.id = m.id;
          renderedNodes.set(m.id, { meta: null, m });
          return;
        }
      }
      renderMessage(m);
    });
    scrollBottom();
    return fresh.length;
  }

  /* 乐观渲染：发送瞬间就把用户自己的消息显示出来。
     为什么需要：实时模式下消息以 WS 流为准，若等回执再渲染，
     在慢网络或 WS 短暂断开时用户会以为"消息没发出去/记录丢了"。
     气泡随后会被服务端回执认领（见 mergeMessages），不会重复。 */
  function renderPendingUser(text) {
    const { wrap, bubble } = bubbleFor('user');
    bubble.textContent = text;
    messagesEl.appendChild(wrap);
    scrollBottom();
    const item = { content: text, wrap, bubble, confirmed: false, id: null };
    pendingUser.push(item);
    return item;
  }

  /* 发送失败时，在最后一条未被认领的乐观气泡上打标 */
  function markPendingFailed() {
    for (let i = pendingUser.length - 1; i >= 0; i -= 1) {
      const p = pendingUser[i];
      if (p.confirmed) continue;
      const body = p.wrap.querySelector('.bubble-wrap');
      if (body && !body.querySelector('.send-failed')) {
        const tip = document.createElement('div');
        tip.className = 'src-line send-failed';
        tip.style.color = 'var(--danger)';
        tip.textContent = '⚠️ 发送失败，请重试（这条消息没有送达客服）';
        body.appendChild(tip);
      }
      return;
    }
  }

  /* 把「本轮命中的相似度明细」补到已经在页面上的机器回复上。
     为什么需要：机器人消息是 WebSocket 先推、/api/chat 响应后到，
     渲染时明细还没拿到，这里按 answer_type 反向匹配最近的一条并就地刷新元信息。 */
  function applyMatchedInfo(info) {
    if (!info) return false;
    const ids = [...renderedIds].sort((a, b) => b - a);
    for (const id of ids) {
      const node = renderedNodes.get(id);
      if (!node || !node.m || node.m.role !== 'bot') continue;
      const a = String(node.m.answer_type || '').replace('kb_context_', '');
      if (a !== info.answer_type) continue;
      node.m.score_detail = info;
      const html = metaHTMLOf(node.m);
      if (html) node.meta.innerHTML = html;
      scrollBottom();
      return true;
    }
    return false;
  }

  function renderSuggestions(list, fallbackList) {
    suggestionsEl.innerHTML = '';
    const source = (list && list.length) ? list : (fallbackList || []);
    source.slice(0, 4).forEach((it) => {
      const chip = document.createElement('button');
      chip.className = 'chip';
      chip.type = 'button';
      chip.textContent = it.question;
      chip.onclick = () => { inputEl.value = it.question; send(); };
      suggestionsEl.appendChild(chip);
    });
  }

  function renderOptions(clarify) {
    optListEl.innerHTML = '';
    if (!clarify) return;
    clarify.options.forEach((opt) => {
      const btn = document.createElement('button');
      btn.className = 'opt-btn';
      btn.type = 'button';
      btn.textContent = opt.label;
      btn.onclick = () => { optListEl.innerHTML = ''; inputEl.value = opt.label; send(); };
      optListEl.appendChild(btn);
    });
  }

  function renderCsat() {
    if (csatRated || csatShown) return;    // 已评价 / 卡片已在页面上 → 不重复插入
    csatShown = true;
    optListEl.innerHTML = '';
    const card = document.createElement('div');
    card.className = 'csat-card';
    card.innerHTML = `
      <h4>请为本次服务评价</h4>
      <p>您的反馈会帮助我们持续改进</p>
      <div class="csat-actions">
        <button class="btn primary" data-score="1">👍 满意</button>
        <button class="btn" data-score="-1">👎 不满意</button>
      </div>
      <div class="csat-reasons" id="csat-reasons" style="display:none;"></div>`;
    optListEl.appendChild(card);
    card.querySelectorAll('[data-score]').forEach((btn) => {
      btn.onclick = async () => {
        const score = Number(btn.dataset.score);
        if (score === 1) { await submitCsat(1, '', ''); return; }
        const box = card.querySelector('#csat-reasons');
        box.style.display = 'flex';
        box.innerHTML = '';
        CSAT_REASONS.forEach((r) => {
          const b = document.createElement('button');
          b.className = 'chip';
          b.type = 'button';
          b.textContent = r;
          b.onclick = () => submitCsat(-1, r, '');
          box.appendChild(b);
        });
      };
    });
    scrollBottom();
  }

  async function submitCsat(score, reason, comment) {
    try {
      const res = await api(`/api/session/${sessionId}/csat`, {
        method: 'POST',
        body: JSON.stringify({ score, reason, comment }),
      });
      csatPending = false;
      csatRated = true;
      csatShown = false;
      optListEl.innerHTML = `<div class="csat-done">${esc(res.message)}</div>`;
      toast(res.message);
    } catch (err) { toast(err.message, true); }
  }

  /* ---------------------------------------------------------- 状态 */
  function updateStatusUI(label, cls) {
    sessionBadge.textContent = label;
    sessionBadge.className = `badge ${cls}`;
  }

  const STATUS_MAP = {
    bot: ['机器人接待中', 'ok'],
    queueing: ['转人工排队中', 'warn'],
    agent: ['人工接待中', 'ctx'],
    closed: ['会话已结束', 'gray'],
    timeout: ['已超时关闭', 'gray'],
  };

  function applyStatus(s) {
    status = s;
    const [label, cls] = STATUS_MAP[s] || [s, 'gray'];
    updateStatusUI(label, cls);
    const waiting = (s === 'queueing' || s === 'agent');
    $('btn-handoff').disabled = waiting;
    inputEl.placeholder = waiting
      ? '客服正在接待，您可以继续补充说明…'
      : '输入您的问题，例如：怎么退货';
    // 排队时：已有位次直接用；没有就补一次 HTTP（status 事件只带状态，位次在消息接口里）
    if (s === 'queueing') {
      if (lastQueuePosition) applyQueuePosition(lastQueuePosition);
      else refreshQueueBadge();
    } else {
      lastQueuePosition = 0;          // 已不在排队：清掉位次，避免迟到的响应把徽标改回排队
    }
    // 只有降级模式才需要轮询；实时模式下永远不轮询。
    // 降级期间不因状态变化停掉轮询：WS 不可用时轮询是唯一通道（keep-alive 也要靠它）
    if (transport === 'fallback') startPolling();
  }

  function applyQueuePosition(pos) {
    // 先把位次记下来：status 与 handoff 事件到达顺序不定，等状态变成 queueing 再显示；
    // 位次响应也可能迟到（此刻已接入人工/已结束），那种情况不改徽标
    if (!pos) return;
    lastQueuePosition = pos;
    if (status !== 'queueing') return;
    updateStatusUI(`转人工排队中（第 ${pos} 位）`, 'warn');
  }

  /* 拉一次会话消息接口，把「第 N 位」补到徽标上。
     同一位次只请求一次，避免 status 与 handoff 两条事件各触发一次 HTTP 请求。 */
  let queueFetching = false;
  function refreshQueueBadge() {
    if (queueFetching || lastQueueFetched === lastQueuePosition) return;
    queueFetching = true;
    lastQueueFetched = lastQueuePosition;
    httpSyncMessages()
      .catch(() => {})
      .finally(() => { queueFetching = false; });
  }

  /* ---------------------------------------------------------- 连接状态徽标
     index.html 不可改，这里用 JS 动态插入到顶部操作区左侧。
     复用 style.css 已有的 .dot.online / .dot.offline / .badge.gray。 */
  function ensureTransportBadge() {
    let el = $('rt-transport');
    if (el) return el;
    const host = document.querySelector('.topbar-actions');
    if (!host) return null;
    el = document.createElement('span');
    el.id = 'rt-transport';
    el.className = 'badge gray';
    el.title = '消息传输通道';
    el.innerHTML = '<i class="dot offline"></i><span id="rt-transport-text">实时连接中…</span>';
    host.insertBefore(el, host.firstChild);
    return el;
  }

  function setTransportUI(mode) {
    const el = ensureTransportBadge();
    if (!el) return;
    const dot = el.querySelector('.dot');
    const text = el.querySelector('#rt-transport-text');
    if (mode === 'ws') {
      el.className = 'badge ok';
      el.title = 'WebSocket 实时推送已连接';
      if (dot) dot.className = 'dot online';
      if (text) text.textContent = '实时（WebSocket）';
    } else if (mode === 'fallback') {
      el.className = 'badge gray';
      el.title = 'WebSocket 不可用，已降级为轮询';
      if (dot) dot.className = 'dot offline';
      if (text) text.textContent = '实时连接中断，已降级为轮询';
    } else {
      el.className = 'badge warn';
      el.title = '正在建立实时连接';
      if (dot) dot.className = 'dot busy';
      if (text) text.textContent = '实时连接中…';
    }
  }

  /* ---------------------------------------------------------- 轮询（降级通道）
     为什么需要降级：WebSocket 在部分企业网络/代理环境下会被拦截或握手失败。
     一旦连续 3 次连不上（或浏览器不支持），就退回原有的 2.5 秒增量轮询，
     保证功能可用；同时后台继续重连，连上后立刻切回实时推送并停掉轮询。 */
  let pollTimer = null;

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(() => {
      if (document.hidden) return;   // 页面隐藏时跳过一次，避免无意义的请求
      httpSyncMessages().catch(() => {});
    }, 2500);
  }
  function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
  }

  /* HTTP 增量同步：降级轮询 + 发送消息后的一次性补齐，共用 mergeMessages 去重 */
  async function httpSyncMessages() {
    const data = await api(`/api/session/${sessionId}/messages?after=${lastId}`);
    mergeMessages(data.messages || []);

    if (pendingClarify) { renderOptions(pendingClarify); pendingClarify = null; }
    if (data.csat_score == null && csatPending && !csatRated) renderCsat();
    if (data.status) applyStatus(data.status);
    applyQueuePosition(data.queue_position);
    return data;
  }

  /* 进入降级模式：开启轮询 + 显示灰色徽标 + 后台继续重连 */
  function enterFallback(reason) {
    if (transport !== 'fallback') {
      transport = 'fallback';
      setTransportUI('fallback');
      toast(reason + '，已降级为轮询（仍在后台尝试重连）', true);
    }
    startPolling();                   // 幂等：无论什么状态都保证轮询在跑
  }

  /* 切回实时模式：停掉轮询 + 绿色徽标 + 增量补数据 */
  function exitFallback() {
    const wasFallback = transport === 'fallback';
    transport = 'ws';
    stopPolling();
    setTransportUI('ws');
    if (wasFallback) toast('已恢复实时推送（WebSocket）');
  }

  /* ---------------------------------------------------------- WebSocket 实时通道 */
  function wsUrl() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    return `${proto}//${location.host}/ws/chat/${encodeURIComponent(sessionId)}`;
  }

  function sendWs(payload) {
    try {
      if (ws && ws.readyState === 1) { ws.send(JSON.stringify(payload)); return true; }
    } catch (e) { /* ignore */ }
    return false;
  }

  /* 指数退避：1s → 2s → 4s → 8s（上限 15s），带 ±20% 抖动避免惊群 */
  function backoffDelay() {
    const base = Math.min(1000 * Math.pow(2, reconnectAttempts), 15000);
    const jitter = base * 0.2 * (Math.random() * 2 - 1);
    return Math.max(300, Math.round(base + jitter));
  }

  function scheduleReconnect() {
    if (wsRetryTimer || wsManualClose) return;
    const delay = backoffDelay();
    reconnectAttempts += 1;
    wsRetryTimer = setTimeout(() => { wsRetryTimer = null; connectWs(); }, delay);
  }

  function connectWs() {
    if (!sessionId) return;                       // 会话还没就绪
    if (!('WebSocket' in window)) {               // 浏览器不支持 → 直接降级
      wsFailures = MAX_WS_FAILURES;
      enterFallback('当前浏览器不支持 WebSocket');
      return;
    }
    if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;  // 已在连接/已连接

    wsManualClose = false;
    wsSessionId = sessionId;
    gotFirstSync = false;
    syncRequested = false;
    wsOpenedAt = 0;                  // 本次尝试是否真的连上，由 onopen 记录
    // 降级期间保持灰点「已降级为轮询」提示，不要被每次重连尝试改回「连接中」
    if (transport !== 'fallback') setTransportUI('connecting');

    let sock;
    try {
      sock = new WebSocket(wsUrl());
    } catch (e) {
      onWsDown('WebSocket 创建失败：' + e.message);
      return;
    }
    ws = sock;

    sock.onopen = () => {
      reconnectAttempts = 0;
      wsOpenedAt = Date.now();
      exitFallback();                             // 停掉轮询、显示绿色「实时」徽标
      // 连接稳定 20 秒才算真正恢复，避免"连上就断"的闪断把失败计数清零
      clearTimeout(wsDecayTimer);
      wsDecayTimer = setTimeout(() => { wsFailures = 0; }, 20000);
      // 重连成功后主动请求 after=lastId 的增量：断线期间的消息一条都不丢。
      // （服务端首帧 sync 已是全量快照，这里再补一次增量，两条路径共用去重逻辑）
      if (!syncRequested) {
        syncRequested = true;
        sendWs({ type: 'sync', after: lastId });
      }
    };
    sock.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      try { handleWsEvent(msg); } catch (e) { toast('实时消息处理异常：' + e.message, true); }
    };
    sock.onerror = () => { /* 统一在 onclose 里处理，避免重复计数 */ };
    sock.onclose = (ev) => {
      const code = ev && ev.code;
      if (code === 1008 || code === 1013) {
        // 会话不存在 / 服务端关闭实时通道：重连也无意义，交给轮询兜底
        ws = null;
        if (!wsManualClose) enterFallback('实时通道不可用');
        return;
      }
      onWsDown('实时连接已断开');
    };
  }

  function onWsDown(reason) {
    ws = null;
    if (wsManualClose) return;
    // 本次连接稳定存活过 20 秒 → 说明网络确实恢复过，失败计数归零；
    // 否则「连上就断」的闪断会累积到阈值，触发轮询降级
    clearTimeout(wsDecayTimer);
    if (wsOpenedAt && Date.now() - wsOpenedAt >= 20000) {
      wsFailures = 0;
    } else {
      wsFailures += 1;
    }
    if (wsFailures >= MAX_WS_FAILURES) {
      enterFallback(reason);                      // 连续 3 次失败 → 轮询兜底
    } else {
      setTransportUI('connecting');
    }
    scheduleReconnect();                          // 无论哪种模式都继续尝试重连
  }

  function closeWs(manual = true) {
    wsManualClose = manual;
    if (wsRetryTimer) { clearTimeout(wsRetryTimer); wsRetryTimer = null; }
    clearTimeout(wsDecayTimer);
    const old = ws;
    ws = null;
    if (old) {
      old.onopen = old.onmessage = old.onerror = old.onclose = null;
      try { old.close(); } catch (e) { /* ignore */ }
    }
  }

  /* 换会话（新建会话）：重建实时连接 */
  function reconnectForSession() {
    closeWs(true);
    wsFailures = 0;
    reconnectAttempts = 0;
    connectWs();
  }

  /* ---------------------------------------------------------- WS 事件分发 */
  function handleWsEvent(msg) {
    switch (msg.type) {
      case 'sync':       handleSync(msg); break;
      case 'message':    handlePushMessage(msg.message); break;
      case 'status':     handleStatusEvent(msg); break;
      case 'handoff':    handleHandoffEvent(msg); break;
      case 'csat_request':
        csatPending = true;
        if (!csatRated) renderCsat();
        break;
      case 'ping':       /* 服务端心跳，客户端无需回包 */ break;
      case 'pong':       break;
      default:           break;
    }
  }

  function handlePushMessage(m) {
    if (!m) return;
    showTyping(false);                 // 收到任何真实消息都收起「正在输入」
    mergeMessages([m]);
  }

  function handleStatusEvent(msg) {
    if (msg.status) applyStatus(msg.status);
    if (msg.queue_position) {
      applyQueuePosition(msg.queue_position);
    } else if (msg.status === 'queueing') {
      refreshQueueBadge();             // status 事件不带位次：补一次 HTTP 增量
    }
  }

  function handleHandoffEvent(msg) {
    if (msg.offline) {
      toast('当前非人工服务时间，请留言或稍后再来');
    } else if (msg.assigned) {
      toast(msg.agent_name ? `已为您接入人工客服「${msg.agent_name}」` : '已为您接入人工客服');
    } else if (msg.queue_position) {
      applyQueuePosition(msg.queue_position);
      toast(`已转人工，正在排队（第 ${msg.queue_position} 位）`);
    } else {
      toast('已请求转人工，客服稍后接入');
    }
  }

  /* 首帧 sync = 服务端全量快照；重连后的 sync（after=lastId）= 增量补齐。
     全量时清屏重渲染，保证与后端一致且绝无重复气泡。 */
  function handleSync(msg) {
    const first = !gotFirstSync;
    gotFirstSync = true;
    const incoming = Array.isArray(msg.messages) ? msg.messages : [];

    if (first || !msg.after) {
      if (incoming.length) {
        // 服务端确实有历史消息：清空并全量重渲染（welcome 占位气泡一并清掉）
        messagesEl.innerHTML = '';
        renderedIds.clear();
      pendingUser.length = 0;
        lastId = 0;
        mergeMessages(incoming);
      } else if (first) {
        // 空快照（新会话）：保留本地欢迎气泡与快捷问题，不要清屏
        scrollBottom();
      } else {
        mergeMessages(incoming);
      }
    } else {
      mergeMessages(incoming);         // 增量：与已有气泡按 id 去重合并
    }

    if (msg.status) applyStatus(msg.status);
    applyQueuePosition(msg.queue_position);
    if (msg.clarify) { pendingClarify = msg.clarify; renderOptions(msg.clarify); }
    if (msg.status === 'closed' || msg.status === 'timeout') {
      if (msg.csat_score == null && !csatRated) { csatPending = true; renderCsat(); }
    }
  }

  /* ---------------------------------------------------------- 会话 */
  async function loadConfig() {
    try {
      const cfg = await api('/api/bot/config');
      $('bot-name').textContent = cfg.bot_name;
      $('bot-avatar').textContent = cfg.bot_avatar || 'AI';
      const svc = cfg.service || {};
      serviceBadge.textContent = svc.service_online
        ? `人工在线 · ${svc.online_agents} 位客服`
        : `非人工服务时间（${svc.service_hours_text}）`;
      serviceBadge.className = 'badge ' + (svc.service_online ? 'ok' : 'gray');
      return cfg;
    } catch (e) {
      serviceBadge.textContent = '服务不可用';
      serviceBadge.className = 'badge warn';
      return { welcome: '您好！', quick_questions: [] };
    }
  }

  async function ensureSession(force = false) {
    if (force) { sessionId = ''; localStorage.removeItem('cs_session_id'); }
    if (sessionId) {
      try {
        const data = await api(`/api/session/${sessionId}`);
        sessionEl.textContent = sessionId;
        applyStatus(data.session.status);
        return data.session;
      } catch (e) { sessionId = ''; }
    }
    const data = await api('/api/session', {
      method: 'POST',
      body: JSON.stringify({ visitor_id: visitorId }),
    });
    sessionId = data.session.id;
    localStorage.setItem('cs_session_id', sessionId);
    sessionEl.textContent = sessionId;
    lastId = 0;
    applyStatus(data.session.status);
    return data.session;
  }

  /* ---------------------------------------------------------- 发送 */
  async function send() {
    const text = inputEl.value.trim();
    if (!text || busy) return;
    busy = true;
    sendBtn.disabled = true;
    inputEl.value = '';
    inputEl.style.height = 'auto';
    optListEl.innerHTML = '';
    csatShown = false;
    suggestionsEl.innerHTML = '';
    lastMatchInfo = null;               // 上一轮的相似度明细不跨轮沿用
    showTyping(true);                   // 「正在输入」指示，收到 WS 消息后移除
    renderPendingUser(text);            // 乐观渲染：立刻显示自己的消息，避免"发出去没反应"的错觉

    try {
      if (status === 'closed' || status === 'timeout') {
        await ensureSession(true);
        messagesEl.innerHTML = '';
        renderedIds.clear();
      pendingUser.length = 0;
        lastId = 0;
        putWelcome({ welcome: WELCOME_TEXT });   // 新会话先放欢迎气泡，sync 全量到达后替换
        reconnectForSession();
      }
      const data = await api('/api/chat', {
        method: 'POST',
        body: JSON.stringify({ session_id: sessionId, message: text, visitor_id: visitorId }),
      });
      // 兜底：某些接口会直接把本轮消息带回来，先去重渲染，再补一次增量同步
      mergeMessages(data.messages || []);
      // 记录本轮命中的相似度明细（词面分 / 语义分），供本条机器回复展示
      const matched = data.matched || {};
      if (matched.lex_score != null || matched.vec_score != null) {
        lastMatchInfo = {
          answer_type: String(data.answer_type || '').replace('kb_context_', ''),
          lex_score: matched.lex_score,
          vec_score: matched.vec_score,
          vector_only: !!matched.vector_only,
        };
        // WS 推送先到时气泡已经渲染完了，这里把明细补上去
        if (applyMatchedInfo(lastMatchInfo)) lastMatchInfo = null;
      }
      if (data.clarify) pendingClarify = data.clarify;
      if (data.csat_requested) csatPending = true;
      if (data.suggestions && data.suggestions.length) {
        renderSuggestions(data.suggestions, []);
      }
      // 每次发送后补一次增量同步（单次请求，不是轮询）：
      // 它是「WS 推送丢失 / 回执乱序」时的兜底，保证用户一定能看到自己这轮的消息与回答。
      // 与 WS 共用 renderedIds 去重，重复拉到也不会出现双气泡。
      try { await httpSyncMessages(); } catch (e) { /* 兜底失败不打断主流程 */ }
      if (data.session_status) applyStatus(data.session_status);
      if (!data.suggestions || !data.suggestions.length) renderSuggestions([], []);
      // WS 正常时机器人回复会立刻推送过来；若迟迟未到则收起指示器
      setTimeout(() => showTyping(false), 1500);
    } catch (err) {
      showTyping(false);
      markPendingFailed();          // 失败要标在气泡上，不能让用户以为已经发出去了
      toast(err.message, true);
    } finally {
      busy = false;
      sendBtn.disabled = false;
      inputEl.focus();
    }
  }

  /* ---------------------------------------------------------- 事件 */
  formEl.addEventListener('submit', (e) => { e.preventDefault(); send(); });
  inputEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
  });
  inputEl.addEventListener('input', () => {
    inputEl.style.height = 'auto';
    inputEl.style.height = `${Math.min(inputEl.scrollHeight, 120)}px`;
  });

  $('btn-handoff').addEventListener('click', async () => {
    try {
      const res = await api(`/api/session/${sessionId}/handoff`, { method: 'POST' });
      toast(res.offline ? '当前非人工服务时间' : '已请求转人工');
      await httpSyncMessages();
      // handoff 结果里带位次且未被分配/离线时，确保徽标显示排队位次
      if (!res.offline && !res.assigned && res.queue_position) {
        applyQueuePosition(res.queue_position);
      }
    } catch (err) { toast(err.message, true); }
  });

  $('btn-close').addEventListener('click', async () => {
    try {
      await api(`/api/session/${sessionId}/close`, {
        method: 'POST', body: JSON.stringify({ reason: 'user' }),
      });
      csatPending = true;
      csatRated = false;
      csatShown = false;
      csatRequested = true;
      await httpSyncMessages();
      renderCsat();                     // 不依赖 WS 也能立即出现评价卡（WS 到达时会被去重）
      toast('会话已结束');
    } catch (err) { toast(err.message, true); }
  });

  $('btn-clear').addEventListener('click', async () => {
    messagesEl.innerHTML = '';
    optListEl.innerHTML = '';
    suggestionsEl.innerHTML = '';
    renderedIds.clear();
      pendingUser.length = 0;
    lastId = 0;
    csatPending = false;
    csatRated = false;
    csatShown = false;
    const session = await ensureSession(true);
    putWelcome(session);
    reconnectForSession();
    toast('已新建会话');
  });

  function putWelcome(session) {
    const cfgText = session && session.welcome ? session.welcome : WELCOME_TEXT;
    const { wrap, bubble } = bubbleFor('bot');
    bubble.textContent = cfgText;
    messagesEl.appendChild(wrap);
    renderSuggestions([], QUICK);
    scrollBottom();
  }

  let QUICK = [];
  let WELCOME_TEXT = '您好！我是智能客服助手 😊';

  /* ---------------------------------------------------------- 初始化 */
  (async function init() {
    setTransportUI('connecting');
    const cfg = await loadConfig();
    QUICK = (cfg.quick_questions || []).map((q) => ({ question: q }));
    WELCOME_TEXT = cfg.welcome || WELCOME_TEXT;
    try {
      const session = await ensureSession();
      const data = await api(`/api/session/${sessionId}`);
      // 先建立实时连接：首帧 sync 会全量渲染消息与状态
      connectWs();
      if (data.session.turn_count > 0) {
        pendingClarify = data.clarify || null;
        if (pendingClarify) renderOptions(pendingClarify);
      } else {
        putWelcome({ welcome: WELCOME_TEXT });
      }
      applyStatus(data.session.status);
      applyQueuePosition(data.queue_position);
    } catch (e) {
      putWelcome({ welcome: WELCOME_TEXT });
      toast('无法连接后端：' + e.message, true);
    }
    inputEl.focus();
  })();

  // 关闭页面时主动断开，避免服务端堆积死连接
  window.addEventListener('beforeunload', () => closeWs(true));
})();
