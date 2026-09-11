(() => {
  'use strict';
  let nextDialogId = 0;
  const el = (tag, className, content) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const button = (label, action, className = 'research-button') => {
    const node = el('button', className, label);
    node.type = 'button';
    node.addEventListener('click', action);
    return node;
  };
  function dialog(className, title) {
    const node = el('dialog', className);
    const heading = el('h2', '', title);
    heading.id = `research-dialog-${++nextDialogId}`;
    node.setAttribute('aria-labelledby', heading.id);
    const header = el('header', 'research-dialog-head');
    header.append(heading, button('Close', () => node.close(), 'research-close'));
    node.append(header);
    document.body.append(node);
    const previous = document.activeElement;
    node.addEventListener('close', () => { node.remove(); previous?.focus(); }, { once: true });
    return node;
  }

  let intakeOpen = false;
  function requestBrief(topic = '') {
    if (intakeOpen) return Promise.resolve(null);
    intakeOpen = true;
    return new Promise(resolve => {
      const modal = dialog('research-intake', 'What would you like to understand?');
      modal.prepend(el('div', 'research-eyebrow', 'DEEP RESEARCH / THE BRIEF'));
      const form = el('form', 'research-brief');
      form.append(el('p', 'research-muted', 'Start with a question. A little context helps the research focus on what matters to you.'));
      const fields = [
        ['topic', 'Your research question', 'For example: How viable is district heating for a small town?', true],
        ['purpose', 'What will you use the answer for?', 'A decision, a comparison, a project, or simply understanding', false],
        ['context', 'What should we know already?', 'Your starting point, assumptions, or sources to investigate', false],
        ['scope', 'Focus and boundaries', 'Dates, region, budget, audience, or what to leave out', false],
      ];
      const inputs = {};
      for (const [key, label, placeholder, required] of fields) {
        const wrapper = el('label', 'research-field', label);
        const input = el('textarea');
        input.name = key;
        input.rows = key === 'topic' ? 3 : 2;
        input.maxLength = key === 'topic' ? 800 : 2000;
        input.placeholder = placeholder;
        input.required = required;
        if (key === 'topic') input.value = topic;
        wrapper.append(input);
        form.append(wrapper);
        inputs[key] = input;
      }
      const footer = el('footer', 'research-intake-foot');
      footer.append(el('p', 'research-muted', 'You’ll get a source-backed presentation, including disagreements and gaps in the evidence.'));
      const submit = el('button', 'research-button primary', 'Start research');
      submit.type = 'submit';
      footer.append(submit);
      form.append(footer);
      let value = null;
      form.addEventListener('submit', event => {
        event.preventDefault();
        if (!inputs.topic.value.trim()) { inputs.topic.focus(); return; }
        value = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
        modal.close();
      });
      modal.addEventListener('close', () => { intakeOpen = false; resolve(value); }, { once: true });
      modal.append(form);
      modal.showModal();
      inputs.topic.focus();
    });
  }

  function sourceLink(source) {
    let url;
    try { url = new URL(source.url); } catch { return el('span', 'research-source-link', source.title || source.location); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return el('span', '', source.title);
    const link = el('a', 'research-source-link', `${source.id} · ${source.title || url.hostname}`);
    link.href = url.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  }

  function evidenceNote(note, data) {
    const article = el('article', 'research-evidence');
    article.append(el('small', `research-relation ${note.relation}`, note.relation), el('p', '', note.finding));
    const details = el('details');
    details.append(el('summary', '', 'Read supporting excerpt'), el('blockquote', '', note.quote));
    const source = data.sources.find(source => source.id === note.source_id);
    if (source) {
      details.append(sourceLink(source));
      details.append(el('small', 'research-muted', `Retrieved ${new Date(source.retrieved * 1000).toLocaleDateString()}${source.truncated ? ' · Partial document' : ''}`));
    }
    article.append(details);
    return article;
  }

  function openPresentation(data) {
    const presentation = data.presentation;
    const modal = dialog('research-presentation', presentation?.title || data.brief.topic);
    modal.prepend(el('div', 'research-eyebrow', presentation ? 'RESEARCH / FINDINGS' : 'RESEARCH / NOTEBOOK'));
    const layout = el('div', 'research-presentation-layout');
    const navigation = el('nav', 'research-slide-nav');
    navigation.setAttribute('aria-label', 'Research sections');
    const content = el('section', 'research-slide');
    content.tabIndex = 0;
    content.setAttribute('aria-live', 'polite');
    const pages = [{ kind: 'overview', title: 'The question' },
      ...(presentation?.slides || []).map(slide => ({ ...slide, kind: 'finding' })),
      { kind: 'questions', title: 'Questions & coverage' },
      { kind: 'limits', title: 'Limits & open questions' },
      { kind: 'sources', title: 'Source ledger' }];
    let index = 0;
    const footer = el('footer', 'research-slide-controls');
    const position = el('span', 'research-muted');
    const previous = button('Previous', () => show(index - 1));
    const next = button('Next', () => show(index + 1));
    footer.append(previous, position, next);
    const tabs = pages.map((page, i) => {
      const tab = button(`${String(i + 1).padStart(2, '0')}  ${page.title}`, () => show(i), 'research-slide-tab');
      navigation.append(tab);
      return tab;
    });
    function show(i) {
      index = Math.max(0, Math.min(pages.length - 1, i));
      const page = pages[index];
      tabs.forEach((tab, n) => {
        tab.classList.toggle('active', n === index);
        if (n === index) tab.setAttribute('aria-current', 'step');
        else tab.removeAttribute('aria-current');
      });
      previous.disabled = index === 0;
      next.disabled = index === pages.length - 1;
      position.textContent = `${index + 1} / ${pages.length}`;
      content.replaceChildren(el('div', 'research-eyebrow', `FIELD NOTES / ${String(index + 1).padStart(2, '0')}`), el('h3', '', page.title));
      if (page.kind === 'overview') {
        content.append(el('p', 'research-lead', presentation?.overview || (data.status === 'running'
          ? 'Research is in progress. The collected context and evidence are available below.'
          : 'This investigation stopped before a presentation was completed. The collected context and evidence are available below.')));
        for (const [key, label] of [['topic', 'Question'], ['purpose', 'Purpose'], ['context', 'Starting context'], ['scope', 'Boundaries']]) {
          if (data.brief[key]) {
            const block = el('div', 'research-context');
            block.append(el('small', 'research-muted', label), el('p', '', data.brief[key]));
            content.append(block);
          }
        }
      } else if (page.kind === 'finding') {
        content.append(el('p', 'research-lead', page.summary), el('small', 'research-muted', 'Synthesis based on the following recorded evidence.'));
        for (const id of page.note_ids) {
          const note = data.notes.find(note => note.id === id);
          if (note) content.append(evidenceNote(note, data));
        }
      } else if (page.kind === 'questions') {
        data.questions.forEach((question, i) => {
          content.append(el('h4', '', `${i + 1}. ${question}`));
          const notes = data.notes.filter(note => note.question === i + 1);
          if (!notes.length) content.append(el('p', 'research-muted', 'No evidence recorded for this question.'));
          notes.forEach(note => content.append(evidenceNote(note, data)));
        });
        if (!data.questions.length) content.append(el('p', 'research-muted', 'No question plan was recorded.'));
      } else if (page.kind === 'limits') {
        const limits = presentation?.limitations || ['Research is incomplete. These notes are not a finished assessment.'];
        limits.forEach(limit => content.append(el('p', 'research-limit', limit)));
        for (const gap of presentation?.gaps || []) {
          content.append(el('h4', '', data.questions[gap.question - 1]), el('p', '', gap.reason));
        }
        content.append(el('p', 'research-muted', 'Citations link findings to retrieved text. They do not independently verify a source’s accuracy.'));
      } else {
        for (const source of data.sources) {
          const entry = el('article', 'research-source');
          entry.append(sourceLink(source), el('small', 'research-muted', `${source.domain} · Retrieved ${new Date(source.retrieved * 1000).toLocaleDateString()}${source.truncated ? ' · Partial document' : ''}`));
          content.append(entry);
        }
        if (!data.sources.length) content.append(el('p', 'research-muted', 'No sources were successfully read.'));
      }
      content.scrollTop = 0;
    }
    layout.append(navigation, content);
    modal.append(layout, footer);
    modal.addEventListener('keydown', event => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || event.target.closest('input, textarea')) return;
      event.preventDefault();
      show(index + (event.key === 'ArrowRight' ? 1 : -1));
    });
    show(0);
    modal.showModal();
  }

  function mount(row, data) {
    if (!row || !data) return;
    row._research = data;
    row.querySelector('.research-result')?.remove();
    const card = el('section', 'research-result');
    card.append(el('div', 'research-eyebrow', data.status === 'complete' ? 'DEEP RESEARCH / READY' : 'DEEP RESEARCH / INCOMPLETE'));
    card.append(el('h3', '', data.presentation?.title || data.brief.topic));
    card.append(el('p', 'research-muted', `${data.sources.length} sources read · ${data.notes.length} evidence notes · ${data.questions.length} questions`));
    if (data.status !== 'complete') card.append(el('p', 'research-muted', 'The investigation ended before a complete presentation was published.'));
    card.append(button(data.presentation ? 'Open presentation' : 'Open research notebook', () => openPresentation(data), 'research-button primary'));
    if (data.status === 'incomplete') {
      card.append(button('Resume research', () => {
        document.dispatchEvent(new CustomEvent('accuretta:resume-research', { detail: data }));
      }));
    }
    (row.querySelector('.bubble-col') || row).append(card);
  }

  function update(row, data) {
    if (!row || !data) return;
    row._research = data;
    if (!row.isConnected) return;
    const deck = document.querySelector('#revealer-deck');
    if (!deck) return;
    let rail = deck.querySelector('.research-rail');
    if (data.status !== 'running') {
      if (rail?.dataset.researchId === data.id) rail.remove();
      mount(row, data);
      return;
    }
    if (!rail || rail.dataset.researchId !== data.id) {
      rail?.remove();
      rail = el('section', 'research-rail');
      rail.dataset.researchId = data.id;
      const symbol = el('span', 'research-rail-symbol');
      symbol.setAttribute('aria-hidden', 'true');
      symbol.innerHTML = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.2"><circle cx="8" cy="8" r="4.5"/><path d="m11.5 11.5 4 4M8 5.5v5M5.5 8h5"/></svg>';
      const copy = el('div', 'research-rail-copy');
      copy.append(el('span', 'research-rail-label', 'Deep Research'), el('span', 'research-rail-activity'));
      const count = el('span', 'research-rail-count');
      const open = button('View notes', () => openPresentation(row._research), 'research-rail-open');
      rail.append(symbol, copy, count, open);
      deck.append(rail);
    }
    const labels = { framing: 'Planning the investigation', reading: 'Reading sources',
      connecting: 'Connecting the evidence', presenting: 'Preparing the presentation' };
    rail.querySelector('.research-rail-activity').textContent = labels[data.phase] || 'Researching';
    rail.querySelector('.research-rail-count').textContent = `${data.sources.length} sources`;
    const coverage = new Set(data.notes.map(note => note.question)).size;
    rail.title = `${data.brief.topic}\n${data.notes.length} saved notes · ${coverage}/${data.questions.length} questions have evidence`;
  }

  function finish(row) {
    if (!row?._research) return;
    const data = row._research;
    if (data.status === 'running') data.status = 'incomplete';
    update(row, data);
  }
  window.AccurettaResearch = { requestBrief, mount, update, finish, openPresentation };
})();
