(() => {
  'use strict';

  const MAX_DIMENSION = 3000;
  const ARTIFACT_PATH = /^\/api\/artifacts\/[a-f0-9]{32}$/i;
  const WORKSPACE_ID = /^[a-f0-9]{32}$/i;
  let corePromise = null;
  let active = null;
  let requestVersion = 0;

  function sourceUrl(value) {
    if (typeof value !== 'string' || !value) return null;
    try {
      const url = new URL(value, window.location.href);
      if (url.origin !== window.location.origin || !/^https?:$/.test(url.protocol)
        || url.username || url.password || url.hash || !ARTIFACT_PATH.test(url.pathname)) return null;
      for (const [key, content] of url.searchParams) {
        if (key !== 'workspace_id' || !WORKSPACE_ID.test(content)) return null;
      }
      if (url.searchParams.getAll('workspace_id').length > 1) return null;
      return url;
    } catch (_) {
      return null;
    }
  }

  function loadCore() {
    if (!corePromise) {
      corePromise = import('./vendor/photoswipe/photoswipe.esm.min.js').catch(error => {
        corePromise = null;
        throw error;
      });
    }
    return corePromise;
  }

  function appendText(parent, tag, className, content) {
    const element = document.createElement(tag);
    element.className = className;
    element.textContent = content;
    parent.append(element);
    return element;
  }

  function appendLinks(parent, original, download) {
    const links = appendText(parent, 'div', 'image-viewer-links', '');
    const downloadLink = appendText(links, 'a', 'image-viewer-link', '下载原图');
    downloadLink.href = download;
    downloadLink.setAttribute('download', '');
    const originalLink = appendText(links, 'a', 'image-viewer-link', '打开原图');
    originalLink.href = original;
    originalLink.target = '_blank';
    originalLink.rel = 'noopener noreferrer';
  }

  function restoreFocus(trigger, version) {
    queueMicrotask(() => {
      if (version === requestVersion && !active && trigger?.isConnected
        && typeof trigger.focus === 'function') trigger.focus({ preventScroll: true });
    });
  }

  function close() {
    requestVersion += 1;
    const previous = active;
    active = null;
    dispose(previous);
  }

  function dispose(viewer) {
    if (!viewer) return;
    try {
      viewer.destroy();
    } catch (_) {
      // A partial initialization must not block navigation or the old preview.
    } finally {
      viewer.element?.remove();
    }
  }

  /**
   * Open one authorized artifact. False means the caller may use its existing
   * preview, provided its task/workspace request is still current. Cancellation
   * also returns false, but never calls onFallback or opens late-loaded content.
   */
  async function open(artifact, { trigger = document.activeElement, onFallback } = {}) {
    close();
    const version = requestVersion;
    const fallback = reason => {
      if (version === requestVersion && typeof onFallback === 'function') {
        try { onFallback(reason); } catch (_) { /* The boolean result still permits fallback. */ }
      }
      return false;
    };
    const url = sourceUrl(artifact?.url);
    const width = artifact?.width;
    const height = artifact?.height;
    if (!url) return fallback('invalid-url');
    if (!Number.isInteger(width) || !Number.isInteger(height) || width <= 0 || height <= 0
      || width > MAX_DIMENSION || height > MAX_DIMENSION) return fallback('unsupported-size');

    // Both actions keep the exact protected artifact and workspace scope.
    const downloadUrl = new URL(url.href);
    downloadUrl.searchParams.set('download', '1');
    const title = typeof artifact.title === 'string' && artifact.title.trim()
      ? artifact.title : '结果图片预览';
    const alt = typeof artifact.alt === 'string' ? artifact.alt : `${title}，${width} × ${height}`;
    const format = typeof artifact.format === 'string' ? artifact.format.toUpperCase() : '';
    let viewer = null;
    try {
      const { default: PhotoSwipe } = await loadCore();
      if (version !== requestVersion) return false;
      viewer = new PhotoSwipe({
        dataSource: [{ src: url.href, width, height, alt }],
        index: 0,
        loop: false,
        arrowPrev: false,
        arrowNext: false,
        counter: false,
        preload: [0, 0],
        mainClass: 'workbench-image-viewer',
        closeTitle: '关闭预览（Esc）',
        zoomTitle: '放大或缩小图片（Z）',
        errorMsg: '图片加载失败。图片可能已被移除，或登录状态已失效。',
        bgOpacity: 0.96,
        wheelToZoom: true,
        imageClickAction: 'zoom',
        doubleTapAction: 'zoom',
        tapAction: false,
        clickToCloseNonZoomable: false,
        closeOnVerticalDrag: false,
        pinchToClose: false,
        allowPanToNext: false,
        returnFocus: false,
        trapFocus: true,
        escKey: true,
        showHideAnimationType: 'none',
        zoomAnimationDuration: 180,
        padding: { top: 72, bottom: 116, left: 20, right: 20 },
      });

      viewer.on('uiRegister', () => {
        viewer.ui.registerElement({
          name: 'workbench-title',
          order: 0,
          onInit(element) {
            element.id = 'workbench-image-viewer-title';
            element.textContent = title;
          },
        });
        viewer.ui.registerElement({
          name: 'workbench-footer',
          appendTo: 'root',
          onInit(element) {
            appendText(element, 'div', 'image-viewer-meta', `${width} × ${height}${format ? ` · ${format}` : ''}`);
            appendText(element, 'div', 'image-viewer-help', '滚轮或双指缩放 · 拖动查看细节 · Esc 关闭');
            appendLinks(element, url.href, downloadUrl.href);
          },
        });
      });
      viewer.addFilter('contentErrorElement', element => {
        element.textContent = '';
        element.setAttribute('role', 'alert');
        appendText(element, 'p', 'image-viewer-error-title', '图片加载失败');
        appendText(element, 'p', 'image-viewer-error-detail', '图片可能已被移除，或登录状态已失效。可尝试打开原图或下载后查看。');
        appendLinks(element, url.href, downloadUrl.href);
        return element;
      });
      viewer.on('afterInit', () => {
        viewer.element.setAttribute('aria-labelledby', 'workbench-image-viewer-title');
        viewer.element.setAttribute('aria-label', '图片预览');
      });
      viewer.on('destroy', () => {
        if (active === viewer) active = null;
        restoreFocus(trigger, requestVersion);
      });
      active = viewer;
      if (!viewer.init()) {
        active = null;
        dispose(viewer);
        return fallback('viewer-unavailable');
      }
      return true;
    } catch (_) {
      if (active === viewer && viewer) {
        active = null;
        dispose(viewer);
      }
      return fallback('module-unavailable');
    }
  }

  window.WorkbenchImageViewer = Object.freeze({ open, close });
})();
