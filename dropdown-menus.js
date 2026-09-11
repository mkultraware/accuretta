(() => {
  'use strict';
  let active = null;
  let typeahead = '';
  let typeaheadTimer;
  const menu = document.createElement('div');
  menu.className = 'unified-select-menu';
  menu.id = 'accuretta-select-menu';
  menu.setAttribute('popover', 'manual');
  menu.hidden = true;
  menu.setAttribute('role', 'listbox');
  const options = () => [...menu.querySelectorAll('[role="option"]:not(:disabled)')];
  function close(restore = false) {
    if (!active) return;
    const previous = active;
    active = null;
    observer.disconnect();
    menu.hidePopover?.();
    menu.hidden = true;
    previous.setAttribute('aria-expanded', 'false');
    if (restore && previous.isConnected) previous.focus();
  }
  function place() {
    if (!active?.isConnected) { close(); return; }
    const rect = active.getBoundingClientRect();
    const width = Math.min(Math.max(rect.width, 224), innerWidth - 24);
    const below = innerHeight - rect.bottom - 12;
    const above = rect.top - 12;
    menu.style.width = `${width}px`;
    menu.style.maxHeight = `${Math.max(80, Math.min(340, Math.max(above, below)))}px`;
    menu.style.left = `${Math.max(12, Math.min(rect.left, innerWidth - width - 12))}px`;
    menu.style.top = `${below >= Math.min(menu.scrollHeight, 260) || below >= above ? rect.bottom + 6 : Math.max(12, rect.top - menu.offsetHeight - 6)}px`;
  }
  function render() {
    if (!active) return;
    if (active.disabled || !active.isConnected) { close(); return; }
    menu.replaceChildren();
    let group = null;
    [...active.options].forEach((option, index) => {
      if (option.hidden || option.parentElement.hidden) return;
      const parent = option.parentElement;
      if (parent.tagName === 'OPTGROUP' && parent !== group) {
        group = parent;
        const label = document.createElement('div');
        label.className = 'unified-menu-heading';
        label.textContent = parent.label;
        menu.append(label);
      }
      const item = document.createElement('button');
      item.type = 'button';
      item.className = 'unified-menu-option';
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.selected));
      item.disabled = option.disabled || (parent.tagName === 'OPTGROUP' && parent.disabled);
      item.textContent = option.label;
      item.addEventListener('click', () => {
        const select = active;
        if (!select || item.disabled) return;
        const changed = select.selectedIndex !== index;
        select.selectedIndex = index;
        close(true);
        if (changed) {
          select.dispatchEvent(new Event('input', { bubbles: true }));
          select.dispatchEvent(new Event('change', { bubbles: true }));
        }
      });
      menu.append(item);
    });
    if (!menu.childElementCount) {
      const empty = document.createElement('p');
      empty.className = 'unified-menu-heading';
      empty.textContent = 'No options available';
      menu.append(empty);
    }
    place();
  }
  const observer = new MutationObserver(render);
  function open(select, last = false) {
    if (active === select) { close(true); return; }
    close();
    active = select;
    (select.closest('dialog') || document.body).append(menu);
    select.setAttribute('aria-haspopup', 'listbox');
    select.setAttribute('aria-controls', menu.id);
    select.setAttribute('aria-expanded', 'true');
    menu.setAttribute('aria-label', select.getAttribute('aria-label') || select.labels?.[0]?.textContent.trim() || select.title || 'Choose an option');
    menu.hidden = false;
    render();
    menu.showPopover?.();
    place();
    observer.observe(select, { childList: true, subtree: true, attributes: true, characterData: true });
    const items = options();
    (items.find(item => item.getAttribute('aria-selected') === 'true') || (last ? items.at(-1) : items[0]))?.focus();
  }
  const isDropdown = target => target instanceof HTMLSelectElement && !target.multiple && target.size <= 1 && !target.disabled;
  document.addEventListener('mousedown', event => {
    if (isDropdown(event.target)) { event.preventDefault(); open(event.target); }
    else if (active && !menu.contains(event.target)) close();
  }, true);
  document.addEventListener('touchstart', event => {
    if (isDropdown(event.target)) { event.preventDefault(); open(event.target); }
    else if (active && !menu.contains(event.target)) close();
  }, { capture: true, passive: false });
  document.addEventListener('keydown', event => {
    if (isDropdown(event.target) && ['Enter', ' ', 'ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault(); open(event.target, event.key === 'ArrowUp'); return;
    }
    if (!active) return;
    if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(true); return; }
    if (event.key === 'Tab') { close(true); return; }
    const items = options();
    const index = items.indexOf(document.activeElement);
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 :
        (index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && event.key !== ' ') {
      event.preventDefault();
      clearTimeout(typeaheadTimer);
      typeahead += event.key.toLocaleLowerCase();
      items.find(item => item.textContent.toLocaleLowerCase().startsWith(typeahead))?.focus();
      typeaheadTimer = setTimeout(() => { typeahead = ''; }, 700);
    }
  }, true);
  document.addEventListener('scroll', event => { if (active && !menu.contains(event.target)) close(); }, true);
  window.addEventListener('resize', () => close());
})();
