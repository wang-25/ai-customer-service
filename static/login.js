/* 登录页：仅在 AUTH_ENABLED=1 时需要。 */
(() => {
  const $ = (id) => document.getElementById(id);
  const toastEl = $('toast');

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function toast(text, isErr = false) {
    toastEl.textContent = text;
    toastEl.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (toastEl.className = 'toast'), 2400);
  }

  async function loadStatus() {
    try {
      const res = await fetch('/api/auth/status');
      const data = await res.json();
      if (!data.auth_enabled) {
        $('notice').style.display = 'block';
        $('notice').textContent = '当前未启用权限认证（演示模式），可直接访问 /agent 与 /admin。';
      }
      const demos = data.demo_accounts || [];
      if (demos.length) {
        $('demo-box').style.display = 'block';
        $('demo-list').innerHTML = demos.map((d) =>
          `<div class="row-demo" data-u="${esc(d.username)}" data-p="${esc(d.password)}">
             <span>${esc(d.username)} / ${esc(d.password)}</span><span>${esc(d.role)}</span>
           </div>`).join('');
        $('demo-list').querySelectorAll('.row-demo').forEach((el) => {
          el.onclick = () => {
            $('username').value = el.dataset.u;
            $('password').value = el.dataset.p;
          };
        });
      }
    } catch (e) { /* ignore */ }
  }

  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const username = $('username').value.trim();
    const password = $('password').value;
    if (!username || !password) { toast('请输入用户名和密码', true); return; }
    $('btn-login').disabled = true;
    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || '登录失败');
      localStorage.setItem('cs_token', data.token);
      localStorage.setItem('cs_account', JSON.stringify(data.account));
      toast('登录成功，正在跳转…');
      const role = data.account.role;
      const target = (role === 'agent') ? '/agent' : (role === 'editor' ? '/admin' : '/dashboard');
      setTimeout(() => { location.href = target; }, 500);
    } catch (err) {
      toast(err.message, true);
    } finally {
      $('btn-login').disabled = false;
    }
  });

  loadStatus();
})();
