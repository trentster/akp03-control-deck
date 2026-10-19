// ── The button panel: one component, wherever a button is edited ─────────────
// A deck key, a menu key and a menu button are edited by this one panel, in two
// states: at rest it says what the button has - the kind of thing assigned, then
// its label, its shortcut and its picture - and pressing Edit opens the editors
// that change those three.  Click a row, pick, done: nothing here navigates
// anywhere, and every write lands in the draft until Save to deck pushes it.
//
// It is one piece of code because it is one thing to use, and because two copies
// of it drifted every time one was changed.  A *target* supplies the only three
// things that genuinely differ: what the panel answers for, what it reads, and
// where a change lands.  The card, the rows, the two states, the choosers and the
// tools strip are made here, once, so no page can render them differently.
//
// A target is:
//   token          what is being edited, in words that identify it ('key:2',
//                  'slot:0:0:1', 'menu:Youtube:3').  A different token is a
//                  different button and starts the panel over at rest.
//   card()         the identity card's content, from controlCardFor()
//   view()         the reading, from panelView(job)
//   thumbKey()     which key's own picture to preview a file-backed icon with
//   contextHTML?() the line above the rows, when the panel must say which corner
//                  of the deck it is answering for
//   goBack?()      where that line's own button goes
//   picksHTML()    the chooser's list, shown under the row that opened it
//   wirePicks?()   wiring for that list, when the page binds its own rows
//   setAction/setIcon/setShortcut   the three writes there are

// What the panel says at rest, and what it offers to change.  A value here is only
// ever a *name*, so it is named as one: nothing claims to describe behaviour, and
// a shortcut is shown only when the assignment really does send keys.
function panelView(job) {
  const MENU_PREFIX = '__menu__:';
  const spec = job.actionId ? (action(job.actionId) || readyMadeAction(job.actionId)) : null;
  // A menu reaches this panel two ways: a menu key of a menu (its own value carries
  // the prefix) and a deck key bound to an action that opens one (`kind: 'menu'`,
  // which names the menu it opens).  Both are menus, and both send no keys.
  const prefixed = Boolean(job.value && job.value.startsWith(MENU_PREFIX));
  const isMenu = spec ? spec.kind === 'menu' : prefixed;
  const menuName = isMenu
    ? ((spec && spec.menu) || (prefixed ? job.value.slice(MENU_PREFIX.length) : null))
    : null;
  const assigned = Boolean(job.value);
  const keys = spec && spec.kind === 'keys' ? spec.keys : null;
  const icon = job.icon || null;
  // A menu's own title is the name that matters on a *deck key*: the key is named for
  // the menu it opens.  A menu key and a menu button are named by their own label -
  // they were given words to say, and those words are what belongs on them.
  const assignedName = isMenu && menuName && job.namedByMenu
    ? title(menuName)
    : (job.current || job.label);
  return {
    // The raw selection the button carries - an action's id, or `__menu__:<name>` for
    // a menu - so the chooser can mark the row this button is already on.  Without it
    // the list opened with nothing lit and the "in use" tag never appeared, on either
    // page, because the comparison it feeds had nothing to compare against.
    value: job.value || null,
    isMenu,
    menuName,
    // Whether anything is assigned at all: what the chooser row's words and its
    // "assign" look hang on.
    assigned,
    // The keys an assignment really sends, so the shortcut card and the editor are
    // filled from this one reading rather than from a second lookup of the action.
    keys: keys || [],
    // Whether this control has a screen at all.  A cap under the LCD keys has
    // none, so nothing offers it a label or a picture.
    screen: job.screen !== false,
    // Held while pressed, or fires once.  Only a keys assignment can be held, so
    // the switch appears exactly when there is something to hold.
    held: job.held === true,
    canHold: Boolean(keys && keys.length),
    // What kind of control this is, for the sentences that must say "key" or
    // "button".  This and the target's own words are the only legitimate differences
    // between the panels.
    noun: job.noun || 'key',
    // The line under the assigned name: a shortcut, a brightness, nothing.
    meta: job.meta || '',
    // The heading names the kind of thing that is assigned, which is what makes
    // the word honest: an action is an action, a menu is a menu, an empty key is
    // not called either.
    heading: isMenu ? 'Menu' : (assigned ? 'Action' : 'Not assigned'),
    label: assigned ? assignedName : 'Nothing yet',
    shortcut: keys ? shortcut(keys) : '',
    shortcutNote: keys ? '' : (isMenu
      ? 'Opening a menu sends no keys'
      : (assigned ? 'This one sends no keys' : 'Nothing to send yet')),
    icon,
    // In the editor, "The icon already on this key" explains where a picture came
    // from; as a *value* in the summary it reads as half a sentence, so here it
    // says what it is.
    iconWords: icon
      ? (icon.startsWith(PACK_PREFIX) ? artworkLabel(icon) : 'This key’s own icon')
      : 'Text only',
    // A menu is not something you can send once, and there is nothing of its own
    // to rename or delete from here: its buttons are designed on the Menus page.
    actionId: isMenu ? null : (job.actionId || null),
    // Whether a picture can be put on this button at all.  On a deck key the artwork
    // lives on the key, so it can be drawn whatever the key does; a menu button's
    // picture lives on the button's own assignment, so with nothing assigned there is
    // nowhere to put one.  The panel says so rather than offering a control that
    // cannot do anything - the words the Menus page used to carry by hand.
    iconUsable: job.iconUsable !== false,
    mine: !isMenu && job.actionId
      ? (state.actions || []).some(entry => entry.id === job.actionId)
      : false,
  };
}

// Reading or changing.  A panel opens at rest and only becomes the editors when
// its own Edit is pressed, so the default view is what the button has and nothing
// else.  Each mount keeps its own state: the deck's panel, the chain's panel and
// the Menus page's panel are three mounts of one component, and opening the
// editors in one must never open them in another.
const panelMounts = new Map();

function panelState(root) {
  if (!root) return null;
  let state = panelMounts.get(root);
  if (!state) {
    state = {target: null, token: null, editing: false, dialLeft: false, open: null, openSlot: null, wired: false,
             snapshot: null};
    panelMounts.set(root, state);
  }
  return state;
}

// The panel a control belongs to, found from inside it.  Everything the panel does
// is scoped this way and never by id: an id lookup finds whichever mount came first
// in the document, and two panels are on screen whenever the Menus page is open.
function panelRootOf(node) {
  return node && node.closest ? node.closest('[data-panel-root]') : null;
}

function panelIn(root, what) {
  return root ? root.querySelector(`[data-panel="${what}"]`) : null;
}

// The markup, written once and never rebuilt.  Editing another button changes the
// words in it, not the DOM, so an open editor keeps its field, its focus and
// anything half-typed into it.
function panelSkeletonHTML() {
  return `
    <section class="selected-control tone-teal" data-panel="card" aria-live="polite" aria-atomic="true"></section>
    <p class="panel-context" data-panel="context" hidden></p>
    <div class="panel-row" data-panel-row="summary">
      <dl class="summary-list">
        <dt>Label</dt><dd><b data-panel="summary-label">Nothing yet</b></dd>
        <dt>Shortcut</dt><dd class="summary-note" data-panel="summary-shortcut"></dd>
        <dt data-panel="summary-icon-label">Icon</dt><dd data-panel="summary-icon-cell"><span class="row-thumb" data-panel="summary-thumb" hidden aria-hidden="true"></span><span data-panel="summary-image">Text only</span></dd>
        <dt data-panel="summary-mode-label" hidden>Response</dt><dd data-panel="summary-mode" hidden></dd>
      </dl>
      <div class="action-tools">
        <button type="button" class="quiet" data-panel-edit>Edit…</button>
        <button type="button" class="quiet" data-panel="test" hidden>Test it now</button>
      </div>
    </div>
    <div class="panel-editors" data-panel="editors" hidden>
      <div class="panel-row" data-panel-row="action">
        <div class="row-head"><p class="row-label" data-panel="action-heading">Action</p></div>
        <button class="row-value is-assign" type="button" data-panel-open="action" aria-expanded="false">
          <b data-panel="action-value">＋ Assign…</b>
          <small class="row-meta" data-panel="action-meta"></small>
        </button>
        <button class="quiet row-reset" type="button" data-tool="reset-action" hidden>Reset to the ready-made one</button>
        <div class="row-choices" data-panel="action-choices" hidden></div>
      </div>
      <div class="panel-row" data-panel-row="dial" hidden>
        <p class="row-label" data-panel="dial-heading">What this dial does</p>
        <div class="dial-slots" data-panel="dial-rows"></div>
        <div class="row-choices" data-panel="dial-choices" hidden></div>
      </div>
      <div class="panel-row" data-panel-row="shortcut" hidden>
        <p class="row-label">Shortcut</p>
        <button class="row-value" type="button" data-panel-open="shortcut" aria-expanded="false">
          <b data-panel="shortcut-value">—</b>
        </button>
        <div class="shortcut-editor" data-panel="shortcut-editor" hidden></div>
      </div>
      <div class="panel-row" data-panel-row="icon">
        <p class="row-label">Icon</p>
        <button class="row-value" type="button" data-panel-open="icon" aria-expanded="false">
          <span class="row-thumb" data-panel="icon-thumb" aria-hidden="true"></span>
          <b data-panel="icon-value">Text only</b>
        </button>
        <div class="row-choices icon-choices" data-panel="icon-choices" hidden></div>
      </div>
      <div class="panel-row" data-panel-row="mode" hidden>
        <label class="row-toggle">
          <input type="checkbox" data-panel="mode">
          <span data-panel="mode-words">Held while pressed</span>
        </label>
      </div>
      <div class="action-tools" data-panel="tools"></div>
    </div>`;
}

// Show the panel for one target.  Called on every render of the page it belongs
// to, so it must be cheap and idempotent: the markup is built on the first call,
// the words are painted on every one.
function mountButtonPanel(root, target) {
  const state = panelState(root);
  if (!root || !state || !target) return;
  state.target = target;
  if (!root.dataset.panelRoot) {
    root.dataset.panelRoot = '';
    root.innerHTML = panelSkeletonHTML();
  }
  root.hidden = false;
  // A different button is a fresh panel; the same button keeps whatever the person
  // was in the middle of.  Identity is the token, so a re-render for any other
  // reason - a status line, a keystroke capture - cannot close an open editor.
  if (state.token !== target.token) {
    state.token = target.token;
    state.open = null;
    state.editing = false;
    state.dialLeft = false;
    closePanelChoosers(root);
  }
  wireButtonPanel(root);
  paintButtonPanel(root);
}

function unmountButtonPanel(root) {
  if (!root) return;
  root.hidden = true;
  const state = panelState(root);
  if (state) { state.token = null; state.open = null; state.editing = false; }
}

// Closing the choosers is a state change, not a CSS trick: what is open is what the
// panel's next paint draws, so a pick and a re-render can never disagree about it.
function closePanelChoosers(root) {
  const state = panelState(root);
  if (!state) return;
  state.open = null;
  ['action-choices', 'icon-choices', 'shortcut-editor'].forEach(what => {
    const box = panelIn(root, what);
    if (box) { box.hidden = true; box.dataset.filled = ''; }
  });
  root.querySelectorAll('[data-panel-open]').forEach(button => button.setAttribute('aria-expanded', 'false'));
  const rename = panelIn(root, 'tools') && panelIn(root, 'tools').querySelector('[data-tool="rename-form"]');
  if (rename) rename.hidden = true;
}

function wireButtonPanel(root) {
  const state = panelState(root);
  if (!root || !state || state.wired) return;
  state.wired = true;
  root.addEventListener('click', event => {
    const hit = selector => event.target.closest(selector);
    const inside = element => element && root.contains(element);
    const edit = hit('[data-panel-edit]');
    if (inside(edit)) {
      state.editing = !state.editing;
      // Entering takes the snapshot Cancel returns to; leaving forgets it, so the next
      // sitting starts from wherever the draft has got to.
      state.snapshot = state.editing ? snapshotDraft() : null;
      // Edit… is the way a dial comes back for a second sitting, so it clears the flag
      // that kept it out of this view.
      state.dialLeft = false;
      if (!state.editing) closePanelChoosers(root);
      paintButtonPanel(root);
      return;
    }
    const cancel = hit('[data-tool="cancel"]');
    if (inside(cancel)) {
      putDraftBack(state.snapshot);
      state.snapshot = null;
      state.editing = false;
      state.dialLeft = true;
      closePanelChoosers(root);
      render();
      return;
    }
    const resetAction = hit('[data-tool="reset-action"]');
    if (inside(resetAction)) {
      const view = state.target && typeof state.target.view === 'function' ? state.target.view() : null;
      if (view && view.actionId && putReadyVersionBack(view.actionId)) {
        render();
        paintButtonPanel(root);
      }
      return;
    }
    const done = hit('[data-tool="done"]');
    if (inside(done)) {
      state.editing = false;
      // A dial must not be put straight back into the editor it just left: Done would
      // then be decoration.  Edit… is one click away in the summary.
      state.dialLeft = true;
      closePanelChoosers(root);
      paintButtonPanel(root);
      return;
    }
    const opener = hit('[data-panel-open]');
    if (inside(opener)) {
      const wanted = opener.dataset.panelOpen;
      // A dial has three rows sharing one chooser, so the row that opened it is part
      // of what is open: pressing the same row again closes the list.
      const slot = opener.dataset.panelSlot || null;
      const same = state.open === wanted && state.openSlot === slot;
      state.open = same ? null : wanted;
      state.openSlot = same ? null : slot;
      paintButtonPanel(root);
      // The list a row opens is loaded when it is opened, not before: a fetch that
      // never comes back must not leave a control that looks dead, so the box is
      // shown first and filled when the answer arrives.
      if (state.open === 'action') openActionChoices(root);
      if (state.open === 'icon') openIconChoices(root);
      return;
    }
    const back = hit('[data-panel-back]');
    if (inside(back) && state.target.goBack) { state.target.goBack(); return; }
    if (state.target.pick && state.target.pick(event, root)) paintButtonPanel(root);
  });
}

// The picture the key wears, at the size the panel shows it: the same source the
// deck itself paints, so the panel cannot claim a picture the key does not have.
// A file-backed icon is previewed from the key that carries it, which is why the
// key index comes from the target rather than from whatever is selected.
function panelThumbHTML(icon, key) {
  return icon ? `<img src="${esc(artworkPreviewSrc(icon, key))}" alt="" width="28" height="28">` : '';
}

// Everything the panel says, painted onto the skeleton.  Every word comes from the
// target's own reading, so a deck key and a menu button are described by one set of
// sentences instead of two that happen to agree today.
function paintButtonPanel(root) {
  const state = panelState(root);
  const target = state && state.target;
  if (!root || !target) return;
  const view = target.view();
  // A dial has no single job to summarise: its three slots are the answer, so its
  // rows are always showing rather than waiting behind an Edit… button.  It arrives
  // already in this view, so it never presses Edit… itself - which means it takes the
  // snapshot a button takes on entering, here, or Cancel would have nowhere to return
  // to.  And a dial that has been left stays left: a Done that dropped you straight
  // back into the editor it just closed would be no way out at all.
  if (typeof target.slots === 'function' && !state.dialLeft) {
    if (!state.editing) state.snapshot = snapshotDraft();
    state.editing = true;
  }

  const card = panelIn(root, 'card');
  if (card) {
    const data = target.card();
    card.className = `selected-control ${data.tone || 'tone-teal'}`;
    card.dataset.controlShape = data.shape || 'lcd';
    card.innerHTML = controlCardInnerHTML(data);
    if (data.aria) card.setAttribute('aria-label', data.aria);
    else card.removeAttribute('aria-label');
  }

  const context = panelIn(root, 'context');
  if (context) {
    const html = target.contextHTML ? target.contextHTML() : '';
    context.innerHTML = html || '';
    context.hidden = !html;
  }

  const summary = root.querySelector('[data-panel-row="summary"]');
  const editors = panelIn(root, 'editors');
  if (summary) summary.hidden = state.editing;
  if (editors) editors.hidden = !state.editing;
  paintSummaryRow(root, view, target);
  if (state.editing) paintEditorRows(root, view, target, state);
}

function paintSummaryRow(root, view, target) {
  const set = (what, words) => { const node = panelIn(root, what); if (node) node.textContent = words; };
  set('summary-label', view.label);
  const shortcutCell = panelIn(root, 'summary-shortcut');
  if (shortcutCell) {
    shortcutCell.textContent = view.shortcut || view.shortcutNote;
    shortcutCell.classList.toggle('summary-note', !view.shortcut);
  }
  // A picture needs no words beside it - the Label row above already names the action.
  set('summary-image', view.icon ? '' : (view.screen ? view.iconWords : 'No screen on this button'));
  // A button under the keys has no screen, so it has no picture - and a line that only
  // exists to say a thing cannot be set is a line the reader has to read and then throw
  // away.  The label and the shortcut are the whole truth about that control.
  const iconLabel = root.querySelector('[data-panel="summary-icon-label"]');
  const iconCell = root.querySelector('[data-panel="summary-icon-cell"]');
  if (iconLabel) iconLabel.hidden = view.screen === false;
  if (iconCell) iconCell.hidden = view.screen === false;
  // The response is worth a line only when it is the unusual one: a tap is what
  // every other button on the deck already does.
  const modeLabel = panelIn(root, 'summary-mode-label');
  const modeCell = panelIn(root, 'summary-mode');
  const heldWords = view.held ? 'Held while pressed' : '';
  if (modeCell) { modeCell.textContent = heldWords; modeCell.hidden = !heldWords; }
  if (modeLabel) modeLabel.hidden = !heldWords;
  const thumb = panelIn(root, 'summary-thumb');
  if (thumb) {
    thumb.innerHTML = panelThumbHTML(view.icon, target.thumbKey());
    thumb.hidden = !view.icon;
  }
  // Test only exists for an action: a menu is not something you can send once.
  const test = panelIn(root, 'test');
  if (test) {
    test.hidden = !view.actionId;
    test.title = 'Presses this shortcut on your Mac, once.';
    test.onclick = () => tryActionNow(view.actionId);
  }
}

// One chooser serves both shapes of panel: the single action row of a control, and
// the box under a dial's three rows.  Which of the two is showing is the target's
// business, not the chooser's.
function actionChoiceBox(root) {
  const state = panelState(root);
  const dial = Boolean(state && state.target && typeof state.target.slots === 'function');
  return panelIn(root, dial ? 'dial-choices' : 'action-choices');
}

// A dial is not one control: it presses, and it turns two ways.  Each way is a slot
// of its own, so the panel offers one row per slot rather than a single job - and no
// response switch, because a turn is over the moment it happens.
function paintDialRows(root, target, state) {
  const rows = panelIn(root, 'dial-rows');
  if (!rows) return;
  const slots = target.slots();
  const wanted = slots.map(slot => slot.slot + (slot.fixed ? '!' : '')).join('|');
  if (rows.dataset.slots !== wanted) {
    // A way of using a dial that nobody can set is stated in its place in the list, not
    // offered: a row that looked settable and then did something else would be worse
    // than no row at all.
    rows.innerHTML = slots.map(slot => (slot.fixed ? `
      <div class="panel-row dial-slot is-fixed" data-slot="${esc(slot.slot)}">
        <div class="row-head"><p class="row-label">${esc(slot.label)}</p></div>
        <p class="row-fixed"><b>${esc(slot.value)}</b><small>${esc(slot.note)}</small></p>
      </div>` : `
      <div class="panel-row dial-slot" data-slot="${esc(slot.slot)}">
        <div class="row-head"><p class="row-label">${esc(slot.label)}</p></div>
        <button class="row-value is-assign" type="button" data-panel-open="action" data-panel-slot="${esc(slot.slot)}" aria-expanded="false" title="${esc(slot.hint)}">
          <b data-slot-value="${esc(slot.slot)}">＋ Assign…</b>
          <small class="row-meta" data-slot-meta="${esc(slot.slot)}"></small>
        </button>
      </div>`)).join('');
    rows.dataset.slots = wanted;
  }
  const showing = state.open === 'action';
  for (const slot of slots) {
    if (slot.fixed) continue;
    const value = rows.querySelector(`[data-slot-value="${slot.slot}"]`);
    if (value) value.textContent = slot.value || '＋ Assign…';
    const meta = rows.querySelector(`[data-slot-meta="${slot.slot}"]`);
    if (meta) meta.textContent = slot.meta || '';
    const opener = rows.querySelector(`[data-panel-slot="${slot.slot}"]`);
    if (opener) {
      opener.classList.toggle('is-assign', !slot.value);
      opener.setAttribute('aria-expanded', String(showing && state.openSlot === slot.slot));
    }
  }
  const box = actionChoiceBox(root);
  if (box) {
    box.hidden = !showing;
    // The list belongs to the row that opened it, so it is filled again whenever a
    // different row asks for it.
    const key = state.openSlot || '';
    if (showing && box.dataset.filled !== key) {
      fillActionChoices(box, root, target);
      box.dataset.filled = key;
    }
  }
}

// Put words in one of the panel's rows.  Named once here rather than rebuilt inside the
// painter, so a row can be painted by a function of its own.
function setRowWords(root, what, words) {
  const node = panelIn(root, what);
  if (node) node.textContent = words;
}

function paintEditorRows(root, view, target, state) {
  // A dial answers for three slots instead of one job, so its own rows replace the
  // single-job rows rather than sitting beside them: one panel, two shapes.
  const dial = typeof target.slots === 'function';
  const dialRow = root.querySelector('[data-panel-row="dial"]');
  if (dialRow) dialRow.hidden = !dial;
  if (dial) {
    for (const name of ['action', 'shortcut', 'mode', 'icon']) {
      const row = root.querySelector(`[data-panel-row="${name}"]`);
      if (row) row.hidden = true;
    }
    paintDialRows(root, target, state);
    // The tools strip is this panel's way out of the editor, and a dial opens straight
    // into the editor, so a dial needs it more than anything else here does.  Returning
    // before it was built left Done and Cancel in the markup and never on the screen.
    paintToolStrip(root, view, target);
    return;
  }
  paintActionRow(root, view);
  paintIconRow(root, view, target);
  paintShortcutRow(root, view);

  paintModeRow(root, view, target);

  paintOpenChooser(root, view, target, state);
  paintToolStrip(root, view, target);
}

// One row at a time, and only the open one is filled.  A list nobody asked for is a list
// that has to be kept in step with the draft for nothing, and filling it here would also
// throw away a filter that is being typed into.
function paintOpenChooser(root, view, target, state) {
  const fill = (name, what, filler) => {
    const box = panelIn(root, what);
    if (!box) return;
    const open = state.open === name;
    box.hidden = !open;
    const opener = root.querySelector(`[data-panel-open="${name}"]`);
    if (opener) opener.setAttribute('aria-expanded', String(open));
    if (open && !box.dataset.filled) { filler(box); box.dataset.filled = '1'; }
  };
  fill('action', 'action-choices', box => fillActionChoices(box, root, target));
  fill('icon', 'icon-choices', box => fillIconChoices(box, root, target));
  fill('shortcut', 'shortcut-editor', box => renderShortcutEditor(
    view.actionId, view.keys, box, target.askedBy()));
}

// The shortcut card means something only for a keystroke action, so it appears for one and
// stays out of the way otherwise - the same rule on every mount, instead of one page hiding
// it and the other explaining why it is empty.
function paintShortcutRow(root, view) {
  const button = root.querySelector('[data-panel-open="shortcut"]');
  if (button) button.title = 'Change the keys this key sends';
  const row = root.querySelector('[data-panel-row="shortcut"]');
  if (row) row.hidden = !view.shortcut;
  setRowWords(root, 'shortcut-value', view.shortcut || '');
}

// The action row: what this control does.  An empty control says what to do about it in
// the same words wherever the panel is mounted, rather than describing the emptiness -
// "Nothing yet / click to choose" named the gap without saying that this row was the thing
// to press.  The row is also the door to the shortcuts the app already knows, so it says so:
// otherwise hovering is the only way to find out it is clickable at all.
//
// A control wearing a saved copy that differs from the ready-made one says so here, with the
// way back beside it.  Silence is what made the volume rows look like the app's own keys
// while the deck was sending F12.
function paintActionRow(root, view) {
  setRowWords(root, 'action-heading', view.heading);
  const pick = root.querySelector('[data-panel-open="action"]');
  if (pick) {
    pick.classList.toggle('is-assign', !view.assigned);
    pick.title = `Choose the shortcut for this ${view.noun}`;
  }
  setRowWords(root, 'action-value', view.assigned ? view.label : '＋ Assign…');
  const meta = panelIn(root, 'action-meta');
  if (meta) {
    // A shortcut already appears in the summary above and again in its own card:
    // repeating it in the chooser row said the same thing three times in one panel.
    const repeatsShortcut = Boolean(view.shortcut && view.meta);
    meta.textContent = view.assigned
      ? (repeatsShortcut ? '' : view.meta)
      : 'Menu or action';
    meta.hidden = !meta.textContent;
  }
  const reset = root.querySelector('[data-tool="reset-action"]');
  if (reset) {
    const changed = view.assigned ? savedCopyDiffersFromReady(view.actionId) : null;
    reset.hidden = !changed;
    if (changed) {
      reset.title = `This ${view.noun} uses your saved version of “${view.label}”. `
        + `Reset it to the one the app ships.`;
    }
  }
}

// The picture row.  A picture needs a screen to live on, so a control without one is not
// offered the row at all rather than offered one that cannot do anything.  With a picture
// assigned the row is the picture and the word Change...; the name beside it was repeating
// in words the picture may not use.  Hiding the name outright would leave the row announced
// as only "Change...", so the name moves onto the button instead.
function paintIconRow(root, view, target) {
  const words = panelIn(root, 'icon-value');
  if (words) {
    words.textContent = view.iconWords;
    words.hidden = !!view.icon;
  }
  const button = root.querySelector('[data-panel-open="icon"]');
  if (button) {
    button.setAttribute('aria-label', view.icon
      ? `Change the icon. Currently: ${view.iconWords}`
      : `Choose the icon for this ${view.noun}`);
    button.title = `Choose the icon for this ${view.noun}`;
  }
  const thumb = panelIn(root, 'icon-thumb');
  if (thumb) {
    thumb.innerHTML = panelThumbHTML(view.icon, target.thumbKey());
    thumb.hidden = !view.icon;
  }
  const row = root.querySelector('[data-panel-row="icon"]');
  if (row) row.hidden = !view.screen;
}

// Held while pressed, or fired once.  Offered only where holding means something - a keys
// assignment - and where the panel's target has a way to write it: a menu's own buttons
// cannot be held, so they are not asked.
function paintModeRow(root, view, target) {
  const row = root.querySelector('[data-panel-row="mode"]');
  if (row) row.hidden = !(view.canHold && typeof target.setMode === 'function');
  const box = panelIn(root, 'mode');
  if (box) {
    box.checked = view.held;
    // The target owns the change: where a hold is written depends on what the panel
    // answers for - a control on the deck, or a menu that claims one - and so do the
    // words that say what happened.
    box.onchange = () => {
      if (target.setMode) target.setMode(box.checked);
    };
  }
  // The explainer lives on the label, as a tooltip.  A line that says one thing does not
  // need a second line underneath saying it again.
  const label = root.querySelector('[data-panel-row="mode"] .row-toggle');
  if (label) {
    label.title = view.held
      ? 'Down while the button is held, up when you let go.'
      : 'Sent and released at once, like every other key on the deck.';
  }
}

// Opening the action list shows it first and fills it when the catalogue arrives:
// waiting for the fetch means a request that never comes back leaves a control that
// looked dead, which is exactly what was reported once.  The deadline is the same
// 4s the deck has always given it.
async function openActionChoices(root) {
  const state = panelState(root);
  const target = state && state.target;
  const box = actionChoiceBox(root);
  if (!box || !target) return;
  fillActionChoices(box, root, target);
  box.dataset.filled = state.openSlot || '';
  box.scrollIntoView({block: 'nearest'});
  const deadline = new Promise(resolve => setTimeout(resolve, 4000));
  try {
    await Promise.race([loadActionCatalogue(), deadline]);
  } catch (error) {
    // The short list is already on screen and says so itself; nothing to add here.
  }
  const live = panelState(root);
  if (live && live.open === 'action' && live.openSlot === state.openSlot) fillActionChoices(box, root, target);
  const filter = box.querySelector('.action-filter');
  if (filter) filter.focus();
}

async function openIconChoices(root) {
  const box = panelIn(root, 'icon-choices');
  if (!box) return;
  const state = panelState(root);
  const target = state && state.target;
  fillIconChoices(box, root, target);
  box.dataset.filled = '1';
  box.scrollIntoView({block: 'nearest'});
  try {
    await Promise.race([loadLibrary({fresh: true}), new Promise(resolve => setTimeout(resolve, 4000))]);
  } catch (error) {
    noteInChoices(box, 'The picture library could not be read - reload the page.');
    return;
  }
  if (panelState(root) && panelState(root).open === 'icon') fillIconChoices(box, root, target);
}

// ── The three caps under the LCD keys ───────────────────────────────────────
// A cap is assigned exactly as an LCD key is, and its panel is this same
// component told a different control.  Two things differ by nature: a cap has no
// screen, so it carries no label and no picture, and it is named by where it sits
// on the deck rather than by a number on a tile.  The firmware's own names for
// them are not in deck order, so the order is kept here, once.
const CAP_CONTROLS = ['button6', 'button8', 'button7'];
const CAP_PLACES = ['left', 'middle', 'right'];

// The three dials, in the order they sit on the deck, and the ways each one can be used.
// The big dial and the right dial are set in full.  The left dial's press is always the
// way back out of a menu, so it is stated rather than offered.  The slot names are the
// ones the deck files a binding under, so what this page writes is what the daemon looks
// up.
const DIAL_ORDER = ['top', 'bottom_left', 'bottom_right'];
const DIAL_KINDS = {
  top: ['press', 'turn_left', 'turn_right'],
  bottom_left: ['press', 'turn_left', 'turn_right'],
  bottom_right: ['press', 'turn_left', 'turn_right'],
};
// What a dial does that is nobody's to set, and what the panel says about it.
const DIAL_FIXED = {
  bottom_left: {press: {value: 'Back one level', note: 'Always, while a menu is open.'}},
};
const DIAL_TITLES = {top: 'Big dial', bottom_left: 'Left dial', bottom_right: 'Right dial'};

function dialSettableKinds(dial) {
  return DIAL_KINDS[dial].filter(kind => !(DIAL_FIXED[dial] || {})[kind]);
}

const DIAL_SLOTS = DIAL_ORDER.flatMap(dial => dialSettableKinds(dial).map(kind => (kind === 'press' ? dial : `${dial}_${kind}`)));
// Which dial and which way each slot in that flat list belongs to.
const DIAL_SLOT_DIALS = DIAL_ORDER.flatMap(dial => dialSettableKinds(dial).map(() => dial));
const DIAL_SLOT_KINDS = DIAL_ORDER.flatMap(dial => dialSettableKinds(dial));

function targetIsDial() {
  return selectedDial !== null;
}

function dialControl() {
  return DIAL_ORDER[selectedDial];
}

// The positions in the flat slot list that belong to one dial, in the order they read.
function dialSlotIndices(index) {
  const before = DIAL_ORDER.slice(0, index).reduce((total, dial) => total + dialSettableKinds(dial).length, 0);
  return dialSettableKinds(DIAL_ORDER[index]).map((_kind, offset) => before + offset);
}

function dialSlotWords(kind) {
  return kind === 'press' ? 'Press' : kind === 'turn_left' ? 'Turn left' : 'Turn right';
}

function dialSlotHint(kind) {
  if (kind === 'press') return 'Choose what pressing this dial down does.';
  return `Choose what turning this dial ${kind === 'turn_left' ? 'left (anticlockwise)' : 'right (clockwise)'} one click does.`;
}

function dialSlotValue(slot) {
  const index = DIAL_SLOTS.indexOf(slot);
  return index < 0 ? null : ((state.dials || [])[index] || null);
}

// The rows the panel shows for a dial, in the order they happen: its press, then each
// way it turns.  A way that is not anybody's to set comes back as a statement instead, so
// the dial reads as one list and the panel never has to guess which rows are which.
function dialSlots() {
  const dial = dialControl();
  const positions = dialSlotIndices(selectedDial);
  let taken = 0;
  return DIAL_KINDS[dial].map(kind => {
    const fixed = (DIAL_FIXED[dial] || {})[kind];
    if (fixed) {
      return {
        slot: kind === 'press' ? dial : `${dial}_${kind}`,
        label: dialSlotWords(kind),
        fixed: true,
        value: fixed.value,
        note: fixed.note,
      };
    }
    const position = positions[taken++];
    const bound = dialSlotValue(DIAL_SLOTS[position]);
    return {
      slot: DIAL_SLOTS[position],
      label: dialSlotWords(kind),
      hint: dialSlotHint(kind),
      value: bound ? title(bound) : '',
      meta: bound ? description(action(bound)) : '',
    };
  });
}

// Which control the deck's panel is answering for.  `selectedCap` is null while
// an LCD key is chosen, so the two can never both be true.
function targetIsCap() {
  return selectedCap !== null;
}

function capControl() {
  return CAP_CONTROLS[selectedCap] || CAP_CONTROLS[0];
}

function capName(index) {
  return `Button ${index + 1} (${CAP_PLACES[index]})`;
}

function capHeld() {
  return (state.held || []).includes(capControl());
}

// Held belongs to the assignment, so it is forgotten the moment the assignment
// stops being a keys action: there is nothing else to hold down.
function forgetHeld(control) {
  state.held = (state.held || []).filter(name => name !== control);
}

function capJob() {
  const value = (state.buttons || [])[selectedCap] || null;
  if (!value) {
    return {label: 'Nothing yet', meta: '', value: null, icon: null, current: null, noun: 'button', screen: false, held: false};
  }
  const spec = action(value) || readyMadeAction(value);
  const keys = spec && spec.kind === 'keys' ? spec.keys : null;
  return {
    label: title(value),
    meta: keys ? shortcut(keys) : (spec && spec.kind === 'brightness' ? `${spec.brightness}%` : ''),
    value,
    icon: null,
    current: title(value),
    actionId: value,
    noun: 'button',
    screen: false,
    held: capHeld(),
  };
}

// The current job of whatever the deck's panel is answering for: the selected key,
// or - while a menu key is being edited - that slot of the page open in the chain.
// The Menus page builds the same shape for the button it is showing, and panelView
// turns either one into the words the panel uses.
function panelJob() {
  if (targetIsCap()) return capJob();
  if (targetIsSlot()) {
    const menuPage = page();
    const item = menuPage ? menuPage.items.find(entry => entry.key === selectedSlot) : null;
    if (!item) return {label: 'Nothing yet', meta: '', value: null, icon: null, current: null, noun: 'key'};
    if (item.submenu) {
      const target = pageTargets(menuPage).find(candidate => candidate.id === item.submenu);
      return {label: `Opens “${target ? target.title : item.submenu}”`, meta: '', value: `__menu__:${item.submenu}`, icon: item.icon || null, current: item.label, noun: 'key'};
    }
    const spec = action(item.action) || readyMadeAction(item.action);
    const keys = spec && spec.kind === 'keys' ? spec.keys : null;
    return {
      label: item.label || title(item.action),
      meta: keys ? shortcut(keys) : (spec && spec.kind === 'brightness' ? `${spec.brightness}%` : ''),
      value: item.action,
      icon: item.icon || null,
      current: item.label,
      actionId: item.action,
      noun: 'key',
    };
  }
  const value = state.bindings ? state.bindings[selectedKey] : null;
  if (!value) return {label: 'Nothing yet', meta: '', value: null, icon: (artworkRefs() || [])[selectedKey] || null, current: null, noun: 'key'};
  if (value.startsWith('__menu__:')) {
    const name = value.slice('__menu__:'.length);
    const menu = (state.menus || []).find(candidate => candidate.name === name);
    // A deck key that opens a menu is named for that menu: "Opens “Youtube”" is what
    // the key is for, and the menu's own title is the name that matters.
    return {label: `Opens “${menu ? title(menu.name) : name}”`, meta: '', value, icon: (artworkRefs() || [])[selectedKey] || null, current: title(name), noun: 'key', namedByMenu: true};
  }
  const spec = action(value) || readyMadeAction(value);
  const keys = spec && spec.kind === 'keys' ? spec.keys : null;
  return {
    label: title(value),
    meta: keys ? shortcut(keys) : (spec && spec.kind === 'brightness' ? `${spec.brightness}%` : ''),
    value,
    icon: (artworkRefs() || [])[selectedKey] || null,
    current: title(value),
    actionId: value,
    noun: 'key',
  };
}

// Every choice the row offers, in groups: the actions the app ships with, then the
// ones this configuration already has.  The menus are deliberately *not* a group
// here - they are offered as a disclosure above these, because a list of forty menus
// above the actions pushed the list people came for off the bottom of the panel.
// The ready-made actions, by id, from whatever the catalogue last answered.  Empty until
// it arrives, which is safe: nothing is filtered out until there is something to filter
// against, so the worst case is the old list rather than a missing one.
function readyActionsById() {
  const out = new Map();
  const sections = (actionCatalogue && actionCatalogue.sections) || [];
  sections.forEach(section => (section.actions || []).forEach(entry => out.set(entry.id, entry)));
  return out;
}

// The library icon an action is drawn with, as a reference a deck file can hold.
// `actionIconFor` answers with a preview URL, which is the wrong currency for a draft:
// the draft stores where a picture comes from, not what it renders as.
function choiceIconReference(actionId) {
  if (!actionId) return '';
  const icon = libraryIcons().find(candidate => candidate.reference
    && (candidate.name === actionId || candidate.action === actionId));
  return icon ? icon.reference : '';
}

function actionChoiceList() {
  const groups = [];
  const sections = (actionCatalogue && actionCatalogue.sections) || [];
  sections.forEach(section => groups.push({
    heading: section.name,
    rows: (section.actions || []).map(entry => ({
      value: entry.id, label: entry.label, note: entry.shortcut || '', icon: entry.icon || entry.id,
    })),
  }));
  // Only what your deck file holds and the app does not.  The sections above already
  // answer for their own ids,
  // so a saved copy of one of them is not news - it is the same action under a second
  // roof, and listing it again made this the longest group in the panel and one that
  // grew with every action ever touched.  What is left is a menu you built, or an action
  // you named yourself: short, and short for good.
  const ready = readyActionsById();
  const mine = (state.actions || []).filter(entry => !ready.has(entry.id));
  if (mine.length) {
    groups.push({heading: `From your deck file · ${mine.length}`, rows: mine.map(entry => {
      const {keys, caps, dials, menus} = actionUses(entry.id);
      return {
        value: entry.id, label: title(entry.id),
        note: entry.kind === 'keys' ? shortcut(entry.keys || []) : entry.kind === 'brightness' ? `${entry.brightness}%` : 'a menu',
        icon: entry.id,
        // An action nothing wears has no way out of the file on its own: the Delete on a
        // control's panel acts on the action that control is wearing, so once nothing
        // wears it there is nothing left to press.  A row a key, a cap, a dial or a menu
        // button still calls keeps its row and gets no button that could only refuse.
        removable: !(keys.length || caps.length || dials.length || menus.length),
      };
    })});
  }
  return groups;
}

// A ready-made action you have changed keeps your version - that is the point of a saved
// copy.  But it has to say that it is your version, and offer the way back: without this
// the row showed a saved copy's words with no hint that the app ships a different one,
// which is exactly how the volume rows came to look like the app's own keys while the
// deck sent something else.
function readyVersionDiffers(entry) {
  const ready = readyActionsById().get(entry && entry.id);
  if (!ready || !entry) return false;
  const mineKeys = (entry.keys || []).join('+');
  const readyKeys = (ready.keys || []).join('+');
  if (ready.brightness !== null && ready.brightness !== undefined) {
    return Number(entry.brightness) !== Number(ready.brightness);
  }
  return mineKeys !== readyKeys;
}

// The panel's own paint carries a state of its own, which shadows the page's; asking
// here keeps the question about the draft where the draft actually is.
function savedCopyDiffersFromReady(actionId) {
  if (!actionId || !Array.isArray(state.actions)) return null;
  const entry = state.actions.find(one => one && one.id === actionId);
  if (!entry || !readyActionsById().has(actionId)) return null;
  return readyVersionDiffers(entry) ? entry : null;
}

function putReadyVersionBack(id) {
  const ready = readyActionsById().get(id);
  if (!ready || !Array.isArray(state.actions)) return false;
  const at = state.actions.findIndex(entry => entry && entry.id === id);
  if (at === -1) return false;
  // The ready-made reading is copied, not referenced: the draft is the thing that gets
  // written out, and it must not be able to edit the catalogue by accident.
  const fresh = JSON.parse(JSON.stringify(ready));
  state.actions[at] = {
    id: fresh.id,
    kind: fresh.brightness !== null && fresh.brightness !== undefined ? 'brightness' : 'keys',
    ...(fresh.brightness !== null && fresh.brightness !== undefined
      ? {brightness: fresh.brightness}
      : {keys: fresh.keys || []}),
  };
  return true;
}

// The menus a button could open, as the rows the rest of the list uses.
function menuChoiceRows() {
  return (state.menus || []).map(menu => ({
    value: `__menu__:${menu.name}`, label: `Open “${title(menu.name)}”`,
    note: `${(menu.pages || []).length} page${(menu.pages || []).length === 1 ? '' : 's'} · takes over the six keys`,
    icon: null,
  }));
}

// The one line the deck's panel needs that no other panel does: while it answers for
// a menu key rather than a deck key, it says which corner of which menu that key is
// in, and offers the way back.  The markup only: the panel's own router routes the
// click through the target's goBack, so nothing here binds by id.
function deckPanelContextHTML() {
  if (!targetIsSlot()) return '';
  const menu = (state.menus || [])[selectedMenu];
  const menuPage = page();
  const where = `${title(menu ? menu.name : 'menu')} › ${menuPage ? menuPage.title : 'page'}`;
  return `Menu key ${selectedSlot + 1} of “${esc(where)}”`
    + ' <button type="button" class="quiet" data-panel-back>Back to the deck keys</button>';
}

const MODIFIER_CHOICES = [['ctrl', '⌃ control'], ['alt', '⌥ option'], ['shift', '⇧ shift'], ['cmd', '⌘ command']];

// The key half of a shortcut is a set of tiles you click, exactly like the
// modifier chips beside them: click ↑ and it lights up, so what is bound is
// visible without reading anything. These are the keys that cannot be typed.
const SHORTCUT_KEYS = [
  ['up', '↑ up'], ['down', '↓ down'], ['left', '← left'], ['right', '→ right'],
  ['enter', '⏎ return'], ['space', 'space'], ['tab', 'tab'], ['escape', 'esc'], ['backspace', '⌫ delete'],
];
const FUNCTION_KEYS = Array.from({ length: 12 }, (_, index) => [`f${index + 1}`, `F${index + 1}`]);

// The keyboard vocabulary the deck can actually send. It mirrors the injector's
// fixed table on purpose: a token outside it is refused rather than guessed, so
// the panel must be able to *express* these keys instead of quietly saving a
// different one. Arrows, Return, Space and the function keys are what most of the
// interesting shortcuts are made of, and none of them can be typed as a
// character — which is why this is a field you press into.
const KEY_EVENT_NAMES = {
  arrowup: 'up', arrowdown: 'down', arrowleft: 'left', arrowright: 'right',
  enter: 'enter', 'return': 'enter', escape: 'escape', ' ': 'space', space: 'space',
  tab: 'tab', backspace: 'backspace', delete: 'delete', insert: 'insert',
  home: 'home', end: 'end', pageup: 'pageup', pagedown: 'pagedown',
};
const KEY_TOKEN_LABELS = {
  up: '↑ up', down: '↓ down', left: '← left', right: '→ right', enter: '⏎ return',
  escape: 'esc', space: 'space', tab: 'tab', backspace: '⌫ backspace', delete: '⌦ delete',
  insert: 'insert', home: 'home', end: 'end', pageup: 'page up', pagedown: 'page down',
};
const MODIFIER_EVENT_NAMES = { control: 'ctrl', alt: 'alt', shift: 'shift', meta: 'cmd' };

function keyLabel(token) {
  return KEY_TOKEN_LABELS[token] || String(token || '').toUpperCase();
}

/** The canonical token for a key event, or null when the deck cannot send it. */
function keyTokenFromEvent(event) {
  const name = String(event.key || '').toLowerCase();
  if (KEY_EVENT_NAMES[name]) return KEY_EVENT_NAMES[name];
  if (/^f([1-9]|1[0-2])$/.test(name)) return name;
  if (/^[a-z0-9]$/.test(name)) return name;
  return null;
}

/** The canonical token for something typed, e.g. "UP" or "↑ up" or "f3" or "n". */
function keyTokenFromText(text) {
  const raw = String(text || '').trim();
  if (!raw) return null;
  // The field *shows* a label ("↑ up"), so the label must read back as its token:
  // otherwise the wiring clears its own value the moment it initialises.
  const label = Object.entries(KEY_TOKEN_LABELS).find(
    ([, shown]) => shown.toLowerCase() === raw.toLowerCase()
  );
  if (label) return label[0];
  const cleaned = raw.toLowerCase().replace(/[⌃⌥⇧⌘]/g, '').trim();
  if (!cleaned) return null;
  const glyphs = { '↑': 'up', '↓': 'down', '←': 'left', '→': 'right', '⏎': 'enter', '⌫': 'backspace', '⌦': 'delete' };
  if (glyphs[cleaned]) return glyphs[cleaned];
  if (KEY_EVENT_NAMES[cleaned]) return KEY_EVENT_NAMES[cleaned];
  if (/^f([1-9]|1[0-2])$/.test(cleaned)) return cleaned;
  if (/^[a-z0-9]$/.test(cleaned)) return cleaned;
  return null;
}

/**
 * Wire a text field so the shortcut can simply be pressed into it.
 *
 * A one-character box made ⌃↑ impossible: "up" has two letters, so the panel
 * silently stored ⌃U instead. Held modifiers fill the chips, and the token is
 * kept beside the displayed label so what is saved is what was pressed.
 */
function wireKeyCapture(input) {
  if (!input) return;
  const container = input.closest('.shortcut-editor, .row-modifiers, .panel-row, .row-new-form') || input.parentElement;
  const chips = () => [...container.querySelectorAll('.modifier')];
  const setToken = token => {
    input.dataset.token = token || '';
    input.value = token ? keyLabel(token) : '';
    input.classList.toggle('is-unknown', !token && Boolean(input.value));
  };
  input.dataset.token = keyTokenFromText(input.value) || '';
  if (input.value) input.value = keyLabel(input.dataset.token);
  input.onkeydown = event => {
    const modifier = MODIFIER_EVENT_NAMES[event.key];
    if (modifier) {
      // Pressing ⌃ or ⌘ toggles its chip, so the whole chord can be pressed.
      const chip = container.querySelector(`[data-modifier="${modifier}"]`);
      if (chip) {
        chip.classList.toggle('on');
        chip.setAttribute('aria-pressed', String(chip.classList.contains('on')));
      }
      event.preventDefault();
      return;
    }
    const token = keyTokenFromEvent(event);
    if (!token) {
      event.preventDefault();
      input.classList.add('is-unknown');
      setStatus('error', `The deck cannot send “${event.key}”. Use a letter, a number, an arrow, Return, Space, Tab, Escape or F1–F12.`);
      return;
    }
    event.preventDefault();
    // Hold ⌃ and press ↑ and the chip fills in too: the chord you press is the
    // chord that gets saved.
    const held = Object.entries(MODIFIER_EVENT_NAMES)
      .filter(([eventName]) => event[`${eventName}Key`])
      .map(([, name]) => name)
      .filter(name => name !== undefined);
    if (held.length) {
      chips().forEach(chip => {
        const on = held.includes(chip.dataset.modifier);
        chip.classList.toggle('on', on);
        chip.setAttribute('aria-pressed', String(on));
      });
    }
    setToken(token);
    // Say something on screen, or a press that changes nothing looks broken:
    // the field flashes green and the status line explains what was set.
    input.classList.add('is-set');
    clearTimeout(input.dataset.setTimer);
    const timer = setTimeout(() => input.classList.remove('is-set'), 900);
    input.dataset.setTimer = timer;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  };
  // Typing still works, for anyone who would rather write "up" or "f3".
  input.oninput = () => {
    const raw = input.value;
    const token = keyTokenFromText(raw);
    if (token) {
      input.dataset.token = token;
      input.value = keyLabel(token);
      input.classList.remove('is-unknown');
      return;
    }
    input.dataset.token = '';
    input.classList.add('is-unknown');
  };
}


// One shortcut editor, both panels.  It is handed the box to fill rather than
// looking one up by id: the deck's key panel and the Menus button panel show the
// same controls for the same action, and an id lookup would always find whichever
// happened to come first in the document.
// The identity card: the number, what kind of control it is, its name, and its
// shortcut underneath.  A menu button is a button with a number, a name and a
// shortcut, exactly as a key is, so both panels draw this from here.  Classes only,
// because the deck's card and a menu button's card can be on the page together.
function controlCardInnerHTML({number, type, title, label, value}) {
  return (
    `<div class="control-identity">
          <span class="control-number" aria-label="Button ${esc(String(Number(number)))}">${esc(number)}</span>
          <div>
            <span class="control-type">${esc(type)}</span>
            <b class="control-title">${esc(title)}</b>
          </div>
        </div>
        <p class="action-description${label === 'Shortcut:' ? ' is-shortcut' : ''}">
          <span>${esc(label)}</span><kbd>${esc(value)}</kbd>
        </p>`
  );
}

// The same content, with the card around it: the Menus panel's whole heading.
function controlCardHTML(data) {
  return (
    `<section class="selected-control ${esc(data.tone)}" data-control-shape="${esc(data.shape || 'lcd')}"
              aria-live="polite" aria-atomic="true"${data.aria ? ` aria-label="${esc(data.aria)}"` : ''}>
      ${controlCardInnerHTML(data)}
    </section>`
  );
}

// What a button's card says about what it does, from the same helper the deck uses.
function controlCardFor(kind, key, menuName, item) {
  const number = String(key + 1).padStart(2, '0');
  const type = kind === 'key' ? `LCD key ${key + 1}` : `Menu button ${key + 1}`;
  const title = item ? item.label : 'Nothing yet';
  const spec = item && item.action ? action(item.action)
    : (item && item.menu ? {kind: 'menu', menu: item.menu} : null);
  const detail = assignmentDetail(spec);
  return {
    number, type, title, tone: keyTone(key), label: detail.label, value: detail.value,
    aria: `Editing ${type}: ${title}. ${detail.label} ${detail.value}`,
  };
}

function renderShortcutEditor(actionId, keys, into = null, askedBy = null) {
  const box = into || $('#shortcutEditor');
  if (!box) return;
  const held = new Set(keys.filter(key => MODIFIER_CHOICES.some(([name]) => name === key)));
  const keyToken = keys.find(key => !MODIFIER_CHOICES.some(([name]) => name === key)) || '';
  const isLetter = /^[a-z0-9]$/.test(keyToken);
  const keyChip = ([token, label]) => `<button type="button" class="key-chip ${token === keyToken ? 'on' : ''}" data-key="${token}" aria-pressed="${token === keyToken}">${esc(label)}</button>`;
  box.innerHTML = MODIFIER_CHOICES.map(([name, label]) => (
    `<button type="button" class="modifier ${held.has(name) ? 'on' : ''}" data-modifier="${name}" aria-pressed="${held.has(name)}">${esc(label)}</button>`
  )).join('')
    + `<div class="key-chips">${SHORTCUT_KEYS.map(keyChip).join('')}</div>`
    + `<details class="key-more"${keyToken.startsWith('f') ? ' open' : ''}><summary>F1–F12${keyToken.startsWith('f') ? ' · ' + keyLabel(keyToken) : ''}</summary>`
    + `<div class="key-chips">${FUNCTION_KEYS.map(keyChip).join('')}</div></details>`
    + `<label class="key-letter ${isLetter ? 'on' : ''}">letter or number <input class="shortcut-key" type="text" maxlength="1" value="${isLetter ? esc(keyToken.toUpperCase()) : ''}" data-token="${isLetter ? esc(keyToken) : ''}" aria-label="letter or number"></label>`
    // A shortcut can be a single key, and for things like seeking in a video it has to
    // be - `j` and `l` are the site's own shortcuts.  Said plainly rather than refused:
    // a bare letter is typed into whatever window is focused, so the warning belongs on
    // the assignment the person is making, not in the way of making it.
    + (keyToken && !held.size && isLetter
      ? `<p class="key-warning">Sends just ${esc(keyToken.toUpperCase())}, with no modifiers: it goes to whatever window has focus, including a text field.</p>`
      : '');

  // One key at a time, whichever way it was chosen: a lit tile or a letter.
  const chosenKey = () => {
    const chip = box.querySelector('.key-chip[data-key].on');
    if (chip) return chip.dataset.key;
    return keyTokenFromText(box.querySelector('.shortcut-key')?.value || '') || '';
  };

  const write = async () => {
    const next = MODIFIER_CHOICES.filter(([name]) => box.querySelector(`[data-modifier="${name}"]`)?.classList.contains('on')).map(([name]) => name);
    const typed = chosenKey();
    if (!typed) {
      setStatus('error', 'Pick a key for this shortcut — click one of the key tiles, or type a letter.');
      return;
    }
    next.push(typed);
    // One key on its own is allowed: a site whose own shortcut is a bare letter, such as
    // YouTube's `j` and `l`, cannot be driven any other way.  The editor says what a bare
    // letter will do to whatever window is focused, which is the honest guard here.
    await stageShortcut(actionId, next, askedBy);
  };

  box.querySelectorAll('.modifier').forEach(button => {
    button.onclick = async () => {
      button.classList.toggle('on');
      button.setAttribute('aria-pressed', String(button.classList.contains('on')));
      await write();
    };
  });
  // Click a key tile and it lights up, exactly like ⌃ does beside it — one key
  // at a time, and the choice is written straight into the action.
  box.querySelectorAll('.key-chip[data-key]').forEach(chip => {
    chip.onclick = async () => {
      box.querySelectorAll('.key-chip[data-key]').forEach(other => {
        other.classList.toggle('on', other === chip);
        other.setAttribute('aria-pressed', String(other === chip));
      });
      const letter = box.querySelector('.shortcut-key');
      if (letter) { letter.value = ''; letter.dataset.token = ''; }
      box.querySelector('.key-letter')?.classList.remove('on');
      await write();
    };
  });
  const letterField = box.querySelector('.shortcut-key');
  if (letterField) {
    letterField.oninput = () => {
      const token = keyTokenFromText(letterField.value);
      if (!token || !/^[a-z0-9]$/.test(token)) { letterField.dataset.token = ''; return; }
      letterField.dataset.token = token;
      box.querySelectorAll('.key-chip[data-key]').forEach(other => {
        other.classList.remove('on');
        other.setAttribute('aria-pressed', 'false');
      });
      box.querySelector('.key-letter')?.classList.add('on');
    };
    letterField.onchange = write;
  }
}

// Changing a shortcut edits the one action that key uses - there is no second
// copy of a keystroke anywhere for the two to disagree.
async function stageShortcut(actionId, keys, askedBy = null) {
  const spec = action(actionId);
  // The label and the fallback below both need to know *who* is asking.  Assuming
  // the deck's selection is how a change made in one panel lands on another's key,
  // so a caller from elsewhere passes its own label and gets no deck fallbacks.
  const label = (askedBy && askedBy.label) || panelJob().current || title(actionId);
  if (spec) {
    const already = (spec.keys || []).join('+') === keys.join('+');
    if (await commit(() => { spec.keys = keys; })) {
      setStatus('dirty', `“${title(actionId)}” now runs ${shortcut(keys)}. Press Save to write it to the deck.`);
    } else if (already) {
      // Pressing the chord that is already set is not a failure. Stay silent and
      // the panel looks broken when in fact the shortcut is exactly right.
      setStatus('saved', `“${title(actionId)}” already runs ${shortcut(keys)} — nothing to change.`);
    }
    return;
  }
  if (askedBy) {
    setStatus('error', `“${title(actionId)}” is not in this configuration yet — assign it on the Deck page first.`);
    return;
  }
  // A shortcut typed for a library action that this configuration does not have
  // yet: create it here, with this key pointed at it, in one step.
  const created = actionFromSpec({id: actionId, label, kind: 'keys', keys});
  if (targetIsSlot()) {
    const menuPage = page();
    if (!menuPage) return;
    const targets = pageTargets(menuPage);
    if (await commit(() => { state.actions.push(created); setSlot(menuPage, {key: selectedSlot, label, action: actionId}); })) {
      setStatus('dirty', `“${label}” now runs ${shortcut(keys)}. Press Save to write it to the deck.`);
    }
    return;
  }
  if (await commit(() => { state.actions.push(created); state.bindings[selectedKey] = actionId; })) {
    setStatus('dirty', `“${label}” now runs ${shortcut(keys)}. Press Save to write it to the deck.`);
  }
}

// ── Looking after an action you already have ────────────────────────────────
// Run it, rename it, get rid of it. All three live under the row that names the
// action, because that is the thing they act on - there is no second page to
// find. Nothing here is a silent dead end: a withheld button says why in its
// tooltip, and one that runs says what happened.

async function tryActionNow(actionId) {
  setStatus('dirty', `Trying “${title(actionId)}”…`);
  try {
    const response = await fetch('/api/action/test', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: actionId}),
    });
    const reply = await response.json();
    if (reply && reply.ok) {
      setStatus('saved', `“${title(actionId)}” was sent to macOS. Nothing happened on screen? That shortcut is one macOS ignores from a deck — the deck is still working.`);
    } else {
      setStatus('error', `“${title(actionId)}” was not sent: ${(reply && reply.reason) || 'the daemon gave no reason'}`);
    }
  } catch (error) {
    setStatus('error', `The daemon could not be asked to try that action: ${error}`);
  }
}

function openRename(actionId, current, root = null) {
  const host = root || $('#actionTools');
  const form = host ? host.querySelector('[data-tool="rename-form"]') : null;
  const field = form ? form.querySelector('.rename-name') : null;
  if (!form || !field) return;
  form.hidden = false;
  field.value = current || '';
  field.focus();
  field.select();
  field.onkeydown = event => {
    if (event.key === 'Enter') { event.preventDefault(); submitRename(actionId, form, field); }
    if (event.key === 'Escape') { event.preventDefault(); form.hidden = true; }
  };
  const save = form.querySelector('[data-tool="rename-save"]');
  const cancel = form.querySelector('[data-tool="rename-cancel"]');
  if (save) save.onclick = () => submitRename(actionId, form, field);
  if (cancel) cancel.onclick = () => { form.hidden = true; };
}

async function submitRename(actionId, form = null, field = null) {
  form = form || $('#actionTools') && $('#actionTools').querySelector('[data-tool="rename-form"]');
  field = field || (form ? form.querySelector('.rename-name') : null);
  const wanted = (field ? field.value : '').trim();
  if (!wanted) { setStatus('error', 'Give the action a name before saving it.'); return; }
  const next = uniqueId(slugify(wanted), actionId);
  const renamed = await commit(() => {
    let entry = state.actions.find(item => item.id === actionId);
    if (!entry) {
      // A ready-made action belongs to the deck. The first thing you change
      // about it becomes a copy of your own, so your version is yours to edit
      // and the library entry stays as it was.
      const ready = readyMadeAction(actionId);
      if (!ready) return;
      entry = {...ready, id: next};
      state.actions.push(entry);
    } else {
      entry.id = next;
    }
    // Every key and menu item that pointed at the old name follows it, so a
    // rename can never leave a key pointing at nothing.
    state.bindings = state.bindings.map(binding => (binding === actionId ? next : binding));
    (state.menus || []).forEach(menu => (menu.pages || []).forEach(menuPage => (menuPage.items || []).forEach(item => {
      if (item.action === actionId) item.action = next;
    })));
  });
  if (form) form.hidden = true;
  if (renamed) {
    setStatus('dirty', `Renamed to “${title(next)}”. Press Save to write it to the deck.`);
    render();
  }
}

// Everywhere an action is still worn: the six keys, the three caps, the dial slots that
// name it, and the menu buttons that call it.  One reading, so the row that offers Delete
// and the Delete that refuses can never disagree about whether an action can go.
//
// The caps and the dials are not in `state.bindings` - that array is the six LCD keys - so
// a reading that stopped at the keys called an action unused while a cap was still wearing
// it, and the list offered a Delete the deck would then refuse.
//
// Where the items actually are.  This read used to walk menu.pages[].items[], which is
// not the shape the draft holds - the editor's menus keep their items at the top - so
// the scan came back empty and the guard let the removal through to the server.  The
// server then refused it and reported the first menu button it happened to reach, which
// was not the button Mark had touched and named a button he could not account for.
function actionUses(actionId) {
  const keys = bindingKeys(actionId);
  const caps = (state.buttons || [])
    .map((bound, index) => (bound === actionId ? `the ${CAP_PLACES[index]} button` : null))
    .filter(Boolean);
  const dials = (state.dials || [])
    .map((bound, position) => (bound === actionId
      ? `${DIAL_TITLES[DIAL_SLOT_DIALS[position]]}, ${dialSlotWords(DIAL_SLOT_KINDS[position]).toLowerCase()}`
      : null))
    .filter(Boolean);
  const menus = [];
  fxMenus().forEach(menu => fxItems(menu).forEach(item => {
    if (item.action === actionId) menus.push(`“${title(menu.name)}” button ${Number(item.key) + 1}`);
  }));
  return {keys, caps, dials, menus};
}

async function removeAction(actionId) {
  const {keys, caps, dials, menus} = actionUses(actionId);
  if (keys.length || caps.length || dials.length || menus.length) {
    const where = [keys.length ? `key ${keys.join(', key ')}` : '', ...caps, ...dials, ...menus]
      .filter(Boolean).join(', ');
    setStatus('error', `“${title(actionId)}” is still used by ${where}. Point those somewhere else first — nothing was removed.`);
    return;
  }
  const removed = await commit(() => { state.actions = state.actions.filter(entry => entry.id !== actionId); });
  if (removed) {
    setStatus('dirty', `“${title(actionId)}” removed. Press Save to write it to the deck.`);
    render();
  }
}

// Everything the panel offers for whatever this key has.  The tools act on the
// assignment, so they change with its kind instead of pretending a menu is an
// action: a menu's buttons are designed on the Menus page, and that is what it
// offers.  Nothing here is a silent dead end - a withheld button says why in its
// tooltip, and one that runs says what happened.
// One tools strip for both panels, and one rename form to go with it.  A menu
// button gets exactly the deck's three verbs acting on exactly the same things:
// the action it runs is one action, whether a deck key or a menu button runs it.
// Everything is found by class inside the panel that asked, so the same strip can
// live in either panel without the two colliding.
// ―― putting an experiment back ―――――――――――――――――――――――――――――――――――――――――――――――――
// The draft is live: a change made in these rows is in it at once, which is why the deck
// drawing shows the change before Done is pressed.  That leaves one honest gap - a person
// who changes something, looks at it and prefers the old one.  Discard exists but throws
// away *everything* unsaved, so Cancel puts back just what this sitting changed.
//
// It needs no field-by-field bookkeeping: while the editor rows are open the only thing
// that can be changed is inside them - reaching another control means selecting it, which
// closes this editor and takes a fresh snapshot.  So the state of the draft when the editor
// opened *is* the state to return to.
const DRAFT_FIELDS = ['bindings', 'buttons', 'dials', 'held', 'icons', 'actions', 'menus', 'screen', 'screens'];

function snapshotDraft() {
  const snap = {};
  for (const field of DRAFT_FIELDS) {
    if (state[field] !== undefined) snap[field] = JSON.parse(JSON.stringify(state[field]));
  }
  return snap;
}

function putDraftBack(snap) {
  if (!snap) return;
  for (const field of Object.keys(snap)) {
    state[field] = JSON.parse(JSON.stringify(snap[field]));
  }
}

// The strip under the editor rows.  Done comes first because it is the way out of this
// view, and a way out must never be lost: Edit… lives in the at-rest summary, and the
// summary is hidden while these rows are showing, so without Done the only way back was
// to select another control and return to this one - Mark reported exactly that.
//
// Test is not here: it acts on the action at rest, where it already is.  One verb, one
// place.  Done is a view control, not a write to the deck - an edit is in the draft the
// moment it is made, and Save to deck still writes the deck.
function actionToolsHTML() {
  return (
    `<button type="button" class="quiet" data-tool="done">Done</button>
      <button type="button" class="quiet" data-tool="cancel">Cancel</button>
      <button type="button" class="quiet" data-tool="rename">Rename…</button>
      <button type="button" class="quiet quiet-danger" data-tool="delete">Delete</button>
      <button type="button" class="quiet" data-tool="menu" hidden>Edit this menu in Menus</button>`
  );
}

function renameFormHTML() {
  return (
    `<div class="row-new-form" data-tool="rename-form" hidden>
        <label>New name <input class="rename-name" type="text" maxlength="32" placeholder="e.g. Mission Control"></label>
        <button type="button" class="primary" data-tool="rename-save">Save name</button>
        <button type="button" class="quiet" data-tool="rename-cancel">Cancel</button>
      </div>`
  );
}

function paintActionTools(root, view, target) {
  if (!root || !view) return;
  const tool = name => root.querySelector(`[data-tool="${name}"]`);
  const form = tool('rename-form');
  if (form) form.hidden = true;
  // Done is the panel's own way out, so the blanket hide below must not take it.  A dial
  // opens straight into this view with nothing to leave, so a dial gets no Done.
  const done = tool('done');
  const cancel = tool('cancel');
  // A dial answers for three slots instead of one job, and that is what the lines below
  // and the verbs that act on a single action are decided by.
  const dial = !!target && typeof target.slots === 'function';
  // A dial gets Done and Cancel like every other control.  It opens straight into this
  // view, so without them the only way out was to select a different control and come
  // back - and Cancel is the only honest answer to "I did not mean that".
  if (done) done.hidden = false;
  if (cancel) cancel.hidden = false;
  // Edit itself is the panel's control now, wired where the panel is wired: nothing
  // here reaches back into the deck's selection.  An empty key is still a key you may
  // want to change, so Edit is always offered.
  if (!view.actionId && !view.menuName) {
    // An empty key still needs its way out, so Done stays while the action tools go.
    root.querySelectorAll('[data-tool]').forEach(one => { one.hidden = one !== done && one !== cancel; });
    return;
  }
  // Only your own actions are yours to rename or delete; ready-made ones ship
  // with the deck, and the buttons say so instead of hiding the reason.
  const rename = tool('rename');
  if (rename) {
    // A dial is not one action: its press and each way it turns are three of them, so
    // there is nothing here for Rename to act on.  Done and Cancel are its whole strip.
    rename.hidden = !view.actionId || dial;
    rename.title = view.mine
      ? 'Changes what this action is called; every key and menu using it follows.'
      : 'Changes what this action is called: your version becomes one of your own to edit.';
    rename.onclick = () => openRename(view.actionId, view.label, root);
  }
  const del = tool('delete');
  if (del) {
    // What Delete means depends on what the panel is answering for.  A deck key has no
    // assignment of its own to clear - assigning something else is how a key is emptied -
    // so its Delete acts on the action, and says so.  A menu button and a cap *can* be
    // left with nothing, and for those Delete clears the button and leaves the action
    // list alone: an action is shared, so removing it is a different job.
    const clears = !!target && typeof target.clearAssignment === 'function';
    del.hidden = dial ? true : (clears ? false : !(view.actionId && view.mine));
    del.title = clears
      ? 'Removes what this button does; the action itself stays in your deck'
      : view.mine
        ? 'Remove this action — only possible while nothing uses it, a key or a menu button'
        : 'Ready-made actions come with the deck; choose “Nothing yet” in the list above to clear this button';
    del.onclick = () => (clears ? target.clearAssignment() : removeAction(view.actionId));
  }
  const menu = tool('menu');
  if (menu) {
    menu.hidden = !view.menuName;
    menu.title = 'Opens the Menus page on this menu, where its buttons are designed.';
    menu.onclick = () => {
      fxMenuName = view.menuName;
      fxSlotKey = null;
      showView('menus');
      render();
    };
  }
}

// The tools strip: one per panel, built once and repainted from the reading.  It sits
// below the three cards - not inside the Action card, where opening its own list pushed
// the buttons around, and not inside the Icon card, which owns the picture only.
function paintToolStrip(root, view, target) {
  const box = panelIn(root, 'tools');
  if (!box) return;
  if (!box.dataset.built) {
    box.innerHTML = actionToolsHTML() + renameFormHTML();
    box.dataset.built = '1';
  }
  paintActionTools(box, view, target);
}
async function pickJob(value, label, slot, icon = '') {
  if (targetIsSlot()) {
    const menuPage = page();
    if (!menuPage) return;
    const targets = pageTargets(menuPage);
    if (value.startsWith('__menu__:')) { await commitSlot(value.slice('__menu__:'.length), true, targets); return; }
    await commitSlot(value, false, targets);
    if (icon) await pickIcon(icon, true);
    return;
  }
  if (targetIsCap()) { await assignActionToCap(value); return; }
  await assignActionToKey(value);
  if (icon) await pickIcon(icon, true);
}

// The same write a deck key makes, aimed at a cap: one slot of `buttons`, with
// the hold forgotten if what is being put there cannot be held.
async function assignActionToCap(value) {
  const index = selectedCap;
  const spec = value ? (action(value) || readyMadeAction(value)) : null;
  const holding = capHeld();
  if (await commit(() => {
    state.buttons = (state.buttons || [null, null, null]).slice();
    state.buttons[index] = value;
    if (holding && (!spec || spec.kind !== 'keys')) forgetHeld(CAP_CONTROLS[index]);
  })) {
    setStatus('dirty', value
      ? `${capName(index)} now runs ${title(value)}. Press Save to write it to the deck.`
      : `${capName(index)} now does nothing. Press Save to write it to the deck.`);
  }
}

// A list that failed to build must say so.  An empty box has no height, so a throw
// inside the painter left the click looking like it did nothing at all.
function noteInChoices(box, message) {
  if (!box) return;
  box.innerHTML = `<p class="row-heading">${esc(message)}</p>`;
}

// Used when the list built, but without the shortcuts that ship with the app: say so
// rather than showing a shorter list and letting it look complete.
function addChoicesNote(box, message) {
  if (!box) return;
  const note = document.createElement('p');
  note.className = 'row-heading';
  note.textContent = message;
  box.appendChild(note);
}

// The list runs to about fifty rows, so the row marked "in use" is usually well
// below the fold: the report was "you actually have to scroll down and down and
// down with the mouse to find what's highlighted".  A mark nobody can reach is
// no mark at all, so bring it into view whenever the list is painted.
function revealCurrentChoice(box) {
  const current = box && box.querySelector('.choice-row.on');
  if (current) current.scrollIntoView({ block: 'nearest' });
}

// ── The chooser list ────────────────────────────────────────────────────────
// One list, one builder, every panel: the menu this button could open behind its
// disclosure, then the actions it could run, then the ones this configuration already
// has.  The two pages used to keep a list each of the same things, and they disagreed
// - one offered the whole library, the other the first sixteen - which is why a key
// could not be pointed at a shortcut the Menus page was happy to offer.  Nothing here
// is page-specific: a pick is handed to the target, which is the only thing that
// knows what "put it on this button" means.
function fillActionChoices(box, root, target) {
  if (!box || !target) return;
  try {
    paintActionChoices(box, root, target);
    if (!box.querySelector('.choice-row')) noteInChoices(box, 'No shortcuts to show here - reload the page.');
    else if (!actionCatalogue) addChoicesNote(box, 'The app\u2019s ready-made shortcuts could not be read - reload the page.');
    // Deferred one frame: at this point the rows are in the DOM but not yet laid
    // out, so scrollIntoView has nothing to measure and the list opened at the top
    // anyway.  Measured: scrollTop stayed 0 until the same call was made later.
    requestAnimationFrame(() => revealCurrentChoice(box));
  } catch (error) {
    noteInChoices(box, 'The shortcut list could not be built - reload the page.');
  }
}

function paintActionChoices(box, root, target) {
  const view = target.view();
  const needle = (box.dataset.filter || '').trim().toLowerCase();
  const menus = state.menus || [];
  const menusOpen = box.dataset.menusOpen === '1';
  const row = (entry, on) => {
    const icon = actionIconFor(entry.icon || entry.value);
    // The picture the list draws beside the name is the library icon this action is
    // drawn with, so choosing the action can bring the picture with it instead of
    // leaving the row blank until someone goes and finds the same icon by hand.
    const reference = choiceIconReference(entry.value);
    const choice = `<button type="button" class="choice-row${on ? ' on' : ''}" data-value="${esc(entry.value)}" data-label="${esc(entry.label)}" data-icon="${esc(reference)}"${on ? ' aria-current="true"' : ''}>`
      + (icon ? `<img src="${esc(icon)}" alt="" width="24" height="24">` : '<span class="choice-glyph" aria-hidden="true">•</span>')
      + `<b>${esc(entry.label)}</b><small>${esc(entry.note || '')}</small>`
      + (on ? '<em>in use</em>' : '') + '</button>';
    // An action nothing wears carries its own way out of the file.  Only the rows that
    // can go get one: a row a key or a menu button still calls has no button that could
    // only refuse, and a row that is in use says so on its face already.
    if (!entry.removable) return choice;
    return `<div class="row-line">${choice}<button type="button" class="quiet row-remove"`
      + ` data-panel-remove="${esc(entry.value)}"`
      + ` title="Remove “${esc(entry.label)}” from your deck file — nothing is using it">Delete</button></div>`;
  };
  const groups = actionChoiceList().map(group => ({
    heading: group.heading,
    rows: group.rows.filter(entry => !needle || entry.label.toLowerCase().includes(needle)
      || (group.heading || '').toLowerCase().includes(needle)),
  })).filter(group => group.rows.length);
  // The menu this button already opens is named on the closed disclosure: with forty
  // menus, the one thing that matters while it is shut is still on screen.
  const opening = view.isMenu && view.menuName
    ? ((menus.find(candidate => candidate.name === view.menuName) || {}).title || view.menuName)
    : null;
  box.innerHTML = `<label class="row-filter"><span aria-hidden="true">⌕</span>`
    + `<input class="action-filter" type="search" value="${esc(box.dataset.filter || '')}" placeholder="Type to narrow the list"></label>`
    + (target.clearable
      ? `<div class="row-list">`
        + `<button type="button" class="choice-row${view.assigned ? '' : ' on'}" data-value="" data-label="Nothing yet">`
        + '<span class="choice-glyph" aria-hidden="true">•</span><b>Nothing yet</b>'
        + '<small>leaves this button empty</small>' + (view.assigned ? '' : '<em>in use</em>') + '</button></div>'
      : '')
    + `<p class="row-heading">Open a menu</p><div class="row-list">`
    + `<button type="button" class="choice-row${opening ? ' on' : ''}" data-menus-toggle aria-expanded="${menusOpen}">`
    + `<span class="choice-glyph" aria-hidden="true">${menusOpen ? '▾' : '▸'}</span>`
    + `<b>${esc(opening ? `Opens “${opening}”` : 'Open a menu…')}</b>`
    + `<small>${menus.length} menu${menus.length === 1 ? '' : 's'} to choose from</small></button>`
    + (menusOpen ? menuChoiceRows().map(entry => row(entry, entry.value === view.value)).join('') : '')
    + '</div>'
    + (groups.map(group => `<p class="row-heading">${esc(group.heading)}</p><div class="row-list">`
      + group.rows.map(entry => row(entry, entry.value === view.value)).join('') + '</div>').join('')
      || '<p class="muted">Nothing matches that.</p>')
    + newActionChoiceHTML();

  const filter = box.querySelector('.action-filter');
  if (filter) {
    filter.oninput = () => {
      box.dataset.filter = filter.value;
      fillActionChoices(box, root, target);
      const again = box.querySelector('.action-filter');
      if (again) { again.focus(); again.setSelectionRange(again.value.length, again.value.length); }
    };
  }
  const toggle = box.querySelector('[data-menus-toggle]');
  if (toggle) {
    toggle.onclick = () => {
      box.dataset.menusOpen = menusOpen ? '0' : '1';
      fillActionChoices(box, root, target);
    };
  }
  box.querySelectorAll('.choice-row[data-value]').forEach(button => {
    button.onclick = () => {
      closePanelChoosers(root);
      // A dial's three rows share this one list, so the row that opened it says which
      // slot the choice belongs to; a control with a single job ignores the third word.
      const openState = panelState(root);
      target.setAction(button.dataset.value, button.dataset.label, openState ? openState.openSlot : null, button.dataset.icon || '');
    };
  });
  // A row that carries its own way out.  An action nothing wears has no other Delete to
  // press - the one on a control's panel acts on the action that control is wearing - so
  // the list it appears in is the only place left to remove it.
  box.querySelectorAll('[data-panel-remove]').forEach(button => {
    button.onclick = () => removeAction(button.dataset.panelRemove);
  });
  wireNewActionChoice(box, root, target);
}

// Your own action, made where you were already looking: name it, tick the modifiers,
// press the key, and it exists and is on this button in one step.
function newActionChoiceHTML() {
  return `<div class="row-new">
        <button type="button" class="choice-row" data-new-row>＋ New action…</button>
        <div class="row-new-form" data-new-form hidden>
          <input class="new-name" type="text" maxlength="32" placeholder="Name it, e.g. Screenshot">
          <div class="row-modifiers">${MODIFIER_CHOICES.map(([name, label]) => (
            `<button type="button" class="modifier" data-modifier="${name}" aria-pressed="false">${esc(label)}</button>`
          )).join('')}<input class="new-key" type="text" placeholder="press a key…" aria-label="press the key for this shortcut"></div>
          <button type="button" class="primary" data-new-add>Add this action</button>
        </div>
      </div>`;
}

function wireNewActionChoice(box, root, target) {
  const opener = box.querySelector('[data-new-row]'), form = box.querySelector('[data-new-form]');
  if (!opener || !form) return;
  opener.onclick = () => {
    form.hidden = !form.hidden;
    if (!form.hidden) form.querySelector('.new-name')?.focus();
  };
  const name = form.querySelector('.new-name');
  if (name) name.oninput = () => { form.dataset.slug = name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); };
  form.querySelectorAll('.modifier').forEach(chip => {
    chip.onclick = () => {
      chip.classList.toggle('on');
      chip.setAttribute('aria-pressed', String(chip.classList.contains('on')));
    };
  });
  wireKeyCapture(form.querySelector('.new-key'));
  const add = form.querySelector('[data-new-add]');
  if (!add) return;
  add.onclick = async () => {
    const label = (name?.value || '').trim();
    const slug = (form.dataset.slug || '').replace(/[^a-z0-9_-]/g, '').slice(0, 64);
    const keys = MODIFIER_CHOICES.filter(([modifier]) => form.querySelector(`[data-modifier="${modifier}"]`)?.classList.contains('on')).map(([modifier]) => modifier);
    const typedField = form.querySelector('.new-key');
    const typed = typedField?.dataset.token || keyTokenFromText(typedField?.value || '') || '';
    if (typed) keys.push(typed);
    if (!label) { setStatus('error', 'Give the action a name first.'); return; }
    if (!/^[a-z]/.test(slug)) { setStatus('error', 'That name needs at least one letter to start its ID.'); return; }
    if (action(slug)) { setStatus('error', `“${label}” already exists — pick it from the list instead.`); return; }
    closePanelChoosers(root);
    // A dial's three rows share this list, so the row that opened it says which slot
    // a brand-new shortcut lands on.
    const openState = panelState(root);
    await target.createAction({label, slug, keys}, openState ? openState.openSlot : null);
  };
}

function fillIconChoices(box, root, target) {
  if (!box || !target) return;
  // A picture needs something to do on a menu button: with nothing assigned there is
  // no item for it to live on, so the row explains the gap instead of opening a picker
  // whose picks would go nowhere.  The deck's own key panel draws artwork on the key,
  // so its target says the picture is usable and this never comes up.
  const view = target.view() || {};
  if (!view.iconUsable) {
    box.innerHTML = '';
    noteInChoices(box, 'An icon goes on a button that does something. Assign one first.');
    return;
  }
  // The same picker the Menus page renders, from the same library, so the two panels
  // cannot drift apart.  Only the meaning of a pick differs, and that belongs to the
  // target: a picture on the deck's selected key, or on the button this panel is for.
  box.innerHTML = iconPickerHTML({selected: view.icon || null});
  if (!box.querySelector('.lib-pic')) noteInChoices(box, 'No pictures to show here - reload the page.');
  wireIconPicker(box, {
    onPick: async reference => {
      closePanelChoosers(root);
      await target.setIcon(reference);
    },
  });
}

// `quiet` is for the times an icon arrives *with* an action: the action's own message is
// the one worth reading, and "will be drawn on key 3" would talk over it.
async function pickIcon(reference, quiet = false) {
  if (targetIsSlot()) {
    const menuPage = page();
    if (!menuPage) return;
    const item = menuPage.items.find(entry => entry.key === selectedSlot);
    if (!item) { setStatus('error', 'Give this key a job first, then choose its icon.'); return; }
    const next = {...item};
    if (reference) next.icon = reference; else delete next.icon;
    if (await commit(() => setSlot(menuPage, next)) && !quiet) {
      setStatus('dirty', reference
        ? `“${artworkLabel(reference)}” will be drawn on key ${selectedSlot + 1}. Press Save to write it to the deck.`
        : `Key ${selectedSlot + 1} will print its word again. Press Save to write it to the deck.`);
    }
    return;
  }
  await assignArtwork(reference, quiet);
}

// ── The deck's own panel ────────────────────────────────────────────────────
// What the deck page answers for: the key selected on the deck's drawing, or - while
// a menu key is being edited - that slot of the page open in the nesting chain.  The
// reading is panelJob(), the words are panelView's, and the three writes are the ones
// the deck has always used.  Nothing here is a second implementation of anything: it
// is the same component as the Menus panel, told what to answer for.
function deckPanelToken() {
  if (targetIsDial()) return `dial:${dialControl()}`;
  if (targetIsCap()) return `cap:${capControl()}`;
  return targetIsSlot() ? `slot:${selectedMenu}:${selectedPage}:${selectedSlot}` : `key:${selectedKey}`;
}

// The card above the panel for a cap: the same card a deck key gets, told that
// this control has no screen, and named by where it sits on the deck.
function capCard(index, bound) {
  const spec = bound ? (action(bound) || readyMadeAction(bound)) : null;
  const detail = assignmentDetail(spec);
  const name = bound ? title(bound) : 'Nothing yet';
  return {
    number: String(index + 1).padStart(2, '0'),
    type: `Button ${index + 1}`,
    title: name,
    tone: keyTone(index),
    label: detail.label,
    value: detail.value,
    shape: 'button',
    aria: `Editing ${capName(index)}: ${name}. ${detail.label} ${detail.value}`,
  };
}

function deckPanelCard() {
  if (targetIsCap()) return capCard(selectedCap, (state.buttons || [])[selectedCap]);
  const bound = state.bindings[selectedKey];
  return controlCardFor('key', selectedKey, null, {label: title(bound), action: bound});
}

// The panel's answers for a dial.  A dial presses and turns two ways, so rather than
// one job it hands the panel a row per slot - the same panel, told how many there are.
function dialPanelCard() {
  const indices = dialSlotIndices(selectedDial);
  const used = indices.filter(position => dialSlotValue(DIAL_SLOTS[position])).length;
  const total = indices.length;
  const name = DIAL_TITLES[dialControl()];
  return {
    number: '◎',
    type: 'Dial',
    title: name,
    label: used ? 'Set' : 'Not set',
    value: used === total ? 'every slot in use'
      : used ? `${used} of ${total} slots in use`
      : 'nothing assigned yet',
    tone: 'tone-teal',
    shape: 'dial',
    aria: `${name}: its press and each way it turns.`,
  };
}

function dialPanelContextHTML() {
  const name = DIAL_TITLES[dialControl()];
  // The truth about a menu, in the words the deck actually follows.
  if (selectedDial === 1) return `${name} goes back a level whenever a menu is open, and its turn does nothing there.`;
  if (selectedDial === 0) return `While a menu is open, the ${name.toLowerCase()} keeps its assignment unless that menu claims the turn; with nothing assigned, it moves the menu instead.`;
  return `${name} stays yours, menu open or not.`;
}

async function pickDialSlot(slot, value, label) {
  const index = DIAL_SLOTS.indexOf(slot);
  if (index < 0) return;
  const kind = DIAL_SLOT_KINDS[index];
  const name = DIAL_TITLES[DIAL_SLOT_DIALS[index]];
  if (value && value.startsWith(MENU_OPEN_PREFIX)) {
    // Opening a menu is a key's job: a dial that enters one is taken over by it in the
    // same breath, which reads as the dial doing something else on the next turn.
    setStatus('error', 'A dial cannot open a menu: put that on one of the LCD keys.');
    return;
  }
  if (value && !action(value) && !readyMadeAction(value)) {
    setStatus('error', 'That action is not available.');
    return;
  }
  const dials = (state.dials || []).slice();
  dials[index] = value || null;
  // A ready-made shortcut is not in the draft until something uses it, and the deck
  // refuses a binding that names an action it does not have, so it is added here.
  const spec = value && !action(value) ? readyMadeAction(value) : null;
  const words = value
    ? `${name} — ${dialSlotWords(kind).toLowerCase()}: “${label || title(value)}”.`
    : `${name} — ${dialSlotWords(kind).toLowerCase()} does nothing now.`;
  if (await commit(() => {
    if (spec) state.actions.push(spec);
    state.dials = dials;
  })) {
    setStatus('dirty', `${words} Press Save to write it to the deck.`);
  }
}

function dialPanelTarget() {
  return {
    token: deckPanelToken(),
    card: dialPanelCard,
    view: () => panelView(panelJob()),
    thumbKey: () => null,
    contextHTML: dialPanelContextHTML,
    goBack: () => { selectedDial = null; render(); },
    // The rows the panel shows: each way the dial can be used, in the order it happens.
    slots: dialSlots,
    clearable: true,
    setAction: (value, label, slot) => pickDialSlot(slot, value, label),
    // The deck's own fallback for who asked, inside stageShortcut.
    askedBy: () => null,
    createAction: async ({label, slug, keys}, slot) => {
      const index = DIAL_SLOTS.indexOf(slot);
      if (index < 0) return;
      const created = actionFromSpec({id: slug, label, kind: 'keys', keys});
      const dials = (state.dials || []).slice();
      dials[index] = slug;
      if (await commit(() => { state.actions.push(created); state.dials = dials; })) {
        const name = DIAL_TITLES[DIAL_ORDER[Math.floor(index / DIAL_SLOT_KINDS.length)]];
        setStatus('dirty', `“${label}” now runs on ${name}. Press Save to write it to the deck.`);
      }
    },
  };
}

function deckPanelTarget() {
  // Answering for a dial is a different shape of answer, so the deck's selection
  // decides which one the panel is handed.
  if (targetIsDial()) return dialPanelTarget();
  return {
    token: deckPanelToken(),
    card: deckPanelCard,
    view: () => panelView(panelJob()),
    thumbKey: () => (targetIsCap() ? null : selectedKey),
    contextHTML: deckPanelContextHTML,
    goBack: () => { assignKind = 'key'; selectedCap = null; render(); },
    // A deck key is emptied by assigning it something else; a menu button and a
    // cap can be left with nothing at all, so only those offer the row.
    clearable: targetIsCap(),
    // The deck's own fallbacks for who asked, inside stageShortcut.
    askedBy: () => null,
    setAction: (value, label, slot, icon) => pickJob(value, label, slot, icon),
    setIcon: reference => (targetIsCap() ? null : pickIcon(reference)),
    // Held while pressed, or fired once.  A cap and an LCD key both keep this on the
    // assignment, so one action can be fired on one control and held on another.
    setMode: async hold => {
      const control = targetIsCap() ? capControl() : `key${selectedKey}`;
      const words = targetIsCap() ? capName(CAP_CONTROLS.indexOf(control)) : `Key ${Number(control.slice(3)) + 1}`;
      const chord = panelJob().keys;
      if (await commit(() => {
        forgetHeld(control);
        if (hold) state.held = [...(state.held || []), control];
      })) {
        setStatus('dirty', hold
          ? `${words} now holds ${shortcut(chord)} down until you let go. Press Save to write it to the deck.`
          : `${words} now taps ${shortcut(chord)} instead of holding it. Press Save to write it to the deck.`);
      }
    },
    createAction: async ({label, slug, keys}) => {
      const created = actionFromSpec({id: slug, label, kind: 'keys', keys});
      const write = () => {
        state.actions.push(created);
        if (targetIsCap()) {
          state.buttons = (state.buttons || [null, null, null]).slice();
          state.buttons[selectedCap] = slug;
        } else if (targetIsSlot()) {
          const menuPage = page();
          if (menuPage) setSlot(menuPage, {key: selectedSlot, label, action: slug});
        } else {
          state.bindings[selectedKey] = slug;
        }
      };
      if (await commit(write)) {
        setStatus('dirty', `“${label}” now runs ${shortcut(keys)}. Press Save to write it to the deck.`);
      }
    },
  };
}

// The deck's panel, mounted and painted on every render - unless the nesting chain has
// taken that column: the chain mounts this same component a second time, and only one
// of the two is ever showing.
function renderDeckButtonPanel() {
  const root = $('#deckPanel');
  if (!root) return;
  const chain = $('#fxButtonPanel');
  if (chain && !chain.hidden) { root.hidden = true; return; }
  mountButtonPanel(root, deckPanelTarget());
}

