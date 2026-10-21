(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  let mode = 'login', csrf = '', registration = {mode:'open', initial_credit:200};
  function select(value) {
    if (value === 'register' && registration.mode === 'closed') value = 'login';
    mode = value; $('emailField').hidden = value !== 'register'; $('activationField').hidden = value !== 'activate';
    $('invitationField').hidden = value !== 'register' || registration.mode !== 'invite';
    $('invitation').required = !$('invitationField').hidden;
    $('activation').required = value === 'activate';
    $('username').readOnly = value === 'activate'; if (value === 'activate') $('username').value = 'moy';
    $('password').autocomplete = value === 'login' ? 'current-password' : 'new-password';
    $('password').minLength = value === 'login' ? 1 : 10;
    $('authTitle').textContent = {login:'欢迎回来', register:'创建你的账号', activate:'启用 moy 管理员'}[value];
    $('authDescription').textContent = {login:'登录后，继续你的项目与图片创作。', register:`${registration.mode === 'invite' ? '凭邀请码注册，' : ''}新账号获得 ${registration.initial_credit} 积分，拥有独立的工作空间。`, activate:'设置密码后，原有项目会出现在你的默认工作空间。'}[value];
    $('authSubmit').textContent = value === 'login' ? '登录工作台' : value === 'register' ? '创建账号并登录' : '设置密码并登录';
    $('authMessage').textContent = '';
  }
  ['login','register','activate'].forEach(value => $(value + 'Tab').addEventListener('click', () => select(value)));
  async function post(path, body) {
    const response = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json', 'X-Workbench-CSRF':csrf}, body:JSON.stringify(body)});
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || '操作未完成'); return data;
  }
  $('authForm').addEventListener('submit', async event => {
    event.preventDefault(); $('authSubmit').disabled = true;
    try {
      const credentials = {username:$('username').value.trim(), password:$('password').value};
      if (mode === 'register') await post('/api/auth/register', {...credentials, email:$('email').value.trim() || null, invitation_code:$('invitation').value.trim() || null});
      if (mode === 'activate') {
        const token = $('activation').value.trim();
        if (token.length < 32 || token.length > 128) throw new Error('请填写完整的本机启用凭据（32–128 个字符）。');
        await post('/api/auth/activate', {activation_token:token, password:credentials.password});
      }
      await post('/api/auth/login', credentials); location.replace('/');
    } catch (error) { $('authMessage').textContent = error.message; $('authMessage').classList.add('error'); }
    finally { $('authSubmit').disabled = false; }
  });
  const activation = new URLSearchParams(location.hash.slice(1)).get('activation');
  if (activation) { select('activate'); $('activation').value = activation; history.replaceState(null, '', '/login'); }
  fetch('/api/auth/config', {cache:'no-store'}).then(response => { if (!response.ok) throw new Error('配置不可用'); return response.json(); }).then(data => {
    csrf = data.csrf_token;
    registration = data.registration || {mode:'open', initial_credit:200};
    $('registerTab').hidden = registration.mode === 'closed';
    $('registrationNotice').hidden = registration.mode !== 'closed';
    select(mode);
    $('authSubmit').disabled = false;
    $('practiceNotice').hidden = data.backend !== 'local';
    if (data.backend === 'local') document.title = '演练环境（固定测试图）· Image Workbench';
  }).catch(() => { $('authMessage').textContent = '连接失败，请刷新后重试。'; });
})();
