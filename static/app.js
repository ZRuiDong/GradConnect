// Ordinary page-size choices work without JavaScript; custom size is an enhancement.
document.querySelectorAll('[data-page-size-form]').forEach(form => {
  const select = form.querySelector('[data-page-size-select]');
  const custom = form.querySelector('[data-page-size-custom]');
  const input = custom.querySelector('input');
  form.querySelector('[data-page-size-custom-option]').hidden = false;
  const sync = (focus = false) => {
    const enabled = select.value === 'custom';
    custom.hidden = !enabled;
    input.disabled = !enabled;
    input.required = enabled;
    if (enabled) select.removeAttribute('name');
    else select.setAttribute('name', 'per_page');
    if (enabled && focus) { input.focus(); input.select(); }
  };
  select.addEventListener('change', () => sync(true));
  window.addEventListener('pageshow', () => sync());
  sync();
});

document.querySelectorAll('form[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
document.querySelectorAll('form[data-upload-form]').forEach(form => {
  form.addEventListener('submit', event => {
    if (form.checkValidity()) {
      const hint = form.querySelector('.upload-hint');
      if (hint) hint.hidden = false;
      if (form.hasAttribute('data-student-import')) {
        if (form.dataset.submitting) {
          event.preventDefault();
          return;
        }
        form.dataset.submitting = '1';
        form.setAttribute('aria-busy', 'true');
      }
    }
  });
});
window.addEventListener('pageshow', event => {
  if (!event.persisted) return;
  document.querySelectorAll('form[data-student-import]').forEach(form => {
    delete form.dataset.submitting;
    form.removeAttribute('aria-busy');
    const hint = form.querySelector('.upload-hint');
    if (hint) hint.hidden = true;
  });
});
document.querySelectorAll('[data-tenure-manager]').forEach(manager => {
  const form = manager.closest('form');
  const isNew = manager.dataset.newStudent === 'true';
  const feedback = manager.querySelector('[data-tenure-feedback]');
  let busy = false, conflict = false;
  const rows = () => Array.from(manager.querySelectorAll('[data-tenure-row]'));
  const fields = row => Object.fromEntries(['position', 'starts_on', 'ends_on'].map(name => [name, row.querySelector(`[name$="-${name}"]`)]));
  const sortRows = () => {
    const key = manager.dataset.tenureSortKey, direction = manager.dataset.tenureSortDirection;
    if (key && !rows().some(row => row.classList.contains('is-editing'))) {
      const compare = new Intl.Collator('zh-CN', {numeric: true}).compare;
      const ordered = rows().sort((a, b) => compare(fields(a)[key].value, fields(b)[key].value) * (direction === 'desc' ? -1 : 1));
      manager.querySelector('#tenure-rows').append(...ordered);
    }
    manager.querySelectorAll('[data-tenure-sort]').forEach(button => {
      const active = button.dataset.tenureSort === key && button.dataset.direction === direction;
      button.classList.toggle('is-active', active); button.setAttribute('aria-pressed', String(active));
    });
  };
  const show = (message, error = false, reload = '') => {
    feedback.textContent = message;
    feedback.classList.toggle('is-error', error);
    feedback.hidden = false;
    if (reload) {
      const link = document.createElement('a');
      link.href = reload; link.textContent = '重新加载并核对';
      feedback.append(' ', link);
    }
  };
  const clearErrors = row => {
    row.querySelectorAll('[data-tenure-field-error], [data-tenure-row-errors]').forEach(el => { el.textContent = ''; });
    Object.values(fields(row)).forEach(input => { input.setCustomValidity(''); input.removeAttribute('aria-invalid'); });
  };
  const refresh = () => {
    rows().forEach(row => {
      Object.entries(fields(row)).forEach(([name, input]) => {
        const value = input.value || (name === 'ends_on' ? '未设结束日期' : '未填写');
        const span = row.querySelector(`[data-tenure-field="${name}"] [data-tenure-value]`);
        span.textContent = value; span.title = value;
      });
    });
    manager.querySelector('[data-tenure-empty]').hidden = rows().some(row => !row.classList.contains('is-deleted'));
    sortRows();
  };
  const validate = row => {
    clearErrors(row);
    const data = fields(row);
    if (!data.position.value.trim()) data.position.setCustomValidity('请填写任职职务。');
    if (!data.starts_on.value) data.starts_on.setCustomValidity('请选择开始日期。');
    if (data.ends_on.value && data.starts_on.value && data.ends_on.value < data.starts_on.value) data.ends_on.setCustomValidity('结束日期不能早于开始日期。');
    const invalid = Object.values(data).find(input => !input.checkValidity());
    if (!invalid) return true;
    invalid.reportValidity();
    show('请先补齐履职信息，并核对起止日期。', true);
    return false;
  };
  const removeDraft = row => {
    row.remove();
    // Only new-account drafts or the final unsaved row can be removed locally.
    if (isNew) {
      rows().forEach((entry, index) => {
        entry.querySelectorAll('[name], [id], label[for]').forEach(el => {
          ['name', 'id', 'for'].forEach(attr => {
            if (el.hasAttribute(attr)) el.setAttribute(attr, el.getAttribute(attr).replace(/tenures-\d+-/g, `tenures-${index}-`));
          });
        });
      });
    }
    manager.querySelector('#id_tenures-TOTAL_FORMS').value = rows().length;
    refresh();
  };
  const persist = async (row, action) => {
    busy = true;
    manager.setAttribute('aria-busy', 'true');
    manager.querySelectorAll('button').forEach(button => { button.disabled = true; });
    Object.values(fields(row)).forEach(input => { input.readOnly = true; });
    const params = new URLSearchParams({action, confirmed: 'yes', tenure_id: row.dataset.tenureId,
      edit_token: form.querySelector('[name="edit_token"]').value});
    if (action === 'save') Object.entries(fields(row)).forEach(([name, input]) => { params.set(name, input.value); });
    show(action === 'delete' ? '正在删除履职记录…' : '正在保存履职记录…');
    try {
      const response = await fetch(manager.dataset.actionUrl, {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value,
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'}, body: params});
      if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('操作未完成，请重新加载页面并确认登录状态后核对。');
      const data = await response.json();
      if (!response.ok) {
        conflict = response.status === 409;
        if (data.errors) Object.entries(data.errors).forEach(([name, errors]) => {
          const target = name === '__all__' ? row.querySelector('[data-tenure-row-errors]') : row.querySelector(`[data-tenure-field="${name}"] [data-tenure-field-error]`);
          if (target) target.textContent = errors.map(error => error.message).join(' ');
          if (fields(row)[name]) fields(row)[name].setAttribute('aria-invalid', 'true');
        });
        show(data.message || '操作未完成，请核对后重试。', true, data.reload_url);
        return;
      }
      form.querySelector('[name="edit_token"]').value = data.edit_token;
      manager.querySelector('[data-tenure-content]').innerHTML = data.html;
      refresh(); show(data.message);
      const saved = data.saved_id && manager.querySelector(`[data-tenure-id="${data.saved_id}"]`);
      if (saved) {
        saved.classList.add('just-saved');
        saved.querySelector('[data-tenure-action="edit"]').focus({preventScroll: true});
        saved.scrollIntoView({block: 'nearest', behavior: 'smooth'});
      }
    } catch (error) {
      show(error.message || '网络异常，请重新加载并核对保存结果。', true);
    } finally {
      busy = false;
      manager.removeAttribute('aria-busy');
      manager.querySelectorAll('button').forEach(button => { button.disabled = conflict; });
      Object.values(fields(row)).forEach(input => { input.readOnly = false; });
    }
  };
  manager.addEventListener('click', event => {
    const add = event.target.closest('[data-add-tenure]');
    const button = event.target.closest('[data-tenure-action]');
    const sort = event.target.closest('[data-tenure-sort]');
    if ((!add && !button && !sort) || busy || conflict) return;
    const active = rows().find(row => row.classList.contains('is-editing') && !row.classList.contains('is-deleted'));
    if (sort) {
      if (active) { show('请先保存或取消正在编辑的履职记录，再调整排序。', true); return; }
      manager.dataset.tenureSortKey = sort.dataset.tenureSort;
      manager.dataset.tenureSortDirection = sort.dataset.direction;
      sortRows(); return;
    }
    if (add) {
      if (active) { show('请先保存或取消正在编辑的履职记录。', true); return; }
      const total = manager.querySelector('#id_tenures-TOTAL_FORMS');
      const index = Number(total.value);
      if (index >= 100) { show('每位学生最多维护100条履职记录。', true); return; }
      manager.querySelector('#tenure-rows').insertAdjacentHTML('beforeend', manager.querySelector('#tenure-empty-form').innerHTML.replaceAll('__prefix__', String(index)));
      total.value = index + 1;
      const row = rows().at(-1);
      row.dataset.draftNew = 'true'; row.classList.add('is-editing');
      refresh(); fields(row).position.focus();
      return;
    }
    const row = button.closest('[data-tenure-row]');
    const action = button.dataset.tenureAction;
    if (action === 'edit') {
      if (active && active !== row) { show('请先保存或取消正在编辑的履职记录。', true); return; }
      row.beforeEdit = Object.fromEntries(Object.entries(fields(row)).map(([name, input]) => [name, input.value]));
      clearErrors(row); row.classList.add('is-editing'); fields(row).position.focus();
    } else if (action === 'cancel') {
      if (row.dataset.draftNew) removeDraft(row);
      else {
        if (row.beforeEdit) Object.entries(row.beforeEdit).forEach(([name, value]) => { fields(row)[name].value = value; });
        clearErrors(row); row.classList.remove('is-editing'); refresh();
      }
    } else if (action === 'delete') {
      if (active && active !== row) { show('请先保存或取消正在编辑的履职记录。', true); return; }
      if (!window.confirm(`确定删除“${row.dataset.recordName}”这条履职记录吗？`)) return;
      if (isNew) { removeDraft(row); show('已从待创建的学生资料中移除该条履职记录。'); }
      else persist(row, 'delete');
    } else if (action === 'save') {
      if (!validate(row)) return;
      const note = isNew ? '确认后将在创建学生账号时一并保存。' : '确认后立即保存本条履职记录。';
      if (!window.confirm(`确定保存“${fields(row).position.value.trim()}”的履职信息吗？${note}`)) return;
      if (isNew) {
        if (!fields(row).ends_on.value) fields(row).ends_on.value = '2999-12-31';
        row.dataset.recordName = fields(row).position.value.trim();
        delete row.dataset.draftNew; row.classList.remove('is-editing');
        refresh(); show('本条履职已确认，将在创建学生账号时一并保存。');
      } else persist(row, 'save');
    }
  });
  manager.addEventListener('input', event => {
    const row = event.target.closest('[data-tenure-row]');
    if (row && event.target.matches('input')) clearErrors(row);
  });
  manager.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.matches('.tenure-input input')) {
      event.preventDefault();
      event.target.closest('[data-tenure-row]').querySelector('[data-tenure-action="save"]').click();
    }
  });
  form.addEventListener('submit', event => {
    if (busy || conflict || rows().some(row => row.classList.contains('is-editing') && !row.classList.contains('is-deleted'))) {
      event.preventDefault();
      show(conflict ? '资料已变化，请重新加载并核对后保存。' : '请先保存或取消正在编辑的履职记录，再提交学生基本资料。', true);
      feedback.scrollIntoView({block: 'nearest'});
    }
  });
  refresh();
  const anchor = window.location?.hash;
  if (anchor && /^#tenure-\d+$/.test(anchor)) manager.querySelector(anchor)?.querySelector('[data-tenure-action="edit"]')?.click();
});

document.querySelectorAll('[data-tenure-delete-form]').forEach(form => {
  const button = form.querySelector('button');
  const hint = form.querySelector('[data-tenure-delete-error]');
  let pending = false;
  button.disabled = false;
  form.addEventListener('submit', async event => {
    // The common data-confirm handler runs first; cancelling must send no request.
    if (event.defaultPrevented) return;
    event.preventDefault();
    if (pending) return;
    pending = true; button.disabled = true;
    hint.textContent = '正在删除…'; hint.hidden = false;
    try {
      const response = await fetch(form.getAttribute('action'), {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': form.querySelector('[name="csrfmiddlewaretoken"]').value}, body: new FormData(form)});
      if (response.redirected || !response.headers.get('content-type')?.includes('application/json')) throw new Error('操作未完成，请刷新页面并核对登录状态。');
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || '删除未完成，请刷新后核对。');
      window.location.reload();
    } catch (error) {
      hint.textContent = error.message || '网络异常，请刷新后核对删除结果。';
      pending = false; button.disabled = false;
    }
  });
});

document.querySelector('[data-select-students]')?.addEventListener('change', event => {
  document.querySelectorAll('input[name="ids"]').forEach(box => { box.checked = event.target.checked; });
});
const filterDropdowns = Array.from(document.querySelectorAll('[data-filter-dropdown]'));
filterDropdowns.forEach(dropdown => {
  const summary = dropdown.querySelector('summary');
  const text = dropdown.querySelector('[data-selection-text]');
  const count = dropdown.querySelector('[data-selection-count]');
  const search = dropdown.querySelector('[data-option-search]');
  const empty = dropdown.querySelector('[data-options-empty]');
  const options = Array.from(dropdown.querySelectorAll('[data-filter-option]'));
  const refreshSelection = () => {
    const selected = options.filter(option => option.querySelector('input').checked);
    const first = selected[0]?.querySelector('span').textContent.trim();
    text.textContent = selected.length ? first + (selected.length > 1 ? ` 等${selected.length}项` : '') : dropdown.dataset.placeholder;
    text.classList.toggle('has-selection', selected.length > 0);
    count.textContent = selected.length;
    count.hidden = selected.length === 0;
    summary.setAttribute('aria-label', `${dropdown.dataset.label}，${text.textContent}`);
  };
  const filterOptions = () => {
    const term = search.value.trim().toLocaleLowerCase();
    let visible = 0;
    options.forEach(option => {
      option.hidden = !option.textContent.toLocaleLowerCase().includes(term);
      if (!option.hidden) visible += 1;
    });
    empty.hidden = visible > 0;
    empty.textContent = options.length ? '没有匹配的选项' : '暂无可选项';
  };
  dropdown.addEventListener('change', refreshSelection);
  dropdown.addEventListener('toggle', () => {
    if (!dropdown.open) return;
    filterDropdowns.forEach(other => { if (other !== dropdown) other.open = false; });
    dropdown.classList.remove('align-end');
    const panel = dropdown.querySelector('.filter-dropdown-panel');
    if (panel.getBoundingClientRect().right > document.documentElement.clientWidth - 16) dropdown.classList.add('align-end');
    search.value = '';
    filterOptions();
  });
  dropdown.addEventListener('keydown', event => {
    if (event.key === 'Escape') { dropdown.open = false; summary.focus(); event.preventDefault(); }
  });
  search.addEventListener('input', filterOptions);
  search.addEventListener('keydown', event => { if (event.key === 'Enter') event.preventDefault(); });
  dropdown.querySelector('[data-clear-options]').addEventListener('click', () => {
    options.forEach(option => { option.querySelector('input').checked = false; });
    refreshSelection();
    dropdown.closest('form').querySelector('[data-filter-dirty]').hidden = false;
  });
  dropdown.querySelector('[data-close-dropdown]').addEventListener('click', () => { dropdown.open = false; summary.focus(); });
  refreshSelection();
});
document.addEventListener('click', event => {
  filterDropdowns.forEach(dropdown => { if (!dropdown.contains(event.target)) dropdown.open = false; });
});
window.addEventListener('pageshow', () => {
  filterDropdowns.forEach(dropdown => { dropdown.dispatchEvent(new Event('change')); });
});
const studentFilterForm = document.querySelector('[data-student-filter]');
const markStudentFilterDirty = event => {
  if (event.target.matches('input[name], select[name]')) studentFilterForm.querySelector('[data-filter-dirty]').hidden = false;
};
studentFilterForm?.addEventListener('input', markStudentFilterDirty);
studentFilterForm?.addEventListener('change', markStudentFilterDirty);
const sidebarGroups = Array.from(document.querySelectorAll('[data-nav-group]'));
const sidebarStorageKey = `gradconnect.sidebar.v1.${document.body.dataset.navRole || 'default'}`;
let sidebarPreferences = {};
const restoreSidebar = () => {
  try {
    const stored = JSON.parse(window.localStorage.getItem(sidebarStorageKey) || '{}');
    sidebarPreferences = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
  } catch (_) { sidebarPreferences = {}; }
  sidebarGroups.forEach(group => {
    group.open = group.dataset.currentGroup === 'true' || sidebarPreferences[group.dataset.navGroup] === true;
  });
};
restoreSidebar();
sidebarGroups.forEach(group => {
  group.addEventListener('toggle', () => {
    sidebarPreferences[group.dataset.navGroup] = group.open;
    try { window.localStorage.setItem(sidebarStorageKey, JSON.stringify(sidebarPreferences)); } catch (_) { /* 保留原生折叠功能 */ }
  });
});
window.addEventListener('pageshow', event => { if (event.persisted) restoreSidebar(); });

// Column preferences contain widths only; table data stays on the server.
document.querySelectorAll('[data-resizable-columns]').forEach(table => {
  const cols = Array.from(table.querySelectorAll('col[data-col-key]'));
  const handles = Array.from(table.querySelectorAll('[data-column-resizer]'));
  const storageKey = `gradconnect.columns.v1.${document.body.dataset.navRole}.${table.dataset.resizableColumns}`;
  const limits = cols.map(col => Number(col.dataset.max));
  const clamp = (index, value) => Math.round(Math.max(Number(cols[index].dataset.min), Math.min(limits[index], value)));
  let saved = {};
  try {
    const value = JSON.parse(window.localStorage.getItem(storageKey) || '{}');
    if (value && typeof value === 'object' && !Array.isArray(value)) saved = value;
  } catch (_) { /* Default widths also work without browser storage. */ }
  const defaults = cols.map(col => Number(col.dataset.width));
  const widths = defaults.slice();
  const growth = cols.map((col, index) => Number(col.dataset.max) - defaults[index]);
  const baseWidth = defaults.reduce((sum, width) => sum + width, 0);
  const capacity = growth.reduce((sum, extra) => sum + extra, 0);
  // Earlier versions stored all columns, including untouched defaults.
  const legacyWidths = cols.every(col => Object.hasOwn(saved, col.dataset.colKey));
  cols.forEach((col, index) => {
    const value = saved[col.dataset.colKey];
    if (!Number.isFinite(value) || !growth[index] || (legacyWidths && value === defaults[index])) {
      delete saved[col.dataset.colKey];
    }
  });
  const apply = () => {
    cols.forEach((col, index) => { col.style.width = `${widths[index]}px`; });
    table.style.width = `${widths.reduce((sum, width) => sum + width, 0)}px`;
    handles.forEach(handle => {
      const index = cols.findIndex(col => col.dataset.colKey === handle.dataset.columnResizer);
      handle.setAttribute('aria-valuenow', String(widths[index]));
      handle.setAttribute('aria-valuemax', String(limits[index]));
    });
  };
  const fit = () => {
    const surplus = Math.max(0, table.parentElement.clientWidth - baseWidth);
    let allocated = 0;
    let cumulative = 0;
    cols.forEach((col, index) => {
      cumulative += growth[index];
      const share = capacity ? Math.round(surplus * cumulative / capacity) : 0;
      const automaticWidth = defaults[index] + share - allocated;
      limits[index] = Math.max(Number(col.dataset.max), automaticWidth);
      widths[index] = clamp(index, saved[col.dataset.colKey] ?? automaticWidth);
      allocated = share;
    });
    apply();
  };
  const persist = () => {
    try { window.localStorage.setItem(storageKey, JSON.stringify(saved)); } catch (_) { /* Resizing remains usable. */ }
  };
  let drag = null;
  handles.forEach(handle => {
    const index = cols.findIndex(col => col.dataset.colKey === handle.dataset.columnResizer);
    if (index < 0) return;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0 || drag) return;
      event.preventDefault();
      handle.focus({preventScroll: true});
      drag = {pointer: event.pointerId, handle, start: event.clientX, width: widths[index]};
      handle.setPointerCapture(event.pointerId);
      document.body.classList.add('is-resizing-column');
    });
    handle.addEventListener('pointermove', event => {
      if (!drag || drag.handle !== handle || drag.pointer !== event.pointerId) return;
      widths[index] = clamp(index, drag.width + event.clientX - drag.start);
      saved[cols[index].dataset.colKey] = widths[index];
      apply();
    });
    const finish = event => {
      if (!drag || drag.handle !== handle || drag.pointer !== event.pointerId) return;
      drag = null;
      document.body.classList.remove('is-resizing-column');
      if (handle.hasPointerCapture(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      persist();
    };
    ['pointerup', 'pointercancel', 'lostpointercapture'].forEach(name => handle.addEventListener(name, finish));
    handle.addEventListener('keydown', event => {
      const step = event.shiftKey ? 50 : 10;
      const targets = {ArrowLeft: widths[index] - step, ArrowRight: widths[index] + step,
        Home: Number(cols[index].dataset.min), End: limits[index]};
      if (!Object.hasOwn(targets, event.key)) return;
      event.preventDefault();
      widths[index] = clamp(index, targets[event.key]);
      saved[cols[index].dataset.colKey] = widths[index];
      apply(); persist();
    });
    handle.addEventListener('dblclick', () => {
      delete saved[cols[index].dataset.colKey];
      fit(); persist();
    });
  });
  document.querySelectorAll('[data-reset-columns]').forEach(button => {
    if (button.dataset.resetColumns !== table.dataset.resizableColumns) return;
    button.addEventListener('click', () => {
      saved = {};
      fit();
      try { window.localStorage.removeItem(storageKey); } catch (_) { /* Ignore unavailable storage. */ }
    });
  });
  fit();
  window.addEventListener('resize', fit);
});

// An open background tab is not activity: send only after a real visible interaction.
if (document.body?.dataset.activityUrl) {
  let lastPing = Date.now(), pinging = false;
  const ping = event => {
    if (!event.isTrusted || document.visibilityState !== 'visible' || pinging || Date.now() - lastPing < 60000) return;
    const token = document.querySelector('[name="csrfmiddlewaretoken"]')?.value;
    if (!token) return;
    lastPing = Date.now(); pinging = true;
    fetch(document.body.dataset.activityUrl, {method: 'POST', credentials: 'same-origin',
      headers: {'X-CSRFToken': token}, keepalive: true}).catch(() => {}).finally(() => { pinging = false; });
  };
  ['pointerdown', 'keydown', 'scroll'].forEach(name => document.addEventListener(name, ping, {passive: true, capture: true}));
}

if (document.body?.dataset.presenceUrl && document.querySelector('[data-student-presence]')) {
  let refreshing = false;
  const refreshPresence = async () => {
    if (refreshing || document.visibilityState !== 'visible') return;
    const dots = Array.from(document.querySelectorAll('[data-student-presence]'));
    const ids = [...new Set(dots.map(dot => dot.dataset.studentPresence))];
    if (!ids.length || ids.length > 100) return;
    refreshing = true;
    try {
      const params = new URLSearchParams(); ids.forEach(id => params.append('ids', id));
      const response = await fetch(document.body.dataset.presenceUrl + '?' + params, {credentials: 'same-origin'});
      if (!response.ok || response.redirected || !response.headers.get('content-type')?.includes('application/json')) return;
      const data = await response.json();
      dots.forEach(dot => {
        const status = data.statuses?.[dot.dataset.studentPresence];
        if (!status || !['online', 'offline', 'restricted'].includes(status.key)) return;
        dot.classList.remove('is-online', 'is-offline', 'is-restricted');
        dot.classList.add('is-' + status.key); dot.title = status.label; dot.setAttribute('aria-label', status.label);
      });
    } catch (_) { /* The existing status remains available if the network is interrupted. */ }
    finally { refreshing = false; }
  };
  window.setInterval(refreshPresence, 60000);
  document.addEventListener('visibilitychange', refreshPresence);
}

// Message counts are per recipient; polling only runs while this tab is visible.
document.querySelectorAll('[data-inbox-count-url]').forEach(link => {
  let busy = false;
  const refresh = async () => {
    if (busy || document.visibilityState !== 'visible') return;
    busy = true;
    try {
      const response = await fetch(link.dataset.inboxCountUrl, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
      if (!response.ok || !response.headers.get('Content-Type')?.includes('application/json')) return;
      const data = await response.json();
      if (!Number.isSafeInteger(data.unread) || data.unread < 0) return;
      const badge = link.querySelector('[data-inbox-count]');
      badge.textContent = data.unread > 99 ? '99+' : String(data.unread);
      badge.hidden = data.unread === 0;
      link.setAttribute('aria-label', `消息中心，${data.unread}条未读消息`);
    } catch (_) { /* The next visible refresh retries without interrupting work. */ }
    finally { busy = false; }
  };
  window.setInterval(refresh, 60000);
  document.addEventListener('visibilitychange', refresh);
});

// A visible detail page acknowledges only messages present before its contents loaded.
document.querySelectorAll('[data-inbox-topic-read]').forEach(form => {
  let busy = false, acknowledged = false;
  const acknowledge = async () => {
    if (busy || acknowledged || document.visibilityState !== 'visible') return;
    busy = true;
    try {
      const response = await fetch(form.action, {method: 'POST', credentials: 'same-origin',
        headers: {'X-CSRFToken': form.querySelector('[name=csrfmiddlewaretoken]').value, 'Accept': 'application/json'}, body: new FormData(form)});
      if (!response.ok || !response.headers.get('Content-Type')?.includes('application/json')) return;
      const data = await response.json();
      if (!Number.isSafeInteger(data.unread) || data.unread < 0) return;
      acknowledged = true;
      document.querySelectorAll('[data-inbox-count-url]').forEach(link => {
        const badge = link.querySelector('[data-inbox-count]');
        badge.textContent = data.unread > 99 ? '99+' : String(data.unread);
        badge.hidden = data.unread === 0;
        link.setAttribute('aria-label', `消息中心，${data.unread}条未读消息`);
      });
    } catch (_) { /* The message center can still acknowledge explicitly. */ }
    finally { busy = false; }
  };
  document.addEventListener('visibilitychange', acknowledge);
  acknowledge();
});
