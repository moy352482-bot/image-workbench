// Account and workspace UI communicates with the page through callbacks.
(() => {
  'use strict';
  function create({ query: $, api, appendText, formatDate, toast, closeImagePreviews,
                    loadProjects, getAccount, onAccountChanged }) {
    function accountDialog(title) {
      $('#accountDialogTitle').textContent = title; const content = $('#accountDialogContent'); content.replaceChildren(); $('#accountDialog').showModal(); return content;
    }
    async function refreshAccount() {
      const account = await api('/api/auth/me'); onAccountChanged(account);
      $('#accountName').textContent = `${account.username}${account.is_admin ? ' · 管理员' : ''}`;
      $('#adminLink').hidden = !account.is_admin;
      const credits = account.credits;
      $('#creditSummary').textContent = credits.is_unlimited ? '积分额度：无限' : `可用 ${credits.remaining_credit} 积分 · 冻结 ${credits.frozen_credit}`;
      const select = $('#workspaceSelect'); select.replaceChildren();
      for (const workspace of account.workspaces) { const option = appendText(select, 'option', '', workspace.name); option.value = workspace.workspace_id; option.selected = workspace.workspace_id === account.workspace_id; }
    }
    $('#closeAccountDialog').addEventListener('click', () => $('#accountDialog').close());
    $('#workspaceSelect').addEventListener('change', async event => { closeImagePreviews(); try { await api('/api/workspaces/select', {method:'POST', body:JSON.stringify({workspace_id:event.target.value})}); location.assign('/'); } catch(error) { toast(error.message, true); } });
    $('#logoutButton').addEventListener('click', async () => { closeImagePreviews(); try { await api('/api/auth/logout', {method:'POST'}); location.replace('/login'); } catch(error) { toast(error.message, true); } });
    $('#newWorkspaceButton').addEventListener('click', () => {
      const content = accountDialog('新建工作空间'); const form = appendText(content, 'form', '', '');
      const label = appendText(form, 'label', 'field-label', '工作空间名称'); label.htmlFor = 'workspaceNameInput';
      const input = appendText(form, 'input', 'workspace-destination', ''); input.id = 'workspaceNameInput'; input.required = true; input.maxLength = 120;
      const submit = appendText(form, 'button', 'primary-button', '创建工作空间'); submit.type = 'submit';
      form.addEventListener('submit', async event => { event.preventDefault(); submit.disabled = true; try { const workspace = await api('/api/workspaces', {method:'POST', body:JSON.stringify({name:input.value.trim()})}); await api('/api/workspaces/select', {method:'POST', body:JSON.stringify({workspace_id:workspace.workspace_id})}); location.assign('/'); } catch(error) { toast(error.message, true); submit.disabled = false; } }); input.focus();
    });
    $('#creditHistoryButton').addEventListener('click', async () => {
      try { const payload = await api('/api/credits'); await refreshAccount(); const content = accountDialog('积分记录');
        appendText(content, 'p', 'dialog-summary', payload.credits.is_unlimited ? '管理员无限额度' : `可用 ${payload.credits.remaining_credit} · 已使用 ${payload.credits.used_credit} · 冻结 ${payload.credits.frozen_credit}`);
        const list = appendText(content, 'div', 'ledger-list', ''); const names = {initial_grant:'初始赠送', freeze:'冻结', consume:'扣除', refund:'退回'};
        for (const entry of payload.transactions) { const row = appendText(list, 'div', 'ledger-row', ''); appendText(row, 'span', '', `${names[entry.type] || entry.type} · ${formatDate(entry.created_at, true)}`); appendText(row, 'strong', '', `${entry.amount} 积分`); }
      } catch(error) { toast(error.message, true); }
    });
    $('#deletedProjectsButton').addEventListener('click', async () => {
      try { const payload = await api('/api/project-actions/deleted'); const content = accountDialog('项目回收站');
        if (!payload.projects.length) appendText(content, 'p', 'dialog-summary', '回收站为空。');
        for (const project of payload.projects) { const row = appendText(content, 'div', 'deleted-row', ''); appendText(row, 'span', '', project.name); const button = appendText(row, 'button', 'secondary-button', '恢复'); button.type = 'button'; button.addEventListener('click', async () => { try { await api(`/api/project-actions/${project.main_project_id}/restore`, {method:'POST'}); row.remove(); await loadProjects(); toast('项目已恢复'); } catch(error) { toast(error.message, true); } }); }
      } catch(error) { toast(error.message, true); }
    });
    window.setInterval(() => { if (getAccount()) refreshAccount().catch(() => {}); }, 15000);
    return { accountDialog, refreshAccount };
  }
  window.WorkbenchAccounts = Object.freeze({ create });
})();
