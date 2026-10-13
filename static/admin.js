/* ============================================================================
 * AI 智能客服 MVP · 运营后台前端逻辑（纯原生 JS：无框架、无构建、无外部 CDN）
 *
 * 七个页签：知识库 / 文档导入 / 会话记录 / 未解决问题 / 机器人设置 / 触发规则 / 客服与账号
 * 所有请求前缀 /api，字段名与后端 app/routers/*.py 实际返回保持一致（已逐一实测）。
 * 安全：任何来自接口的文本在插入 HTML 前都必须经过 esc()，防止知识库内容注入。
 * ==========================================================================*/
(function () {
  'use strict';

  /* ======================================================================== */
  /* 0. 通用工具                                                              */
  /* ======================================================================== */

  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  /** HTML 转义工具：所有插值内容必须经过它 */
  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** 文本截断（先转成字符串，避免 null） */
  function clip(text, max) {
    const s = String(text === null || text === undefined ? '' : text);
    return s.length > max ? s.slice(0, max) + '…' : s;
  }

  /** 单行折叠展示（换行转空格），用于表格里的长文本 */
  function oneline(text) {
    return String(text === null || text === undefined ? '' : text).replace(/\s+/g, ' ').trim();
  }

  const dash = (value) => (value === null || value === undefined || value === '' ? '—' : value);

  /** 轻提示 */
  const toastEl = $('#toast');
  function toast(message, isError) {
    toastEl.textContent = message;
    toastEl.className = 'toast show' + (isError ? ' err' : '');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => { toastEl.className = 'toast'; }, isError ? 3600 : 2400);
  }

  /** 统一请求封装：
   *  - 自动带 Content-Type: application/json（FormData 除外，交给浏览器设 boundary）
   *  - 自动附带 localStorage.cs_token 的 Bearer 头
   *  - 401 → 跳转登录页；非 2xx → 抛出后端 detail 文案
   */
  async function api(path, options) {
    const opts = Object.assign({}, options || {});
    const headers = Object.assign({}, opts.headers || {});
    const isForm = typeof FormData !== 'undefined' && opts.body instanceof FormData;
    if (!isForm) headers['Content-Type'] = 'application/json';
    const token = localStorage.getItem('cs_token');
    if (token) headers['Authorization'] = 'Bearer ' + token;
    opts.headers = headers;

    let res;
    try {
      res = await fetch(path, opts);
    } catch (err) {
      throw new Error('网络请求失败，请确认本地服务是否还在运行');
    }

    if (res.status === 401) {
      toast('登录已过期，正在跳转登录页…', true);
      setTimeout(() => { location.href = '/login'; }, 600);
      const e401 = new Error('未登录或登录已过期');
      e401.status = 401;
      throw e401;
    }

    const raw = await res.text();
    let data = null;
    if (raw) { try { data = JSON.parse(raw); } catch (err) { data = null; } }

    if (!res.ok) {
      let detail = (data && data.detail) || ('请求失败（HTTP ' + res.status + '）');
      // FastAPI 校验错误 detail 是数组
      if (Array.isArray(detail)) {
        detail = detail.map((d) => (d && (d.msg || d.message)) || JSON.stringify(d)).join('；');
      } else if (typeof detail !== 'string') {
        detail = JSON.stringify(detail);
      }
      const error = new Error(detail);
      error.status = res.status;
      throw error;
    }
    return data;
  }

  /** 请求中禁用按钮（带文案切换），结束后恢复 */
  async function withBusy(button, task, busyText) {
    const original = button ? button.textContent : '';
    if (button) {
      button.disabled = true;
      if (busyText) button.textContent = busyText;
    }
    try {
      return await task();
    } finally {
      if (button) {
        button.disabled = false;
        button.textContent = original;
      }
    }
  }

  /** 加载失败时，把当前页签里仍停留在「加载中…」的占位换成错误文案（避免界面卡在加载态） */
  function clearLoadingState(err) {
    const pane = $('.tab-pane.active');
    if (!pane) return;
    const message = (err && err.message) || '加载失败';
    $$('tbody', pane).forEach((tbody) => {
      if (tbody.textContent.indexOf('加载中') < 0) return;
      const table = tbody.closest('table');
      const cols = table ? table.querySelectorAll('thead th').length : 1;
      tbody.innerHTML = '<tr><td colspan="' + (cols || 1) + '"><div class="empty">加载失败：' +
        esc(message) + '</div></td></tr>';
    });
    $$('.empty', pane).forEach((el) => {
      if (el.textContent === '加载中…') el.textContent = '加载失败：' + message;
    });
  }

  /** 统一错误提示：403 单独给「无权访问」文案（知识运营/客服等角色受限） */
  function reportError(err) {
    clearLoadingState(err);
    if (err && err.status === 403) {
      toast('当前角色无权访问：' + err.message, true);
      return;
    }
    toast((err && err.message) || '操作失败', true);
  }

  function tableLoading(tbody, cols) {
    tbody.innerHTML = '<tr><td colspan="' + cols + '"><div class="empty">加载中…</div></td></tr>';
  }

  function toggleEmpty(tbody, emptyEl, isEmpty, message) {
    if (isEmpty) {
      tbody.innerHTML = '';
      if (message) emptyEl.textContent = message;
      emptyEl.style.display = 'block';
    } else {
      emptyEl.style.display = 'none';
    }
  }

  /* 弹窗控制 */
  function showModal(id) { $('#' + id).classList.add('show'); }
  function hideModal(id) { $('#' + id).classList.remove('show'); }
  function bindModalDismiss(maskId, closeIds) {
    const mask = $('#' + maskId);
    mask.addEventListener('click', (e) => { if (e.target === mask) hideModal(maskId); });
    closeIds.forEach((id) => $('#' + id).addEventListener('click', () => hideModal(maskId)));
  }
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    $$('.modal-mask.show').forEach((m) => m.classList.remove('show'));
  });

  /* ======================================================================== */
  /* 1. 全局状态与常量                                                        */
  /* ======================================================================== */

  const state = {
    authEnabled: false,
    account: null,
    roles: [],
    thresholds: { high_confidence: null, mid_confidence: null },
    slots: [],            // [{id,name}] 来自 /api/knowledge
    categories: [],
    kbItems: [],
    docs: [],
    currentDocId: null,
    currentDoc: null,
    sessions: [],
    fallbacks: [],
    fallbackCounter: {},
    botConfig: null,
    embedding: null,      // /api/admin/embedding 最近一次状态（向量召回面板）
    accounts: [],
    loadedTabs: {},
  };

  /* 后端 handoff.describe_trigger 的同一套映射（会话列表接口不返回 label） */
  const TRIGGER_LABELS = {
    user_request: '用户主动要求转人工',
    fallback_count: '连续兜底未解决',
    sentiment: '负面情绪触发',
    risky_keyword: '高风险关键词触发',
    turn_limit: '会话轮次超限',
    clarify_limit: '澄清轮次超限',
    agent_timeout: '客服长时间未回复',
  };

  const RESOLVED_LABELS = { bot: '机器人解决', agent: '人工解决', unresolved: '未解决' };

  const STATUS_BADGE = {
    bot: 'badge',
    queueing: 'badge warn',
    agent: 'badge ctx',
    closed: 'badge gray',
    timeout: 'badge gray',
  };

  const DOC_STATUS = { draft: '草稿', published: '已发布', disabled: '已下架' };
  const DOC_STATUS_BADGE = { draft: 'badge warn', published: 'badge ok', disabled: 'badge gray' };

  const WEEKDAYS = [['1', '周一'], ['2', '周二'], ['3', '周三'], ['4', '周四'], ['5', '周五'], ['6', '周六'], ['0', '周日']];

  const MSG_ROLE = { user: '访客', bot: '机器人', agent: '客服', system: '系统' };

  const ruleName = (type) => TRIGGER_LABELS[type] || type || '—';

  /* ======================================================================== */
  /* 2. 页签切换                                                              */
  /* ======================================================================== */

  const tabLoaders = {
    kb: () => loadKnowledge(),
    docs: () => loadDocuments(),
    sessions: () => loadSessions(),
    fallbacks: () => loadFallbacks(),
    bot: () => loadBotConfig().then(() => loadEmbeddingStatus()),
    rules: () => loadRules(),
    agents: () => loadAgents(),
  };

  function switchTab(name, skipLoad) {
    $$('#tabs .tab').forEach((btn) => btn.classList.toggle('active', btn.dataset.tab === name));
    $$('.tab-pane').forEach((pane) => pane.classList.toggle('active', pane.id === 'pane-' + name));
    const loader = tabLoaders[name];
    if (!loader || skipLoad) return;
    Promise.resolve()
      .then(loader)
      .then(() => { state.loadedTabs[name] = true; })
      .catch(reportError);
  }

  function bindTabs() {
    $('#tabs').addEventListener('click', (e) => {
      const btn = e.target.closest('.tab');
      if (btn && btn.dataset.tab) switchTab(btn.dataset.tab);
    });
  }

  /* ======================================================================== */
  /* 3. 顶栏：健康检查 / 认证状态                                             */
  /* ======================================================================== */

  async function loadHealth() {
    const health = await api('/api/health');
    const service = health.service || {};
    state.thresholds = health.thresholds || state.thresholds;

    // 向量召回状态 /api/health 也会下发：先渲染一版，切到「机器人设置」时再拉最新
    if (health.embedding) {
      state.embedding = health.embedding;
      renderEmbeddingStatus(health.embedding);
    }

    $('#kb-count').textContent = health.knowledge_count;
    $('#doc-count').textContent = health.document_count;
    $('#badge-knowledge').textContent = '知识 ' + health.knowledge_count + ' 条';
    $('#badge-knowledge').className = 'badge ok';
    $('#service-hours-text').textContent = dash(service.service_hours_text);
    $('#badge-threshold').textContent = '阈值 ' + health.thresholds.high_confidence + ' / ' + health.thresholds.mid_confidence;
    $('#badge-threshold').className = 'badge gray';
    $('#badge-threshold').title = '直接作答 ≥ ' + health.thresholds.high_confidence +
      '；作答+确认 ≥ ' + health.thresholds.mid_confidence +
      '（大模型：' + (health.llm_enabled ? '已启用' : '未配置') + '）';

    const online = !!service.service_online;
    const queue = service.queue_count || 0;
    const badge = $('#badge-service');
    badge.textContent = (online ? '人工在线' : '非服务时间') +
      ' · 在线客服 ' + (service.online_agents || 0) + (queue ? ' · 排队 ' + queue : '');
    badge.className = online ? (queue > 0 ? 'badge warn' : 'badge ok') : 'badge warn';
    badge.title = '服务时段：' + (service.service_hours_text || '—') +
      '；接受转人工：' + (service.accepting ? '是' : '否');
  }

  async function loadAuth() {
    const status = await api('/api/auth/status');
    state.authEnabled = !!status.auth_enabled;
    state.roles = status.roles || [];
    const accountBadge = $('#badge-account');
    const logout = $('#btn-logout');
    if (!state.authEnabled) {
      accountBadge.style.display = 'none';
      logout.style.display = 'none';
      return;
    }
    try {
      const me = await api('/api/auth/me');
      state.account = me.account || null;
      if (state.account) {
        accountBadge.textContent = state.account.name + '（' + (state.account.role_label || state.account.role) + '）';
        accountBadge.style.display = '';
      }
    } catch (err) {
      /* 401 已由 api() 处理跳转 */
    }
    logout.style.display = '';
    logout.addEventListener('click', () => {
      localStorage.removeItem('cs_token');
      location.href = '/login';
    });
  }

  /* ======================================================================== */
  /* 4. 页签一：知识库                                                        */
  /* ======================================================================== */

  function slotName(slotId) {
    const hit = state.slots.find((s) => s.id === slotId);
    return hit ? hit.name : slotId;
  }

  function renderCategoryOptions(selectEl, categories, allLabel) {
    const keep = selectEl.value;
    selectEl.innerHTML = '<option value="">' + esc(allLabel) + '</option>' +
      categories.map((c) => '<option value="' + esc(c) + '">' + esc(c) + '</option>').join('');
    if (categories.indexOf(keep) >= 0) selectEl.value = keep;
  }

  function renderCategoryDatalist(categories) {
    $('#cat-list').innerHTML = categories.map((c) => '<option value="' + esc(c) + '"></option>').join('');
  }

  function renderSlotOptions() {
    const select = $('#kb-f-slot');
    const keep = select.value;
    select.innerHTML = '<option value="">不需要澄清</option>' +
      state.slots.map((s) => '<option value="' + esc(s.id) + '">' + esc(s.name) + '</option>').join('');
    select.value = keep;
  }

  /** 知识库列表（搜索 250ms 防抖由事件绑定负责） */
  async function loadKnowledge() {
    const tbody = $('#kb-tbody');
    tableLoading(tbody, 9);
    const params = new URLSearchParams();
    const q = $('#kb-search').value.trim();
    const category = $('#kb-category').value;
    const enabled = $('#kb-enabled').value;
    if (q) params.set('q', q);
    if (category) params.set('category', category);
    if (enabled) params.set('enabled', enabled);

    const data = await api('/api/knowledge?' + params.toString());
    state.kbItems = data.items || [];
    state.categories = data.categories || [];
    state.slots = data.slots || [];

    renderCategoryOptions($('#kb-category'), state.categories, '全部分类');
    renderCategoryDatalist(state.categories);
    renderSlotOptions();

    $('#kb-tip').textContent = '共 ' + data.total + ' 条' +
      (q || category || enabled ? '（已筛选）' : '') + ' · 修改即时生效，机器人下一次回答就会用到';

    if (!state.kbItems.length) {
      toggleEmpty(tbody, $('#kb-empty'), true, '没有符合条件的数据');
      return;
    }
    toggleEmpty(tbody, $('#kb-empty'), false);
    tbody.innerHTML = state.kbItems.map((item) => {
      const similar = (item.similar || []);
      const keywords = (item.keywords || []);
      return '<tr>' +
        '<td class="small">' + item.id + '</td>' +
        '<td><span class="badge gray">' + esc(item.category) + '</span></td>' +
        '<td class="q">' + esc(item.question) +
          '<div class="small mono">' + esc(item.source_ref || '') + '</div></td>' +
        '<td class="ans ans-cell"><span class="cell-clip" title="' + esc(oneline(item.answer)) + '">' +
          esc(clip(oneline(item.answer), 46)) + '</span></td>' +
        '<td class="small">' + (similar.length
          ? '<span title="' + esc(similar.join('\n')) + '">' + esc(clip(similar.join(' / '), 18)) + '</span>'
          : '—') + '</td>' +
        '<td class="small">' + (keywords.length
          ? '<span title="' + esc(keywords.join('、')) + '">' + esc(clip(keywords.join('、'), 14)) + '</span>'
          : '—') + '</td>' +
        '<td>' + (item.slot
          ? '<span class="tag slot">' + esc(slotName(item.slot)) + '</span>'
          : '<span class="small">—</span>') + '</td>' +
        '<td>' + (item.enabled
          ? '<span class="badge ok">启用</span>'
          : '<span class="badge gray">停用</span>') + '</td>' +
        '<td><div class="ops">' +
          '<button class="btn sm" data-act="edit" data-id="' + item.id + '">编辑</button>' +
          '<button class="btn sm danger" data-act="del" data-id="' + item.id + '">删除</button>' +
        '</div></td>' +
      '</tr>';
    }).join('');
  }

  /** 知识条目弹窗（新增 / 编辑共用；文档切片也复用） */
  function openKbModal(item) {
    const isEdit = !!(item && item.id);
    $('#kb-modal-title').textContent = isEdit ? ('编辑知识条目 #' + item.id) : '新增知识条目';
    $('#kb-f-id').value = isEdit ? item.id : '';
    $('#kb-f-question').value = item ? item.question : '';
    $('#kb-f-answer').value = item ? item.answer : '';
    $('#kb-f-category').value = item ? item.category : '';
    $('#kb-f-similar').value = item ? (item.similar || []).join('\n') : '';
    $('#kb-f-keywords').value = item ? (item.keywords || []).join(', ') : '';
    $('#kb-f-enabled').value = item ? String(!!item.enabled) : 'true';
    renderSlotOptions();
    $('#kb-f-slot').value = item && item.slot ? item.slot : '';
    $('#kb-f-source').textContent = item && item.source_ref
      ? ('溯源：' + item.source_ref + (item.doc_id ? '（文档 #' + item.doc_id + ' 第 ' + item.chunk_index + ' 片）' : ''))
      : '';
    $('#kb-f-question').disabled = false;
    showModal('kb-modal-mask');
    setTimeout(() => $('#kb-f-question').focus(), 50);
  }

  async function saveKnowledge() {
    const id = $('#kb-f-id').value;
    const isEdit = !!id;
    const question = $('#kb-f-question').value.trim();
    const answer = $('#kb-f-answer').value.trim();
    const slotValue = $('#kb-f-slot').value;
    if (!question || !answer) {
      toast('标准问与标准答不能为空', true);
      return;
    }
    const payload = {
      question: question,
      answer: answer,
      category: $('#kb-f-category').value.trim() || '未分类',
      similar: $('#kb-f-similar').value.split('\n').map((s) => s.trim()).filter(Boolean),
      keywords: $('#kb-f-keywords').value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
      // 说明：后端 PUT 会忽略 null（不清空），因此编辑时用空串清除槽位
      slot: slotValue ? slotValue : (isEdit ? '' : null),
      enabled: $('#kb-f-enabled').value === 'true',
    };

    await withBusy($('#kb-save'), async () => {
      try {
        const res = isEdit
          ? await api('/api/knowledge/' + encodeURIComponent(id), { method: 'PUT', body: JSON.stringify(payload) })
          : await api('/api/knowledge', { method: 'POST', body: JSON.stringify(payload) });
        hideModal('kb-modal-mask');
        toast(res.message || '保存成功');
        await loadKnowledge();
        await loadHealth();
        if (state.currentDocId) await refreshDocDetail();
        // 用刚保存的标准问跑一次检索测试，直观证明「即时生效」
        $('#kt-query').value = question;
        switchTab('kb', true);   // 切到知识库页签（列表已在上面刷新，避免重复请求）
        await runKnowledgeTest();
      } catch (err) {
        reportError(err);
      }
    }, '保存中…');
  }

  async function deleteKnowledge(id) {
    if (!confirm('确认删除知识条目 #' + id + '？删除后该条立即失效。')) return;
    try {
      const res = await api('/api/knowledge/' + encodeURIComponent(id), { method: 'DELETE' });
      toast(res.message || '已删除');
      await loadKnowledge();
      await loadHealth();
      if (state.currentDocId) await refreshDocDetail();
    } catch (err) {
      reportError(err);
    }
  }

  const LEVEL_BADGE = {
    '直接作答': '<span class="badge ok">直接作答</span>',
    '作答+确认': '<span class="badge warn">作答+确认</span>',
    '进入兜底': '<span class="badge gray">进入兜底</span>',
  };

  /** 渲染检索候选（知识库检索测试；文档预检复用，但没有 level 字段） */
  function renderCandidates(candidates, opts) {
    const withLevel = !(opts && opts.noLevel);
    if (!candidates.length) {
      return '<div class="small">没有命中任何候选，机器人将使用兜底话术。</div>';
    }
    return candidates.map((c) => {
      const score = typeof c.score === 'number' ? c.score.toFixed(4) : esc(c.score);
      const level = withLevel
        ? (LEVEL_BADGE[c.level] || '<span class="badge gray">' + esc(c.level || '—') + '</span>')
        : '';
      const clarify = c.clarify
        ? '<span class="tag slot">需先澄清：' + esc(c.clarify) + '</span>'
        : '';
      const slot = c.slot ? '<span class="tag slot">绑定槽位 ' + esc(slotName(c.slot)) + '</span>' : '';
      return '<div class="item">' +
        '<div class="hit">' +
          '<b>#' + c.id + '</b> ' + esc(c.question) + ' ' + clarify + slot +
          '<div class="small">分类 ' + esc(c.category) + ' · 命中问法：' + esc(c.matched_surface) +
            ' · 溯源：' + esc(c.source_ref || '—') + '</div>' +
          '<div class="small">正文：' + esc(clip(oneline(c.answer), 110)) + '</div>' +
        '</div>' +
        '<div class="score"><b>' + score + '</b><div style="margin-top:4px;">' + level + '</div></div>' +
      '</div>';
    }).join('');
  }

  async function runKnowledgeTest() {
    const query = $('#kt-query').value.trim();
    if (!query) { toast('请先输入测试问题', true); return; }
    const box = $('#kt-result');
    box.innerHTML = '<div class="small">检索中…</div>';
    await withBusy($('#kt-run'), async () => {
      try {
        const data = await api('/api/knowledge/test', {
          method: 'POST',
          body: JSON.stringify({ query: query, top_k: 5 }),
        });
        const thr = data.thresholds || {};
        box.innerHTML =
          '<div class="small thr-line">归一化：<code>' + esc(data.normalized_query) + '</code> · ' +
          '阈值：直接作答 ≥ ' + esc(thr.high) + '，作答+确认 ≥ ' + esc(thr.mid) + '，低于则进入兜底</div>' +
          renderCandidates(data.candidates || []);
      } catch (err) {
        box.innerHTML = '<div class="small">测试失败：' + esc(err.message) + '</div>';
        reportError(err);
      }
    }, '测试中…');
  }

  function bindKnowledge() {
    $('#kt-run').addEventListener('click', runKnowledgeTest);
    $('#kt-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') runKnowledgeTest(); });

    let timer = null;
    $('#kb-search').addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => loadKnowledge().catch(reportError), 250);
    });
    $('#kb-category').addEventListener('change', () => loadKnowledge().catch(reportError));
    $('#kb-enabled').addEventListener('change', () => loadKnowledge().catch(reportError));

    $('#kb-new').addEventListener('click', () => openKbModal(null));
    $('#kb-save').addEventListener('click', saveKnowledge);

    $('#kb-reset').addEventListener('click', async () => {
      if (!confirm('将清空现有知识库与文档，并重新导入种子数据，确认继续？')) return;
      await withBusy($('#kb-reset'), async () => {
        try {
          const res = await api('/api/knowledge/reset-seed', { method: 'POST' });
          toast(res.message || '已重置为种子数据');
          state.currentDocId = null;
          state.currentDoc = null;
          $('#doc-detail').style.display = 'none';
          await loadKnowledge();
          await loadHealth();
          await loadDocuments();
        } catch (err) {
          reportError(err);
        }
      }, '重置中…');
    });

    $('#kb-tbody').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const id = btn.dataset.id;
      if (btn.dataset.act === 'edit') {
        const item = state.kbItems.find((i) => String(i.id) === String(id));
        if (item) openKbModal(item);
      } else if (btn.dataset.act === 'del') {
        deleteKnowledge(id);
      }
    });
  }

  /* ======================================================================== */
  /* 5. 页签二：文档导入                                                      */
  /* ======================================================================== */

  async function loadDocuments() {
    const tbody = $('#doc-list-tbody');
    tableLoading(tbody, 7);
    const data = await api('/api/documents');
    state.docs = data.items || [];
    if (!state.docs.length) {
      toggleEmpty(tbody, $('#doc-list-empty'), true, '还没有导入过文档');
      return;
    }
    toggleEmpty(tbody, $('#doc-list-empty'), false);
    tbody.innerHTML = state.docs.map((doc) => {
      const statusKey = doc.status || 'draft';
      return '<tr>' +
        '<td class="small">' + doc.id + '</td>' +
        '<td class="q">' + esc(doc.title) +
          '<div class="small mono">' + esc(doc.source_name || '—') + '</div></td>' +
        '<td class="small">' + esc(doc.source_type || 'text') + '</td>' +
        '<td><span class="' + (DOC_STATUS_BADGE[statusKey] || 'badge gray') + '">' +
          esc(DOC_STATUS[statusKey] || statusKey) + '</span></td>' +
        '<td class="small">' + dash(doc.chunk_count) + '</td>' +
        '<td class="small">' + esc(doc.updated_at || '') + '</td>' +
        '<td><button class="btn sm" data-act="open" data-id="' + doc.id + '">查看</button></td>' +
      '</tr>';
    }).join('');
  }

  /** 打开文档详情：切片预览 + 召回预检 + 发布/下架/删除 */
  async function openDocument(docId) {
    state.currentDocId = docId;
    $('#doc-detail').style.display = '';
    $('#doc-chunks').innerHTML = '<tr><td colspan="6"><div class="empty">加载中…</div></td></tr>';
    $('#doc-test-result').innerHTML = '';
    await refreshDocDetail();
    $('#doc-detail').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  async function refreshDocDetail() {
    const docId = state.currentDocId;
    if (!docId) return;
    const data = await api('/api/documents/' + encodeURIComponent(docId));
    state.currentDoc = data.document;
    const doc = data.document || {};
    const chunks = data.chunks || [];
    const statusKey = doc.status || 'draft';

    $('#doc-detail-title').textContent = '#' + doc.id + ' ' + doc.title;
    $('#doc-detail-meta').textContent =
      '状态 ' + (DOC_STATUS[statusKey] || statusKey) +
      ' · 切片 ' + chunks.length + ' 片（已发布 ' + (doc.published_count || 0) + ' 片）' +
      ' · 来源 ' + (doc.source_name || '—') +
      ' · 更新于 ' + (doc.updated_at || '—');
    $('#doc-publish-on').disabled = false;
    $('#doc-publish-off').disabled = false;

    if (!chunks.length) {
      toggleEmpty($('#doc-chunks'), $('#doc-chunks-empty'), true, '该文档没有切片');
      return;
    }
    toggleEmpty($('#doc-chunks'), $('#doc-chunks-empty'), false);
    $('#doc-chunks').innerHTML = chunks.map((chunk) => {
      return '<tr>' +
        '<td class="small mono">' + esc(chunk.source_ref || '—') + '</td>' +
        '<td class="small">' + chunk.id + '</td>' +
        '<td class="q">' + esc(chunk.question) + '</td>' +
        '<td class="chunk-ans small">' + esc(clip(oneline(chunk.answer), 90)) + '</td>' +
        '<td>' + (chunk.enabled
          ? '<span class="badge ok">启用</span>'
          : '<span class="badge gray">草稿</span>') + '</td>' +
        '<td><button class="btn sm" data-act="chunk-edit" data-id="' + chunk.id + '">编辑</button></td>' +
      '</tr>';
    }).join('');
  }

  /** 粘贴文本导入 */
  async function importPasted() {
    const title = $('#doc-title').value.trim();
    const content = $('#doc-content').value.trim();
    if (!title) { toast('请填写文档标题', true); return; }
    if (content.length < 10) { toast('正文太短，至少 10 个字符才能切分', true); return; }
    await withBusy($('#doc-import-text'), async () => {
      try {
        const res = await api('/api/documents/import', {
          method: 'POST',
          body: JSON.stringify({
            title: title,
            content: content,
            category: $('#doc-category').value.trim(),
            source_name: title,
            source_type: 'text',
            publish: $('#doc-publish').checked,
          }),
        });
        toast(res.message || '导入成功');
        $('#doc-import-tip').textContent =
          '已切分 ' + res.chunk_count + ' 个片段（' + (DOC_STATUS[res.status] || res.status) + '）。';
        await loadDocuments();
        await loadHealth();
        await openDocument(res.document_id);
      } catch (err) {
        reportError(err);
      }
    }, '导入中…');
  }

  /** 文件上传导入（multipart/form-data，不手动设 Content-Type） */
  async function importFile() {
    const input = $('#doc-file');
    if (!input.files || !input.files.length) { toast('请先选择要上传的文件', true); return; }
    const file = input.files[0];
    const form = new FormData();
    form.append('file', file);
    form.append('title', $('#doc-title').value.trim());
    form.append('category', $('#doc-category').value.trim());
    form.append('publish', $('#doc-publish').checked ? 'true' : 'false');

    await withBusy($('#doc-import-file'), async () => {
      try {
        const res = await api('/api/documents/upload', { method: 'POST', body: form });
        toast(res.message || '上传成功');
        $('#doc-import-tip').textContent =
          '已解析 ' + file.name + '，切分 ' + res.chunk_count + ' 个片段（' + (DOC_STATUS[res.status] || res.status) + '）。';
        input.value = '';
        await loadDocuments();
        await loadHealth();
        await openDocument(res.document_id);
      } catch (err) {
        reportError(err);
      }
    }, '上传中…');
  }

  async function publishDocument(enabled) {
    const docId = state.currentDocId;
    if (!docId) return;
    const button = enabled ? $('#doc-publish-on') : $('#doc-publish-off');
    await withBusy(button, async () => {
      try {
        const res = await api('/api/documents/' + encodeURIComponent(docId) + '/publish', {
          method: 'POST',
          body: JSON.stringify({ enabled: !!enabled }),
        });
        toast(res.message || '已更新发布状态');
        await refreshDocDetail();
        await loadDocuments();
        await loadHealth();
        if ($('#pane-kb').classList.contains('active')) await loadKnowledge();
      } catch (err) {
        reportError(err);
      }
    }, '处理中…');
  }

  async function deleteDocument() {
    const docId = state.currentDocId;
    if (!docId) return;
    if (!confirm('确认删除文档 #' + docId + ' 及其全部切片？删除后不可恢复。')) return;
    await withBusy($('#doc-delete'), async () => {
      try {
        const res = await api('/api/documents/' + encodeURIComponent(docId), { method: 'DELETE' });
        toast(res.message || '文档已删除');
        state.currentDocId = null;
        state.currentDoc = null;
        $('#doc-detail').style.display = 'none';
        await loadDocuments();
        await loadHealth();
        if ($('#pane-kb').classList.contains('active')) await loadKnowledge();
      } catch (err) {
        reportError(err);
      }
    }, '删除中…');
  }

  async function testDocumentRecall() {
    const docId = state.currentDocId;
    if (!docId) { toast('请先选择文档', true); return; }
    const query = $('#doc-test-query').value.trim();
    if (!query) { toast('请输入用于预检的一句话', true); return; }
    const box = $('#doc-test-result');
    box.innerHTML = '<div class="small">预检中…</div>';
    await withBusy($('#doc-test-run'), async () => {
      try {
        const data = await api('/api/documents/' + encodeURIComponent(docId) + '/test', {
          method: 'POST',
          body: JSON.stringify({ query: query, top_k: 5 }),
        });
        box.innerHTML = '<div class="small thr-line">预检语句：<code>' + esc(data.query) +
          '</code> · 命中 ' + (data.candidates || []).length + ' 个片段（该接口不返回命中等级）</div>' +
          renderCandidates(data.candidates || [], { noLevel: true });
      } catch (err) {
        box.innerHTML = '<div class="small">预检失败：' + esc(err.message) + '</div>';
        reportError(err);
      }
    }, '预检中…');
  }

  function bindDocuments() {
    $('#doc-import-text').addEventListener('click', importPasted);
    $('#doc-import-file').addEventListener('click', importFile);
    $('#doc-refresh').addEventListener('click', () => loadDocuments().catch(reportError));
    $('#doc-publish-on').addEventListener('click', () => publishDocument(true));
    $('#doc-publish-off').addEventListener('click', () => publishDocument(false));
    $('#doc-delete').addEventListener('click', deleteDocument);
    $('#doc-test-run').addEventListener('click', testDocumentRecall);
    $('#doc-test-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') testDocumentRecall(); });

    $('#doc-list-tbody').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act="open"]');
      if (btn) openDocument(btn.dataset.id).catch(reportError);
    });

    $('#doc-chunks').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act="chunk-edit"]');
      if (!btn) return;
      // 取回完整条目后复用知识条目弹窗（保存走 PUT /api/knowledge/{id}）
      api('/api/knowledge/' + encodeURIComponent(btn.dataset.id))
        .then((item) => openKbModal(item))
        .catch(reportError);
    });
  }

  /* ======================================================================== */
  /* 6. 页签三：会话记录                                                      */
  /* ======================================================================== */

  function sessionQuery(buildFor) {
    const params = new URLSearchParams();
    const status = $('#ss-status').value;
    const since = $('#ss-since').value;
    const until = $('#ss-until').value;
    const handoff = $('#ss-handoff').value;
    const badCsat = $('#ss-badcsat').value;
    const keyword = $('#ss-keyword').value.trim();
    const category = $('#ss-category').value;
    if (status) params.set('status', status);
    // 后端按字符串比较 created_at（"YYYY-MM-DD HH:MM:SS"），补全时间保证当天数据不被漏掉
    if (since) params.set('since', since + ' 00:00:00');
    if (until) params.set('until', until + ' 23:59:59');
    if (handoff) params.set('handoff_only', handoff);
    if (badCsat) params.set('bad_csat', badCsat);
    if (keyword) params.set('keyword', keyword);
    if (category) params.set('category', category);
    if (buildFor === 'export') {
      // 导出走 <a>/location.href，token 只能放在查询串里
      params.set('token', localStorage.getItem('cs_token') || '');
    } else {
      params.set('limit', '50');
      params.set('offset', '0');
    }
    return params;
  }

  function csatCell(score) {
    if (score === 1) return '<span class="badge ok">好评</span>';
    if (score === -1) return '<span class="tag risk">差评</span>';
    return '<span class="small">未评价</span>';
  }

  async function loadSessions() {
    const tbody = $('#ss-tbody');
    tableLoading(tbody, 13);
    const data = await api('/api/admin/sessions?' + sessionQuery('list').toString());
    state.sessions = data.items || [];
    state.categories = data.categories || state.categories;

    // 状态下拉来自接口
    const statusSelect = $('#ss-status');
    const keep = statusSelect.value;
    statusSelect.innerHTML = '<option value="">全部状态</option>' +
      (data.statuses || []).map((s) => '<option value="' + esc(s.value) + '">' + esc(s.label) + '</option>').join('');
    statusSelect.value = keep;
    renderCategoryOptions($('#ss-category'), data.categories || [], '全部分类');
    renderCategoryDatalist(state.categories);

    $('#ss-tip').textContent = '命中 ' + state.sessions.length + ' 条 / 期间共 ' + data.total +
      ' 条会话（最多返回 50 条，导出 CSV 可拿全量）';

    if (!state.sessions.length) {
      toggleEmpty(tbody, $('#ss-empty'), true, '没有符合条件的会话');
      return;
    }
    toggleEmpty(tbody, $('#ss-empty'), false);
    tbody.innerHTML = state.sessions.map((s) => {
      return '<tr>' +
        '<td class="small mono"><span title="' + esc(s.id) + '">' + esc(clip(s.id, 12)) + '</span></td>' +
        '<td class="small">' + esc(clip(s.visitor_id || '匿名访客', 14)) + '</td>' +
        '<td class="small nowrap">' + esc(s.created_at || '') + '</td>' +
        '<td><span class="' + (STATUS_BADGE[s.status] || 'badge gray') + '">' +
          esc(s.status_label || s.status) + '</span></td>' +
        '<td class="small">' + dash(s.turn_count) + '</td>' +
        '<td class="small">' + (s.fallback_count
          ? '<span class="badge warn">' + s.fallback_count + '</span>'
          : '0') + '</td>' +
        '<td class="q">' + esc(clip(oneline(s.last_question) || '（未识别）', 40)) + '</td>' +
        '<td class="small">' + esc(dash(s.last_category)) + '</td>' +
        '<td class="small">' + (s.handoff_reason
          ? '<span title="' + esc(s.handoff_detail || '') + '">' + esc(ruleName(s.handoff_reason)) + '</span>'
          : '—') + '</td>' +
        '<td class="small">' + esc(s.resolved_by ? (RESOLVED_LABELS[s.resolved_by] || s.resolved_by) : '—') + '</td>' +
        '<td>' + csatCell(s.csat_score) + '</td>' +
        '<td class="small">' + dash(s.message_count) + '</td>' +
        '<td><button class="btn sm" data-act="view" data-id="' + esc(s.id) + '">查看</button></td>' +
      '</tr>';
    }).join('');
  }

  function renderSessionDetail(data) {
    const s = data.session || {};
    const messages = data.messages || [];
    const handoffs = data.handoff || [];
    const agent = data.agent || {};

    const kv = [
      ['会话ID', s.id], ['访客', s.visitor_id || '匿名访客'],
      ['状态', s.status_label || s.status], ['创建时间', s.created_at],
      ['更新时间', s.updated_at], ['结束时间', s.ended_at || '—'],
      ['结束原因', s.end_reason || '—'], ['轮次', s.turn_count],
      ['兜底次数', s.fallback_count], ['澄清轮次', s.clarify_round],
      ['最后意图', s.last_question || '—'], ['最后分类', s.last_category || '—'],
      ['待澄清槽位', s.pending_slot || '—'], ['归因', s.resolved_by ? (RESOLVED_LABELS[s.resolved_by] || s.resolved_by) : '—'],
      ['接待客服', agent.name || '—'], ['消息数', s.message_count !== undefined ? s.message_count : messages.length],
      ['进入队列', s.queue_at || '—'], ['分配时间', s.assigned_at || '—'],
      ['转人工原因', s.handoff_reason ? (ruleName(s.handoff_reason) + (s.handoff_detail ? '（' + s.handoff_detail + '）' : '')) : '—'],
    ];

    const kvHtml = '<div class="summary-card"><div class="kv">' + kv.map(([k, v]) =>
      '<div><div class="k">' + esc(k) + '</div><div class="v">' + esc(v === null || v === undefined || v === '' ? '—' : v) + '</div></div>'
    ).join('') + '</div></div>';

    const msgHtml = messages.length
      ? messages.map((m) => {
        const role = m.role || 'bot';
        const sources = m.source_details || [];
        const srcHtml = sources.length
          ? '<div class="src-list">命中知识：' + sources.map((sd) =>
              '#' + esc(sd.id) + ' ' + esc(clip(sd.question, 40)) + '（' + esc(sd.source_ref || '—') + '）'
            ).join('；') + '</div>'
          : '';
        const metaBits = [];
        if (m.answer_type) metaBits.push('类型 ' + m.answer_type);
        if (m.kb_category) metaBits.push('分类 ' + m.kb_category);
        if (typeof m.score === 'number') metaBits.push('分数 ' + m.score.toFixed(3));
        if (m.context_used) metaBits.push('使用了上下文');
        if (m.agent) metaBits.push('客服 ' + m.agent);
        return '<div class="msg-line ' + esc(role) + '">' +
          '<div class="msg-head"><span>' + esc(MSG_ROLE[role] || role) + '</span>' +
            '<span>' + esc(m.created_at || '') + '</span></div>' +
          esc(m.content || '') + srcHtml +
          (metaBits.length ? '<div class="src-list">' + esc(metaBits.join(' · ')) + '</div>' : '') +
        '</div>';
      }).join('')
      : '<div class="empty">该会话没有消息</div>';

    const handoffHtml = handoffs.length
      ? handoffs.map((h) => '<div class="handoff-item">' +
          '<b>' + esc(h.trigger_label || ruleName(h.trigger_type)) + '</b>' +
          (h.trigger_detail ? ' · ' + esc(h.trigger_detail) : '') +
          '<div class="small">进入队列 ' + esc(h.queue_at || '—') +
            ' · 分配 ' + esc(h.assigned_at || '—') +
            ' · 客服ID ' + esc(h.agent_id === null || h.agent_id === undefined ? '—' : h.agent_id) + '</div>' +
        '</div>').join('')
      : '<div class="small">无转人工记录</div>';

    const csatHtml = '<div class="csat-box">' +
      '<div><b>满意度：</b>' + (s.csat_score === 1 ? '好评' : s.csat_score === -1 ? '差评' : '未评价') + '</div>' +
      '<div class="small" style="margin-top:4px;">原因：' + esc(dash(s.csat_reason)) + '</div>' +
      '<div class="small">评论：' + esc(dash(s.csat_comment)) + '</div>' +
      '</div>';

    return kvHtml +
      '<h3 style="margin:14px 0 8px; font-size:14px;">消息流 <span class="sub">（机器人消息下方为命中知识溯源）</span></h3>' +
      '<div class="chat-log">' + msgHtml + '</div>' +
      '<h3 style="margin:14px 0 8px; font-size:14px;">转人工记录</h3>' + handoffHtml +
      '<h3 style="margin:14px 0 8px; font-size:14px;">满意度详情</h3>' + csatHtml;
  }

  async function openSession(id) {
    const body = $('#sess-body');
    $('#sess-modal-title').textContent = '会话详情';
    body.innerHTML = '<div class="empty">加载中…</div>';
    showModal('sess-modal-mask');
    try {
      const data = await api('/api/admin/sessions/' + encodeURIComponent(id));
      $('#sess-modal-title').textContent = '会话详情 · ' + ((data.session && data.session.id) || id);
      body.innerHTML = renderSessionDetail(data);
    } catch (err) {
      body.innerHTML = '<div class="empty">' + esc(err.message) + '</div>';
      reportError(err);
    }
  }

  function exportSessions() {
    const url = '/api/admin/sessions/export.csv?' + sessionQuery('export').toString();
    location.href = url;
    // 说明：后端导出接口只接受 since / until（其余筛选不影响导出，最多 5000 条）
    toast('正在导出 CSV（后端仅按日期区间导出全量，最多 5000 条）');
  }

  function bindSessions() {
    $('#ss-query').addEventListener('click', () => loadSessions().catch(reportError));
    $('#ss-export').addEventListener('click', exportSessions);
    $('#ss-keyword').addEventListener('keydown', (e) => { if (e.key === 'Enter') loadSessions().catch(reportError); });
    $('#ss-reset').addEventListener('click', () => {
      $('#ss-status').value = '';
      $('#ss-since').value = '';
      $('#ss-until').value = '';
      $('#ss-handoff').value = '';
      $('#ss-badcsat').value = '';
      $('#ss-category').value = '';
      $('#ss-keyword').value = '';
      loadSessions().catch(reportError);
    });
    $('#ss-tbody').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act="view"]');
      if (btn) openSession(btn.dataset.id);
    });
    $('#sess-close').addEventListener('click', () => hideModal('sess-modal-mask'));
  }

  /* ======================================================================== */
  /* 7. 页签四：未解决问题（Badcase 回流）                                    */
  /* ======================================================================== */

  async function loadFallbacks() {
    const tbody = $('#fb-tbody');
    tableLoading(tbody, 7);
    const handled = $('#fb-handled').value;
    const params = new URLSearchParams();
    params.set('limit', '100');
    if (handled !== '') params.set('handled', handled);
    const data = await api('/api/admin/fallbacks?' + params.toString());
    state.fallbacks = data.items || [];

    // 同一问题在列表中出现次数（后端不提供聚合字段，这里按当前列表统计）
    const counter = {};
    state.fallbacks.forEach((item) => { counter[item.query] = (counter[item.query] || 0) + 1; });
    state.fallbackCounter = counter;

    const keyword = $('#fb-filter').value.trim();
    const rows = keyword
      ? state.fallbacks.filter((item) => item.query.indexOf(keyword) >= 0)
      : state.fallbacks;

    $('#fb-tip').textContent = '当前列表 ' + rows.length + ' 条（最多 100 条）·「出现次数」按相同问题聚合统计' +
      (keyword ? ' · 已按关键词过滤' : '');

    if (!rows.length) {
      toggleEmpty(tbody, $('#fb-empty'), true, '暂无未解决问题');
      return;
    }
    toggleEmpty(tbody, $('#fb-empty'), false);
    tbody.innerHTML = rows.map((item) => {
      const times = counter[item.query] || 1;
      return '<tr>' +
        '<td class="small">' + item.id + '</td>' +
        '<td class="q">' + esc(item.query) + '</td>' +
        '<td>' + (times > 1
          ? '<span class="badge warn">×' + times + '</span>'
          : '<span class="small">×1</span>') + '</td>' +
        '<td class="small nowrap">' + esc(item.created_at || '') + '</td>' +
        '<td class="small mono">' + esc(clip(item.session_id || '—', 14)) + '</td>' +
        '<td>' + (item.handled
          ? '<span class="badge ok">已处理</span>'
          : '<span class="badge gray">未处理</span>') + '</td>' +
        '<td><button class="btn sm primary" data-act="reflow" data-id="' + item.id + '">回流为知识</button></td>' +
      '</tr>';
    }).join('');
  }

  function openFallbackModal(id) {
    const item = state.fallbacks.find((f) => String(f.id) === String(id));
    if (!item) return;
    $('#fb-modal-mask').dataset.fid = item.id;
    $('#fb-f-query').value = item.query;
    $('#fb-f-answer').value = '';
    $('#fb-f-category').value = item.category || '';
    showModal('fb-modal-mask');
    setTimeout(() => $('#fb-f-answer').focus(), 50);
  }

  async function saveFallback() {
    const fid = $('#fb-modal-mask').dataset.fid;
    const answer = $('#fb-f-answer').value.trim();
    if (!fid) { toast('未选中记录', true); return; }
    if (!answer) { toast('请填写标准答', true); return; }
    await withBusy($('#fb-save'), async () => {
      try {
        const res = await api('/api/admin/fallbacks/' + encodeURIComponent(fid) + '/to-knowledge', {
          method: 'POST',
          body: JSON.stringify({
            category: $('#fb-f-category').value.trim() || '未分类',
            answer: answer,
          }),
        });
        hideModal('fb-modal-mask');
        toast(res.message || '已回流为新知识条目');
        await loadFallbacks();
        await loadHealth();
        if (state.loadedTabs.kb) await loadKnowledge();
      } catch (err) {
        reportError(err);
      }
    }, '回流中…');
  }

  function bindFallbacks() {
    $('#fb-refresh').addEventListener('click', () => loadFallbacks().catch(reportError));
    $('#fb-handled').addEventListener('change', () => loadFallbacks().catch(reportError));
    let timer = null;
    $('#fb-filter').addEventListener('input', () => {
      clearTimeout(timer);
      timer = setTimeout(() => loadFallbacks().catch(reportError), 250);
    });
    $('#fb-save').addEventListener('click', saveFallback);
    $('#fb-tbody').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act="reflow"]');
      if (btn) openFallbackModal(btn.dataset.id);
    });
  }

  /* ======================================================================== */
  /* 8. 页签五：机器人设置                                                    */
  /* ======================================================================== */

  function rangeRowHtml(range) {
    const start = (range && range[0]) || '09:00';
    const end = (range && range[1]) || '21:00';
    return '<div class="range-row">' +
      '<input type="time" class="r-start" value="' + esc(start) + '" />' +
      '<span class="sep">至</span>' +
      '<input type="time" class="r-end" value="' + esc(end) + '" />' +
      '<button class="btn sm ghost" data-act="del-range">删除</button>' +
    '</div>';
  }

  function renderHoursEditor(hours) {
    const box = $('#hours-editor');
    box.innerHTML = WEEKDAYS.map(([key, name]) => {
      const ranges = (hours && Array.isArray(hours[key])) ? hours[key] : [];
      const body = ranges.length
        ? ranges.map(rangeRowHtml).join('')
        : '<div class="off-tip">当天不服务</div>';
      return '<div class="hour-row" data-day="' + key + '">' +
        '<div class="day">' + name + '</div>' +
        '<div class="ranges">' + body + '</div>' +
        '<div class="ops" style="flex-direction:column; gap:6px;">' +
          '<button class="btn sm" data-act="add-range">+ 时段</button>' +
          '<button class="btn sm ghost" data-act="clear-day">不服务</button>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  /** 读取服务时段编辑器当前值 → {day: [[start,end], ...]} */
  function collectHours() {
    const result = {};
    $$('#hours-editor .hour-row').forEach((row) => {
      const ranges = [];
      $$('.range-row', row).forEach((range) => {
        const start = $('.r-start', range).value;
        const end = $('.r-end', range).value;
        if (start && end) ranges.push([start, end]);
      });
      result[row.dataset.day] = ranges;
    });
    return result;
  }

  async function loadBotConfig() {
    const data = await api('/api/admin/bot-config');
    const cfg = data.config || {};
    state.botConfig = cfg;
    state.slots = (data.slots || []).map((s) => ({ id: s.id, name: s.name }));

    $('#bot-name').value = cfg.bot_name || '';
    $('#bot-avatar').value = cfg.bot_avatar || '';
    $('#bot-welcome').value = cfg.welcome || '';
    $('#bot-ending').value = cfg.ending || '';
    $('#bot-fallback').value = cfg.fallback || '';
    $('#bot-handoff').value = cfg.handoff || '';
    $('#bot-handoff-offline').value = cfg.handoff_offline || '';
    $('#bot-handoff-prefix').value = cfg.handoff_trigger_prefix || '';
    $('#bot-handoff-suggest').value = cfg.handoff_suggest || '';
    $('#bot-quick').value = (cfg.quick_questions || []).join('\n');
    $('#bot-hours-text').value = cfg.service_hours_text || '';
    renderHoursEditor(cfg.service_hours || {});
    renderSlotsReadonly(data.slots || []);
    renderSlotOptions();
  }

  function renderSlotsReadonly(slots) {
    const box = $('#slots-list');
    if (!slots.length) {
      box.innerHTML = '<div class="small">没有内置槽位规则。</div>';
      return;
    }
    box.innerHTML = slots.map((slot) => {
      const options = (slot.options || []).map((o) => o.label).filter(Boolean);
      return '<div class="slot-card">' +
        '<h4>' + esc(slot.name) + ' <span class="small mono">' + esc(slot.id) + '</span></h4>' +
        '<div class="small">追问话术：' + esc(slot.question || '—') + '</div>' +
        '<div class="small">触发：分类「' + esc(slot.when_category || '—') + '」且命中关键词 ' +
          esc((slot.when_keywords || []).join('、') || '—') + '</div>' +
        '<div class="small opt">澄清选项：' +
          (options.length ? options.map((label) => '<span class="tag slot">' + esc(label) + '</span>').join('') : '—') +
        '</div>' +
      '</div>';
    }).join('');
  }

  async function saveBotConfig() {
    const payload = {
      bot_name: $('#bot-name').value.trim(),
      bot_avatar: $('#bot-avatar').value.trim(),
      welcome: $('#bot-welcome').value,
      ending: $('#bot-ending').value,
      fallback: $('#bot-fallback').value,
      handoff: $('#bot-handoff').value,
      handoff_offline: $('#bot-handoff-offline').value,
      handoff_trigger_prefix: $('#bot-handoff-prefix').value,
      handoff_suggest: $('#bot-handoff-suggest').value,
      quick_questions: $('#bot-quick').value.split('\n').map((s) => s.trim()).filter(Boolean),
      service_hours: collectHours(),
      service_hours_text: $('#bot-hours-text').value.trim(),
    };
    if (!payload.bot_name) { toast('机器人名称不能为空', true); return; }
    await withBusy($('#bot-save'), async () => {
      try {
        const res = await api('/api/admin/bot-config', { method: 'PUT', body: JSON.stringify(payload) });
        state.botConfig = res.config || state.botConfig;
        toast(res.message || '配置已保存');
        await loadHealth();
      } catch (err) {
        reportError(err);
      }
    }, '保存中…');
  }

  function bindBot() {
    $('#bot-save').addEventListener('click', saveBotConfig);
    $('#bot-reload').addEventListener('click', () => loadBotConfig().catch(reportError));
    $('#hours-editor').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const row = btn.closest('.hour-row');
      const day = row.dataset.day;
      const hours = collectHours();
      if (btn.dataset.act === 'add-range') {
        if (!hours[day] || !hours[day].length) hours[day] = [['09:00', '21:00']];
        else hours[day].push(['09:00', '21:00']);
      } else if (btn.dataset.act === 'clear-day') {
        hours[day] = [];
      } else if (btn.dataset.act === 'del-range') {
        const ranges = $$('.range-row', row);
        const index = ranges.indexOf(btn.closest('.range-row'));
        if (index >= 0 && hours[day]) hours[day].splice(index, 1);
      }
      renderHoursEditor(hours);
    });
  }

  /* ======================================================================== */
  /* 8.1 机器人设置内嵌：向量召回（Embedding）面板                            */
  /*     - 状态：GET  /api/admin/embedding                                    */
  /*     - 重建：POST /api/admin/embedding/rebuild?force=true|false           */
  /*     - 对照：POST /api/admin/embedding/test                               */
  /*     命中等级阈值取自 /api/health 的 thresholds（初始化时缓存）           */
  /* ======================================================================== */

  /* 一键填入的示例问题：最后一条用于演示「无关问题仍然兜底」 */
  const EMBED_SAMPLES = [
    ['钱什么时候回来', '同义表达：词面命中「退款」相关但分数低，向量把语义补齐'],
    ['登不上去了', '同义表达：词面几乎命中不了「忘记密码怎么办」'],
    ['贵不贵', '口语化：同义词典映射到「价格」'],
    ['换一个行不行', '口语化：同义词典映射到「换货」'],
    ['退钱到哪了', '同义表达：词面分数偏低'],
    ['今天天气怎么样', '无关问题：两列都进入兜底，演示向量召回不会乱答'],
  ];

  const EMBED_MODE_LABEL = { local: '离线（零依赖）', api: '在线语义向量', off: '已关闭' };

  /** 保留数字位数（非数字原样展示），用于分数展示 */
  function embedScore(value, digits) {
    return typeof value === 'number' ? value.toFixed(digits === undefined ? 4 : digits) : dash(value);
  }

  /** 按 /api/health 的阈值判定命中等级（与后端 high_confidence / mid_confidence 同一套口径） */
  function embedLevel(score) {
    const high = state.thresholds.high_confidence;
    const mid = state.thresholds.mid_confidence;
    if (typeof score !== 'number') return { text: '—', cls: 'badge gray' };
    if (typeof high === 'number' && score >= high) return { text: '直接作答', cls: 'badge ok' };
    if (typeof mid === 'number' && score >= mid) return { text: '作答+确认', cls: 'badge warn' };
    return { text: '进入兜底', cls: 'badge gray' };
  }

  /** 状态卡片：provider / 模型 / 维度 / 覆盖 / 构建时间 / 融合权重 + 场景提示 */
  function renderEmbeddingStatus(st) {
    const box = $('#embed-status');
    if (!box) return;
    if (!st) { box.innerHTML = '<div class="small">状态加载中…</div>'; return; }

    const w = st.weights || {};
    const provider = st.provider || 'off';
    const stale = Number(st.stale) || 0;
    const indexed = Number(st.indexed) || 0;
    const total = Number(st.total) || 0;
    const covered = total > 0 && indexed >= total && stale === 0;
    const kindLabel = st.kind === 'dense' ? 'dense 稠密向量' : (st.kind === 'sparse' ? 'sparse 稀疏向量' : '未启用向量');

    const stats =
      '<div class="embed-stat">' +
        '<div class="k">当前 provider</div>' +
        '<div class="v">' + esc(dash(st.provider_label)) + '</div>' +
        '<div class="badges">' +
          '<span class="' + (provider === 'off' ? 'badge gray' : (provider === 'api' ? 'badge ctx' : 'badge ok')) + '">' +
            esc(provider) + '</span>' +
          '<span class="tag">配置 EMBEDDING_PROVIDER=' + esc(dash(st.configured_mode)) + '</span>' +
          '<span class="tag">' + esc(kindLabel) + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="embed-stat">' +
        '<div class="k">向量模型</div>' +
        '<div class="v">' + esc(dash(st.model)) + '</div>' +
        '<div class="badges"><span class="tag">' + esc(EMBED_MODE_LABEL[provider] || provider) + '</span>' +
          (provider === 'api'
            ? '<span class="' + (st.api_ready ? 'badge ok' : 'badge warn') + '">API Key ' +
              (st.api_ready ? '已配置' : '未配置') + '</span>'
            : '') +
        '</div>' +
      '</div>' +
      '<div class="embed-stat">' +
        '<div class="k">向量维度 dim</div>' +
        '<div class="v">' + esc(dash(st.dim)) + ' <small>维' +
          (st.kind === 'sparse' ? '（稀疏向量为词表大小）' : '') + '</small></div>' +
      '</div>' +
      '<div class="embed-stat">' +
        '<div class="k">索引覆盖</div>' +
        '<div class="v">' + indexed + ' / ' + total + ' ' +
          (covered ? '<span class="badge ok">已全覆盖</span>' : '<span class="badge gray">未全覆盖</span>') + '</div>' +
        '<div class="badges">' + (stale > 0
          ? '<span class="badge warn">有 ' + stale + ' 条待重建</span>'
          : '<span class="tag">无待重建条目</span>') + '</div>' +
      '</div>' +
      '<div class="embed-stat">' +
        '<div class="k">最近构建时间</div>' +
        '<div class="v" style="font-size:13.5px;">' + esc(dash(st.built_at)) + '</div>' +
        '<div class="badges"><span class="' + (st.enabled ? 'badge ok' : 'badge gray') + '">' +
          (st.enabled ? '向量召回已启用' : '向量召回未启用') + '</span></div>' +
      '</div>' +
      '<div class="embed-stat" style="grid-column:1 / -1;">' +
        '<div class="k">融合权重</div>' +
        '<div class="v" style="font-size:13.5px;">向量权重 ' + esc(dash(w.vector_weight)) +
          ' · 余弦下限 ' + esc(dash(w.min_cosine)) + ' · 双通道一致加成 ' + esc(dash(w.agree_bonus)) + '</div>' +
        '<div class="embed-note">最终分 = max(词面分, ' + esc(dash(w.vector_weight)) + ' × 向量余弦) + 双通道一致加成；' +
          '余弦低于 ' + esc(dash(w.min_cosine)) + ' 视为不可信而忽略。</div>' +
      '</div>';

    /* 场景提示：离线模式 / 已关闭 / 选了在线但没配 Key */
    let tip = '';
    if (provider === 'off' || !st.enabled) {
      tip = '<div class="embed-tip bad"><div>⛔ <b>向量召回已关闭，当前为纯词面检索</b>：' +
        '同义表达（如「钱什么时候回来」↔「退款多久到账」）会漏召回，未覆盖说法只能进入兜底。<br />' +
        '把环境变量 <code class="mono">EMBEDDING_PROVIDER</code> 设为 <code class="mono">local</code> 并重启服务即可启用' +
        '（离线同义词图向量，零依赖、可离线运行）。</div></div>';
    } else if (provider === 'api' && !st.api_ready) {
      tip = '<div class="embed-tip warn"><div>⚠️ 已选择<b>在线语义向量</b>（<code class="mono">EMBEDDING_PROVIDER=api</code>），' +
        '但未检测到 <code class="mono">EMBEDDING_API_KEY</code>：当前会退回离线同义词图向量。<br />' +
        '配置 <code class="mono">EMBEDDING_API_KEY</code> 后重启服务可启用真实语义向量。</div></div>';
    } else if (provider === 'local' && !st.api_ready) {
      tip = '<div class="embed-tip info"><div>💡 当前使用<b>离线</b>同义词图向量（零依赖、可离线运行）。' +
        '配置 <code class="mono">EMBEDDING_API_KEY</code> 后重启可启用<b>在线语义向量</b>，能理解同义词典未覆盖的说法。</div></div>';
    }
    if (stale > 0) {
      tip += '<div class="embed-tip warn"><div>有 <b>' + stale + '</b> 条知识尚未建入向量索引，' +
        '点「增量重建」即可补齐（只处理新增/变更条目）。</div></div>';
    }

    box.innerHTML = '<div class="embed-stats">' + stats + '</div>' + tip;
  }

  /** 拉取向量召回状态（面板入口 + 刷新按钮） */
  async function loadEmbeddingStatus() {
    const box = $('#embed-status');
    if (box && !state.embedding) box.innerHTML = '<div class="small">状态加载中…</div>';
    try {
      const st = await api('/api/admin/embedding');
      state.embedding = st;
      renderEmbeddingStatus(st);
      return st;
    } catch (err) {
      if (box) box.innerHTML = '<div class="small">向量召回状态加载失败：' + esc(err.message) + '</div>';
      reportError(err);
      return null;
    }
  }

  /** 重建索引：force=false 增量、force=true 全量（二次确认） */
  async function rebuildEmbedding(force, button) {
    const total = state.embedding ? state.embedding.total : 0;
    if (force && !confirm('全量重建会忽略现有索引，对全部 ' + (total || '所有') +
      ' 条知识重新计算向量，确认继续？')) return;
    await withBusy(button, async () => {
      try {
        const res = await api('/api/admin/embedding/rebuild?force=' + (force ? 'true' : 'false'), {
          method: 'POST',
          body: JSON.stringify({}),
        });
        const result = res.result || {};
        toast((res.message || '索引已更新') + '：' + dash(result.indexed) + ' 条 / ' +
          dash(result.elapsed_ms) + ' 毫秒');
        // 优先用返回里的最新状态刷新卡片，避免多发一次请求
        if (res.status) {
          state.embedding = res.status;
          renderEmbeddingStatus(res.status);
        } else {
          await loadEmbeddingStatus();
        }
        if (result.api_errors) toast('有 ' + result.api_errors + ' 次向量接口调用失败', true);
      } catch (err) {
        reportError(err);
      }
    }, '重建中…');
  }

  /** 单列候选（纯词面 / 词面+向量 共用）：ID、标准问、分类、score 大字 + lex/vec 小标注 + 命中等级 */
  function embedColumnHtml(candidates, opts) {
    const list = candidates || [];
    if (!list.length) {
      return '<div class="empty">没有命中任何候选，机器人将使用兜底话术</div>';
    }
    const other = (opts && opts.otherScores) || {};
    return list.map((c) => {
      const level = embedLevel(c.score);
      const otherScore = other[c.id];
      // 同一候选在另一列的分数：本列比另一列高 → 标绿，说明是本通道把它抬上来的
      const gain = typeof otherScore === 'number' && typeof c.score === 'number' && c.score > otherScore + 1e-9;
      const gainTip = gain
        ? '<span class="badge ok">较另一列 +' + embedScore(c.score - otherScore, 3) + '</span>'
        : '';
      return '<div class="embed-hit' + (gain ? ' gain' : '') + '">' +
        '<div class="main">' +
          '<div class="q">#' + esc(c.id) + ' ' + esc(c.question) + '</div>' +
          '<div class="small">分类 ' + esc(dash(c.category)) + ' · 命中问法 ' + esc(dash(c.matched_surface)) +
            ' · 溯源 ' + esc(dash(c.source_ref)) + (c.slot ? ' · 槽位 ' + esc(slotName(c.slot)) : '') + '</div>' +
          '<div class="badges">' +
            '<span class="' + level.cls + '">' + esc(level.text) + '</span>' +
            (gainTip || '') +
          '</div>' +
        '</div>' +
        '<div class="score">' +
          '<b>' + embedScore(c.score) + '</b>' +
          '<div class="calc">词面 ' + embedScore(c.lex_score) + ' · 向量 ' + embedScore(c.vec_score) + '</div>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  /** 两列 Top1 差异横幅：结论变化（命中等级不同）或命中条目不同都要醒目提示 */
  function embedDeltaHtml(data) {
    const lex = (data.lexical_only || [])[0];
    const vec = (data.with_vector || [])[0];
    if (!lex && !vec) return '';
    const lexLevel = lex ? embedLevel(lex.score) : null;
    const vecLevel = vec ? embedLevel(vec.score) : null;
    const sameId = !!(lex && vec && String(lex.id) === String(vec.id));
    const changed = !sameId || (lexLevel && vecLevel && lexLevel.text !== vecLevel.text);

    const desc = (c, level) => (c
      ? '#' + esc(c.id) + '「' + esc(clip(c.question, 22)) + '」' + (level ? esc(level.text) : '—') +
        '（' + embedScore(c.score) + '）'
      : '无候选');

    if (!changed) {
      const levelText = (vecLevel || lexLevel || {}).text || '—';
      const tail = levelText === '进入兜底'
        ? '两列都进入兜底，向量召回没有把无关问题误判成命中，仍会走兜底话术。'
        : '该问题词面本身已经足够明确，向量召回没有改变结论。';
      return '<div class="embed-delta miss">' +
        '<div class="title">两列结论一致</div>' +
        '纯词面与词面+向量的 Top1 相同，命中等级都是「' + esc(levelText) + '」：' +
        desc(lex, lexLevel) + '。' + tail + '</div>';
    }

    let why = '';
    if (sameId && typeof lex.score === 'number' && typeof vec.score === 'number') {
      const high = state.thresholds.high_confidence;
      why = '向量余弦 ' + embedScore(vec.vec_score) + ' 把该条的分数从 ' + embedScore(lex.score) +
        ' 抬到 ' + embedScore(vec.score) + '，跨过了「直接作答」阈值 ' + esc(dash(high)) + '。';
    } else if (!sameId) {
      why = '向量通道把另一条更贴近语义的知识顶到了第一：' +
        (lex ? '纯词面 #' + esc(lex.id) : '纯词面无候选') + ' → ' +
        (vec ? '向量 #' + esc(vec.id) : '向量无候选') + '。';
    }
    return '<div class="embed-delta">' +
      '<div class="title">⚡ 向量召回改变了结果</div>' +
      '纯词面：' + desc(lex, lexLevel) + ' → 词面+向量：' + desc(vec, vecLevel) + '<br />' + why + '</div>';
  }

  /** 渲染对照测试结果：同义词典命中 + 两列并排 + 差异横幅 */
  function renderEmbedTest(data) {
    const box = $('#embed-result');
    const syn = data.synonyms_hit || [];
    const lex = data.lexical_only || [];
    const vec = data.with_vector || [];
    const prov = data.provider || {};

    const lexScores = {};
    lex.forEach((c) => { lexScores[c.id] = c.score; });
    const vecScores = {};
    vec.forEach((c) => { vecScores[c.id] = c.score; });

    const thr = state.thresholds || {};
    const synHtml = syn.length
      ? syn.map((w) => '<span class="tag slot">' + esc(w) + '</span>').join('')
      : '<span class="small">未命中同义词典</span>';

    box.innerHTML =
      '<div class="embed-kv">' +
        '<span class="small">测试语句</span><code>' + esc(data.query) + '</code>' +
        '<span class="small">· 同义词典命中</span>' + synHtml +
      '</div>' +
      '<div class="small thr-line" style="margin-top:6px;">' +
        '当前通道：' + esc(dash(prov.provider_label)) + '（' + esc(dash(prov.provider)) + '） · ' +
        '命中等级阈值：直接作答 ≥ ' + esc(dash(thr.high_confidence)) +
        '，作答+确认 ≥ ' + esc(dash(thr.mid_confidence)) + '，低于则进入兜底' +
      '</div>' +
      embedDeltaHtml(data) +
      '<div class="embed-cols">' +
        '<div class="embed-col">' +
          '<div class="head"><span>🔤 纯词面检索</span><span class="small">共 ' + lex.length + ' 条候选</span></div>' +
          '<div class="body">' + embedColumnHtml(lex, { otherScores: vecScores }) + '</div>' +
        '</div>' +
        '<div class="embed-col vector">' +
          '<div class="head"><span>🧠 词面 + 向量召回</span><span class="small">共 ' + vec.length + ' 条候选</span></div>' +
          '<div class="body">' + embedColumnHtml(vec, { otherScores: lexScores }) + '</div>' +
        '</div>' +
      '</div>';
  }

  /** 对照测试：同一句话分别取「关闭向量」与「开启向量」两套结果 */
  async function runEmbeddingTest() {
    const query = $('#embed-query').value.trim();
    if (!query) { toast('请先输入要对照测试的问题', true); return; }
    const box = $('#embed-result');
    box.innerHTML = '<div class="small">对照测试中…</div>';
    await withBusy($('#embed-test-run'), async () => {
      try {
        const data = await api('/api/admin/embedding/test', {
          method: 'POST',
          body: JSON.stringify({ query: query, top_k: 5 }),
        });
        renderEmbedTest(data);
      } catch (err) {
        box.innerHTML = '<div class="small">对照测试失败：' + esc(err.message) + '</div>';
        reportError(err);
      }
    }, '测试中…');
  }

  function renderEmbedSamples() {
    const box = $('#embed-chips');
    box.innerHTML = '<span class="small">示例：</span>' + EMBED_SAMPLES.map(([text, tip]) =>
      '<button class="chip" data-sample="' + esc(text) + '" title="' + esc(tip) + '">' + esc(text) + '</button>'
    ).join('');
  }

  function bindEmbedding() {
    renderEmbedSamples();
    $('#embed-refresh').addEventListener('click', () => {
      withBusy($('#embed-refresh'), () => loadEmbeddingStatus(), '刷新中…');
    });
    $('#embed-rebuild-inc').addEventListener('click', () => rebuildEmbedding(false, $('#embed-rebuild-inc')));
    $('#embed-rebuild-full').addEventListener('click', () => rebuildEmbedding(true, $('#embed-rebuild-full')));
    $('#embed-test-run').addEventListener('click', runEmbeddingTest);
    $('#embed-query').addEventListener('keydown', (e) => { if (e.key === 'Enter') runEmbeddingTest(); });
    // 一键填入示例问题并立即跑对照测试
    $('#embed-chips').addEventListener('click', (e) => {
      const chip = e.target.closest('[data-sample]');
      if (!chip) return;
      $('#embed-query').value = chip.dataset.sample;
      runEmbeddingTest();
    });
  }

  /* ======================================================================== */
  /* 9. 页签六：触发规则                                                      */
  /* ======================================================================== */

  async function loadRules() {
    const tbody = $('#rules-tbody');
    tableLoading(tbody, 6);
    const data = await api('/api/admin/trigger-rules');
    const rules = data.items || [];
    state.rules = rules;

    if (!rules.length) {
      toggleEmpty(tbody, $('#rules-empty'), true, '暂无规则');
    } else {
      toggleEmpty(tbody, $('#rules-empty'), false);
      tbody.innerHTML = rules.map((rule) => {
        const type = rule.rule_type;
        return '<tr>' +
          '<td class="q">' + esc(rule.label) + '<div class="small mono">' + esc(type) + '</div></td>' +
          '<td class="small">' + esc(rule.desc || '') + '</td>' +
          '<td><input type="number" min="0" max="999" id="rule-th-' + esc(type) + '" value="' + esc(rule.threshold) + '" /></td>' +
          '<td><input type="checkbox" id="rule-en-' + esc(type) + '"' + (rule.enabled ? ' checked' : '') + ' /></td>' +
          '<td class="small nowrap">' + esc(rule.updated_at || '') + '</td>' +
          '<td><button class="btn sm primary" data-act="save-rule" data-rule="' + esc(type) + '">保存</button></td>' +
        '</tr>';
      }).join('');
    }

    const asTags = (words) => (words || []).map((w) =>
      '<span class="tag">' + esc(w) + '</span>').join('') || '<span class="small">—</span>';
    $('#sentiment-words').innerHTML = asTags(data.sentiment_words);
    $('#risky-words').innerHTML = (data.risky_words || []).map((w) =>
      '<span class="tag risk">' + esc(w) + '</span>').join('') || '<span class="small">—</span>';
  }

  async function saveRule(ruleType, button) {
    const thresholdInput = $('#rule-th-' + ruleType);
    const enabledInput = $('#rule-en-' + ruleType);
    if (!thresholdInput || !enabledInput) return;
    const threshold = parseInt(thresholdInput.value, 10);
    if (isNaN(threshold) || threshold < 0 || threshold > 999) {
      toast('阈值需为 0-999 的整数', true);
      return;
    }
    await withBusy(button, async () => {
      try {
        // 后端 enabled 为 int(0/1)，这里显式发 1/0
        const res = await api('/api/admin/trigger-rules/' + encodeURIComponent(ruleType), {
          method: 'PUT',
          body: JSON.stringify({ threshold: threshold, enabled: enabledInput.checked ? 1 : 0 }),
        });
        toast(res.message || '规则已更新');
        const rule = (res.items || []).find((r) => r.rule_type === ruleType);
        if (rule) thresholdInput.value = rule.threshold;
        if (rule) enabledInput.checked = !!rule.enabled;
      } catch (err) {
        reportError(err);
      }
    }, '保存中…');
  }

  function bindRules() {
    $('#rules-tbody').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act="save-rule"]');
      if (btn) saveRule(btn.dataset.rule, btn);
    });
  }

  /* ======================================================================== */
  /* 10. 页签七：客服与账号                                                   */
  /* ======================================================================== */

  async function loadAgents() {
    const tbody = $('#agents-tbody');
    tableLoading(tbody, 6);
    try {
      const data = await api('/api/admin/agents');
      const agents = data.items || [];
      if (!agents.length) {
        toggleEmpty(tbody, $('#agents-empty'), true, '暂无客服坐席');
      } else {
        toggleEmpty(tbody, $('#agents-empty'), false);
        tbody.innerHTML = agents.map((a) => {
          const status = a.status || 'offline';
          const dotClass = status === 'online' ? 'online' : (status === 'busy' ? 'busy' : 'offline');
          const load = (a.current_load === null || a.current_load === undefined) ? 0 : a.current_load;
          const max = (a.max_concurrent === null || a.max_concurrent === undefined) ? '—' : a.max_concurrent;
          const full = typeof max === 'number' && load >= max;
          return '<tr>' +
            '<td class="small">' + a.id + '</td>' +
            '<td class="dt-name q">' + esc(a.name) + '</td>' +
            '<td><span class="dot ' + dotClass + '"></span>' + esc(a.status_label || status) + '</td>' +
            '<td>' + (full ? '<span class="badge warn">' : '<span class="badge gray">') +
              load + ' / ' + esc(max) + '</span></td>' +
            '<td>' + (a.active_sessions
              ? '<span class="badge ctx">' + a.active_sessions + ' 个会话</span>'
              : '<span class="small">0</span>') + '</td>' +
            '<td class="small">' + esc(a.account_id === null || a.account_id === undefined ? '—' : ('#' + a.account_id)) + '</td>' +
          '</tr>';
        }).join('');
      }
    } catch (err) {
      toggleEmpty(tbody, $('#agents-empty'), true, err.status === 403 ? '当前角色无权访问' : err.message);
      reportError(err);
    }
    await loadAccounts();
  }

  async function loadAccounts() {
    const tbody = $('#accounts-tbody');
    tableLoading(tbody, 7);
    try {
      const data = await api('/api/admin/accounts');
      state.accounts = data.items || [];
      state.roles = (data.roles && data.roles.length) ? data.roles : state.roles;
      renderRoleOptions();
      if (!state.accounts.length) {
        toggleEmpty(tbody, $('#accounts-empty'), true, '暂无账号数据（AUTH_ENABLED=0 演示模式下不初始化账号）');
        return;
      }
      toggleEmpty(tbody, $('#accounts-empty'), false);
      tbody.innerHTML = state.accounts.map((acc) => {
        return '<tr>' +
          '<td class="small">' + acc.id + '</td>' +
          '<td class="mono">' + esc(acc.username) + '</td>' +
          '<td class="q">' + esc(acc.name || '—') + '</td>' +
          '<td><span class="badge gray">' + esc(acc.role_label || acc.role) + '</span></td>' +
          '<td>' + (acc.enabled
            ? '<span class="badge ok">启用</span>'
            : '<span class="badge gray">停用</span>') + '</td>' +
          '<td class="small nowrap">' + esc(acc.created_at || '') + '</td>' +
          '<td><div class="ops">' +
            '<button class="btn sm" data-act="edit" data-id="' + acc.id + '">编辑</button>' +
            '<button class="btn sm danger" data-act="del" data-id="' + acc.id + '">删除</button>' +
          '</div></td>' +
        '</tr>';
      }).join('');
    } catch (err) {
      toggleEmpty(tbody, $('#accounts-empty'), true, err.status === 403 ? '当前角色无权访问' : err.message);
      reportError(err);
    }
  }

  function renderRoleOptions() {
    const select = $('#acc-f-role');
    const roles = state.roles.length ? state.roles : [
      { value: 'admin', label: '管理员' },
      { value: 'supervisor', label: '客服主管' },
      { value: 'agent', label: '客服' },
      { value: 'editor', label: '知识运营' },
    ];
    select.innerHTML = roles.map((r) =>
      '<option value="' + esc(r.value) + '">' + esc(r.label) + '</option>').join('');
  }

  function openAccountModal(account) {
    const isEdit = !!(account && account.id);
    renderRoleOptions();
    $('#acc-modal-title').textContent = isEdit ? ('编辑账号 #' + account.id) : '新增账号';
    $('#acc-f-id').value = isEdit ? account.id : '';
    $('#acc-f-username').value = isEdit ? account.username : '';
    $('#acc-f-username').disabled = isEdit;
    $('#acc-f-password').value = '';
    $('#acc-pwd-hint').textContent = isEdit ? '（留空表示不修改）' : '*（6-64 位）';
    $('#acc-f-name').value = isEdit ? (account.name || '') : '';
    $('#acc-f-role').value = isEdit ? account.role : 'agent';
    $('#acc-f-enabled').value = String(isEdit ? !!account.enabled : true);
    showModal('acc-modal-mask');
    setTimeout(() => (isEdit ? $('#acc-f-name') : $('#acc-f-username')).focus(), 50);
  }

  async function saveAccount() {
    const id = $('#acc-f-id').value;
    const isEdit = !!id;
    const username = $('#acc-f-username').value.trim();
    const password = $('#acc-f-password').value;
    const name = $('#acc-f-name').value.trim();
    const role = $('#acc-f-role').value;
    const enabled = $('#acc-f-enabled').value === 'true';

    if (!isEdit && (username.length < 3 || username.length > 30)) {
      toast('用户名需 3-30 位', true);
      return;
    }
    if (!isEdit && (password.length < 6 || password.length > 64)) {
      toast('密码需 6-64 位', true);
      return;
    }
    if (isEdit && password && (password.length < 6 || password.length > 64)) {
      toast('新密码需 6-64 位', true);
      return;
    }

    await withBusy($('#acc-save'), async () => {
      try {
        let res;
        if (isEdit) {
          const payload = { name: name, role: role, enabled: enabled };
          if (password) payload.password = password;
          res = await api('/api/admin/accounts/' + encodeURIComponent(id), {
            method: 'PUT',
            body: JSON.stringify(payload),
          });
        } else {
          res = await api('/api/admin/accounts', {
            method: 'POST',
            body: JSON.stringify({ username: username, password: password, name: name, role: role, enabled: enabled }),
          });
        }
        hideModal('acc-modal-mask');
        toast(res.message || '已保存');
        await loadAccounts();
        await loadAgents();
      } catch (err) {
        if (err.status === 400 && err.message.indexOf('用户名已存在') >= 0) {
          toast('用户名已存在，请换一个', true);
        } else {
          reportError(err);
        }
      }
    }, '保存中…');
  }

  async function deleteAccount(id) {
    const acc = state.accounts.find((a) => String(a.id) === String(id));
    const label = acc ? (acc.username + ' / ' + (acc.name || '')) : ('#' + id);
    if (!confirm('确认删除账号 ' + label + '？该操作不可恢复。')) return;
    try {
      const res = await api('/api/admin/accounts/' + encodeURIComponent(id), { method: 'DELETE' });
      toast(res.message || '账号已删除');
      await loadAccounts();
      await loadAgents();
    } catch (err) {
      reportError(err);
    }
  }

  function bindAgents() {
    $('#agents-refresh').addEventListener('click', () => loadAgents().catch(reportError));
    $('#acc-new').addEventListener('click', () => openAccountModal(null));
    $('#acc-save').addEventListener('click', saveAccount);
    $('#accounts-tbody').addEventListener('click', (e) => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      if (btn.dataset.act === 'edit') {
        const acc = state.accounts.find((a) => String(a.id) === String(btn.dataset.id));
        if (acc) openAccountModal(acc);
      } else if (btn.dataset.act === 'del') {
        deleteAccount(btn.dataset.id);
      }
    });
  }

  /* ======================================================================== */
  /* 11. 初始化                                                               */
  /* ======================================================================== */

  function bindModalDismissers() {
    bindModalDismiss('kb-modal-mask', ['kb-cancel']);
    bindModalDismiss('sess-modal-mask', ['sess-close']);
    bindModalDismiss('fb-modal-mask', ['fb-cancel']);
    bindModalDismiss('acc-modal-mask', ['acc-cancel']);
  }

  async function init() {
    bindModalDismissers();
    bindTabs();
    bindKnowledge();
    bindDocuments();
    bindSessions();
    bindFallbacks();
    bindBot();
    bindEmbedding();
    bindRules();
    bindAgents();

    // 顶栏信息（失败也不阻塞页面主体）
    try { await loadHealth(); } catch (err) { toast('健康检查失败：' + err.message, true); }
    try { await loadAuth(); } catch (err) { reportError(err); }

    // 默认页签：知识库
    try {
      await loadKnowledge();
      state.loadedTabs.kb = true;
    } catch (err) {
      reportError(err);
    }
  }

  document.addEventListener('DOMContentLoaded', init);
})();
