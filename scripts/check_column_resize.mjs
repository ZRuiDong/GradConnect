// Dependency-free interaction checks; these do not replace browser visual review.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const key = 'gradconnect.columns.v1.admin.students';
function fixture(stored = null, unavailable = false) {
  const storage = new Map(stored === null ? [] : [[key, stored]]);
  const classes = new Set();
  const columns = [
    {colKey: 'select', width: '46', min: '46', max: '46'},
    {colKey: 'identity', width: '190', min: '120', max: '480'},
    {colKey: 'actions', width: '300', min: '280', max: '420'},
  ].map(dataset => ({dataset, style: {}}));
  function element(dataset) {
    const listeners = new Map(), attributes = new Map(), pointers = new Set();
    return {dataset, attributes, style: {},
      addEventListener(name, callback) { listeners.set(name, [...(listeners.get(name) || []), callback]); },
      setAttribute(name, value) { attributes.set(name, value); },
      focus() {}, setPointerCapture(id) { pointers.add(id); }, hasPointerCapture(id) { return pointers.has(id); },
      releasePointerCapture(id) { pointers.delete(id); },
      fire(name, extra = {}) {
        const event = {button: 0, pointerId: 1, clientX: 100, preventDefault() { this.prevented = true; }, ...extra};
        for (const fn of listeners.get(name) || []) fn(event);
        return event;
      },
    };
  }
  const handles = columns.slice(1).map(col => element({columnResizer: col.dataset.colKey}));
  const reset = element({resetColumns: 'students'});
  const table = {dataset: {resizableColumns: 'students'}, style: {},
    querySelectorAll: selector => selector.startsWith('col[') ? columns : handles};
  const document = {body: {dataset: {navRole: 'admin'}, classList: {add: name => classes.add(name), remove: name => classes.delete(name)}},
    querySelector: () => null, addEventListener() {},
    querySelectorAll: selector => selector === '[data-resizable-columns]' ? [table] : selector === '[data-reset-columns]' ? [reset] : []};
  const window = {addEventListener() {}, localStorage: {
    getItem(name) { if (unavailable) throw Error('Storage disabled'); return storage.get(name) ?? null; },
    setItem(name, value) { if (unavailable) throw Error('Storage disabled'); storage.set(name, value); },
    removeItem(name) { if (unavailable) throw Error('Storage disabled'); storage.delete(name); },
  }};
  vm.runInNewContext(source, {document, window});
  return {columns, handles, reset, table, storage, classes};
}

let f = fixture();
assert.equal(f.table.style.width, '536px');
const handle = f.handles[0];
handle.fire('pointerdown');
assert.ok(f.classes.has('is-resizing-column'));
handle.fire('pointermove', {clientX: 180, pointerId: 2});
assert.equal(f.columns[1].style.width, '190px'); // Another finger must not change this drag.
handle.fire('pointermove', {clientX: 180});
assert.equal(f.columns[1].style.width, '270px');
assert.equal(f.table.style.width, '616px');
handle.fire('pointerup');
assert.equal(f.classes.size, 0);
assert.equal(JSON.parse(f.storage.get(key)).identity, 270);
assert.equal(handle.attributes.get('aria-valuenow'), '270');

handle.fire('keydown', {key: 'End'});
assert.equal(f.columns[1].style.width, '480px');
handle.fire('keydown', {key: 'ArrowRight'});
assert.equal(f.columns[1].style.width, '480px');
handle.fire('keydown', {key: 'ArrowLeft', shiftKey: true});
assert.equal(f.columns[1].style.width, '430px');
handle.fire('keydown', {key: 'Home'});
assert.equal(f.columns[1].style.width, '120px');
handle.fire('dblclick');
assert.equal(f.columns[1].style.width, '190px');
handle.fire('pointerdown');
handle.fire('pointermove', {clientX: -900});
assert.equal(f.columns[1].style.width, '120px');
handle.fire('pointercancel');
assert.equal(f.classes.size, 0);
handle.fire('pointermove', {clientX: 200});
assert.equal(f.columns[1].style.width, '120px');
f.reset.fire('click');
assert.equal(f.table.style.width, '536px');
assert.equal(f.storage.has(key), false);

f = fixture(JSON.stringify({identity: 9999, actions: -1, select: 9999}));
assert.equal(f.columns[0].style.width, '46px');
assert.equal(f.columns[1].style.width, '480px');
assert.equal(f.columns[2].style.width, '280px');
for (const value of ['broken-json', 'null', '[]', '{"identity":"9999"}']) {
  assert.equal(fixture(value).columns[1].style.width, '190px');
}
f = fixture(null, true);
f.handles[0].fire('keydown', {key: 'ArrowRight'});
assert.equal(f.columns[1].style.width, '200px');
f.reset.fire('click');
assert.equal(f.columns[1].style.width, '190px');
console.log('PASS: pointer drag, multi-pointer isolation, width bounds, keyboard, cancel, accessibility values, persistence, reset and unavailable/corrupt storage.');
