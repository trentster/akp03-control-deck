let state, selectedKey = 0, selectedMenu = 0, selectedPage = 0, selectedSlot = 0, slotMode = 'action';
// Which of the three caps under the LCD keys is being edited, or null while an
// LCD key is.  The two never overlap: the panel answers for one control at a time.
let selectedCap = null;
// Which of the three dials is being edited, or null while something else is.  A dial
// is the one control whose panel answers for three slots at once, so it holds its own
// place rather than sharing the caps'.
let selectedDial = null;
let source = null, dirty = false, saving = false;
let actionEdit = null;
// What the chooser is answering for: a deck key ("key") or a content key inside
// a menu page ("slot").
let assignKind = 'key';

const $ = selector => document.querySelector(selector);
const esc = value => String(value).replace(/[&<>"']/g, character => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
}[character]));

function title(id) {
  return String(id || 'Unassigned').replace(/[-_]/g, ' ').replace(/\b\w/g, character => character.toUpperCase());
}

// ── Live deck state (read-only) ─────────────────────────────────────────────
// The daemon hosts this editor and streams its own ControlFeed over SSE.
// Connection state arrives as `state` frames; physical control presses as
// `control` frames (dials are single-shot and send no release). The browser's
// EventSource handles reconnection on its own.

const PRESS_FADE_MS = 1400;
const KEY_GLYPHS = {
  ctrl: '⌃', control: '⌃', shift: '⇧', alt: '⌥', option: '⌥',
  cmd: '⌘', command: '⌘', meta: '⌘', super: '⌘',
  enter: '⏎', return: '⏎', space: '␣', tab: '⇥', esc: '⎋', escape: '⎋',
  delete: '⌫', backspace: '⌫',
};
const PHASES = {
  ready: ['Deck connected', '', 'ok'],
  starting: ['Connecting to the deck…', 'Opening the deck through the local helper.', 'wait'],
  waiting: ['Deck not connected', 'Waiting for the deck to be plugged in and authorised.', 'wait'],
  lost: ['Deck connection lost', 'Waiting to reconnect to the deck.', 'bad'],
  blocked: ['Deck not answering', 'The deck is plugged in, but nothing is coming from it. Try another USB port, or a powered hub.', 'bad'],
};
const activeControls = new Set();
const pressTimers = new Map();

// A dial turn is state-less on the wire: the six frozen control names carry the
// direction, and only the presentation was previously losing it.  The lookup
// below keeps both directions pointing at the one physical dial (the dial's
// `data-turn` then shows which way) and the sweep is a Web Animation so a
// second turn the same way in a row always re-animates -- re-adding an
// unchanged CSS class would not restart a CSS animation.
const DIAL_NAMES = {top: 'top', bottomLeft: 'bottom_left', bottomRight: 'bottom_right'};
const SWEEP_MS = 400;

function dialTurn(name) {
  const match = /^(top|bottomLeft|bottomRight)(Clockwise|CounterClockwise)$/.exec(name);
  if (!match) return null;
  return {dial: DIAL_NAMES[match[1]], clockwise: match[2] === 'Clockwise'};
}

function prefersReducedMotion() {
  return typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function cueDialTurn({dial, clockwise}) {
  const element = document.querySelector(`[data-control="${dial}"]`);
  if (!element) return;
  element.dataset.turn = clockwise ? 'cw' : 'ccw';
  const marker = element.querySelector('.dial-marker');
  if (!marker || typeof marker.animate !== 'function' || prefersReducedMotion()) return;
  if (marker.animation) marker.animation.cancel();
  // Out-and-back nudge: the marker swings out in the turn's direction and
  // returns, so a fresh call re-runs it and the direction of travel is the cue.
  marker.animation = marker.animate([
    {transform: 'rotate(0deg)'},
    {transform: `rotate(${clockwise ? '' : '-'}105deg)`, offset: 0.5},
    {transform: 'rotate(0deg)'},
  ], {duration: SWEEP_MS, easing: 'ease-in-out'});
}

function keyGlyph(name) {
  return KEY_GLYPHS[String(name).toLowerCase()] || title(name);
}

function shortcut(keys) {
  return keys.map(keyGlyph).join(' ');
}

function controlElement(name) {
  if (/^key[0-5]$/.test(name)) return document.querySelector(`#deckKeys .key[data-key="${name.slice(3)}"]`);
  if (/^button[6-8]$/.test(name)) return document.querySelector(`[data-control="${name}"]`);
  if (/^top/.test(name)) return document.querySelector('[data-control="top"]');
  if (/^bottomLeft/.test(name)) return document.querySelector('[data-control="bottom_left"]');
  if (/^bottomRight/.test(name)) return document.querySelector('[data-control="bottom_right"]');
  return null;
}

function paintControl(name, on) {
  const element = controlElement(name);
  if (element) element.classList.toggle('pressed', on);
}

function releaseControl(name) {
  const timer = pressTimers.get(name);
  if (timer) { clearTimeout(timer); pressTimers.delete(name); }
  activeControls.delete(name);
  paintControl(name, false);
}

function pressControl(name) {
  const existing = pressTimers.get(name);
  if (existing) { clearTimeout(existing); pressTimers.delete(name); }
  activeControls.add(name);
  paintControl(name, true);
  const turn = dialTurn(name);
  if (turn) {
    cueDialTurn(turn);
    // A dial turn sends no release at all, so its light is a momentary cue and must
    // clear itself.  A key or button does send one - and a hold lasts as long as the
    // person keeps the button down, which is the whole point of a hold - so those stay
    // lit until the real release arrives.  A release lost to a cable must not stick
    // forever: the next press of the same control repaints it and its release clears it.
    pressTimers.set(name, setTimeout(() => releaseControl(name), PRESS_FADE_MS));
  }
}

function onControlFrame(frame) {
  if (!frame || typeof frame.control !== 'string' || !frame.control) return;
  if (frame.state === 'release') releaseControl(frame.control);
  else pressControl(frame.control);
}

function repaintActiveControls() {
  activeControls.forEach(name => paintControl(name, true));
}

function setDeckStatus(phase) {
  const [label, detail, tone] = PHASES[phase] || ['Unknown deck state', 'The daemon reported an unrecognised phase.', 'wait'];
  const block = $('#deckLive');
  if (block) block.className = `sample live-status ${tone}`;
  const stateText = $('#deckState');
  if (stateText) stateText.textContent = label;
  const detailText = $('#deckDetail');
  // A phase that needs no second line gets none: an empty paragraph would leave
  // a gap where there is nothing to say.
  if (detailText) { detailText.textContent = detail; detailText.hidden = !detail; }
}

// ── Device mirror (what the panel is showing) ───────────────────────────────
// The daemon publishes `screens` frames describing its own paint: each key's
// text or artwork, its tile colour, the open menu and the dial's highlight.
// This panel renders exactly that. It must never work out for itself what the
// deck "should" show - a second implementation would drift from the panel.

const CSS_COLOUR = /^(#[0-9a-fA-F]{3,8}|[a-zA-Z]+)$/;

function mirrorColour(value, fallback) {
  return typeof value === 'string' && CSS_COLOUR.test(value) ? value : fallback;
}

function mirrorFace(screen) {
  if (screen.kind === 'image' && screen.revision) {
    const url = `/api/screen/${screen.key}.png?v=${encodeURIComponent(screen.revision)}`;
    return `<img src="${url}" alt="Artwork on key ${screen.key + 1}" width="60" height="60">`;
  }
  return `<span class="mirror-text">${esc(screen.text || '')}</span>`;
}

let lastScreens = null;

// Any place that wants to show the panel's true state carries `data-mirror` and
// is painted from the same frame, so the Deck view and the Icons view can never
// disagree about what the hardware is showing.
function renderScreens(screens) {
  lastScreens = screens;
  document.querySelectorAll('[data-mirror]').forEach(block => paintMirror(block, screens));
}

function paintMirror(block, screens) {
  const box = block.querySelector('[data-mirror-keys]');
  const layerText = block.querySelector('[data-mirror-layer]');
  if (!box || !layerText) return;
  if (!screens || !Array.isArray(screens.keys) || !screens.keys.length) {
    layerText.textContent = 'Waiting for the daemon…';
    box.innerHTML = '';
    return;
  }
  const layer = screens.layer || { kind: 'idle' };
  if (layer.dark) {
    // Dark keys look like nothing at all, so this is the one idle state worth a
    // sentence: the difference between asleep and broken.
    layerText.textContent = 'The keys are dark: the deck has been idle. Any touch wakes it.';
  } else if (layer.kind === 'menu') {
    const dial = (layer.selected_key === null || layer.selected_key === undefined)
      ? 'nothing selected' : `dial on key ${layer.selected_key + 1}`;
    layerText.textContent = `Menu “${layer.title || layer.page}” · ${dial}`;
  } else {
    // Nothing to announce: what the keys are showing is already on the keys.
    layerText.textContent = '';
  }
  if (layerText) layerText.hidden = !layerText.textContent;
  box.innerHTML = screens.keys.map(screen => {
    const classes = ['mirror-key'];
    if (screen.selected) classes.push('is-selected');
    const style = screen.kind === 'image'
      ? ''
      : ` style="background:${mirrorColour(screen.background, 'black')};color:${mirrorColour(screen.foreground, 'white')}"`;
    return `<figure class="${classes.join(' ')}" data-key="${screen.key}">`
      + `<div class="mirror-face"${style}>${mirrorFace(screen)}</div>`
      + `<figcaption>KEY ${screen.key + 1}</figcaption></figure>`;
  }).join('');
}

function connectFeed() {
  const feed = new EventSource('/api/events');
  feed.addEventListener('state', event => {
    try { setDeckStatus(JSON.parse(event.data).phase); } catch (error) { /* ignore malformed frame */ }
  });
  feed.addEventListener('control', event => {
    try { onControlFrame(JSON.parse(event.data)); } catch (error) { /* ignore malformed frame */ }
  });
  feed.addEventListener('screens', event => {
    try { renderScreens(JSON.parse(event.data)); } catch (error) { /* ignore malformed frame */ }
  });
  // A page that loads between frames still needs the current ones straight away.
  fetch('/api/screens')
    .then(response => response.json())
    .then(body => renderScreens(body.screens))
    .catch(() => { /* the live frames will arrive shortly */ });
  // EventSource reconnects by itself; on a drop say so until the next seeded
  // state frame proves the daemon is back.
  feed.addEventListener('error', () => {
    const block = $('#deckLive');
    if (block) block.className = 'sample live-status wait';
    const stateText = $('#deckState');
    if (stateText) stateText.textContent = 'Reconnecting to the daemon…';
    const detailText = $('#deckDetail');
    if (detailText) detailText.textContent = 'The live feed dropped; retrying.';
  });
  return feed;
}

function action(id) {
  return state.actions.find(candidate => candidate.id === id);
}

function description(candidate) {
  if (!candidate) return 'No action assigned';
  if (candidate.kind === 'keys') return shortcut(candidate.keys);
  if (candidate.kind === 'brightness') return `Brightness ${candidate.brightness}%`;
  if (candidate.kind === 'menu') return `${title(candidate.menu)} menu`;
  return `Reviewed menu: ${title(candidate.route)}`;
}

function assignmentDetail(candidate) {
  if (!candidate) return {label: 'Status:', value: 'No action assigned', shortcut: false};
  if (candidate.kind === 'keys') return {label: 'Shortcut:', value: candidate.keys.map(title).join(' + '), shortcut: true};
  if (candidate.kind === 'brightness') return {label: 'Sets:', value: `Brightness ${candidate.brightness}%`, shortcut: false};
  if (candidate.kind === 'menu') return {label: 'Opens:', value: `${title(candidate.menu)} menu`, shortcut: false};
  return {label: 'Opens:', value: title(candidate.route), shortcut: false};
}

function keyTone(index) {
  const keyNumber = index + 1;
  if (keyNumber % 3 === 0) return 'tone-red';
  if (keyNumber % 2 === 0) return 'tone-gold';
  return 'tone-teal';
}

// ── Save to the deck (the daemon owns the write) ────────────────────────────
// The editor validates drafts in memory through POST /api/draft for immediate
// feedback, but the *saved* configuration is owned by the service.  Pressing
// Save sends the draft to POST /api/save, which merges it over the daemon's
// on-disk config (preserving everything this editor does not model), writes it
// atomically, and rebinds the running deck.  The response says exactly where it
// saved and whether it was applied; nothing here ever claims a save that did
// not happen.

// Success is a moment, not a state: the line says what happened and then goes quiet,
// leaving the panel to speak for itself.  A standing condition - unsaved work, a
// failure, a deck that has not caught up - stays on screen, because it is still true.
let statusTimer = null;

function setStatus(tone, message) {
  const notice = $('#notice');
  if (!notice) return;
  if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
  notice.classList.remove('is-clearing');
  notice.textContent = message;
  notice.dataset.tone = tone || '';
  if (tone === 'saving' || tone === 'saved') {
    statusTimer = setTimeout(() => {
      notice.classList.add('is-clearing');
      statusTimer = setTimeout(() => {
        notice.textContent = '';
        notice.classList.remove('is-clearing');
        statusTimer = null;
      }, 450);
    }, 2600);
  }
}

// Choosing an icon, an action, a menu or a binding all stage the same way: the
// draft changes here and nothing reaches the hardware until it is pushed.
function markDirty(what) {
  dirty = true;
  renderSaveBar();
  setStatus('dirty', what === 'staged' ? 'Staged' : 'Unsaved changes');
}

function renderSource() {
  const pill = $('#sourcePill');
  if (pill) {
    // "Saved configuration" next to the title told nobody anything: being the saved
    // configuration is what this editor is.  The pill is kept for the two cases that
    // do need saying - still loading, or a draft that cannot be saved at all.
    const label = !source ? 'Loading…'
      : source.source === 'sample' ? 'Sample draft'
      : '';
    pill.textContent = label;
    pill.hidden = !label;
  }
  renderSaveBar();
}

// What the two buttons offer follows from one question: is there anything to save?
// A Save that is always lit invites a click that changes nothing, and a Discard that
// is always there neither says what it throws away nor that there is anything to.
function renderSaveBar() {
  const saveButton = $('#saveChanges');
  const discard = $('#discardChanges');
  const saveable = !!(source && source.can_save);
  const waiting = dirty && saveable;
  if (saveButton) {
    saveButton.disabled = !waiting;
    saveButton.title = !saveable ? 'This one cannot be saved.'
      : waiting ? 'Write your changes to the deck.'
      : 'Nothing to save — the deck already matches what you see.';
  }
  if (discard) {
    discard.hidden = !waiting;
    discard.disabled = !waiting;
    discard.title = 'Throw away your unsaved changes and go back to what is on the deck.';
  }
}

async function validateDraft() {
  const response = await fetch('/api/draft', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(state),
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error);
  state = data.draft;
  render();
}

async function saveToDeck() {
  if (saving || !source || !source.can_save) return;
  saving = true;
  const button = $('#saveChanges');
  if (button) { button.disabled = true; button.setAttribute('aria-busy', 'true'); }
  setStatus('saving', 'Saving…');
  try {
    const response = await fetch('/api/save', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(state),
    });
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'The daemon refused the save.');
    state = data.draft;
    dirty = false;
    renderSaveBar();
    // Never print the daemon's own note: it names the config file's path, which
    // is our business, not the person using the editor.
    setStatus(data.applied === false ? 'warn' : 'saved',
      data.applied === false ? 'Saved, but the deck has not picked it up yet.' : 'Saved to the deck.');
    render();
    // The push may have changed what the keys wear, so ask for the truth again.
    refreshArtworkStage();
  } catch (error) {
    // The daemon refused it or could not write; the previous config is intact.
    setStatus('error', `Not saved: ${error.message}`);
  } finally {
    saving = false;
    // The buttons are left to the state rules, not set here: a save that has just
    // succeeded leaves nothing to save, and this block would otherwise light Save
    // back up the moment the request finished.
    renderSaveBar();
    if (button) button.removeAttribute('aria-busy');
  }
}

async function discardChanges() {
  try {
    // Ask the server to forget the staged draft and re-read the file: fetching
    // /api/state alone would hand back the very changes being discarded.
    const response = await fetch('/api/draft/discard', {method: 'POST'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'The saved configuration could not be re-read.');
    state = data.draft;
    dirty = false;
    renderSource();
    setStatus('saved', 'Reloaded what is on the deck.');
    render();
    refreshArtworkStage();
  } catch (error) {
    setStatus('error', `Could not reload the saved configuration: ${error.message}`);
  }
}

async function commit(change) {
  const before = structuredClone(state);
  change();
  try {
    await validateDraft();
    markDirty();
    return true;
  } catch (error) {
    state = before;
    setStatus('error', error.message);
    render();
    return false;
  }
}

// ── Actions this editor can create safely ───────────────────────────────────
// A fresh configuration has no actions at all, so the editor has to be able to
// *make* the ordinary ones.  The catalogue is served by the daemon
// (`GET /api/actions/ready`) so the library page, the guard on an icon's action
// and the menu item picker all read the same list instead of three copies.
// Each entry is a plain keyboard shortcut or a brightness level - the exact safe
// action kinds the daemon accepts - and it is created in the draft the moment a
// key is pointed at it, in one step.

let READY_MADE = [];
let actionCatalogue = null, actionFilter = '';

async function loadActionCatalogue() {
  if (actionCatalogue) return actionCatalogue;
  const response = await fetch('/api/actions/ready');
  if (!response.ok) throw Error('The action library could not be read.');
  actionCatalogue = await response.json();
  READY_MADE = actionCatalogue.sections.flatMap(section => section.actions);
  return actionCatalogue;
}

// The shortcut tokens the deck understands.  Checking a typed key against this
// list catches a typo in plain words instead of letting it reach the deck as a
// shortcut that quietly does nothing.
const MODIFIER_TOKENS = ['ctrl', 'alt', 'shift', 'cmd'];
const MODIFIER_NAMES = {ctrl: 'Control', alt: 'Option', shift: 'Shift', cmd: 'Command'};
// A deck-picker row carrying this prefix opens a menu instead of running an action.
const MENU_OPEN_PREFIX = '__menu__:';
const NAMED_KEYS = ['space', 'tab', 'enter', 'escape', 'backspace', 'delete', 'insert',
  'home', 'end', 'pageup', 'pagedown', 'left', 'right', 'up', 'down'];
const SIMPLE_KEY = /^(?:[a-z0-9]|f(?:[1-9]|1[0-2]))$/;

function canonicalKey(value) {
  return String(value || '').trim().toLowerCase();
}

function isKnownKey(value) {
  const key = canonicalKey(value);
  return key !== '' && (NAMED_KEYS.includes(key) || SIMPLE_KEY.test(key));
}

function actionFromSpec(spec) {
  return spec.brightness != null
    ? {id: spec.id, kind: 'brightness', brightness: spec.brightness}
    : {id: spec.id, kind: 'keys', keys: spec.keys.slice()};
}

function readyMadeAction(id) {
  const spec = READY_MADE.find(candidate => candidate.id === id);
  return spec ? actionFromSpec(spec) : null;
}

// Turn a friendly name into a valid, unique action id ([a-z][a-z0-9_-]*, <=32
// characters so it still fits an LCD key).  An existing action being renamed is
// skipped so it can keep its own name.
function slugify(name) {
  let slug = String(name || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  if (!slug) return 'action';
  if (!/^[a-z]/.test(slug)) slug = `a-${slug}`.slice(0, 32);
  return slug;
}

function uniqueId(base, skipId) {
  const taken = new Set(state.actions.map(item => item.id).filter(id => id !== skipId));
  let id = base, number = 2;
  while (taken.has(id)) id = `${base.slice(0, 29)}-${number++}`;
  return id;
}

function bindingKeys(id) {
  return state.bindings.map((binding, index) => (binding === id ? index + 1 : null)).filter(Boolean);
}

// ── Assigning an action to a key ────────────────────────────────────────────
// Choosing happens on the action library page, so all that is left here is the
// write path: clear a key, add-and-assign a ready-made action, assign one the
// draft already has, or point the key at a menu.

async function assignActionToKey(value) {
  const keyNumber = selectedKey + 1;
  if (value.startsWith(MENU_OPEN_PREFIX)) {
    await assignMenuOpen(value.slice(MENU_OPEN_PREFIX.length));
    return;
  }
  if (value === '') {
    if (await commit(() => { state.bindings[selectedKey] = null; })) {
      setStatus('dirty', `Cleared Key ${keyNumber}.`);
    }
    return;
  }
  if (!action(value)) {
    const spec = readyMadeAction(value);
    if (!spec) { setStatus('error', 'That action is not available.'); return; }
    if (await commit(() => { state.actions.push(spec); state.bindings[selectedKey] = value; })) {
      setStatus('dirty', `${title(value)} added and assigned to Key ${keyNumber}.`);
    }
    return;
  }
  if (await commit(() => { state.bindings[selectedKey] = value; })) {
    setStatus('dirty', `${title(value)} assigned to Key ${keyNumber}.`);
  }
}

// ── The "where it goes" column, shared by the Icons and Actions pages ───────
function keyColumnCards() {
  const refs = artworkRefs();
  return state.bindings.map((id, index) => {
    const source = refs ? artworkPreviewSrc(refs[index], index) : null;
    const staged = artworkStage && artworkStage.pending ? artworkStage.pending.includes('key' + index) : false;
    const keys = id ? ((action(id) || {}).keys || []) : [];
    const combo = keys.length ? shortcut(keys) : '';
    return `<button type="button" class="icon-key ${index === selectedKey ? 'selected' : ''}" data-key="${index}" aria-pressed="${index === selectedKey}">`
      + `<span class="icon-key-face">${source ? `<img src="${esc(source)}" alt="" width="44" height="44">` : ''}</span>`
      + `<span class="icon-key-words"><b>Key ${index + 1}</b>`
      + `<small>${esc(title(id))}${combo ? ` · ${esc(combo)}` : ''}</small>`
      + (staged ? '<em>staged</em>' : '')
      + '</span></button>';
  }).join('');
}

function wireKeyColumn(container) {
  if (!container) return false;
  container.innerHTML = keyColumnCards();
  container.querySelectorAll('button').forEach(button => {
    button.onclick = () => {
      selectedKey = +button.dataset.key;
      render();
    };
  });
  return true;
}

// The picture that suits an action, when the icon library holds one: the general
// pack names the action, the macOS pack names the icon after it.
function actionIconFor(actionId) {
  const icon = libraryIcons().find(candidate => candidate.name === actionId || candidate.action === actionId);
  return icon ? icon.preview : null;
}

function actionTile(entry, running) {
  const reference = entry.value != null ? entry.value : entry.id;
  const onKey = reference === running;
  const elsewhere = onKey ? [] : bindingKeys(reference);
  const classes = ['icon-tile', 'action-tile', onKey ? 'on-key' : ''].filter(Boolean).join(' ');
  const icon = actionIconFor(entry.icon || entry.id);
  // Once this configuration defines the action, ITS shortcut is the truth: the
  // tile must not keep advertising the library's version after an edit.
  const mine = action(entry.id);
  const combo = mine
    ? (mine.kind === 'brightness' ? `${mine.brightness}%` : shortcut(mine.keys || []))
    : entry.shortcut;
  const edited = Boolean(mine) && !entry.brightness && Boolean(entry.keys && entry.keys.length)
    && shortcut(mine?.keys || []) !== entry.shortcut;
  // Only one marker, and only when it says something you would act on: the key
  // this action already sits on. "Added" was noise on half the library.
  const marker = onKey ? '<em>on this key</em>' : (elsewhere.length ? `<em>on key ${elsewhere.join(' &amp; ')}</em>` : '');
  return `<button type="button" class="${classes}" data-value="${esc(reference)}"`
    + ` title="${esc(entry.blurb || '')}${edited ? ' You have edited this action.' : ''}${elsewhere.length ? ` Already on Key ${elsewhere.join(' and Key ')}.` : ''}">`
    + (icon ? `<img src="${esc(icon)}" alt="" width="40" height="40">` : '<span class="action-glyph" aria-hidden="true">⌘</span>')
    + `<b>${esc(entry.label)}</b>`
    + (combo ? `<small class="action-shortcut">${esc(combo)}</small>` : '')
    + marker
    + '</button>';
}

// ── The chooser ─────────────────────────────────────────────────────────────
// One surface answers one question at every level: "what should this do?".
// A target is either a deck key or a content key inside a menu page, and the
// answer is either a navigation (a menu, or a page of this menu) or an action.
// Both routes are offered side by side, and a menu can be walked into so its own
// keys are assigned by the same page.

function targetIsSlot() {
  return assignKind === 'slot';
}

function currentTargetValue() {
  if (targetIsSlot()) {
    const menuPage = page();
    const item = menuPage ? menuPage.items.find(entry => entry.key === selectedSlot) : null;
    return item ? (item.submenu ? item.submenu : item.action) : null;
  }
  return state.bindings[selectedKey] || null;
}

function targetWords() {
  if (targetIsSlot()) return `content key ${selectedSlot + 1}`;
  return `key ${selectedKey + 1}`;
}

// What the person may navigate to from here: the deck's menus from a key, or the
// other pages of THIS menu from a content key. Cross-menu links are not
// expressible in the config, so they are not offered.
function routeSection() {
  if (targetIsSlot()) {
    const menuPage = page();
    const targets = menuPage ? pageTargets(menuPage) : [];
    const cards = targets.map(target => ({
      title: target.title,
      detail: 'Opens that page of this menu',
      value: target.id,
      descend: null,
    }));
    return {
      id: 'pages',
      heading: 'Go to a page',
      blurb: cards.length
        ? 'A page of this same menu, with its own six keys.'
        : 'This menu has only one page, so add another page in Menus first.',
      extra: cards.length ? '' : '<button type="button" class="quiet" data-view-jump="menus">Open Menus →</button>',
      cards,
    };
  }
  const cards = state.menus.map(menu => ({
    title: `Open: ${title(menu.name)}`,
    detail: menu.pages.length
      ? `Fills the six keys — ${menu.pages.length} page${menu.pages.length === 1 ? '' : 's'}`
      : 'No pages yet, so it would show nothing',
    value: `${MENU_OPEN_PREFIX}${menu.name}`,
    descend: menu.name,
  }));
  return {
    id: 'menus',
    heading: 'Open a menu',
    blurb: cards.length
      ? 'A menu takes over the six keys until you press the bottom-left knob to go back.'
      : 'This deck declares no menus yet. Build one, then come back and assign it.',
    extra: '<button type="button" class="quiet" data-view-jump="menus">Build a menu →</button>',
    cards,
  };
}

function routeCard(card, running) {
  const onTarget = card.value === running;
  // Already assigned: the honest primary action is to take it off again, not to
  // assign it once more.
  const primary = onTarget
    ? `<button type="button" class="quiet" data-assign="">Remove</button>`
    : `<button type="button" class="primary" data-assign="${esc(card.value)}">Assign</button>`;
  return `<article class="route-card ${onTarget ? 'on-target' : ''}">
    <div class="route-words"><b>${esc(card.title)}</b><small>${esc(card.detail)}</small></div>
    <div class="route-tools">
      ${primary}
      ${card.descend ? `<button type="button" class="quiet" data-descend="${esc(card.descend)}">Set up its keys →</button>` : ''}
    </div></article>`;
}

function renderRoutes(running) {
  const box = $('#assignRoutes');
  if (!box) return;
  const section = routeSection();
  const clearable = targetIsSlot()
    ? Boolean(page() && page().items.some(entry => entry.key === selectedSlot))
    : Boolean(running);
  box.innerHTML = `<section class="route-group">
      <h2>${esc(section.heading)} <small>${esc(section.blurb)}</small></h2>
      ${section.cards.length ? `<div class="route-cards">${section.cards.map(card => routeCard(card, running)).join('')}</div>` : ''}
      ${section.extra}
    </section>
    ${clearable ? `<p class="assign-clear"><button type="button" class="quiet" data-assign="">Clear this ${targetIsSlot() ? 'content key' : 'key'}</button>
      <span class="muted">${targetIsSlot() ? 'Leaves the content key blank.' : 'Then no action runs when it is pressed.'}</span></p>` : ''}`;
  box.querySelectorAll('[data-assign]').forEach(button => {
    button.onclick = event => { event.stopPropagation(); assignToTarget(button.dataset.assign); };
  });
  box.querySelectorAll('[data-descend]').forEach(button => {
    button.onclick = event => { event.stopPropagation(); openMenuInChooser(button.dataset.descend); };
  });
  box.querySelectorAll('[data-view-jump]').forEach(button => {
    button.onclick = () => showView(button.dataset.viewJump);
  });
}

// Walking into a menu: the same page, now answering for that menu's content keys.
function openMenuInChooser(name) {
  const index = state.menus.findIndex(menu => menu.name === name);
  if (index < 0) { setStatus('error', 'That menu no longer exists.'); return; }
  selectedMenu = index;
  const menu = state.menus[index];
  const rootIndex = menu.pages.findIndex(entry => entry.id === menu.root);
  selectedPage = rootIndex >= 0 ? rootIndex : 0;
  selectedSlot = 0;
  assignKind = 'slot';
  render();
}

function backToDeckKeys() {
  assignKind = 'key';
  render();
}

// Assigning writes to whichever target the page is answering for, and only ever
// into the draft: the deck is written by Save to deck and nothing else.
async function assignToTarget(value) {
  if (targetIsSlot()) {
    if (value === '') { await clearSlot(); return; }
    const menuPage = page();
    if (!menuPage) return;
    const targets = pageTargets(menuPage);
    if (targets.some(target => target.id === value)) { await commitSlot(value, true, targets); return; }
    await commitSlot(value, false, targets);
    return;
  }
  await assignActionToKey(value);
}

// How a content key reads in the chooser's column: what it is on the left, what
// it does on the right. A key holding an action must never also say "nothing".
function slotCardWords(item) {
  if (!item) return {label: 'Empty', detail: 'Nothing on this key yet'};
  const label = item.label || 'Empty';
  if (item.submenu) return {label, detail: 'Opens another page'};
  const summary = itemSummary(item);
  const keys = (action(item.action) || {}).keys || [];
  const combo = keys.length ? shortcut(keys) : '';
  return {label, detail: sameWords(label, summary) ? (combo || summary) : summary};
}

function renderSlotColumn(container) {
  const menuPage = page();
  if (!container) return;
  if (!menuPage) { container.innerHTML = '<p class="muted">This menu has no pages yet.</p>'; return; }
  const existing = Object.fromEntries(menuPage.items.map(item => [item.key, item]));
  container.innerHTML = [0, 1, 2, 3, 4, 5].map(key => {
    const words = slotCardWords(existing[key]);
    return `<button type="button" class="icon-key ${key === selectedSlot ? 'selected' : ''}" data-slot="${key}" aria-pressed="${key === selectedSlot}">`
      + `<span class="icon-key-face slot-face">${key + 1}</span>`
      + `<span class="icon-key-words"><b>${esc(words.label)}</b><small>${esc(words.detail)}</small></span>`
      + '</button>';
  }).join('');
  container.querySelectorAll('button[data-slot]').forEach(button => {
    button.onclick = () => { selectedSlot = +button.dataset.slot; render(); };
  });
}

function renderActionLibrary() {
  const keysBox = $('#actionKeyList'), slotsBox = $('#actionSlotList');
  if (!keysBox || !slotsBox) return;
  const onSlot = targetIsSlot();

  // Which column is showing, and what the page is answering for.
  keysBox.hidden = onSlot;
  slotsBox.hidden = !onSlot;
  const back = $('#assignBackToKeys');
  if (back) {
    back.hidden = !onSlot;
    back.onclick = backToDeckKeys;
  }
  const crumb = $('#assignCrumb');
  const whereNote = $('#assignWhereNote');
  if (onSlot) {
    const menu = state.menus[selectedMenu];
    const menuPage = page();
    if (crumb) {
      crumb.hidden = false;
      crumb.textContent = `${menu ? title(menu.name) : 'Menu'} › ${menuPage ? menuPage.title : 'page'} — assigning this page's keys`;
    }
    if (whereNote) whereNote.textContent = 'Pick a content key here, then decide what it does.';
    renderSlotColumn(slotsBox);
  } else {
    if (crumb) crumb.hidden = true;
    if (whereNote) whereNote.textContent = 'Pick a key, then decide what it does. Nothing reaches the deck until you press Save to deck.';
    wireKeyColumn(keysBox);
  }

  const running = currentTargetValue();
  const heading = $('#actionLibraryTitle');
  if (heading) {
    // The target's current value is already on its card in the left column, so
    // the heading stays a clean single question.
    heading.textContent = `What should ${targetWords()} do?`;
  }
  const eyebrow = $('#assignEyebrow');
  if (eyebrow) eyebrow.textContent = onSlot ? 'Inside this menu' : 'Assign';

  renderRoutes(running);

  const holder = $('#actionSections'), count = $('#actionCount'), note = $('#actionNote');
  if (!holder) return;
  if (!actionCatalogue) {
    if (count) count.textContent = 'Loading the library…';
    return;
  }

  const needle = actionFilter.trim().toLowerCase();
  let shown = 0, total = 0;
  const blocks = [];
  actionCatalogue.sections.forEach(section => {
    total += section.actions.length;
    const entries = section.actions.filter(entry => !needle
      || entry.label.toLowerCase().includes(needle)
      || entry.id.includes(needle)
      || entry.blurb.toLowerCase().includes(needle)
      || entry.shortcut.toLowerCase().includes(needle));
    shown += entries.length;
    if (!entries.length) return;
    blocks.push(`<section class="library-pack">
      <h3 title="${esc(section.blurb)}">${esc(section.name)} <small>${entries.length}${needle ? ` of ${section.actions.length}` : ''} · ${esc(section.blurb)}</small></h3>
      <div class="library-grid action-grid">${entries.map(entry => actionTile(entry, running)).join('')}</div>
    </section>`);
  });

  // The menus themselves are offered above, as a route, so they are not repeated
  // in the action library: one chooser, one place per kind of answer.

  holder.innerHTML = blocks.join('') || '<p class="muted">Nothing matches that filter. Clear it to see the whole library.</p>';
  holder.querySelectorAll('.action-tile').forEach(tile => {
    tile.onclick = () => assignToTarget(tile.dataset.value);
  });
  if (count) {
    count.textContent = needle
      ? `${shown} of ${total} actions match “${actionFilter.trim()}”`
      : `${total} actions ready to use — click one to put it on key ${selectedKey + 1}`;
  }
  if (note) {
    note.hidden = false;
    note.textContent = `Nothing reaches the deck until you press Save to deck — until then this is only staged for ${targetWords()}.`;
  }
  document.querySelectorAll('[data-mirror]').forEach(block => paintMirror(block, lastScreens));
}

// The deck's own settings.  The entry lives with the deck's own state and the panel
// opens above it, because what is inside belongs to the deck rather than to the page
// you happen to be looking at.  Nothing here is stored in the DOM as a second copy of
// the truth: the panel's own `hidden` is the state, and the entry says so for a reader.
function settingsPanel() {
  return $('#settingsPanel');
}

function openSettings() {
  const panel = settingsPanel();
  const entry = $('#settingsEntry');
  if (!panel || !entry) return;
  panel.hidden = false;
  entry.setAttribute('aria-expanded', 'true');
  // Focus lands on the first thing worth touching, so a keyboard never has to hunt
  // for the way in; closing puts it back on the entry that opened it.
  const select = $('#screenBlankAfter');
  if (select) select.focus();
}

function closeSettings() {
  const panel = settingsPanel();
  const entry = $('#settingsEntry');
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  if (entry) {
    entry.setAttribute('aria-expanded', 'false');
    entry.focus();
  }
}

// Backup and restore live with the deck's other settings. A backup carries the whole
// setup and the pictures something actually points at - a menu restored without them
// would come back with blank keys, which is the reason they travel at all.
function wireBackup() {
  const save = $('#backupSave');
  const restore = $('#backupRestore');
  const file = $('#backupFile');
  const note = $('#backupNote');
  if (!save || !restore || !file) return;
  const defaultNote = note ? note.textContent : '';
  const say = text => { if (note) note.textContent = text; };

  save.onclick = async () => {
    say('Packing up your setup…');
    try {
      const response = await fetch('/api/backup');
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        say(body.reason || 'The backup could not be made.');
        return;
      }
      const blob = await response.blob();
      const header = response.headers.get('Content-Disposition') || '';
      const named = header.match(/filename="([^"]+)"/);
      const name = named ? named[1] : 'akp03-backup.akp03';
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 2000);
      say(`Saved ${name} to your downloads.`);
    } catch (error) {
      say(`The backup could not be made: ${error.message}`);
    }
  };

  restore.onclick = () => file.click();
  file.onchange = async () => {
    const chosen = file.files && file.files[0];
    file.value = '';
    if (!chosen) return;
    say(`Reading ${chosen.name}…`);
    try {
      const response = await fetch('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/zip' },
        body: await chosen.arrayBuffer(),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok || !body.ok) {
        say(body.reason || 'That file could not be restored.');
        return;
      }
      // The draft is the whole app's, so take the restored one and draw it again rather
      // than reloading the page: the sentence saying what happened stays on screen.
      state = body.draft;
      dirty = true;
      renderSource();
      setStatus('saved', 'Restored into the draft — press Save to deck to keep it.');
      render();
      refreshArtworkStage();
      say(body.persistence || defaultNote);
    } catch (error) {
      say(`That file could not be restored: ${error.message}`);
    }
  };
}

function wireSettings() {
  const panel = settingsPanel();
  const entry = $('#settingsEntry');
  if (!panel || !entry) return;
  entry.onclick = () => (panel.hidden ? openSettings() : closeSettings());
  const close = $('#settingsClose');
  if (close) close.onclick = closeSettings;
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeSettings();
  });
  // A click anywhere else means it is done with: the panel is small, and leaving it
  // open over the page it was opened from would be clutter rather than convenience.
  document.addEventListener('pointerdown', event => {
    if (panel.hidden) return;
    if (panel.contains(event.target) || entry.contains(event.target)) return;
    closeSettings();
  });
}

// What the deck does when nobody is using it.  The choices are the ones a person
// actually wants, and a value that is not among them - a hand-written 120, say - is
// offered as it is rather than snapped to the nearest one, so the control never
// says the deck is set to something it is not.
const SCREEN_CHOICES = [
  [0, 'Off — the keys stay lit'],
  [60, '1 minute'],
  [300, '5 minutes'],
  [900, '15 minutes'],
  [1800, '30 minutes'],
  [3600, '1 hour'],
];

function renderScreenSleep() {
  const select = $('#screenBlankAfter');
  if (!select) return;
  const seconds = Number.isFinite(state.screen) ? state.screen : 900;
  // Every choice stays on the list, Off included: turning the countdown off has to
  // be as reachable as setting it, whatever the deck is set to now.
  const choices = SCREEN_CHOICES.slice();
  if (!choices.some(([value]) => value === seconds)) choices.push([seconds, `${seconds} seconds`]);
  choices.sort((a, b) => a[0] - b[0]);
  select.innerHTML = choices
    .map(([value, label]) => `<option value="${value}">${esc(label)}</option>`).join('');
  select.value = String(seconds);
  select.onchange = () => {
    state.screen = Number(select.value);
    // The deck is written by Save to deck and nothing else, so this is a draft edit
    // like any other: it shows as a change, and Discard puts it back.
    markDirty();
  };
}

function renderDeck() {
  // The deck is drawn once, by deckdraw.js, and mounted here as it is on the Menus page.
  // This page supplies only what the drawing cannot know: what each of the six faces
  // shows, which control is chosen, and what each control is called out loud.
  const refs = artworkRefs() || [];
  const capSaid = index => {
    const bound = (state.buttons || [])[index];
    return `${capName(index)}. ${bound ? title(bound) : 'Nothing assigned yet'}`;
  };
  const dialSaid = index => {
    const positions = dialSlotIndices(index);
    const used = positions.filter(position => (state.dials || [])[position]).length;
    return used
      ? `${DIAL_TITLES[DIAL_ORDER[index]]}. ${used} of ${positions.length} slots in use.`
      : `${DIAL_TITLES[DIAL_ORDER[index]]}. Nothing assigned yet.`;
  };
  mountDeckDrawing($('#deckDevice'), {
    aria: 'AKP03 landscape control deck',
    labels: false,
    capsLabel: 'The three buttons under the LCD keys. Choose one to assign what it does.',
    dialsLabel: 'The dials. Choose one to assign what its press and each way it turns does.',
    faces: state.bindings.map((id, index) => {
      const art = artworkPreviewSrc(refs[index], index);
      // One selection at a time: while a cap or a dial is chosen, no key is drawn as
      // selected.
      const chosen = selectedCap === null && selectedDial === null && index === selectedKey;
      return {
        art,
        label: title(id),
        selected: chosen,
        title: `${String(index + 1).padStart(2, '0')} ${title(id)}`,
      };
    }),
    caps: (state.buttons || []).map((bound, index) => ({
      selected: selectedCap === index,
      title: capSaid(index),
      aria: capSaid(index),
    })),
    dials: DIAL_ORDER.map((control, index) => ({
      selected: selectedDial === index,
      title: dialSaid(index),
      aria: dialSaid(index),
    })),
  });

  // The keys select exactly as a cap or a dial does, and the panel below then answers for
  // whichever one was chosen.
  $('#deckDevice').querySelectorAll('.key').forEach(button => {
    button.onclick = () => {
      selectedKey = +button.dataset.key;
      // Choosing a key is choosing away from a cap or a dial: the panel answers for one
      // control, so the other selections are let go here.
      selectedCap = null;
      selectedDial = null;
      // A full render: the panel below answers for the selected key, so switching keys
      // must refresh it in the same breath or it describes the old one.
      render();
    };
  });

  // The three caps under the LCD keys select exactly as a key does, and the panel below
  // then answers for the cap instead of the key.
  document.querySelectorAll('#deckDevice .button-cap').forEach(cap => {
    const index = Number(cap.dataset.cap);
    cap.onclick = () => {
      selectedCap = index;
      selectedDial = null;
      render();
    };
  });

  // The three dials select exactly as a cap does, and the panel then answers for that
  // dial's three slots - its press, and each way it turns - instead of one job.
  document.querySelectorAll('#deckDevice .dial').forEach(dial => {
    const index = Number(dial.dataset.dial);
    if (!Number.isInteger(index)) return;
    dial.onclick = () => {
      selectedDial = index;
      selectedCap = null;
      render();
    };
  });

  // The card above the panel is written by the panel itself now, from the same builder
  // a menu button's card comes from: the deck's key and a menu button are one component
  // in two mounts, so their headers cannot drift apart.
  // A re-render rebuilds the key buttons; keep any live press lit.
  repaintActiveControls();
}

function page() {
  return state.menus[selectedMenu]?.pages[selectedPage];
}

function pageTargets(selectedPageData) {
  return state.menus[selectedMenu].pages.filter(candidate => candidate.id !== selectedPageData.id);
}

function itemSummary(item) {
  if (!item) return 'Choose what this key does';
  return item.action ? title(item.action) : `↗ Opens: ${pageTitle(item.submenu)}`;
}

// Compare two labels ignoring case and punctuation, so "Copy" and "Copy",
// or "Play / Pause" and "Play Pause", count as the same words.
function sameWords(left, right) {
  const words = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  return words(left) === words(right);
}

function pageTitle(id) {
  return state.menus[selectedMenu]?.pages.find(candidate => candidate.id === id)?.title || 'another page';
}

function setSlot(menuPage, item) {
  // Assigning a job keeps whatever the key is drawn with: the icon is about the
  // face, not the job, so replacing the action must not silently strip it.
  const previous = menuPage.items.find(entry => entry.key === item.key);
  const merged = previous && previous.icon && item.icon === undefined
    ? {...item, icon: previous.icon}
    : item;
  menuPage.items = menuPage.items.filter(entry => entry.key !== merged.key).concat([merged]).sort((first, second) => first.key - second.key);
}

async function commitSlot(value, isSubmenu, targets) {
  const menuPage = page();
  if (!menuPage || value === '') return;
  if (isSubmenu) {
    const targetPage = targets.find(candidate => candidate.id === value);
    if (!targetPage) return;
    if (await commit(() => setSlot(menuPage, {key: selectedSlot, label: targetPage.title, submenu: targetPage.id}))) {
      setStatus('dirty', `Content key ${selectedSlot + 1} now opens “${targetPage.title}”.`);
    }
    return;
  }
  // The chooser hands over a plain action id, which may be one the draft does not
  // have yet: create it from the library on demand, in the same step. (The old
  // dropdown needed a "ready:" prefix for this; nothing sends one now.)
  const actionId = value.startsWith('ready:') ? value.slice('ready:'.length) : value;
  const spec = READY_MADE.find(candidate => candidate.id === actionId);
  if (!action(actionId)) {
    if (!spec) { setStatus('error', 'That action is not available.'); return; }
    const created = actionFromSpec(spec);
    const label = spec.label || title(actionId);
    if (await commit(() => { state.actions.push(created); setSlot(menuPage, {key: selectedSlot, label, action: actionId}); })) {
      setStatus('dirty', `Content key ${selectedSlot + 1} now runs “${label}”.`);
    }
    return;
  }
  const label = spec?.label || title(actionId);
  if (await commit(() => setSlot(menuPage, {key: selectedSlot, label, action: actionId}))) {
    setStatus('dirty', `Content key ${selectedSlot + 1} now runs “${label}”.`);
  }
}

async function clearSlot() {
  const menuPage = page();
  if (!menuPage) return;
  if (!menuPage.items.some(entry => entry.key === selectedSlot)) return;
  if (menuPage.items.length === 1) { setStatus('error', 'A page needs at least one content key, so the last one cannot be cleared.'); return; }
  if (await commit(() => { menuPage.items = menuPage.items.filter(entry => entry.key !== selectedSlot); })) {
    setStatus('dirty', `Cleared content key ${selectedSlot + 1}.`);
  }
}

// A menu-open action is an ordinary safe action of kind 'menu'; the deck opens
// the named custom menu when its key is pressed.  An existing one is reused so a
// menu never accumulates several opening actions.
async function assignMenuOpen(name) {
  const menu = state.menus.find(entry => entry.name === name);
  if (!menu) { setStatus('error', 'That menu no longer exists.'); return; }
  const existing = state.actions.find(candidate => candidate.kind === 'menu' && candidate.menu === name);
  const actionId = existing ? existing.id : uniqueId(slugify(`${name} menu`));
  if (await commit(() => {
    if (!existing) state.actions.push({id: actionId, kind: 'menu', menu: name});
    state.bindings[selectedKey] = actionId;
  })) {
    setStatus('dirty', `Key ${selectedKey + 1} now opens the ${title(name)} menu.`);
  }
}

// ── The Actions view: create, rename, re-edit and delete safe actions ───────
// Everything here edits the in-memory draft; the daemon is still the only thing
// that writes the file, and it still refuses anything that is not a safe
// shortcut, a brightness level, or a reviewed menu action.

function actionEditorHtml(candidate, mode) {
  const id = esc(candidate.id);
  if (mode === 'rename') {
    return `<form class="action-inline" data-inline="rename" data-id="${id}">
      <label for="rename-${id}">New name</label>
      <input id="rename-${id}" data-role="name" type="text" autocomplete="off" value="${esc(title(candidate.id))}">
      <div class="action-inline-actions"><button type="submit" class="primary">Save name</button><button type="button" class="quiet" data-cancel>Cancel</button></div>
      <p class="helper" role="alert" hidden></p>
    </form>`;
  }
  if (candidate.kind === 'keys') {
    const mods = MODIFIER_TOKENS.map(token => `<label><input type="checkbox" data-mod="${token}" ${candidate.keys.includes(token) ? 'checked' : ''}> ${esc(keyGlyph(token))} ${esc(MODIFIER_NAMES[token])}</label>`).join('');
    const plain = candidate.keys.filter(token => !MODIFIER_TOKENS.includes(token)).join(' ');
    return `<form class="action-inline" data-inline="edit" data-id="${id}">
      <fieldset class="key-mods"><legend>Hold down</legend>${mods}</fieldset>
      <label for="key-${id}">Then press one key</label>
      <input id="key-${id}" data-role="key" type="text" autocomplete="off" value="${esc(plain)}">
      <div class="action-inline-actions"><button type="submit" class="primary">Save shortcut</button><button type="button" class="quiet" data-cancel>Cancel</button></div>
      <p class="helper" role="alert" hidden></p>
    </form>`;
  }
  if (candidate.kind === 'brightness') {
    return `<form class="action-inline" data-inline="edit" data-id="${id}">
      <label for="level-${id}">Brightness percent (1–100)</label>
      <input id="level-${id}" data-role="brightness" type="number" min="1" max="100" value="${candidate.brightness}">
      <div class="action-inline-actions"><button type="submit" class="primary">Save brightness</button><button type="button" class="quiet" data-cancel>Cancel</button></div>
      <p class="helper" role="alert" hidden></p>
    </form>`;
  }
  return '<p class="helper">This action points at a menu. Open Menus to change what it does.</p>';
}

function renderActions() {
  const list = $('#actionList');
  if (!list) return;
  const ownCount = $('#actionOwnCount');
  if (ownCount) {
    ownCount.textContent = state.actions.length
      ? `${state.actions.length} defined in this configuration`
      : 'none yet — choose one above and it is created for you';
  }
  list.innerHTML = state.actions.map(candidate => {
    const editing = actionEdit && actionEdit.id === candidate.id ? actionEdit.mode : null;
    return `<article class="action-row">
      <div class="action-row-main"><b>${esc(title(candidate.id))}</b><small>${esc(description(candidate))}</small></div>
      <div class="action-tools">
        <button type="button" data-action-tool="rename" data-id="${esc(candidate.id)}">Rename</button>
        <button type="button" data-action-tool="edit" data-id="${esc(candidate.id)}">Edit</button>
        <button type="button" data-action-tool="delete" data-id="${esc(candidate.id)}">Delete</button>
      </div>
      ${editing ? actionEditorHtml(candidate, editing) : ''}
    </article>`;
  }).join('') || '<p class="muted">No actions yet. Pick a ready-made action from a key, or add one below.</p>';
  list.querySelectorAll('[data-action-tool]').forEach(button => {
    button.onclick = () => actionTool(button.dataset.actionTool, button.dataset.id);
  });
  list.querySelectorAll('form[data-inline]').forEach(form => {
    form.onsubmit = event => { event.preventDefault(); submitActionEdit(form); };
    form.querySelectorAll('[data-cancel]').forEach(button => {
      button.onclick = () => { actionEdit = null; renderActions(); };
    });
  });
}

function actionTool(mode, id) {
  if (mode === 'delete') { deleteAction(id); return; }
  actionEdit = {id, mode};
  renderActions();
}

async function deleteAction(id) {
  const used = bindingKeys(id);
  if (used.length) {
    setStatus('error', `“${title(id)}” is still used by Key ${used.join(' and Key ')}. Point those keys at another action first, then delete it.`);
    actionEdit = null;
    renderActions();
    return;
  }
  if (await commit(() => { state.actions = state.actions.filter(candidate => candidate.id !== id); })) {
    actionEdit = null;
    setStatus('dirty', `Deleted “${title(id)}”.`);
  }
}

async function submitActionEdit(form) {
  const id = form.dataset.id;
  const candidate = action(id);
  if (!candidate) return;
  const error = form.querySelector('[role="alert"]');
  const fail = message => { error.textContent = message; error.hidden = false; };
  if (form.dataset.inline === 'rename') {
    const name = form.querySelector('[data-role="name"]').value;
    if (!name.trim()) return fail('Give the action a short name.');
    const newId = uniqueId(slugify(name), id);
    if (newId === id) { actionEdit = null; renderActions(); return; }
    if (await commit(() => {
      state.actions = state.actions.map(item => (item.id === id ? {...item, id: newId} : item));
      state.bindings = state.bindings.map(binding => (binding === id ? newId : binding));
    })) {
      actionEdit = null;
      setStatus('dirty', `Renamed “${title(id)}” to “${title(newId)}”.`);
    } else {
      fail('That name was not accepted. Try a different one.');
    }
    return;
  }
  if (candidate.kind === 'keys') {
    const chosen = Array.from(form.querySelectorAll('input[data-mod]')).filter(box => box.checked).map(box => box.dataset.mod);
    const key = canonicalKey(form.querySelector('[data-role="key"]').value);
    if (!isKnownKey(key)) return fail('Type one key, like z, enter, or f5.');
    const keys = [...MODIFIER_TOKENS.filter(token => chosen.includes(token)), key];
    if (await commit(() => {
      state.actions = state.actions.map(item => (item.id === id ? {id, kind: 'keys', keys} : item));
    })) {
      actionEdit = null;
      setStatus('dirty', `Updated “${title(id)}”.`);
    } else {
      fail('The deck did not accept that shortcut.');
    }
    return;
  }
  if (candidate.kind === 'brightness') {
    const level = Number(form.querySelector('[data-role="brightness"]').value);
    if (!Number.isInteger(level) || level < 1 || level > 100) return fail('Choose a brightness from 1 to 100.');
    if (await commit(() => {
      state.actions = state.actions.map(item => (item.id === id ? {id, kind: 'brightness', brightness: level} : item));
    })) {
      actionEdit = null;
      setStatus('dirty', `Updated “${title(id)}”.`);
    } else {
      fail('The deck did not accept that brightness.');
    }
  }
}

function wireActionCreate() {
  const form = $('#actionCreateForm');
  if (!form) return;
  const type = $('#actionCreateType');
  const keysBox = $('#actionCreateKeys');
  const brightnessBox = $('#actionCreateBrightness');
  const sync = () => {
    const isKeys = type.value === 'keys';
    keysBox.hidden = !isKeys;
    brightnessBox.hidden = isKeys;
  };
  type.onchange = sync;
  sync();
  form.onsubmit = async event => {
    event.preventDefault();
    const error = $('#actionCreateError');
    const fail = message => { error.textContent = message; error.hidden = false; };
    const name = $('#actionCreateName').value;
    if (!name.trim()) return fail('Give the action a short name.');
    const id = uniqueId(slugify(name));
    let candidate;
    if (type.value === 'brightness') {
      const level = Number($('#actionCreateBrightnessLevel').value);
      if (!Number.isInteger(level) || level < 1 || level > 100) return fail('Choose a brightness from 1 to 100.');
      candidate = {id, kind: 'brightness', brightness: level};
    } else {
      const chosen = Array.from(keysBox.querySelectorAll('input[data-mod]')).filter(box => box.checked).map(box => box.dataset.mod);
      const key = canonicalKey($('#actionCreateKey').value);
      if (!isKnownKey(key)) return fail('Type one key, like z, enter, or f5.');
      candidate = {id, kind: 'keys', keys: [...MODIFIER_TOKENS.filter(token => chosen.includes(token)), key]};
    }
    error.hidden = true;
    if (await commit(() => { state.actions = state.actions.concat(candidate); })) {
      $('#actionCreateName').value = '';
      $('#actionCreateKey').value = '';
      keysBox.querySelectorAll('input[data-mod]').forEach(box => { box.checked = false; });
      setStatus('dirty', `Added “${title(id)}”.`);
    } else {
      fail('The deck did not accept that action.');
    }
  };
}

// ── Artwork: choosing an icon is staging, never a push ──────────────────────
// The library lives in the editor (GET /api/icons) and every assignment is a
// *reference* held in the draft.  The device only changes when the draft is
// pushed with Save to deck, so this panel can offer the whole library without
// touching a single key.

let artworkStage = {};

// One place decides which view is showing, so a button inside a view (like
// "Choose an icon…") can move the person without duplicating the nav logic.
function showView(name) {
  // Settings belongs to the deck, not to the page being left behind: going somewhere
  // else puts it away rather than leaving it floating over the new page.
  closeSettings();
  document.querySelectorAll('.nav').forEach(nav => nav.classList.toggle('active', nav.dataset.view === name));
  document.querySelectorAll('.view').forEach(view => view.classList.toggle('active', view.id === name));
  const crumb = $('#crumb');
  if (crumb) crumb.textContent = title(name);
  showHelpFor(name);
}

// The guide answers for the page you are on.  The question mark used to open the icon
// part from every page, which answers a question nobody asked while they are looking at
// the deck.  One document, one section per page, and the link says where it goes.
const HELP_SECTIONS = {
  deck: {anchor: 'deck', says: 'the deck — what each control does, and what saving means'},
  menus: {anchor: 'menus', says: 'menus — what a menu can take over, and how to get back out'},
  icons: {anchor: 'icons', says: 'icons — how to draw one, and how to build a set'},
};

function showHelpFor(name) {
  const link = $('#helpLink');
  const section = HELP_SECTIONS[name] || HELP_SECTIONS.deck;
  if (!link) return;
  link.href = `/assets/guide.html#${section.anchor}`;
  link.title = `Open the guide: ${section.says}`;
  link.setAttribute('aria-label', `Open the guide: ${section.says}`);
}

function artworkRefs() {
  return Array.isArray(state && state.icons) ? state.icons : null;
}

async function refreshArtworkStage() {
  try {
    const response = await fetch('/api/draft/diff');
    artworkStage = response.ok ? await response.json() : {};
  } catch (error) {
    artworkStage = {};
  }
}
// An icon says which action it was drawn for, and the key runs whatever it is
// bound to.  Choosing the scissors does not make a key cut, so when the two
// disagree this says so outright instead of letting the key wear a lie.
async function assignArtwork(reference, quiet = false) {
  const refs = artworkRefs();
  if (!refs) return;
  refs[selectedKey] = reference;
  state.icons = refs;
  markDirty('staged');
  try {
    await validateDraft();
  } catch (error) {
    showArtworkError(error.message || 'The deck would not accept that icon.');
    return;
  }
  await refreshArtworkStage();
  // Quiet when the icon came in with an action: the action's own message is the one
  // worth reading, and staging news would talk over it.
  if (quiet) return;
  setStatus('dirty', reference
    ? `${artworkLabel(reference)} is staged for key ${selectedKey + 1}.`
    : `Key ${selectedKey + 1} is staged with no icon.`);
}

// The library is shown as a page, not a dropdown: a gallery of every pack with
// the six keys beside it, so the whole set is visible at once and choosing is
// one click rather than a scroll inside a card.
function render() {
  renderDeck();
  renderScreenSleep();
  // The Menus workshop and the deck's nesting chain are the two views of one
  // configuration: menus are designed on the first, and the same menus are shown
  // running - key by key, arrow by arrow - on the second.
  renderMenusFlow();
  renderDeckChain();
  renderActions();
  // The key panel answers for the selected key in one place: what it does, its
  // shortcut, what it looks like.  The old page-per-picker renderers are retired
  // with the views they drew.
  // The panel below the deck: the same component the Menus page mounts, told to
  // answer for the selected key.  One component, so the two pages cannot drift.
  renderDeckButtonPanel();
}

// Keys that only send a shortcut do nothing at all while macOS withholds the
// Accessibility grant, and no LCD face can show that. Say it in words, with the
// one action that fixes it, rather than letting someone decide the key is broken.
function renderPermission() {
  const banner = $('#permissionBanner');
  if (!banner) return;
  const info = source ? source.injection : null;
  if (!info || info.permitted !== false) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  banner.classList.remove('is-ok');
  const headline = $('#permissionHeadline'), detail = $('#permissionDetail');
  if (headline) headline.textContent = 'Shortcut keys cannot be sent yet';
  if (detail) {
    detail.textContent = 'macOS has not granted Accessibility to the key-injection helper, so every key that '
      + 'sends a shortcut — ⌘N, Undo, Mission Control — does nothing when you press it. Grant it once and '
      + 'they start working; no restart needed.';
  }
  const button = $('#permissionRequest');
  if (!button) return;
  button.hidden = info.can_request === false;
  button.onclick = async () => {
    button.disabled = true;
    button.textContent = 'Asking macOS…';
    try {
      const response = await fetch('/api/permission/request', { method: 'POST' });
      const result = await response.json();
      const fresh = await fetch('/api/state');
      source = await fresh.json();
      if (result.granted) {
        banner.classList.add('is-ok');
        if (headline) headline.textContent = 'Permission granted — shortcut keys now work';
        if (detail) detail.textContent = 'macOS granted Accessibility to the helper. Press the key again.';
        button.hidden = true;
        setTimeout(() => { banner.hidden = true; }, 8000);
      } else {
        if (detail) {
          detail.textContent = 'macOS has not confirmed the grant yet. Switch on “AKP03KeyInject” in System '
            + 'Settings › Privacy & Security › Accessibility, then reload this page.';
        }
      }
    } catch (error) {
      setStatus('error', 'Could not ask macOS for permission: ' + error.message);
    } finally {
      button.disabled = false;
      button.textContent = 'Ask macOS for permission';
    }
  };
}

async function start() {
  const response = await fetch('/api/state');
  source = await response.json();
  state = source.draft;
  dirty = false;
  render();
  renderSource();
  renderPermission();
  // At rest the status line says nothing: the pill beside the title already
  // states where this configuration stands, and quiet is what "fine" looks like.
  setStatus(source.can_save ? '' : 'warn',
    source.can_save ? '' : 'Sample draft — this one cannot be saved.');
  const saveButton = $('#saveChanges');
  if (saveButton) saveButton.onclick = saveToDeck;
  const discardButton = $('#discardChanges');
  if (discardButton) discardButton.onclick = discardChanges;
  const actionFilterBox = $('#actionFilter');
  if (actionFilterBox) actionFilterBox.oninput = () => { actionFilter = actionFilterBox.value; renderActionLibrary(); };
  wireActionCreate();
  // The library is needed to know what each icon is drawn for, not only to
  // browse it, so it is fetched with the page; names only resolve once it lands.
  loadLibrary().then(() => render()).catch(() => { /* the library view says so */ });
  loadActionCatalogue().then(() => render()).catch(() => { /* the library view says so */ });
  refreshArtworkStage();
  document.querySelectorAll('.nav').forEach(button => {
    button.onclick = () => showView(button.dataset.view);
  });
  wireSettings();
  wireBackup();
  // The guide link answers for the page the app opens on, not only for the ones a
  // person clicks through afterwards.
  const showing = document.querySelector('.view.active');
  showHelpFor(showing ? showing.id : 'deck');
  connectFeed();
}

start().catch(error => {
  setStatus('error', 'Could not load local draft: ' + error.message);
});
