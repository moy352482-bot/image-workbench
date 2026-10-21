(() => {
  'use strict';
  const MIME = 'application/x-image-workbench-asset';
  function node(tag, text, cls) {
    const element = document.createElement(tag); element.textContent = text || ''; if (cls) element.className = cls; return element;
  }
  function menu(items, notice) {
    const details = node('details', '', 'asset-actions-menu');
    details.append(node('summary', '更多操作'));
    const list = node('div', '', 'asset-actions-list');
    for (const item of items) {
      const button = node(item.href ? 'a' : 'button', item.label, 'text-button');
      if (item.href) button.href = item.href;
      else {
        button.type = 'button';
        button.addEventListener('click', async () => {
          if (button.disabled) return; button.disabled = true;
          try { await item.run(); details.open = false; }
          catch (error) { notice(error.message, true); }
          finally { button.disabled = false; }
        });
      }
      list.append(button);
    }
    details.append(list); return details;
  }
  function preview(asset) {
    const dialog = node('dialog', '', 'asset-preview-dialog');
    const close = node('button', '关闭预览', 'secondary-button'); close.type = 'button'; close.addEventListener('click', () => dialog.close());
    const media = node(asset.media_type === 'video' ? 'video' : 'img');
    media.src = asset.url; media.setAttribute('aria-label', asset.filename || '素材预览');
    if (asset.media_type === 'video') { media.controls = true; media.preload = 'metadata'; }
    else media.alt = asset.filename || '素材预览';
    dialog.append(close, media); document.body.append(dialog); dialog.showModal();
    dialog.addEventListener('close', () => { if (media.pause) media.pause(); media.removeAttribute('src'); dialog.remove(); }, { once: true });
  }
  function draggable(element, asset, context) {
    element.draggable = true;
    element.addEventListener('dragstart', event => {
      event.dataTransfer.setData(MIME, JSON.stringify({ artifact_id: asset.artifact_id, media_type: asset.media_type || 'image',
        shot_id: asset.shot_id, ...context }));
      event.dataTransfer.effectAllowed = 'copy';
    });
  }
  function dropzone(element, receive, notice) {
    const supported = event => [...(event.dataTransfer?.types || [])].includes(MIME);
    element.addEventListener('dragover', event => { if (supported(event)) { event.preventDefault(); element.classList.add('dragover'); } });
    element.addEventListener('dragleave', () => element.classList.remove('dragover'));
    element.addEventListener('drop', async event => {
      if (!supported(event)) return;
      event.preventDefault(); event.stopPropagation(); element.classList.remove('dragover');
      try {
        const asset = JSON.parse(event.dataTransfer.getData(MIME));
        if (!/^[a-f0-9]{32}$/.test(asset.artifact_id)) throw new Error('素材引用无效，请重新拖拽');
        await receive(asset);
      } catch (error) { notice(error.message, true); }
    });
  }
  async function extractFrame(asset, last) {
    const video = document.createElement('video'); video.muted = true; video.preload = 'auto';
    const waitFor = (event, begin) => new Promise((resolve, reject) => {
      const cleanup = () => { clearTimeout(timer); video.removeEventListener(event, done); video.removeEventListener('error', fail); };
      const done = () => { cleanup(); resolve(); };
      const fail = () => { cleanup(); reject(new Error('视频无法解码，请下载后检查文件或使用受支持的 MP4 视频')); };
      const timer = setTimeout(fail, 15000); video.addEventListener(event, done, { once: true }); video.addEventListener('error', fail, { once: true }); begin();
    });
    try {
      await waitFor('loadeddata', () => { video.src = asset.url; video.load(); });
      if (!video.videoWidth || !video.videoHeight || !Number.isFinite(video.duration)) throw new Error('无法识别视频画面，请换一个视频重试');
      if (last) await waitFor('seeked', () => { video.currentTime = Math.max(0, video.duration - 0.001); });
      const canvas = document.createElement('canvas'); canvas.width = video.videoWidth; canvas.height = video.videoHeight;
      canvas.getContext('2d').drawImage(video, 0, 0);
      const blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/png'));
      if (!blob) throw new Error('画面提取失败，请重试');
      return new File([blob], `${asset.filename || '视频'}-${last ? '尾帧' : '首帧'}.png`, { type: 'image/png' });
    } finally { video.pause(); video.removeAttribute('src'); video.load(); }
  }
  window.IWAssetActions = { menu, preview, draggable, dropzone, extractFrame };
})();
