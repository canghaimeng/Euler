/* SPDX-License-Identifier: Apache-2.0 */
// Copyright 2026 Qiuyu Chen (canghaimeng).
(() => {
  'use strict';
  const key = 'euler-dashboard-lang';
  const params = new URLSearchParams(location.search);
  const page = document.body.dataset.reader === 'story' ? 'story' : 'help';
  const project = params.get('project');
  let saved = ''; try { saved = localStorage.getItem(key) || ''; } catch (_) {}
  let lang = ['en', 'zh'].includes(params.get('lang')) ? params.get('lang') : ['en', 'zh'].includes(saved) ? saved : 'en';
  let request = 0;
  const copy = {
    en: { back: 'Back to project', help: 'User guide', story: 'Research story', contents: 'On this page', loading: 'Loading the introduction…', error: 'The introduction could not be loaded. Please reload this page.', footer: 'Euler · Follow the question, the progress, and the evidence.', snapshot: 'Published proof snapshot. This site does not display live teams or current work in other projects.' },
    zh: { back: '返回项目', help: '使用指南', story: '研究故事', contents: '本文目录', loading: '正在读取介绍…', error: '暂时无法读取介绍，请刷新页面重试。', footer: 'Euler · 跟随问题、进展与证据。', snapshot: '公开证明快照：本站不显示实时团队或其他项目当前的工作状态。' },
  };
  function url(name) {
    const u = new URL(name, location.href); u.search = ''; u.hash = '';
    u.searchParams.set('lang', lang); if (project) u.searchParams.set('project', project);
    return u.href;
  }
  async function render() {
    const generation = ++request;
    document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';
    document.querySelectorAll('[data-copy]').forEach(e => { e.textContent = copy[lang][e.dataset.copy]; });
    document.querySelectorAll('[data-reader-lang]').forEach(e => { e.setAttribute('aria-pressed', String(e.dataset.readerLang === lang)); });
    document.querySelectorAll('[data-reader-href]').forEach(e => { e.href = url(e.dataset.readerHref === 'back' ? '../' : e.dataset.readerHref + '.html'); });
    const badge = document.querySelector('#reader-mode'); badge.hidden = document.body.dataset.edition !== 'snapshot';
    const article = document.querySelector('#reader-article');
    const toc = document.querySelector('#reader-toc');
    article.textContent = copy[lang].loading; toc.replaceChildren();
    try {
      const response = await fetch(`${page}.${lang}.md?v=project-create-guide-20261012`);
      if (!response.ok) throw new Error('Introduction unavailable');
      const markdown = await response.text(); if (generation !== request) return;
      article.innerHTML = window.markdownit({ html: false, linkify: false, typographer: false }).render(markdown);
      document.title = `${article.querySelector('h1')?.textContent || copy[lang][page]} · Euler`;
      const heading = document.createElement('strong'); heading.textContent = copy[lang].contents; toc.append(heading);
      article.querySelectorAll('h2').forEach((h, i) => {
        h.id = 'section-' + i;
        const a = document.createElement('a'); a.href = '#' + h.id; a.textContent = h.textContent; toc.append(a);
      });
      article.querySelectorAll('a').forEach(a => {
        const raw = a.getAttribute('href') || '';
        if (/^DASHBOARD-GUIDE(?:\.zh)?\.md$/.test(raw)) a.href = url('help.html');
        if (/^RESEARCH-STORY(?:\.zh)?\.md$/.test(raw)) a.href = url('story.html');
        if (a.origin !== location.origin) { a.target = '_blank'; a.rel = 'noopener noreferrer'; }
      });
      try { localStorage.setItem(key, lang); } catch (_) {}
      if (/^#section-\d+$/.test(location.hash)) document.getElementById(location.hash.slice(1))?.scrollIntoView();
    } catch (_) { if (generation === request) article.textContent = copy[lang].error; }
  }
  document.querySelectorAll('[data-reader-lang]').forEach(button => button.addEventListener('click', () => {
    lang = button.dataset.readerLang;
    const u = new URL(location.href); u.searchParams.set('lang', lang); history.replaceState(null, '', u);
    render();
  }));
  render();
})();
