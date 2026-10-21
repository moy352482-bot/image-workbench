(() => {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const requestedParams = new URL(location.href).searchParams;
  const state = {
    config: null, csrf: '', projects: [], subprojects: [], tasks: [], projectId: '', subprojectId: '', taskId: '', detail: null,
    overview: null, files: [], referenceArtifacts: [], baseArtifact: null, polling: 0, busy: false, selectionVersion: 0, projectsLoadVersion: 0,
    account: null, showArchived: false,
  };
  const storageKeys = { project: 'image-workbench.project', subproject: 'image-workbench.subproject', task: 'image-workbench.task', pendingSave: 'image-workbench.pending-task-request', pendingProject: 'image-workbench.pending-project-request' };
  const ui = {
    projectList: $('#projectList'), projectName: $('#activeProjectName'), heading: $('#pageHeading'),
    taskList: $('#taskList'), taskCount: $('#taskCount'), prompt: $('#promptInput'), promptCount: $('#promptCount'),
    ratio: $('#ratioSelect'), imageInput: $('#imageInput'), previews: $('#filePreviewList'), basePanel: $('#baseImagePanel'),
    basePreview: $('#baseImagePreview'), refUpload: $('#referenceUploadPanel'), form: $('#taskForm'),
    saveButton: $('#saveTaskButton'), formMessage: $('#formMessage'), resultContent: $('#resultContent'),
    details: $('#taskDetails'), resultState: $('#resultState'), backendBadge: $('#backendBadge'), workerBadge: $('#workerBadge'),
    toastRegion: $('#toastRegion'), projectDialog: $('#projectDialog'), projectForm: $('#projectForm'),
    projectNameInput: $('#projectNameInput'), projectDescriptionInput: $('#projectDescriptionInput'), projectSubmit: $('#createProjectSubmit'), imageDialog: $('#imageDialog'),
    mobileProjectsToggle: $('#mobileProjectsToggle'), editHelp: $('#editHelp'),
    mainOverview: $('#mainOverview'), welcome: $('#welcomeRow'), workspace: $('#workspaceGrid'), mainName: $('#activeMainName'), childCrumb: $('#breadcrumbChild'),
    taskEditDialog: $('#taskEditDialog'), moveTaskDialog: $('#moveTaskDialog'),
  };

  const { formatDate, formatBytes, formatSeconds, executionDuration, executionTimingText, statusText, artifactLabel, statusClass, modeName } = window.WorkbenchFormat;
  const { closeImagePreviews, openLightbox } = window.WorkbenchPreview.create({ query: $, dialog: ui.imageDialog });
  const api = window.WorkbenchApi.create({
    getWorkspaceId: () => state.account?.workspace_id || (/^[0-9a-f]{32}$/.test(requestedParams.get('workspace_id') || '') ? requestedParams.get('workspace_id') : null),
    getCsrfToken: () => state.csrf,
    onUnauthorized: () => { closeImagePreviews(); location.replace('/login'); },
  });
  const { accountDialog, refreshAccount } = window.WorkbenchAccounts.create({
    query: $, api, appendText, formatDate, toast, closeImagePreviews, loadProjects,
    getAccount: () => state.account, onAccountChanged: account => { state.account = account; },
  });

  function requestId() {
    const bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  }
  function setMessage(message, isError = false) {
    ui.formMessage.textContent = message || '';
    ui.formMessage.classList.toggle('error', Boolean(isError));
  }
  function toast(message, isError = false) {
    const node = document.createElement('div');
    node.className = `toast${isError ? ' error' : ''}`;
    node.textContent = message;
    ui.toastRegion.append(node);
    window.setTimeout(() => node.remove(), 4200);
  }
  function setBusy(value) {
    state.busy = value;
    ui.saveButton.disabled = value || !state.subprojectId || Boolean(state.config?.project_migration_required);
    $('#createProjectSubmit').disabled = value || Boolean(state.config?.project_migration_required);
  }
  function writesBlocked() { return Boolean(state.config?.project_migration_required); }
  function clearDraftMedia() {
    state.baseArtifact = null;
    state.files = []; state.referenceArtifacts = [];
    ui.imageInput.value = '';
    ui.basePanel.hidden = true;
    ui.refUpload.hidden = false;
    ui.basePreview.replaceChildren();
    localStorage.removeItem(storageKeys.pendingSave);
    renderFiles();
    updateEditHelp();
  }
  function appendText(parent, tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text == null ? '' : String(text);
    parent.append(node);
    return node;
  }
  function safeImage(artifact, alt) {
    const image = document.createElement('img');
    image.src = artifact.url;
    image.alt = alt;
    image.loading = 'lazy';
    image.referrerPolicy = 'same-origin';
    return image;
  }
  function saveLocalSelection() {
    if (state.projectId) localStorage.setItem(storageKeys.project, state.projectId);
    if (state.subprojectId) localStorage.setItem(storageKeys.subproject, state.subprojectId);
    if (state.taskId) localStorage.setItem(storageKeys.task, state.taskId);
  }
  function renderProjects() {
    ui.projectList.replaceChildren();
    if (state.projects.length === 0) {
      appendText(ui.projectList, 'div', 'project-item', '还没有项目');
      return;
    }
    for (const project of state.projects) {
      const mainId = project.main_project_id || project.project_id;
      const group = appendText(ui.projectList, 'div', `project-tree-group${mainId === state.projectId ? ' expanded' : ''}`, '');
      const row = appendText(group, 'div', 'project-tree-main', '');
      const expand = appendText(row, 'button', 'project-expand', mainId === state.projectId ? '⌄' : '›');
      expand.type = 'button'; expand.setAttribute('aria-label', '展开或折叠子项目');
      expand.setAttribute('aria-expanded', String(mainId === state.projectId));
      expand.addEventListener('click', () => { const expanded = group.classList.toggle('expanded'); expand.textContent = expanded ? '⌄' : '›'; expand.setAttribute('aria-expanded', String(expanded)); });
      const main = appendText(row, 'button', `project-item${mainId === state.projectId && !state.subprojectId ? ' selected' : ''}`, '');
      const marker = appendText(main, 'span', 'project-symbol', ''); marker.setAttribute('aria-hidden', 'true');
      appendText(main, 'span', 'project-name', project.name || '未命名主项目');
      main.addEventListener('click', () => selectMainProject(mainId));
      const options = appendText(row, 'button', 'project-options', '⋯'); options.type = 'button'; options.setAttribute('aria-label', `管理项目 ${project.name}`);
      options.addEventListener('click', async () => { await selectMainProject(mainId); openProjectDialog('edit-main', project); });
      const children = appendText(group, 'div', 'project-tree-children', '');
      for (const sub of (project.subprojects || [])) {
        const child = appendText(children, 'button', `project-child${sub.subproject_id === state.subprojectId ? ' selected' : ''}`, '');
        child.type = 'button';
        appendText(child, 'span', 'project-child-dot', '');
        appendText(child, 'span', 'project-name', sub.name || '未命名子项目');
        appendText(child, 'span', 'project-child-count', Number(sub.task_count) || 0);
        child.addEventListener('click', () => selectSubproject(mainId, sub.subproject_id));
      }
    }
  }
  async function loadProjects(preferredMainId, preferredSubId) {
    const requestVersion = ++state.projectsLoadVersion;
    const selectionAtStart = state.selectionVersion;
    const payload = await api('/api/main-projects');
    if (requestVersion !== state.projectsLoadVersion) return;
    state.projects = (Array.isArray(payload.main_projects) ? payload.main_projects : []).filter(item => state.showArchived ? item.status === 'archived' : item.status !== 'archived');
    if (selectionAtStart !== state.selectionVersion) { renderProjects(); return; }
    const rememberedMain = preferredMainId || localStorage.getItem(storageKeys.project);
    const chosen = state.projects.find(project => project.project_id === rememberedMain || project.main_project_id === rememberedMain) || state.projects[0];
    const oldMain = state.projectId;
    state.projectId = chosen ? (chosen.main_project_id || chosen.project_id) : '';
    const desiredSub = preferredSubId || localStorage.getItem(storageKeys.subproject);
    const rememberedChild = chosen && (chosen.subprojects || []).find(item => item.subproject_id === desiredSub);
    state.subprojects = chosen ? (chosen.subprojects || []) : [];
    renderProjects();
    if (oldMain && oldMain !== state.projectId) clearDraftMedia();
    if (chosen) {
      localStorage.setItem(storageKeys.project, state.projectId);
      if (rememberedChild) await selectSubproject(state.projectId, rememberedChild.subproject_id, true);
      else await selectMainProject(state.projectId, true);
    } else {
      resetSelection(''); state.overview = null; state.subprojects = [];
      ui.mainName.textContent = '请选择项目'; ui.heading.textContent = '把想法变成图像';
      renderTasks(); showNoTask(); showMainOverview(null);
      localStorage.removeItem(storageKeys.project); localStorage.removeItem(storageKeys.subproject); localStorage.removeItem(storageKeys.task);
    }
  }
  function closeMobileProjectNav() {
    $('.sidebar').classList.remove('mobile-open');
    ui.mobileProjectsToggle.setAttribute('aria-expanded', 'false');
  }
  function resetSelection(mainId, subId = '') {
    closeImagePreviews();
    stopPolling(); state.selectionVersion += 1;
    if (state.projectId !== mainId || state.subprojectId !== subId) clearDraftMedia();
    state.projectId = mainId; state.subprojectId = subId; state.taskId = ''; state.detail = null; state.tasks = [];
    localStorage.setItem(storageKeys.project, mainId);
    if (subId) localStorage.setItem(storageKeys.subproject, subId); else localStorage.removeItem(storageKeys.subproject);
    localStorage.removeItem(storageKeys.task);
    renderProjects(); renderTasks(); showNoTask();
    ui.welcome.hidden = Boolean(mainId);
    ui.mainOverview.hidden = Boolean(subId);
    ui.workspace.hidden = !subId;
    ui.childCrumb.hidden = !subId;
    const main = state.projects.find(item => (item.main_project_id || item.project_id) === mainId);
    if (main) state.subprojects = Array.isArray(main.subprojects) ? main.subprojects : [];
    ui.mainName.textContent = main ? main.name : '主项目';
    const sub = state.subprojects.find(item => item.subproject_id === subId);
    ui.projectName.textContent = sub ? sub.name : '项目总览';
    ui.heading.textContent = sub ? sub.name : (main ? main.name : '创作项目');
    ui.saveButton.disabled = !subId || state.busy || Boolean(state.config.project_migration_required);
    return ++state.selectionVersion;
  }
  async function selectMainProject(projectId, quiet = false) {
    closeMobileProjectNav();
    const version = resetSelection(projectId, '');
    try {
      const payload = await api(`/api/main-projects/${encodeURIComponent(projectId)}`);
      if (version !== state.selectionVersion || state.projectId !== projectId || state.subprojectId) return;
      state.overview = payload.main_project || null;
      state.subprojects = Array.isArray(payload.subprojects) ? payload.subprojects : (state.overview?.subprojects || []);
      renderProjects(); renderMainOverview(payload);
    } catch (error) { if (!quiet) toast(error.message, true); }
  }
  function countMarkup(label, value) {
    const item = appendText(document.createElement('div'), 'div', 'overview-count', '');
    appendText(item, 'strong', '', Number(value) || 0); appendText(item, 'span', '', label); return item;
  }
  function renderMainOverview(payload) {
    const main = payload.main_project || {};
    state.overview = main;
    $('#overviewName').textContent = main.name || '未命名主项目';
    $('#overviewDescription').textContent = main.description || '还没有项目描述。';
    const counts = $('#overviewCounts'); counts.replaceChildren();
    counts.append(countMarkup('子项目', main.subproject_count));
    counts.append(countMarkup('任务', main.task_count));
    counts.append(countMarkup('图片', main.image_count));
    const cards = $('#subprojectCards'); cards.replaceChildren();
    const subs = Array.isArray(payload.subprojects) ? payload.subprojects : (main.subprojects || []);
    state.subprojects = subs;
    if (!subs.length) appendText(cards, 'div', 'overview-empty', '这个主项目还没有子项目，创建一个子项目后即可保存任务。');
    for (const sub of subs) {
      const card = appendText(cards, 'article', 'subproject-card', '');
      appendText(card, 'h4', '', sub.name || '未命名子项目');
      appendText(card, 'p', '', sub.description || '还没有子项目描述。');
      const cardCounts = appendText(card, 'div', 'subproject-card-counts', '');
      appendText(cardCounts, 'span', '', `${Number(sub.task_count) || 0} 个任务`);
      appendText(cardCounts, 'span', '', `${Number(sub.image_count) || 0} 张图片`);
      const actions = appendText(card, 'div', 'subproject-card-actions', '');
      const open = appendText(actions, 'button', 'primary-button', '打开子项目'); open.type = 'button';
      open.addEventListener('click', () => selectSubproject(state.projectId, sub.subproject_id));
      const edit = appendText(actions, 'button', 'secondary-button', '编辑'); edit.type = 'button';
      edit.disabled = writesBlocked();
      edit.addEventListener('click', () => openProjectDialog('edit-sub', sub));
    }
    const recent = $('#recentTasks'); recent.replaceChildren();
    const tasks = Array.isArray(payload.recent_tasks) ? payload.recent_tasks : [];
    if (!tasks.length) appendText(recent, 'div', 'overview-empty', '还没有最近任务。');
    for (const task of tasks) {
      const row = appendText(recent, 'button', 'recent-task-row', ''); row.type = 'button';
      const title = appendText(row, 'span', 'recent-task-title', task.name || task.prompt || '未命名任务');
      appendText(row, 'span', 'recent-task-location', task.subproject_name || '未分类任务');
      appendText(row, 'span', 'recent-task-status', statusText(task.status, task.status_label));
      appendText(row, 'span', 'recent-task-date', formatDate(task.updated_at || task.created_at, true));
      row.addEventListener('click', () => {
        localStorage.setItem(storageKeys.task, task.task_id);
        selectSubproject(state.projectId, task.subproject_id);
      });
    }
    $('#newSubprojectButton').disabled = Boolean(state.config.project_migration_required);
    $('#editMainProjectButton').disabled = Boolean(state.config.project_migration_required);
  }
  function showMainOverview(payload) {
    if (!payload) { ui.welcome.hidden = false; ui.mainOverview.hidden = true; ui.workspace.hidden = true; $('#overviewName').textContent = ''; $('#overviewDescription').textContent = ''; $('#overviewCounts').replaceChildren(); $('#subprojectCards').replaceChildren(); $('#recentTasks').replaceChildren(); return; }
    renderMainOverview(payload);
  }
  async function selectSubproject(mainId, subId, quiet = false) {
    closeMobileProjectNav();
    const preferredTaskId = localStorage.getItem(storageKeys.task);
    const version = resetSelection(mainId, subId);
    try {
      const payload = await api(`/api/subprojects/${encodeURIComponent(subId)}/tasks`);
      if (version !== state.selectionVersion || state.projectId !== mainId || state.subprojectId !== subId) return;
      state.tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
      state.subprojects = state.projects.find(p => (p.main_project_id || p.project_id) === mainId)?.subprojects || [];
      const chosen = state.tasks.find(task => task.task_id === preferredTaskId) || state.tasks[0];
      state.taskId = chosen ? chosen.task_id : '';
      renderProjects(); renderTasks();
      if (chosen) await loadTaskDetail(chosen.task_id, true, version);
      else showNoTask();
    } catch (error) { if (!quiet) toast(error.message, true); }
  }
  async function selectProject(projectId) {
    return selectMainProject(projectId);
  }
  async function loadTasks(preferredId) {
    if (!state.subprojectId) return;
    const version = state.selectionVersion;
    try {
      const payload = await api(`/api/subprojects/${encodeURIComponent(state.subprojectId)}/tasks`);
      if (version !== state.selectionVersion) return;
      state.tasks = Array.isArray(payload.tasks) ? payload.tasks : [];
      if (payload.subproject) {
        const main = state.projects.find(item => (item.main_project_id || item.project_id) === state.projectId);
        if (main) {
          const children = Array.isArray(main.subprojects) ? main.subprojects : [];
          main.subprojects = children.some(item => item.subproject_id === payload.subproject.subproject_id)
            ? children.map(item => item.subproject_id === payload.subproject.subproject_id ? payload.subproject : item)
            : [...children, payload.subproject];
        }
      }
      const remembered = preferredId || localStorage.getItem(storageKeys.task);
      const chosen = state.tasks.find(task => task.task_id === remembered) || state.tasks[0];
      state.taskId = chosen ? chosen.task_id : '';
      renderProjects();
      renderTasks();
      if (chosen) await loadTaskDetail(chosen.task_id, false);
      else { state.detail = null; showNoTask(); }
    } catch (error) {
      if (version !== state.selectionVersion) return;
      toast(error.message, true);
      state.tasks = [];
      renderTasks();
      showNoTask();
    }
  }
  function renderTasks() {
    ui.taskList.replaceChildren();
    ui.taskCount.textContent = `${state.tasks.length} 个任务`;
    if (!state.tasks.length) {
      const empty = document.createElement('div');
      empty.className = 'list-empty';
      appendText(empty, 'span', 'empty-orbit', '✳');
      appendText(empty, 'strong', '', '还没有任务');
      appendText(empty, 'span', '', state.projectId ? '保存一个任务后，它会出现在这里。' : '先创建一个项目，再开始创作。');
      ui.taskList.append(empty);
      return;
    }
    for (const task of state.tasks) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `task-row${task.task_id === state.taskId ? ' selected' : ''}`;
      const thumb = appendText(button, 'span', 'task-thumb', '✧');
      const main = appendText(button, 'span', 'task-row-main', '');
      appendText(main, 'span', 'task-row-title', task.name || task.prompt || '未填写描述');
      const sub = appendText(main, 'span', 'task-row-sub', '');
      appendText(sub, 'span', '', modeName(task.operation));
      appendText(sub, 'span', '', `· ${Number(task.input_count) || 0} 张参考`);
      const seconds = executionDuration(task);
      if (!['draft', 'queued', 'generating', 'checking', 'uncertain'].includes(task.status) && seconds !== null && seconds !== undefined) {
        appendText(sub, 'span', '', `· 用时 ${formatSeconds(seconds)}`);
      }
      const right = appendText(button, 'span', 'task-row-right', '');
      const status = appendText(right, 'span', statusClass(task.status), statusText(task.status, task.status_label));
      status.classList.add('status-pill');
      appendText(right, 'span', 'task-date', formatDate(task.created_at, true));
      button.addEventListener('click', () => selectTask(task.task_id));
      const actions = appendText(right, 'span', 'task-row-actions', '');
      const edit = appendText(actions, 'button', 'task-inline-action', '编辑'); edit.type = 'button';
      edit.disabled = writesBlocked();
      edit.addEventListener('click', event => { event.stopPropagation(); openTaskEdit(task); });
      const move = appendText(actions, 'button', 'task-inline-action', '移动'); move.type = 'button';
      move.disabled = writesBlocked();
      move.addEventListener('click', event => { event.stopPropagation(); openMoveTask(task); });
      ui.taskList.append(button);
      const thumbHolder = thumb;
      const matchingDetail = state.detail && state.detail.task_id === task.task_id ? state.detail : null;
      const preview = task.preview_artifact || (matchingDetail && matchingDetail.outputs && matchingDetail.outputs[0]);
      if (preview) {
        thumbHolder.replaceChildren(safeImage(preview, '任务结果缩略图'));
      }
    }
  }
  async function selectTask(taskId) {
    if (taskId === state.taskId) return;
    closeImagePreviews();
    state.taskId = taskId;
    state.detail = null;
    saveLocalSelection();
    renderTasks();
    await loadTaskDetail(taskId, false);
  }
  function showNoTask() {
    stopPolling();
    ui.details.hidden = true;
    ui.resultContent.hidden = false;
    ui.resultState.className = 'status-pill status-neutral';
    ui.resultState.textContent = state.projectId ? '等待选择' : '尚未开始';
    renderTasks();
  }
  async function loadTaskDetail(taskId, quiet, expectedVersion) {
    if (!state.subprojectId || !taskId) return;
    const version = expectedVersion ?? state.selectionVersion;
    try {
      const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
      if (state.taskId !== taskId || state.selectionVersion !== version) return;
      state.detail = detail;
      state.tasks = state.tasks.map(task => task.task_id === taskId ? { ...task, ...detail } : task);
      saveLocalSelection();
      renderTasks();
      renderDetail(detail);
      if (['queued', 'generating', 'checking', 'uncertain'].includes(detail.status)) startPolling();
      else stopPolling();
    } catch (error) {
      if (state.selectionVersion !== version) return;
      if (!quiet) toast(error.message, true);
      if (!quiet) showNoTask();
    }
  }
  function startPolling() {
    if (state.polling) return;
    state.polling = window.setInterval(() => { if (state.taskId && !state.busy) loadTaskDetail(state.taskId, true); }, 3000);
  }
  function stopPolling() {
    if (state.polling) window.clearInterval(state.polling);
    state.polling = 0;
  }
  function setResultStatus(detail) {
    const status = detail.status;
    ui.resultState.className = `status-pill ${statusClass(status)}`;
    ui.resultState.textContent = statusText(status, detail.status_label);
  }
  function renderDetail(detail) {
    ui.resultContent.hidden = true;
    ui.details.hidden = false;
    ui.details.replaceChildren();
    setResultStatus(detail);
    const banner = document.createElement('div');
    banner.className = 'detail-banner';
    const idLine = appendText(banner, 'div', 'task-id', '');
    appendText(idLine, 'span', '', `任务 ID  ${detail.task_id || ''}`);
    const copy = appendText(idLine, 'button', 'copy-button', '复制');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try { await navigator.clipboard.writeText(detail.task_id || ''); toast('任务 ID 已复制'); }
      catch (error) { toast('无法访问剪贴板，请手动选择任务 ID。', true); }
    });
    appendText(banner, 'span', 'task-date', formatDate(detail.created_at));
    ui.details.append(banner);
    const taskTools = document.createElement('div'); taskTools.className = 'task-controls';
    const editInfo = appendText(taskTools, 'button', 'secondary-button', '编辑名称与描述'); editInfo.type = 'button'; editInfo.addEventListener('click', () => openTaskEdit(detail));
    const moveInfo = appendText(taskTools, 'button', 'secondary-button', '移动到子项目'); moveInfo.type = 'button'; moveInfo.addEventListener('click', () => openMoveTask(detail));
    ui.details.append(taskTools);
    if (detail.execution_backend === 'local') {
      appendText(ui.details, 'div', 'practice-notice', '这是演练使用的固定测试图片，未调用 Codex，也未按提示词生成。');
    }
    if (typeof detail.estimated_credit === 'number') {
      const labels = {waiting_credit:'执行前冻结', credit_insufficient:'积分不足', credit_frozen:'已冻结', running:'执行中，积分已冻结', completed:'已扣除', refunded:'失败已退回'};
      const amount = typeof detail.reserved_credit === 'number' ? detail.reserved_credit : detail.estimated_credit;
      appendText(ui.details, 'div', 'credit-task-note', `本次 ${amount} 积分 · ${labels[detail.credit_status] || ''}`);
      if ((detail.can_generate || detail.can_retry || detail.can_restore) && amount !== detail.estimated_credit) {
        appendText(ui.details, 'div', 'credit-task-note', `再次执行预计 ${detail.estimated_credit} 积分，提交时冻结。`);
      }
    }

    const controls = document.createElement('div');
    controls.className = 'task-controls';
    if (detail.can_generate || detail.can_retry) {
      const run = appendText(controls, 'button', 'run-button', detail.can_retry ? (state.config.backend === 'local' ? '重新演练' : '重试生成') : (state.config.backend === 'local' ? '开始演练' : '开始生成'));
      run.type = 'button';
      run.disabled = state.busy || writesBlocked();
      run.addEventListener('click', () => runTask(detail));
    }
    if (detail.can_restore) {
      const restore = appendText(controls, 'button', 'secondary-button restore-button', '恢复归档结果');
      restore.type = 'button';
      restore.disabled = state.busy || writesBlocked();
      restore.addEventListener('click', () => restoreTask(detail));
    }
    if (controls.childElementCount) ui.details.append(controls);

    const timingText = executionTimingText(detail);
    if (timingText) {
      const statusLine = document.createElement('div');
      statusLine.className = 'detail-error';
      statusLine.classList.add('detail-progress');
      appendText(statusLine, 'span', '', timingText);
      statusLine.title = detail.execution_timing_source === 'job_clock'
        ? '从本次执行器开始到作业结束，不含排队；远端任务包含图片回传与登记。'
        : '从本次执行开始计时，不含任务保存后的等待和排队时间。';
      ui.details.append(statusLine);
    }
    if (detail.error_summary) appendText(ui.details, 'div', 'detail-error', detail.error_summary);

    const outputs = Array.isArray(detail.outputs) ? detail.outputs : [];
    if (outputs.length) {
      const grid = document.createElement('div');
      grid.className = 'result-grid';
      for (const artifact of outputs) grid.append(renderArtifact(artifact, detail.base_image));
      ui.details.append(grid);
    } else if (!['queued', 'generating', 'checking'].includes(detail.status)) {
      appendText(ui.details, 'div', 'result-empty-copy', '');
      const hint = ui.details.lastElementChild;
      appendText(hint, 'strong', '', detail.status === 'failed' ? '这次执行没有产出图片' : '还没有结果图片');
      appendText(hint, 'span', '', detail.status === 'failed' ? '查看上方错误摘要，调整需求后可显式重试。' : '保存任务后，可在准备好时点击开始执行。');
    }

    const meta = document.createElement('div');
    meta.className = 'detail-meta';
    appendText(meta, 'span', '', `${detail.name || '未命名任务'} · ${modeName(detail.operation)} · ${detail.aspect_ratio || 'auto'}`);
    appendText(meta, 'span', '', `${Number(detail.input_count) || 0} 张参考图 · ${outputs.length} 张结果`);
    ui.details.append(meta);

    if (detail.base_image) {
      const section = makeSection('基底图片');
      const chip = document.createElement('div');
      chip.className = 'input-chip';
      chip.append(safeImage(detail.base_image, '任务基底图片'));
      appendText(chip, 'span', '', `${detail.base_image.width || '—'} × ${detail.base_image.height || '—'} · ${String(detail.base_image.format || '').toUpperCase()}`);
      section.append(chip);
      ui.details.append(section);
    }
    if (detail.prompt) {
      const section = makeSection('创作需求');
      appendText(section, 'div', 'prompt-preview', detail.prompt);
      ui.details.append(section);
    }
    if (detail.description) {
      const section = makeSection('任务描述');
      appendText(section, 'div', 'prompt-preview', detail.description);
      ui.details.append(section);
    }
    if (Array.isArray(detail.inputs) && detail.inputs.length) {
      const section = makeSection('输入图片');
      const chips = appendText(section, 'div', 'input-chip-list', '');
      for (const artifact of detail.inputs) {
        const chip = document.createElement('div');
        chip.className = 'input-chip';
        chip.append(safeImage(artifact, '输入图片预览'));
        appendText(chip, 'span', '', `${String(artifact.format || '').toUpperCase()} · ${artifact.width || '—'} × ${artifact.height || '—'} · ${formatBytes(artifact.size_bytes)}`);
        chips.append(chip);
      }
      ui.details.append(section);
    }
    if (detail.timings) {
      const remoteArchive = String(detail.execution_backend || '').startsWith('remote-');
      const section = makeSection(remoteArchive ? '服务端归档耗时' : '执行阶段耗时');
      const timings = appendText(section, 'div', 'timing-grid', '');
      const labels = remoteArchive
        ? [['validate_seconds', '检查'], ['register_seconds', '登记'], ['total_seconds', '归档总耗时']]
        : [['prepare_seconds', '准备'], [detail.timings.codex_seconds == null ? 'backend_seconds' : 'codex_seconds', detail.timings.codex_seconds == null ? '后端执行' : 'Codex 执行'], ['validate_seconds', '检查'], ['register_seconds', '登记'], ['total_seconds', '执行记录合计']];
      for (const [key, label] of labels) {
        const cell = appendText(timings, 'div', 'timing-item', '');
        appendText(cell, 'span', '', label);
        appendText(cell, 'strong', '', formatSeconds(detail.timings[key]));
      }
      ui.details.append(section);
    }
    if (Array.isArray(detail.executions) && detail.executions.length) {
      const section = makeSection('执行记录');
      const list = appendText(section, 'div', 'execution-list', '');
      for (const execution of detail.executions) {
        const row = appendText(list, 'div', 'execution-row', '');
        appendText(row, 'span', '', `${statusText(execution.status)} · ${formatDate(execution.created_at)}`);
        appendText(row, 'span', '', `${execution.failure_type || '执行记录'} · ${formatSeconds(execution.total_seconds)}`);
      }
      ui.details.append(section);
    }
    appendSourceVersions(detail);
  }
  function makeSection(title) {
    const section = document.createElement('section');
    section.className = 'detail-section';
    appendText(section, 'h3', 'detail-section-title', title);
    return section;
  }
  function renderArtifact(artifact, baseArtifact) {
    const card = document.createElement('article');
    card.className = 'artifact-card';
    const imageWrap = document.createElement('button');
    imageWrap.type = 'button';
    imageWrap.className = 'artifact-image-wrap';
    imageWrap.setAttribute('aria-label', `查看结果大图，${artifact.width || '未知'} × ${artifact.height || '未知'}`);
    imageWrap.append(safeImage(artifact, `任务结果图片，${artifact.width || ''} × ${artifact.height || ''}`));
    appendText(imageWrap, 'span', 'artifact-kind', artifactLabel(artifact));
    imageWrap.addEventListener('click', () => openLightbox(artifact, imageWrap));
    card.append(imageWrap);
    const info = appendText(card, 'div', 'artifact-info', '');
    appendText(info, 'div', 'artifact-meta', `${artifact.width || '—'} × ${artifact.height || '—'} · ${String(artifact.format || '').toUpperCase()} · ${formatBytes(artifact.size_bytes)}`);
    appendText(info, 'div', 'artifact-meta', `来源版本 · ${artifact.source_artifact_id || '原始输出'} · ${formatDate(artifact.created_at, true)}`);
    const actions = appendText(info, 'div', 'artifact-actions', '');
    const download = appendText(actions, 'a', 'secondary-button', '下载');
    download.href = artifact.download_url;
    download.setAttribute('download', '');
    if (baseArtifact && window.WorkbenchImageComparison) {
      const compare = appendText(actions, 'button', 'secondary-button compare-artifact', '与基底对比');
      compare.type = 'button';
      compare.addEventListener('click', async () => {
        closeImagePreviews();
        try { await window.WorkbenchImageComparison.open(baseArtifact, artifact, { trigger: compare }); }
        catch (error) { toast('图片对比暂时不可用，请重新打开或下载原图查看。', true); }
      });
    }
    if (artifact.review_status !== 'approved') {
      const approve = appendText(actions, 'button', 'secondary-button approve-artifact', '确认通过');
      approve.type = 'button';
      approve.disabled = writesBlocked();
      approve.addEventListener('click', () => approveArtifact(artifact));
    }
    const branch = appendText(actions, 'button', 'secondary-button', '基于此图继续修改');
    branch.type = 'button';
    branch.addEventListener('click', () => startEditFromArtifact(artifact));
    const sendToVideo = async (purpose, navigate=true) => {
      const project = await api('/api/video/projects/from-subproject',{method:'POST',body:JSON.stringify({subproject_id:artifact.subproject_id})});
      await api(`/api/video/projects/${project.project_id}/tray`,{method:'POST',body:JSON.stringify({artifact_id:artifact.artifact_id,media_type:'image',action:'add'})});
      if(navigate) location.assign(`/video?${new URLSearchParams({workspace_id:state.account.workspace_id,project:project.project_id,asset_id:artifact.artifact_id,asset_action:purpose})}#pipelineVideos`);
      else toast('已加入当前子项目的素材暂存区。');
    };
    actions.append(window.IWAssetActions.menu([
      {label:'继续修改',run:()=>startEditFromArtifact(artifact)},
      {label:'用于视频生成',run:()=>sendToVideo('video')},
      {label:'设为参考图',run:async()=>{ const context=imageContext(); if(context) await attachImages([artifact],'reference',context); }},
      {label:'设为首帧',run:()=>sendToVideo('first')},{label:'设为尾帧',run:()=>sendToVideo('last')},
      {label:'加入素材暂存区',run:()=>sendToVideo('video',false)},{label:'下载',href:artifact.download_url}
    ],toast));
    return card;
  }
  function appendSourceVersions(detail) {
    const artifacts = [...(detail.outputs || []), ...(detail.inputs || [])];
    const ids = new Set();
    for (const artifact of artifacts) {
      if (!artifact.source_artifact_id || ids.has(artifact.source_artifact_id)) continue;
      ids.add(artifact.source_artifact_id);
      const section = makeSection('来源版本');
      appendText(section, 'div', 'prompt-preview', `来源图片 ID：${artifact.source_artifact_id}`);
      ui.details.append(section);
    }
  }
  async function runTask(detail) {
    if (writesBlocked()) { toast('已有数据需要先完成项目层级迁移。', true); return; }
    if (state.busy) return;
    setBusy(true);
    setMessage('正在提交执行请求…');
    try {
      const body = { request_id: requestId(), confirm_generation: true };
      if (detail.can_retry) body.retry_of = (detail.current_job && detail.current_job.job_id) || detail.last_execution_id;
      await api(`/api/tasks/${encodeURIComponent(detail.task_id)}/run`, { method: 'POST', body: JSON.stringify(body) });
      setMessage('已提交，正在读取执行状态。');
      toast(state.config.backend === 'local' ? '演练已开始，正在检查任务状态。' : '执行已提交，正在检查任务状态。');
      await loadTaskDetail(detail.task_id, false);
    } catch (error) {
      setMessage(error.message, true);
      toast(error.message, true);
    } finally { await refreshAccount(); setBusy(false); if (state.detail) renderDetail(state.detail); }
  }
  async function restoreTask(detail) {
    if (writesBlocked()) { toast('已有数据需要先完成项目层级迁移。', true); return; }
    if (state.busy) return;
    const executionId = detail.last_execution_id || (detail.executions && detail.executions[0] && detail.executions[0].execution_id);
    if (!executionId) { toast('没有可恢复的归档记录。', true); return; }
    setBusy(true);
    try {
      await api(`/api/tasks/${encodeURIComponent(detail.task_id)}/restore`, { method: 'POST', body: JSON.stringify({ request_id: requestId(), execution_id: executionId }) });
      toast('已提交归档恢复，请等待任务状态更新。');
      await loadTaskDetail(detail.task_id, false);
    } catch (error) { toast(error.message, true); }
    finally { setBusy(false); if (state.detail) renderDetail(state.detail); }
  }
  async function approveArtifact(artifact) {
    if (writesBlocked()) { toast('已有数据需要先完成项目层级迁移。', true); return; }
    try {
      await api(`/api/artifacts/${encodeURIComponent(artifact.artifact_id)}/approve`, { method: 'POST', body: JSON.stringify({}) });
      toast('已标记为通过审核。');
      await loadTasks(state.taskId);
    } catch (error) { toast(error.message, true); }
  }
  function startEditFromArtifact(artifact) {
    const project = state.projects.find(item => (item.main_project_id || item.project_id) === artifact.main_project_id);
    const subproject = (project?.subprojects || []).find(item => item.subproject_id === artifact.subproject_id);
    if (!project || !subproject) { toast('找不到这张图片所属的子项目。', true); return; }
    stopPolling();
    state.selectionVersion += 1;
    state.projectId = project.main_project_id || project.project_id;
    state.subprojectId = subproject.subproject_id;
    state.taskId = '';
    state.detail = null;
    state.baseArtifact = artifact;
    state.files = []; state.referenceArtifacts = [];
    ui.imageInput.value = '';
    localStorage.removeItem(storageKeys.pendingSave);
    localStorage.setItem(storageKeys.project, state.projectId);
    localStorage.setItem(storageKeys.subproject, subproject.subproject_id);
    localStorage.removeItem(storageKeys.task);
    renderProjects();
    ui.welcome.hidden = true; ui.mainOverview.hidden = true; ui.workspace.hidden = false; ui.childCrumb.hidden = false;
    ui.mainName.textContent = project.name;
    ui.projectName.textContent = subproject.name;
    ui.heading.textContent = subproject.name;
    $('input[name="operation"][value="edit"]').checked = true;
    updateEditHelp();
    ui.basePanel.hidden = false;
    ui.basePreview.replaceChildren();
    ui.basePreview.append(safeImage(artifact, '所选基底图片预览'));
    const meta = appendText(ui.basePreview, 'span', 'base-meta', '');
    appendText(meta, 'span', '', `基于 ${String(artifact.format || '').toUpperCase()} 图片`);
    appendText(meta, 'span', '', `${artifact.width || '—'} × ${artifact.height || '—'} · 可直接开新分支`);
    ui.refUpload.hidden = false;
    renderFiles();
    ui.prompt.focus();
    setMessage('已选定基底图片。填写修改需求后保存为新任务。');
    const branchSelectionVersion = state.selectionVersion;
    loadTasks('').then(() => {
      if (branchSelectionVersion !== state.selectionVersion || state.subprojectId !== subproject.subproject_id) return;
      state.taskId = ''; state.detail = null; showNoTask();
    });
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
  function clearBaseArtifact() {
    state.baseArtifact = null;
    ui.basePanel.hidden = true;
    ui.refUpload.hidden = false;
    updateEditHelp();
  }
  let projectDialogMode = 'new-main';
  let projectDialogTarget = null;
  function openProjectDialog(mode = 'new-main', target = null) {
    projectDialogMode = mode; projectDialogTarget = target;
    const editing = mode.startsWith('edit-');
    const sub = mode.includes('sub');
    $('#projectDialogEyebrow').textContent = editing ? (sub ? '子项目设置' : '主项目设置') : (sub ? '新建子项目' : '新建主项目');
    $('#projectDialogTitle').textContent = editing ? `编辑${sub ? '子项目' : '主项目'}` : '为灵感命名';
    $('#createProjectSubmit').textContent = editing ? '保存修改' : `创建${sub ? '子项目' : '主项目'}`;
    ui.projectNameInput.value = editing && target ? target.name || '' : '';
    ui.projectDescriptionInput.value = editing && target ? target.description || '' : '';
    ui.projectNameInput.required = true;
    ui.projectDialog.querySelector('.project-actions')?.remove();
    if (mode === 'edit-main' && target && state.account) {
      const actions = appendText(ui.projectForm, 'div', 'project-actions', '');
      const archive = appendText(actions, 'button', 'secondary-button', target.status === 'archived' ? '取消归档' : '归档项目'); archive.type = 'button';
      archive.addEventListener('click', async () => {
        try { await api(`/api/main-projects/${target.main_project_id}`, {method:'PATCH', body:JSON.stringify({status:target.status === 'archived' ? 'active' : 'archived'})}); ui.projectDialog.close(); await loadProjects(); toast('项目归档状态已更新'); }
        catch (error) { toast(error.message, true); }
      });
      const move = appendText(actions, 'button', 'secondary-button', '移到其他 Workspace'); move.type = 'button';
      move.addEventListener('click', () => {
        ui.projectDialog.close(); const content = accountDialog('移动项目');
        appendText(content, 'p', 'dialog-summary', `将“${target.name}”及全部子项目、任务和图片移到所选工作空间。`);
        const select = appendText(content, 'select', 'workspace-destination', ''); select.setAttribute('aria-label', '目标工作空间');
        for (const workspace of state.account.workspaces.filter(item => item.workspace_id !== state.account.workspace_id)) { const option = appendText(select, 'option', '', workspace.name); option.value = workspace.workspace_id; }
        const submit = appendText(content, 'button', 'primary-button', '移动项目'); submit.type = 'button'; submit.disabled = !select.options.length;
        if (!select.options.length) appendText(content, 'p', 'dialog-summary', '请先在侧栏新建另一个工作空间。');
        submit.addEventListener('click', async () => {
          submit.disabled = true;
          try { await api(`/api/project-actions/${target.main_project_id}/move`, {method:'POST', body:JSON.stringify({workspace_id:select.value})}); $('#accountDialog').close(); await loadProjects(); toast('项目已移入目标工作空间'); }
          catch(error) { toast(error.message, true); submit.disabled = false; }
        });
      });
      const remove = appendText(actions, 'button', 'danger-button', '删除项目'); remove.type = 'button';
      remove.addEventListener('click', () => {
        ui.projectDialog.close(); const content = accountDialog('删除项目');
        appendText(content, 'p', 'dialog-summary', `将“${target.name}”移入回收站。图片和历史记录会保留，可随时恢复。`);
        const submit = appendText(content, 'button', 'danger-button', '确认移入回收站'); submit.type = 'button';
        submit.addEventListener('click', async () => { submit.disabled = true; try { await api(`/api/project-actions/${target.main_project_id}`, {method:'DELETE'}); $('#accountDialog').close(); await loadProjects(); toast('项目已移入回收站'); } catch(error) { toast(error.message, true); submit.disabled = false; } });
      });
    }
    ui.projectDialog.showModal();
    window.setTimeout(() => ui.projectNameInput.focus(), 20);
  }
  function persistentProjectRequestId(name) {
    let saved;
    try { saved = JSON.parse(localStorage.getItem(storageKeys.pendingProject) || 'null'); } catch (error) { saved = null; }
    if (!saved || saved.name !== name) {
      saved = { name, id: requestId() };
      localStorage.setItem(storageKeys.pendingProject, JSON.stringify(saved));
    }
    return saved.id;
  }
  async function createProject(event) {
    event.preventDefault();
    if (writesBlocked()) { toast('已有数据需要先完成项目层级迁移。', true); return; }
    const name = ui.projectNameInput.value.trim();
    const description = ui.projectDescriptionInput.value.trim();
    if (!name || state.busy) return;
    setBusy(true);
    try {
      let payload;
      const completedMode = projectDialogMode;
      if (completedMode === 'edit-main') {
        payload = await api(`/api/main-projects/${encodeURIComponent(state.projectId)}`, { method: 'PATCH', body: JSON.stringify({ name, description }) });
      } else if (completedMode === 'edit-sub') {
        payload = await api(`/api/subprojects/${encodeURIComponent(projectDialogTarget.subproject_id)}`, { method: 'PATCH', body: JSON.stringify({ name, description }) });
      } else if (completedMode === 'new-sub') {
        payload = await api(`/api/main-projects/${encodeURIComponent(state.projectId)}/subprojects`, { method: 'POST', body: JSON.stringify({ request_id: persistentProjectRequestId(`${state.projectId}:${name}`), name, description }) });
      } else {
        payload = await api('/api/main-projects', { method: 'POST', body: JSON.stringify({ request_id: persistentProjectRequestId(name), name, description }) });
      }
      localStorage.removeItem(storageKeys.pendingProject);
      ui.projectDialog.close();
      if (completedMode.startsWith('edit-')) toast('项目信息已更新。'); else toast('项目已创建。');
      await loadProjects(completedMode === 'new-main' ? (payload.main_project_id || payload.project_id) : state.projectId,
        completedMode === 'new-sub' ? (payload.subproject_id || payload.id) : state.subprojectId);
    } catch (error) { toast(error.message, true); }
    finally { setBusy(false); }
  }
  function openTaskEdit(task) {
    if (writesBlocked()) { toast('已有数据需要先完成项目层级迁移。', true); return; }
    const detail = state.detail?.task_id === task.task_id ? state.detail : task;
    $('#taskNameInput').value = detail.name || '';
    $('#taskDescriptionInput').value = detail.description || '';
    ui.taskEditDialog.dataset.taskId = task.task_id;
    ui.taskEditDialog.showModal();
  }
  async function saveTaskEdit(event) {
    event.preventDefault();
    const taskId = ui.taskEditDialog.dataset.taskId;
    if (!taskId) return;
    const version = state.selectionVersion;
    try {
      const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`, { method: 'PATCH', body: JSON.stringify({ name: $('#taskNameInput').value.trim(), description: $('#taskDescriptionInput').value.trim() }) });
      if (version !== state.selectionVersion || taskId !== state.taskId) return;
      ui.taskEditDialog.close();
      state.detail = detail;
      renderDetail(detail);
      await loadTasks(taskId);
      toast('任务信息已更新。');
    } catch (error) { toast(error.message, true); }
  }
  let moveTaskTargetId = '';
  function openMoveTask(task) {
    if (writesBlocked()) { toast('已有数据需要先完成项目层级迁移。', true); return; }
    const options = state.subprojects.filter(sub => sub.subproject_id !== state.subprojectId && (sub.status || 'active') === 'active');
    if (!options.length) { toast('当前主项目下没有其他可移动到的子项目。', true); return; }
    moveTaskTargetId = task.task_id;
    const source = state.subprojects.find(sub => sub.subproject_id === state.subprojectId);
    $('#moveTaskSummary').textContent = `“${task.name || task.prompt || '未命名任务'}”将从「${source?.name || '当前子项目'}」移动到你选择的目标子项目。`;
    const select = $('#moveTargetSelect'); select.replaceChildren();
    for (const sub of options) {
      const option = document.createElement('option'); option.value = sub.subproject_id;
      option.textContent = `${sub.name}（${Number(sub.task_count) || 0} 个任务）`; select.append(option);
    }
    ui.moveTaskDialog.showModal();
  }
  async function moveTask(event) {
    event.preventDefault();
    const targetId = $('#moveTargetSelect').value;
    const taskId = moveTaskTargetId;
    const expected = state.subprojectId;
    if (!taskId || !targetId || targetId === expected) return;
    try {
      await api(`/api/tasks/${encodeURIComponent(taskId)}/move`, { method: 'POST', body: JSON.stringify({ target_subproject_id: targetId, expected_subproject_id: expected }) });
      ui.moveTaskDialog.close(); moveTaskTargetId = '';
      localStorage.setItem(storageKeys.task, taskId);
      await loadProjects(state.projectId, targetId);
      toast('任务已移动，目标子项目已打开。');
    } catch (error) { toast(error.message, true); }
  }
  function getOperation() { return $('input[name="operation"]:checked').value; }
  function updateEditHelp() { ui.editHelp.hidden = getOperation() !== 'edit' || Boolean(state.baseArtifact); }
  function fingerprint() {
    const fileSignature = state.files.map(file => `${file.name}:${file.size}:${file.lastModified}`).join('|');
    return [state.projectId, state.subprojectId, ui.prompt.value, getOperation(), ui.ratio.value, state.baseArtifact ? state.baseArtifact.artifact_id : '', fileSignature, state.referenceArtifacts.map(a => a.artifact_id).join(',')].join('\n');
  }
  function persistentTaskRequestId() {
    const signature = fingerprint();
    let saved;
    try { saved = JSON.parse(localStorage.getItem(storageKeys.pendingSave) || 'null'); } catch (error) { saved = null; }
    if (!saved || saved.signature !== signature) {
      saved = { signature, id: requestId() };
      localStorage.setItem(storageKeys.pendingSave, JSON.stringify(saved));
    }
    return saved.id;
  }
  async function saveTask(event) {
    event.preventDefault();
    if (state.busy || window.IWImageInput.uploading) return;
    if (!state.subprojectId) { setMessage('请先打开一个子项目，再保存任务。', true); return; }
    if (state.config.project_migration_required) { setMessage('已有数据需要先完成项目层级迁移。', true); return; }
    const prompt = ui.prompt.value.trim();
    if (!prompt) { setMessage('请先写下你想要的画面。', true); ui.prompt.focus(); return; }
    if (state.files.length > state.config.max_input_images) { setMessage(`最多可添加 ${state.config.max_input_images} 张参考图片。`, true); return; }
    if (state.files.some(file => file.size > state.config.max_image_bytes)) { setMessage(`单张图片不能超过 ${formatBytes(state.config.max_image_bytes)}。`, true); return; }
    if (state.files.reduce((sum, file) => sum + file.size, 0) > state.config.max_upload_bytes) { setMessage(`图片总大小不能超过 ${formatBytes(state.config.max_upload_bytes)}。`, true); return; }
    const saveMainId = state.projectId;
    const saveSubId = state.subprojectId;
    const saveVersion = state.selectionVersion;
    setBusy(true);
    setMessage('正在保存任务…');
    const form = new FormData();
    form.set('request_id', persistentTaskRequestId());
    form.set('operation', getOperation());
    form.set('prompt', prompt);
    form.set('aspect_ratio', ui.ratio.value);
    if (state.baseArtifact) form.set('base_artifact_id', state.baseArtifact.artifact_id);
    form.set('reference_artifact_ids', JSON.stringify(state.referenceArtifacts.map(a => a.artifact_id)));
    for (const file of state.files) form.append('files', file, file.name);
    try {
      const detail = await api(`/api/subprojects/${encodeURIComponent(saveSubId)}/tasks`, { method: 'POST', body: form });
      if (saveVersion !== state.selectionVersion || saveMainId !== state.projectId || saveSubId !== state.subprojectId) {
        localStorage.removeItem(storageKeys.pendingSave);
        toast('任务已保存到原子项目。当前页面仍停留在你刚选择的子项目。');
        return;
      }
      localStorage.removeItem(storageKeys.pendingSave);
      state.taskId = detail.task_id;
      state.baseArtifact = null;
      localStorage.setItem(storageKeys.task, detail.task_id);
      ui.basePanel.hidden = true;
      ui.refUpload.hidden = false;
      state.files = []; state.referenceArtifacts = [];
      ui.imageInput.value = '';
      renderFiles();
      ui.prompt.value = '';
      ui.prompt.dispatchEvent(new Event('input'));
      setMessage('任务已保存。开始执行需要你单独点击按钮。');
      toast('任务已保存为草稿。');
      await loadTasks(detail.task_id);
    } catch (error) {
      setMessage(`${error.message} 可再次保存以重试。`, true);
      toast(error.message, true);
    } finally {
      setBusy(false);
      if (state.detail && state.detail.task_id === state.taskId) renderDetail(state.detail);
    }
  }
  function renderFiles() {
    ui.previews.replaceChildren();
    for (const artifact of state.referenceArtifacts) {
      const tile = appendText(ui.previews, 'div', 'file-tile', '');
      tile.append(safeImage(artifact, '参考图片预览'));
      appendText(tile, 'span', '', artifact.filename || '项目图片');
      const remove = appendText(tile, 'button', 'remove-file', '×'); remove.type = 'button';
      remove.setAttribute('aria-label', '移除参考图片');
      remove.addEventListener('click', () => { state.referenceArtifacts = state.referenceArtifacts.filter(a => a.artifact_id !== artifact.artifact_id); renderFiles(); });
    }
    for (const [index, file] of state.files.entries()) {
      const tile = document.createElement('div');
      tile.className = 'file-tile';
      const preview = document.createElement('img');
      preview.alt = `参考图片 ${index + 1} 预览`;
      preview.src = URL.createObjectURL(file);
      preview.addEventListener('load', () => URL.revokeObjectURL(preview.src), { once: true });
      tile.append(preview);
      appendText(tile, 'span', '', file.name);
      const remove = appendText(tile, 'button', 'remove-file', '×');
      remove.type = 'button';
      remove.setAttribute('aria-label', `移除第 ${index + 1} 张参考图片`);
      remove.addEventListener('click', () => {
        state.files.splice(index, 1);
        syncFileInput();
        renderFiles();
      });
      ui.previews.append(tile);
    }
  }
  function syncFileInput() {
    const transfer = new DataTransfer();
    for (const file of state.files) transfer.items.add(file);
    ui.imageInput.files = transfer.files;
  }
  function addFiles(fileList) {
    const supported = Array.from(fileList).filter(file => ['image/png', 'image/jpeg', 'image/webp'].includes(file.type));
    const unique = supported.filter(file => !state.files.some(existing => existing.name === file.name && existing.size === file.size && existing.lastModified === file.lastModified));
    const max = state.config ? state.config.max_input_images : 8;
    const nextFiles = [...state.files, ...unique];
    const exceedsCount = nextFiles.length > max;
    state.files = nextFiles.slice(0, max);
    syncFileInput();
    renderFiles();
    if (fileList.length && supported.length !== fileList.length) toast('只支持 PNG、JPG 和 WEBP 图片。', true);
    if (exceedsCount) toast(`最多可添加 ${max} 张图片。`, true);
    if (state.files.some(file => file.size > (state.config ? state.config.max_image_bytes : 20 * 1024 * 1024))) toast('有图片超过单张大小限制。', true);
  }
  async function refreshAll() {
    try { await refreshWorkerStatus(); await loadProjects(state.projectId); toast('项目和任务状态已刷新。'); }
    catch (error) { toast(error.message, true); }
  }
  async function refreshWorkerStatus() {
    state.config = await api('/api/config');
    state.csrf = state.config.csrf_token || '';
    const remote = state.config.execution_mode === 'remote';
    ui.workerBadge.textContent = remote
      ? (state.config.worker_running ? '本机执行器在线' : '本机执行器离线 · 新任务排队')
      : (state.config.worker_running ? '执行器已运行' : '执行器未启动');
  }
  async function init() {
    try {
      await refreshWorkerStatus();
      await refreshAccount();
      for (const key of Object.keys(storageKeys)) storageKeys[key] += `.${state.account.account_id}.${state.account.workspace_id}`;
      const local = state.config.backend === 'local';
      ui.backendBadge.classList.add(local ? 'practice' : 'live');
      ui.backendBadge.replaceChildren();
      const dot = document.createElement('i');
      ui.backendBadge.append(dot, document.createTextNode(local ? '演练环境 · 固定测试图'
        : (state.config.execution_mode === 'remote' ? '云端提交 · 本机 Codex 生成' : '本地 Codex · 手动启动')));
      $('#practiceNotice').hidden = !local;
      if (local) document.title = '演练环境（固定测试图）· Image Workbench';
      if (state.config.execution_mode === 'remote') {
        setInterval(() => refreshWorkerStatus().catch(() => { ui.workerBadge.textContent = '服务器连接中断'; }), 15000);
      }
      ui.imageInput.accept = 'image/png,image/jpeg,image/webp';
      $('#uploadSizeHint').textContent = `PNG、JPG、WEBP · 总计 ${formatBytes(state.config.max_upload_bytes)}`;
      await loadProjects(requestedParams.get('main_project_id'),requestedParams.get('subproject_id'));
      const incoming=requestedParams.get('asset_id');
      if(incoming && state.subprojectId) {
        const assets=await api(`/api/subprojects/${state.subprojectId}/assets`);
        const asset=assets.assets.find(item=>item.artifact_id===incoming);
        if(!asset) toast('传入素材不属于当前子项目，请重新选择。',true);
        else {
          const purpose=requestedParams.get('asset_action')==='reference'?'reference':'edit';
          if(purpose==='edit') $('input[name="operation"][value="edit"]').checked=true;
          await attachImages([asset],purpose,imageContext());
        }
      }
      ui.saveButton.disabled = !state.subprojectId || Boolean(state.config.project_migration_required);
      if (state.config.project_migration_required) {
        $('#migrationNotice').hidden = false;
        $('#newProjectButton').disabled = true;
        $('#sidebarNewProject').disabled = true;
        $('#newSubprojectButton').disabled = true;
      }
    } catch (error) {
      ui.backendBadge.textContent = '连接失败';
      ui.workerBadge.textContent = '请检查服务连接';
      ui.backendBadge.classList.remove('live', 'practice');
      toast(error.message, true);
    }
  }

  $('#archiveFilter').addEventListener('click', async () => { state.showArchived = !state.showArchived; $('#archiveFilter').textContent = state.showArchived ? '返回进行中项目' : '查看已归档项目'; await loadProjects(); });
  ui.form.addEventListener('submit', saveTask);
  ui.prompt.addEventListener('input', () => { ui.promptCount.textContent = `${ui.prompt.value.length} / 4000`; });
  
  $('#newProjectButton').addEventListener('click', () => openProjectDialog('new-main'));
  $('#sidebarNewProject').addEventListener('click', () => openProjectDialog('new-main'));
  $('#newSubprojectButton').addEventListener('click', () => openProjectDialog('new-sub'));
  $('#editMainProjectButton').addEventListener('click', () => openProjectDialog('edit-main', state.overview));
  ui.mobileProjectsToggle.addEventListener('click', () => {
    const open = $('.sidebar').classList.toggle('mobile-open');
    ui.mobileProjectsToggle.setAttribute('aria-expanded', String(open));
  });
  ui.projectForm.addEventListener('submit', createProject);
  document.querySelectorAll('.close-dialog').forEach(button => button.addEventListener('click', () => ui.projectDialog.close()));
  $('#taskEditForm').addEventListener('submit', saveTaskEdit);
  document.querySelectorAll('.close-task-dialog').forEach(button => button.addEventListener('click', () => ui.taskEditDialog.close()));
  $('#moveTaskForm').addEventListener('submit', moveTask);
  document.querySelectorAll('.close-move-dialog').forEach(button => button.addEventListener('click', () => ui.moveTaskDialog.close()));
  $('#refreshButton').addEventListener('click', refreshAll);
  $('#clearBaseImage').addEventListener('click', clearBaseArtifact);
  function imageContext() {
    if (!state.subprojectId) { toast('请先打开一个子项目，再添加图片。', true); return null; }
    return { sub: state.subprojectId, workspace: state.account?.workspace_id, revision: state.selectionVersion,
      purpose: getOperation() === 'edit' ? 'edit' : 'reference' };
  }
  function currentContext(context) { return context.sub === state.subprojectId && context.workspace === state.account?.workspace_id && context.revision === state.selectionVersion; }
  async function attachImages(assets, purpose, context) {
    if (!currentContext(context)) return toast('图片已保存到原项目；请在当前项目重新选择素材。');
    if (purpose === 'library') return toast('图片已保存到项目素材库。');
    if (purpose === 'edit' && !state.baseArtifact && assets.length === 1) {
      state.baseArtifact = assets[0]; ui.basePanel.hidden = false; ui.basePreview.replaceChildren(safeImage(assets[0], '待编辑图片')); updateEditHelp(); toast('图片已设为待编辑图片，现有参考素材已保留。'); return;
    }
    if (purpose === 'edit') {
      const choice = await window.IWImageInput.choose('待编辑图片已有内容或包含多张图片，请选择用途', [{value:'reference',label:'追加为参考图片'}, {value:'library',label:'仅保存到项目素材库'}]);
      if (choice !== 'reference' || !currentContext(context)) return;
    }
    const next = [...new Map([...state.referenceArtifacts, ...assets].map(a => [a.artifact_id, a])).values()];
    if (next.length > 8) throw new Error('任务最多引用 8 张图片；图片已保存到项目素材库，请减少选择后重试');
    state.referenceArtifacts = next; renderFiles(); toast('图片已登记并添加到当前任务。');
  }
  window.IWImageInput.configure({
    notice: toast, context: imageContext,
    limits: () => ({ count: 9, single: state.config?.max_image_bytes || 20 * 1024 * 1024, total: state.config?.max_upload_bytes || 40 * 1024 * 1024 }),
    async receive(files, purpose, context) {
      const form = new FormData(); files.forEach(file => form.append('files', file));
      const result = await api(`/api/subprojects/${context.sub}/assets`, { method: 'POST', body: form, headers: context.workspace ? {'X-Workspace-ID': context.workspace} : {} });
      await attachImages(result.assets, purpose, context);
    }
  });
  window.IWImageInput.bind({ input: ui.imageInput, zone: ui.refUpload });
  $('#chooseProjectImages').addEventListener('click', async () => {
    const context = imageContext(); if (!context) return;
    try {
      const result = await api(`/api/subprojects/${context.sub}/assets`);
      const assets = await window.IWImageInput.selectAssets(result.assets);
      if (assets.length) await attachImages(assets, context.purpose, context);
    } catch (error) { toast(error.message, true); }
  });
  document.querySelectorAll('input[name="operation"]').forEach(input => input.addEventListener('change', () => {
    if (getOperation() === 'generate' && state.baseArtifact) clearBaseArtifact();
    updateEditHelp();
  }));
  init();
})();
