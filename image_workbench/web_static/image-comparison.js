(function () {
  'use strict';

  const VENDOR_URL = '/static/vendor/img-comparison-slider/index.js?v=8.0.7-workbench.1';
  const IMAGE_TIMEOUT_MS = 20000;
  let vendorPromise;
  let current = null;

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function dimension(value) {
    return Number.isSafeInteger(value) && value > 0;
  }

  function artifactSource(artifact) {
    if (!artifact || typeof artifact.url !== 'string' ||
        !/^[a-f0-9]{32}$/i.test(artifact.artifact_id || '')) {
      throw new Error('图片信息不完整，无法打开对比。');
    }
    let url;
    try { url = new URL(artifact.url, window.location.origin); }
    catch (error) { throw new Error('图片地址不可用于对比，请重新选择当前任务的图片。'); }
    if (url.origin !== window.location.origin || url.username || url.password || url.hash ||
        url.pathname !== `/api/artifacts/${artifact.artifact_id}`) {
      throw new Error('图片地址不可用于对比，请重新选择当前任务的图片。');
    }
    const keys = Array.from(url.searchParams.keys());
    const workspace = url.searchParams.get('workspace_id');
    if (keys.some(key => key !== 'workspace_id') || keys.length > 1 ||
        (workspace !== null && !/^[a-f0-9]{32}$/i.test(workspace))) {
      throw new Error('图片地址不可用于对比，请重新选择当前任务的图片。');
    }
    return { url: url.href, workspace, width: artifact.width, height: artifact.height };
  }

  function isCurrent(session) {
    return current === session && !session.closed;
  }

  function releaseImages(session) {
    for (const cancel of Array.from(session.pendingLoads)) cancel();
    for (const image of session.images) {
      image.removeAttribute('src');
      image.removeAttribute('srcset');
      image.remove();
    }
    session.images.length = 0;
  }

  function close() {
    const session = current;
    if (!session) return;
    current = null;
    session.closed = true;
    if (session.slider) {
      // The local 8.0.7 lifecycle patch disconnects its ResizeObserver on
      // removal. End its active gestures before removing the element as well.
      if (session.dragging) window.dispatchEvent(new MouseEvent('mouseup'));
      session.slider.dispatchEvent(new Event('blur'));
    }
    session.events.abort();
    releaseImages(session);
    if (session.slider) session.slider.replaceChildren();
    session.slider = null;
    if (session.dialog.open) session.dialog.close();
    session.dialog.remove();
    const trigger = session.trigger;
    session.trigger = null;
    if (trigger && trigger.isConnected && typeof trigger.focus === 'function') {
      trigger.focus({ preventScroll: true });
    }
  }

  function createSession(trigger) {
    const dialog = element('dialog', 'image-comparison-dialog');
    dialog.id = 'imageComparisonDialog';
    dialog.setAttribute('aria-labelledby', 'imageComparisonTitle');
    dialog.setAttribute('aria-describedby', 'imageComparisonDescription');
    const header = element('header', 'image-comparison-header');
    const heading = element('div', 'image-comparison-heading');
    const title = element('h2', '', '基底与结果对比');
    title.id = 'imageComparisonTitle';
    const description = element('p', '', '查看本次编辑前后的变化。对比仅用于查看。');
    description.id = 'imageComparisonDescription';
    heading.append(title, description);
    const closeButton = element('button', 'image-comparison-close', '关闭');
    closeButton.type = 'button';
    closeButton.autofocus = true;
    closeButton.setAttribute('aria-label', '关闭图片对比');
    header.append(heading, closeButton);
    const status = element('p', 'image-comparison-status', '正在加载基底与结果图片…');
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    const content = element('div', 'image-comparison-content');
    content.setAttribute('aria-busy', 'true');
    dialog.append(header, status, content);
    const session = {
      dialog, status, content, images: [], pendingLoads: new Set(),
      events: new AbortController(), slider: null, dragging: false, closed: false,
      trigger: trigger || document.activeElement,
    };
    const options = { signal: session.events.signal };
    closeButton.addEventListener('click', close, options);
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); }, options);
    dialog.addEventListener('close', () => { if (isCurrent(session)) close(); }, options);
    let backdropDown = false;
    const outside = event => {
      const rect = dialog.getBoundingClientRect();
      return event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right ||
        event.clientY < rect.top || event.clientY > rect.bottom);
    };
    dialog.addEventListener('pointerdown', event => { backdropDown = outside(event); }, options);
    dialog.addEventListener('click', event => { if (backdropDown && outside(event)) close(); }, options);
    return session;
  }

  function loadImage(session, source, label) {
    const image = element('img', 'image-comparison-image');
    image.alt = label;
    image.decoding = 'async';
    image.draggable = false;
    session.images.push(image);
    return new Promise((resolve, reject) => {
      let finished = false;
      let timer;
      const finish = error => {
        if (finished) return;
        finished = true;
        window.clearTimeout(timer);
        image.removeEventListener('load', onLoad);
        image.removeEventListener('error', onError);
        session.pendingLoads.delete(cancel);
        if (error) reject(error); else resolve(image);
      };
      const onLoad = () => {
        if (dimension(image.naturalWidth) && dimension(image.naturalHeight)) finish();
        else finish(new Error(`${label}无法读取，请刷新任务后重试。`));
      };
      const onError = () => finish(new Error(`${label}加载失败，可能已失效或当前无权访问。请刷新任务后重试。`));
      const cancel = () => finish(new Error('图片对比已关闭。'));
      session.pendingLoads.add(cancel);
      image.addEventListener('load', onLoad);
      image.addEventListener('error', onError);
      timer = window.setTimeout(() => finish(new Error(`${label}加载超时，请关闭后重试。`)), IMAGE_TIMEOUT_MS);
      image.src = source.url;
    });
  }

  function loadVendor() {
    if (window.customElements && window.customElements.get('img-comparison-slider')) return Promise.resolve();
    if (!window.customElements || !window.ResizeObserver || !window.CSSStyleSheet ||
        !window.CSSStyleSheet.prototype.replaceSync || !('adoptedStyleSheets' in Document.prototype)) {
      return Promise.reject(new Error('当前浏览器不支持拖动对比。'));
    }
    if (!vendorPromise) {
      vendorPromise = new Promise((resolve, reject) => {
        const script = element('script');
        script.src = VENDOR_URL;
        script.async = true;
        let finished = false;
        const finish = error => {
          if (finished) return;
          finished = true;
          script.onload = null;
          script.onerror = null;
          if (error) { script.remove(); reject(error); } else resolve();
        };
        script.onload = () => finish(window.customElements.get('img-comparison-slider') ? null : new Error('对比组件未能加载。'));
        script.onerror = () => finish(new Error('对比组件未能加载。'));
        document.head.append(script);
      }).catch(error => {
        // A completed network failure is safe to retry on the next open.
        vendorPromise = null;
        throw error;
      });
    }
    return new Promise((resolve, reject) => {
      // A timeout ends this UI wait, not the shared script request. Removing a
      // downloading script cannot reliably stop its later execution; keep that
      // request so a reopen cannot register the custom element a second time.
      const timer = window.setTimeout(() => reject(new Error('对比组件加载超时。')), 15000);
      vendorPromise.then(() => {
        window.clearTimeout(timer);
        resolve();
      }, error => {
        window.clearTimeout(timer);
        reject(error);
      });
    });
  }

  function sizeLabel(image) {
    return `${image.naturalWidth} × ${image.naturalHeight}`;
  }

  function renderSideBySide(session, images, reason) {
    session.status.textContent = reason;
    const row = element('div', 'image-comparison-pair');
    for (let index = 0; index < images.length; index += 1) {
      const figure = element('figure', 'image-comparison-figure');
      const caption = element('figcaption', '', `${index === 0 ? '基底' : '结果'} · ${sizeLabel(images[index])}`);
      const canvas = element('div', 'image-comparison-canvas');
      canvas.append(images[index]);
      figure.append(caption, canvas);
      row.append(figure);
    }
    session.content.replaceChildren(row);
  }

  function renderSlider(session, images) {
    const labels = element('div', 'image-comparison-labels');
    labels.append(element('span', '', `基底 · ${sizeLabel(images[0])}`), element('span', '', `结果 · ${sizeLabel(images[1])}`));
    const hint = element('p', 'image-comparison-hint', '拖动分隔线查看变化；也可按 Tab 聚焦图片，按 ← / → 每次调整 5%，Home / End 查看单张。按 Esc 关闭。');
    hint.id = 'imageComparisonKeyboardHint';
    const stage = element('div', 'image-comparison-stage');
    const slider = element('img-comparison-slider', 'image-comparison-slider');
    session.slider = slider;
    slider.setAttribute('role', 'slider');
    slider.setAttribute('aria-label', '基底与结果对比，调整基底图片的显示比例');
    slider.setAttribute('aria-describedby', hint.id);
    slider.setAttribute('aria-orientation', 'horizontal');
    slider.setAttribute('aria-valuemin', '0');
    slider.setAttribute('aria-valuemax', '100');
    // The upstream RAF keyboard loop can miss a quick key press. Use discrete
    // native key-repeat steps, including Home/End, through its public value API.
    slider.setAttribute('keyboard', 'disabled');
    slider.setAttribute('hover', 'false');
    slider.setAttribute('direction', 'horizontal');
    slider.tabIndex = 0;
    const updateValue = () => {
      const value = Math.round(slider.value);
      slider.setAttribute('aria-valuenow', String(value));
      slider.setAttribute('aria-valuetext', `基底 ${value}%，结果 ${100 - value}%`);
    };
    const options = { signal: session.events.signal };
    slider.addEventListener('slide', updateValue, options);
    slider.addEventListener('keydown', event => {
      const actions = {
        ArrowLeft: () => slider.value - 5,
        ArrowRight: () => slider.value + 5,
        Home: () => 0,
        End: () => 100,
      };
      const action = actions[event.key];
      if (!action) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      slider.value = Math.max(0, Math.min(100, action()));
      updateValue();
    }, { ...options, capture: true });
    slider.addEventListener('mousedown', () => { session.dragging = true; }, options);
    window.addEventListener('mouseup', () => { session.dragging = false; }, options);
    images[0].slot = 'first';
    images[1].slot = 'second';
    slider.append(...images);
    stage.append(slider);
    session.content.replaceChildren(labels, stage, hint);
    const fitCanvas = () => {
      const width = images[0].naturalWidth;
      const height = images[0].naturalHeight;
      // Both images share this exact canvas; no stretching or independent crop.
      slider.style.width = `${Math.min(width, 1080, width / height * window.innerHeight * 0.62)}px`;
    };
    window.addEventListener('resize', fitCanvas, options);
    fitCanvas();
    updateValue();
    session.status.textContent = '同尺寸画布；左侧为基底，右侧为结果。透明区域使用相同棋盘背景。';
  }

  async function open(baseArtifact, outputArtifact, options = {}) {
    close();
    const session = createSession(options.trigger);
    current = session;
    document.body.append(session.dialog);
    try {
      session.dialog.showModal();
    } catch (error) {
      close();
      throw error;
    }
    try {
      const sources = [artifactSource(baseArtifact), artifactSource(outputArtifact)];
      if (sources[0].workspace !== sources[1].workspace) throw new Error('只能对比当前工作空间内的基底与结果。');
      const images = await Promise.all([
        loadImage(session, sources[0], '基底图片'),
        loadImage(session, sources[1], '结果图片'),
      ]);
      if (!isCurrent(session)) return false;
      const complete = sources.every(source => dimension(source.width) && dimension(source.height));
      const accurate = complete && sources.every((source, index) =>
        source.width === images[index].naturalWidth && source.height === images[index].naturalHeight);
      const same = accurate && sources[0].width === sources[1].width && sources[0].height === sources[1].height;
      if (!same) {
        const reason = !complete ? '尺寸信息不完整，已并排显示实际画布；图片保持原始比例。' :
          !accurate ? '实际图片尺寸与记录不一致，已并排显示实际画布；图片保持原始比例。' :
            '两张图片的尺寸或比例不同，已并排显示；图片保持原始比例。';
        renderSideBySide(session, images, reason);
      } else {
        try {
          await loadVendor();
          if (!isCurrent(session)) return false;
          renderSlider(session, images);
        } catch (error) {
          if (!isCurrent(session)) return false;
          renderSideBySide(session, images, '拖动对比暂时不可用，已改为并排查看；图片保持原始比例。');
        }
      }
      session.content.setAttribute('aria-busy', 'false');
      return true;
    } catch (error) {
      if (!isCurrent(session)) return false;
      releaseImages(session);
      session.content.replaceChildren();
      session.content.setAttribute('aria-busy', 'false');
      session.status.classList.add('image-comparison-error');
      session.status.textContent = error.message || '图片加载失败，请刷新任务后重试。';
      return false;
    }
  }

  window.addEventListener('pagehide', close);
  window.WorkbenchImageComparison = Object.freeze({ open, close });
})();
