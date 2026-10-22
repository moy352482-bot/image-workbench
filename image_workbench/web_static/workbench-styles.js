(() => {
  'use strict';

  function create({ query, api, toast, onExample }) {
    const select = query('#imageStyleSelect');
    const dialog = query('#imageStylesDialog');
    const cards = query('#imageStyleCards');
    const status = query('#imageStylesStatus');
    let styles = [];
    let current = null;
    let loading = false;

    function text(parent, tag, className, value) {
      const element = document.createElement(tag);
      element.className = className;
      element.textContent = value;
      parent.append(element);
      return element;
    }
    function selectionChanged() {
      const chosen = styles.find(item => item.id === select.value);
      query('#imageStyleSummary').textContent = chosen
        ? `${chosen.summary} 你的明确要求优先。`
        : '生成时自动整理画面要求，保留你指定的内容、文字和参考约束。';
    }
    function show(style) {
      current = style;
      query('#imageStyleDetail').hidden = false;
      for (const card of cards.querySelectorAll('button')) {
        card.setAttribute('aria-pressed', String(card.dataset.styleId === style.id));
      }
      query('#imageStylePreview').src = style.preview_url;
      query('#imageStylePreview').alt = style.preview_alt;
      query('#imageStylePreviewLink').href = style.preview_url;
      query('#imageStyleName').textContent = style.name;
      query('#imageStyleCategory').textContent = style.category;
      query('#imageStyleDescription').textContent = style.summary;
      query('#imageStyleExample').value = style.example_prompt;
      query('#imageStyleRatio').textContent = `示例比例 ${style.aspect_ratio} · 可替换主体、场景和文案后复用`;
      const sources = query('#imageStyleSources');
      sources.replaceChildren();
      text(sources, 'span', '', '研究参考：');
      for (const source of style.sources || []) {
        const link = text(sources, 'a', '', source.name);
        link.href = source.url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
      }
    }
    async function load() {
      if (loading) return;
      loading = true;
      status.textContent = '正在加载风格与案例…';
      query('#retryImageStyles').hidden = true;
      try {
        const catalog = await api('/api/image-styles');
        styles = catalog.styles;
        const previous = select.value;
        select.replaceChildren();
        const automatic = text(select, 'option', '', '遵循原始需求');
        automatic.value = '';
        cards.replaceChildren();
        for (const style of styles) {
          const option = text(select, 'option', '', style.name);
          option.value = style.id;
          const card = text(cards, 'button', 'image-style-card', '');
          card.type = 'button';
          card.dataset.styleId = style.id;
          card.setAttribute('aria-label', `查看${style.name}风格案例`);
          card.setAttribute('aria-pressed', 'false');
          const image = document.createElement('img');
          image.src = style.preview_url;
          image.alt = style.preview_alt;
          image.loading = 'lazy';
          image.addEventListener('error', () => { image.hidden = true; card.classList.add('preview-unavailable'); });
          card.append(image);
          text(card, 'strong', '', style.name);
          text(card, 'span', '', style.tags.join(' · '));
          card.addEventListener('click', () => {
            show(style);
            if (window.matchMedia('(max-width: 650px)').matches) query('#imageStyleDetail').scrollIntoView({ block: 'start' });
          });
        }
        select.value = styles.some(item => item.id === previous) ? previous : '';
        selectionChanged();
        status.textContent = `${styles.length} 种风格 · 预览图不会作为参考图自动发送`;
        if (styles.length) show(styles.find(item => item.id === select.value) || styles[0]);
      } catch (error) {
        status.textContent = `风格加载失败：${error.message}`;
        query('#retryImageStyles').hidden = false;
      } finally {
        loading = false;
      }
    }
    function choose() {
      if (!current) return;
      select.value = current.id;
      select.dispatchEvent(new Event('change'));
    }
    select.addEventListener('change', selectionChanged);
    query('#browseImageStyles').addEventListener('click', () => {
      dialog.showModal();
      if (!styles.length) load();
      else show(styles.find(item => item.id === select.value) || current || styles[0]);
    });
    query('#closeImageStyles').addEventListener('click', () => dialog.close());
    query('#backToImageStyles').addEventListener('click', () => { dialog.scrollTop = 0; });
    query('#retryImageStyles').addEventListener('click', load);
    query('#applyImageStyle').addEventListener('click', () => {
      if (!current) return;
      choose();
      dialog.close();
      toast(`已应用「${current.name}」，创作需求保持不变。`);
    });
    query('#useImageStyleExample').addEventListener('click', () => {
      if (!current) return;
      const previousStyle = select.value;
      choose();
      onExample(current, previousStyle);
      dialog.close();
      toast('案例已填入，可修改后保存，也可撤销填入。');
    });
    query('#copyImageStyleExample').addEventListener('click', async () => {
      if (!current) return;
      try {
        await navigator.clipboard.writeText(current.example_prompt);
        toast('案例提示词已复制。');
      } catch {
        query('#imageStyleExample').focus();
        query('#imageStyleExample').select();
        toast('已选中提示词，请按 Ctrl+C 或 Cmd+C 复制。');
      }
    });
    return { load, value: () => select.value, setValue(value) { select.value = value || ''; selectionChanged(); } };
  }
  window.WorkbenchStyles = Object.freeze({ create });
})();
