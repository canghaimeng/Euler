/* SPDX-License-Identifier: Apache-2.0 */
// Copyright 2026 Qiuyu Chen (canghaimeng).
(() => {
  const messages = {
    en: { help: 'User guide', title: 'New to this project? Start with the question and the current gap.', body: 'Read the goal first. During exploration, follow current work and missing premises; after completion, follow the conclusion and its evidence. Open one node when you need the original argument. A new project does not need a paper or Lean files to show research progress.' },
    zh: { help: '使用指南', title: '第一次看这个项目？从原问题和当前缺口开始。', body: '先读目标。探索中看当前工作与缺少的前提；完成后看主结论及验收依据。需要细节时，再打开一个节点阅读原始论证。新项目即使没有论文或 Lean 文件，也可以展示研究进展。' },
  };
  function update() {
    const lang = document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
    const params = new URLSearchParams(location.search);
    const selected = document.querySelector('#project-selector')?.value;
    const project = selected || params.get('project');
    document.querySelectorAll('[data-entry-copy]').forEach(e => { e.textContent = messages[lang][e.dataset.entryCopy]; });
    document.querySelectorAll('[data-entry-page]').forEach(a => {
      const u = new URL(a.getAttribute('href'), location.href); u.search = '';
      u.searchParams.set('lang', lang); if (project) u.searchParams.set('project', project); a.href = u.href;
    });
  }
  new MutationObserver(update).observe(document.documentElement, { attributes: true, attributeFilter: ['lang'] });
  const select = document.querySelector('#project-selector');
  if (select) { select.addEventListener('change', () => queueMicrotask(update)); new MutationObserver(update).observe(select, { childList: true }); }
  window.addEventListener('popstate', update); update();
})();
