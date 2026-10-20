(() => {
  'use strict';
  if (window.IWImageInput) return;
  const mimeTypes = new Set(['image/png', 'image/jpeg', 'image/webp']);
  let handler = null, uploading = false;
  const make = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  async function choose(title, choices) {
    const dialog = make('dialog', '', 'asset-choice-dialog');
    dialog.append(make('h2', title));
    const result = new Promise(resolve => {
      for (const choice of choices) {
        const button = make('button', choice.label, 'secondary-button');
        button.type = 'button';
        button.addEventListener('click', () => { resolve(choice.value); dialog.close(); });
        dialog.append(button);
      }
      const cancel = make('button', '取消', 'text-button');
      cancel.type = 'button'; cancel.addEventListener('click', () => dialog.close()); dialog.append(cancel);
      dialog.addEventListener('close', () => { resolve(null); dialog.remove(); }, { once: true });
    });
    document.body.append(dialog); dialog.showModal();
    return result;
  }
  async function selectAssets(assets) {
    const dialog = make('dialog', '', 'asset-choice-dialog');
    dialog.append(make('h2', '选择当前子项目图片'));
    const grid = make('div', '', 'asset-picker-grid');
    for (const asset of assets) {
      const label = make('label', '', 'asset-picker-item');
      const input = make('input'); input.type = 'checkbox'; input.value = asset.artifact_id;
      const image = make('img'); image.src = asset.url; image.alt = asset.filename || '项目图片'; image.loading = 'lazy';
      label.append(input, image, make('span', asset.filename || asset.artifact_id.slice(0, 8))); grid.append(label);
    }
    if (!assets.length) grid.append(make('p', '当前子项目还没有图片，请先上传或粘贴。'));
    dialog.append(grid);
    const result = new Promise(resolve => {
      const select = make('button', '添加所选素材', 'primary-button');
      select.type = 'button'; select.disabled = !assets.length;
      select.addEventListener('click', () => {
        const ids = new Set([...grid.querySelectorAll('input:checked')].map(input => input.value));
        resolve(assets.filter(asset => ids.has(asset.artifact_id))); dialog.close();
      });
      const cancel = make('button', '取消', 'text-button'); cancel.type = 'button'; cancel.addEventListener('click', () => dialog.close());
      dialog.append(select, cancel);
      dialog.addEventListener('close', () => { resolve([]); dialog.remove(); }, { once: true });
    });
    document.body.append(dialog); dialog.showModal(); return result;
  }
  async function receive(files, purpose) {
    if (!handler || !files.length) return;
    if (uploading) return handler.notice('图片正在保存，请稍候再添加。', true);
    const limits = handler.limits();
    if (files.length > (limits.count || 9)) return handler.notice(`一次最多添加 ${limits.count || 9} 张图片。`, true);
    if (files.some(file => !mimeTypes.has(file.type) && !/\.(png|jpe?g|webp)$/i.test(file.name))) return handler.notice('只支持 PNG、JPG 和 WEBP 图片。', true);
    if (files.some(file => !file.size || file.size > limits.single) || files.reduce((sum, file) => sum + file.size, 0) > limits.total) return handler.notice('图片为空或超过大小上限，请压缩或分批上传。', true);
    // Capture destination before opening the purpose dialog or awaiting network I/O.
    const context = handler.context();
    if (!context) return;
    uploading = true;
    const preview = make('div', '', 'asset-input-progress'); preview.setAttribute('role', 'status');
    preview.append(make('span', '正在校验并保存图片…'));
    const urls = [];
    for (const file of files) {
      const image = make('img'); image.alt = '待上传图片预览'; image.src = URL.createObjectURL(file); urls.push(image.src); preview.append(image);
    }
    document.body.append(preview);
    try {
      const target = purpose || context.purpose || await choose('检测到图片，请选择用途', [
        { value: 'reference', label: '添加到当前图片任务' }, { value: 'library', label: '保存到项目素材库' },
        { value: 'first', label: '设为当前镜头首帧' }, { value: 'last', label: '设为当前镜头尾帧' }
      ]);
      if (target) { await handler.receive(files, target, context); document.dispatchEvent(new CustomEvent('asset-library-change')); }
    } catch (error) {
      handler.notice(`${error.message} 图片未加入当前任务，可重试；已填写的内容已保留。`, true);
    } finally {
      uploading = false; preview.remove(); urls.forEach(url => URL.revokeObjectURL(url));
    }
  }
  document.addEventListener('paste', event => {
    if (!handler || event.defaultPrevented || document.querySelector('dialog[open]')) return;
    const data = event.clipboardData;
    if (!data) return;
    const editable = event.target.closest?.('input, textarea, [contenteditable="true"]');
    if (editable && data.getData('text/plain')) return;
    const files = [...(data.items || [])].filter(item => item.kind === 'file' && item.type.startsWith('image/')).map(item => item.getAsFile()).filter(Boolean);
    if (!files.length) return;
    event.preventDefault();
    receive(files);
  });
  function bind({ input, zone, purpose }) {
    if (input && !input.dataset.assetInputBound) {
      input.dataset.assetInputBound = 'true';
      input.addEventListener('change', () => { const files = [...input.files]; input.value = ''; receive(files, purpose); });
    }
    if (!zone || zone.dataset.assetInputBound) return;
    zone.dataset.assetInputBound = 'true';
    for (const type of ['dragenter', 'dragover']) zone.addEventListener(type, event => {
      if (![...(event.dataTransfer?.types || [])].includes('Files')) return;
      event.preventDefault(); zone.classList.add('dragover');
    });
    zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
    zone.addEventListener('drop', event => {
      zone.classList.remove('dragover');
      const files = [...(event.dataTransfer?.files || [])]; if (!files.length) return;
      event.preventDefault(); event.stopPropagation(); receive(files, purpose);
    });
  }
  window.IWImageInput = { configure(value) { handler = value; }, bind, receive, choose, selectAssets, get uploading() { return uploading; } };
})();
