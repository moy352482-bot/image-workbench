// Owns preview cancellation and focus restoration; no task or account state.
(() => {
  'use strict';
  function create({ query: $, dialog }) {
      let imageViewVersion = 0;
      let imagePreviewTrigger = null;
    function closeImagePreviews() {
      imageViewVersion += 1;
      window.WorkbenchImageViewer?.close();
      window.WorkbenchImageComparison?.close();
      if (dialog.open) dialog.close();
      $('#lightboxImage').removeAttribute('src');
      $('#lightboxDownload').removeAttribute('href');
    }
    async function openLightbox(artifact, trigger) {
      closeImagePreviews();
      const version = imageViewVersion;
      try {
        if (await window.WorkbenchImageViewer?.open(artifact, { trigger })) return;
      } catch (error) { /* The original preview remains available if the viewer cannot load. */ }
      if (version !== imageViewVersion) return;
      imagePreviewTrigger = trigger;
      const image = $('#lightboxImage');
      const status = $('#lightboxStatus');
      status.hidden = false;
      status.textContent = '正在加载图片…';
      image.onload = () => { status.hidden = true; };
      image.onerror = () => { status.hidden = false; status.textContent = '图片加载失败，请关闭预览后刷新页面，确认登录状态。'; };
      image.src = artifact.url;
      $('#lightboxTitle').textContent = '结果图片预览';
      $('#lightboxMeta').textContent = `${artifact.width || '—'} × ${artifact.height || '—'} · ${String(artifact.format || '').toUpperCase()}`;
      const download = $('#lightboxDownload');
      download.href = artifact.download_url;
      download.setAttribute('download', '');
      dialog.showModal();
      if (Number(artifact.width) > 3000 || Number(artifact.height) > 3000) {
        $('#lightboxMeta').append(document.createTextNode(' · 超大图片，可下载原图查看细节'));
      }
    }
    $('#closeImageDialog').addEventListener('click', () => dialog.close());
    dialog.addEventListener('click', event => { if (event.target === dialog) dialog.close(); });
    dialog.addEventListener('close', () => {
      $('#lightboxImage').removeAttribute('src');
      $('#lightboxDownload').removeAttribute('href');
      if (imagePreviewTrigger?.isConnected) imagePreviewTrigger.focus({ preventScroll: true });
      imagePreviewTrigger = null;
    });
    window.addEventListener('pagehide', closeImagePreviews);
    return { closeImagePreviews, openLightbox };
  }
  window.WorkbenchPreview = Object.freeze({ create });
})();
