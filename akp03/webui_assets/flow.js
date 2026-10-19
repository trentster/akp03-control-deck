/* Menus and the deck's nesting chain.

   The editor has two jobs and they now live in two places:

   * **Menus** is the workshop.  A plain list of the menus that exist; click one
     and it opens: a title, and up to six buttons.  A button either runs an
     action or opens another menu, which is how menus nest.  There are no pages.
   * **The Deck page** shows the same configuration running.  Press a key that
     opens a menu and the menu appears underneath it, joined by an arrow, with its
     buttons in order; press one of those and its sub-menu appears below that.
     A button that opens nothing simply opens its own controls on the right.

   Everything here edits the same draft through `commit()`, so nothing reaches
   the deck until Save to deck is pressed. */

let fxMenuName = null;      // the menu open on the Menus page
let fxSlotKey = null;       // the button selected on it
let fxCapKey = null;        // the button under the keys selected on it, if any
let fxDialControl = null;   // the dial selected on it, if any
// The dials a menu may take over, drawn as the deck draws them: one big knob above two
// small ones.  The left dial is here because a person needs to see that it is fixed and
// why, but nothing about it is settable while a menu is open.
const FX_DIALS = [
  {control: 'top', name: 'Big dial', short: 'Big', big: true},
  {control: 'bottom_left', name: 'Left dial', short: 'Left', big: false},
  {control: 'bottom_right', name: 'Right dial', short: 'Right', big: false},
];

// What a dial does while a menu is open, in the fewest words that are still true.  This
// is the sentence the drawing used to repeat under every slot, once per dial instead.
function fxDialsSaid(control, claims) {
  const taken = Object.keys(claims || {}).some(slot => slot === control || slot.startsWith(`${control}_turn_`));
  if (control === 'bottom_left') return 'Always goes back a level';
  if (taken) return 'This menu has taken it';
  return control === 'top' ? 'Turns to move the highlight, presses to select' : 'The deck’s own job';
}
// How an action reads in one line beside a slot, the same way the Deck page shows it.
function fxActionMeta(actionId) {
  const spec = actionId ? (action(actionId) || readyMadeAction(actionId)) : null;
  if (!spec) return '';
  if (spec.kind === 'keys') return shortcut(spec.keys);
  if (spec.kind === 'brightness') return `${spec.brightness}%`;
  return '';
}

// The parts of one dial, as this page understands them.  The big dial and the right dial
// are a menu's to take, part by part; the left dial's parts are stated, because nothing
// about it is settable while a menu is open - it is the way back out.
function fxDialParts(control) {
  if (control === 'bottom_left') {
    return {
      slots: [],
      fixed: [
        {slot: 'bottom_left', way: 'Press', value: 'Back one level', note: 'Always, while a menu is open.'},
        {slot: 'bottom_left_turn_left', way: 'Turn left', value: 'Nothing', note: 'A menu keeps its own turns while it is open.'},
        {slot: 'bottom_left_turn_right', way: 'Turn right', value: 'Nothing', note: 'A menu keeps its own turns while it is open.'},
      ],
      context: 'The left dial is the way back out of a menu, so nothing on it is settable while one is open.',
    };
  }
  return {
    slots: [
      {slot: control, way: 'Press'},
      {slot: `${control}_turn_left`, way: 'Turn left'},
      {slot: `${control}_turn_right`, way: 'Turn right'},
    ],
    fixed: [],
    context: control === 'top'
      ? 'Set nothing here and the big dial keeps moving the highlight and pressing to select.'
      : 'Set nothing here and the right dial keeps the job the Deck page gives it.',
  };
}

let fxDeep = [];            // buttons pressed below the deck's key: [{key: n}, …]
let fxChainButton = null;   // a chain button that opens nothing, shown on the right
let fxChainClosed = false;  // the chain was put away, so the live mirror shows instead
let fxChainKey = null;      // which deck key the chain belongs to
// Which row of which panel is open in its editors is the panel's own business now:
// the component keeps that state per mount, so a render here cannot close an editor
// that belongs to another panel.
let fxNewMenuOpen = false;  // the inline "new menu" form
let fxRenaming = null;      // the menu whose title is being edited in place
let fxDeleting = null;      // the menu whose deletion is being confirmed
let fxWired = false;

// ── reading the draft ──────────────────────────────────────────────────────
function fxMenus() { return (state && state.menus) || []; }
function fxMenu(name) { return fxMenus().find(menu => menu.name === name) || null; }
function fxItems(menu) { return (menu && menu.items) || []; }
function fxItem(menu, key) { return fxItems(menu).find(item => item.key === key) || null; }
function fxMenuByKey(index) {
  const id = state.bindings[index];
  const candidate = id ? action(id) : null;
  return candidate && candidate.kind === 'menu' ? candidate.menu : null;
}
function fxKeysOpening(name) {
  return state.bindings
    .map((id, index) => (id && fxMenuByKey(index) === name ? index + 1 : null))
    .filter(Boolean);
}
// What a button's face shows, and the reading the panel used to do for itself, both
// come from the panel component now: a menu button's label, shortcut and picture are
// read once, by panelView, for every panel.
function fxPicHTML(reference, size) {
  if (!reference) return '';
  return `<img src="${esc(artworkPreviewSrc(reference, 0))}" alt=""${size ? ` width="${size}" height="${size}"` : ''}>`;
}

// ── the Menus page ─────────────────────────────────────────────────────────
const FX_KEYS = [0, 1, 2, 3, 4, 5];

function renderMenusFlow() {
  const host = $('#fxMenusRoot');
  if (!host) return;
  const menus = fxMenus();
  if (!fxMenu(fxMenuName)) { fxMenuName = menus.length ? menus[0].name : null; fxSlotKey = null; fxCapKey = null; fxDialControl = null; }
  // Opening a menu lands on its first button, so the panel on the right says
  // something instead of asking for a click first.  Not while a button under the keys
  // is the one being edited, though: that choice is the person's.
  if (fxSlotKey === null && fxCapKey === null && fxMenu(fxMenuName)) {
    const first = fxItems(fxMenu(fxMenuName))[0];
    if (first) fxSlotKey = first.key;
  }
  host.innerHTML = `
    <aside class="fx-list">
      ${fxNewMenuOpen ? `<form class="fx-new-form" data-fx-newmenuform>
          <input id="fxNewMenuTitle" type="text" maxlength="32" placeholder="Menu name, e.g. Youtube" autocomplete="off">
          <div class="fx-tools"><button type="submit" class="fx-pick on">Add menu</button>
          <button type="button" class="fx-pick" data-fx-cancelnew>Cancel</button></div>
        </form>`
        : '<button class="fx-new-menu" type="button" data-fx-newmenu><span class="fx-plus">＋</span>New menu</button>'}
      <div class="fx-rows">
        ${menus.map(menu => `<button class="fx-menu-entry${menu.name === fxMenuName ? ' on' : ''}" data-fx-menu="${esc(menu.name)}">
          <span class="fx-menu-glyph" aria-hidden="true">${'<i></i>'.repeat(FX_KEYS.length)}</span>
          <span class="fx-menu-name">${esc(menu.title)}</span><span class="fx-chev">›</span></button>`).join('')
          || '<p class="fx-nothing">No menus yet.</p>'}
      </div>
    </aside>
    <div class="fx-canvas">${fxCanvasHTML()}</div>
    <aside class="fx-insp">${fxInspectorHTML()}</aside>`;
  fxMountMenusPanel();
  fxMountDeck(fxMenu(fxMenuName));
  wireFlow();
}

function fxCanvasHTML() {
  const menu = fxMenu(fxMenuName);
  if (!menu) {
    return `<div class="fx-empty">Nothing to design yet. Add a menu on the left — it starts empty, and you give
      each of its buttons something to do. A button can run an action, or open another menu.</div>`;
  }
  const keys = fxKeysOpening(menu.name);
  const reach = keys.length
    ? `On <b>key ${keys.join(' and key ')}</b> of the deck`
    : 'Not on a deck key yet';
  const count = fxItems(menu).length;
  const heading = fxRenaming === menu.name
    ? `<form class="fx-rename" data-fx-renameform="${esc(menu.name)}">
         <input id="fxRenameTitle" type="text" maxlength="32" value="${esc(menu.title)}" autocomplete="off">
         <button class="fx-kebab is-primary" type="submit">Save name</button>
         <button class="fx-kebab" type="button" data-fx-cancelrename>Cancel</button>
       </form>`
    : `<h1 class="fx-title">${esc(menu.title)}</h1>`;
  const tools = fxRenaming === menu.name ? '' : `<div class="fx-menu-tool">
        <button class="fx-kebab" type="button" data-fx-renamemenu="${esc(menu.name)}">Rename…</button>
        <button class="fx-kebab" type="button" data-fx-deletemenu="${esc(menu.name)}">Delete</button>
      </div>`;
  // Deleting a menu is asked about in the page, not in a browser dialog: the
  // consequences are worth reading before agreeing to them.
  const confirming = fxDeleting === menu.name ? `
    <div class="fx-confirm">
      <div><b>Delete the menu “${esc(menu.title)}”?</b>
      <p>Any button that opened it is removed, and any deck key that opened it is left empty.
        This is staged until you press Save to deck.</p></div>
      <button class="fx-kebab fx-danger" type="button" data-fx-confirmdelete="${esc(menu.name)}">Delete this menu</button>
      <button class="fx-kebab" type="button" data-fx-canceldelete>Keep it</button>
    </div>` : '';
  return `
    <div class="fx-head">
      <div>${heading}<p class="fx-meta">${reach}</p></div>
      ${tools}
    </div>
    ${confirming}
    <div class="fx-deckmount" id="fxDeckMount"></div>`;
}

// What a button's face will show on the deck: its picture when it has one, and
// otherwise the label the deck itself draws onto the tile.  Same rule, and asked
// of the deck's own renderer, so the canvas previews the real thing instead of
// guessing at it - a menu button with no artwork used to show no picture at all.
function fxFaceSrc(item, key) {
  if (!item) return null;
  if (item.icon) return artworkPreviewSrc(item.icon, key);
  return item.label ? `/api/label/${key}.png?text=${encodeURIComponent(item.label)}` : null;
}

// What the drawing needs to know about this menu, and nothing more: the faces its six
// keys show, and whether this menu has taken a button or a dial over.  The deck itself
// is drawn by deckdraw.js, the same drawing the Deck page uses.
function fxDeckView(menu) {
  const claims = menu.controls || [];
  const dialClaims = menu.dials || {};
  return {
    aria: 'The deck while this menu is open',
    labels: true,
    capsLabel: 'The three buttons under the LCD keys, while this menu is open',
    dialsLabel: 'The dials, while this menu is open',
    faces: FX_KEYS.map(key => {
      const item = fxItem(menu, key);
      return {
        art: fxFaceSrc(item, key),
        label: item ? item.label : 'Empty',
        badge: item && item.menu ? 'menu \u25b8' : '',
        empty: !item,
        selected: fxSlotKey === key,
        title: item ? item.label : 'Empty',
      };
    }),
    caps: CAP_CONTROLS.map((control, index) => {
      const claim = claims[index] || null;
      const said = claim
        ? `${title(claim.action)}${claim.mode === 'hold' ? ', held while pressed' : ''}`
        : 'its ordinary deck job';
      return {
        claimed: Boolean(claim),
        selected: fxCapKey === index,
        title: `${capName(index)}: ${said}`,
        aria: `${capName(index)}: ${said}`,
      };
    }),
    dials: FX_DIALS.map(entry => {
      const said = fxDialsSaid(entry.control, dialClaims);
      return {
        claimed: Object.prototype.hasOwnProperty.call(dialClaims, entry.control),
        selected: fxDialControl === entry.control,
        title: `${entry.name}: ${said}`,
        aria: `${entry.name}: ${said}`,
      };
    }),
  };
}

function fxMountDeck(menu) {
  const mount = $('#fxDeckMount');
  if (!mount || !menu) return;
  mountDeckDrawing(mount, fxDeckView(menu));
}

function fxInspectorHTML() {
  const menu = fxMenu(fxMenuName);
  if (!menu) return '<div class="fx-empty">Add a menu to design its buttons.</div>';
  if (fxSlotKey === null && fxCapKey === null && fxDialControl === null) {
    return '<div class="fx-empty">Select a button to give it something to do, one of the buttons under the keys to say what this menu does with it, or a dial to say what it does while this menu is open.</div>';
  }
  // The panel is the deck's own component, mounted here: it builds its own markup, so
  // all this leaves behind is the place it lives.
  return '<div class="key-panel" data-fx-menupanel></div>';
}

// ── The Menus page's panel ─────────────────────────────────────────────────
// The same component the deck mounts, told what to answer for: one button of the menu
// being designed.  Its reading is the button's own - its label is the words it was
// given, not the name of the menu it opens - and its three writes land on that button.
function menusButtonTarget(menuName, key) {
  const item = () => {
    const menu = fxMenu(menuName);
    return menu ? fxItem(menu, key) : null;
  };
  const job = () => {
    const button = item();
    const spec = button && button.action ? (action(button.action) || readyMadeAction(button.action)) : null;
    const keys = spec && spec.kind === 'keys' ? spec.keys : null;
    return {
      value: button ? (button.menu ? `__menu__:${button.menu}` : (button.action || null)) : null,
      label: button ? button.label : 'Nothing yet',
      current: button ? button.label : null,
      meta: keys ? shortcut(keys) : (spec && spec.kind === 'brightness' ? `${spec.brightness}%` : ''),
      icon: (button && button.icon) || null,
      actionId: (button && button.action) || null,
      noun: 'button',
      // A menu button's picture lives on its assignment, so a button with nothing
      // assigned has nowhere to keep one - the panel says that instead of offering
      // a picker that could not save anything.
      iconUsable: Boolean(button),
    };
  };
  return {
    token: `menu:${menuName}:${key}`,
    card: () => controlCardFor('menu-button', key, menuName, item()),
    view: () => panelView(job()),
    thumbKey: () => key,
    // A menu button can be left with nothing at all: it is not a key on the deck, so
    // this panel offers the row that empties it.
    clearable: true,
    // Told which button asked, so a shortcut change lands on this button's action and
    // never on whatever the deck's own panel happens to have selected.
    askedBy: () => ({label: (item() || {}).label}),
    setAction: async (value, label, slot, icon) => {
      if (!value) return fxClearButton(menuName, key);
      if (value.startsWith('__menu__:')) return fxSetButtonMenu(menuName, key, value.slice('__menu__:'.length));
      // The list drew a picture beside this action, so the button takes the picture
      // too: pick "Undo" and you get Undo's keys *and* Undo's icon in one click.
      await fxSetButtonAction(menuName, key, value);
      if (icon) await fxSetIcon(menuName, key, icon);
    },
    setIcon: reference => fxSetIcon(menuName, key, reference),
    // A menu button's Delete takes the button off the menu.  It must never reach the deck
    // file's action list: "copy" is a ready-made shortcut used by keys and by other menu
    // buttons, and Mark pressed Delete wanting the button gone, not the action.
    clearAssignment: () => fxClearButton(menuName, key),
    createAction: async ({label, slug, keys}) => {
      const created = actionFromSpec({id: uniqueId(slug), label, kind: 'keys', keys});
      if (!(await commit(() => state.actions.push(created)))) return;
      await fxSetButtonAction(menuName, key, created.id);
    },
  };
}

// What this menu does with one of the three buttons under the keys while it is open.
// The claim lives on the menu, not on the button, so the panel is told which menu and
// which button; a slot with nothing in it means the deck leaves that button its own job.
function menusCapTarget(menuName, index) {
  const claim = () => {
    const menu = fxMenu(menuName);
    return (menu && (menu.controls || [])[index]) || null;
  };
  const job = () => {
    const taken = claim();
    const spec = taken ? (action(taken.action) || readyMadeAction(taken.action)) : null;
    const keys = spec && spec.kind === 'keys' ? spec.keys : null;
    return {
      value: taken ? taken.action : null,
      label: taken ? title(taken.action) : 'Its own job',
      current: taken ? title(taken.action) : null,
      meta: keys ? shortcut(keys) : (spec && spec.kind === 'brightness' ? `${spec.brightness}%` : ''),
      icon: null,
      actionId: taken ? taken.action : null,
      noun: 'button',
      // A button under the keys has no screen, so nothing offers it a label or a picture.
      screen: false,
      held: Boolean(taken && taken.mode === 'hold'),
      canHold: Boolean(keys && keys.length),
    };
  };
  return {
    token: `menu-cap:${menuName}:${index}`,
    card: () => {
      const taken = claim();
      const detail = taken
        ? assignmentDetail(action(taken.action) || readyMadeAction(taken.action))
        : {label: 'Nothing of its own', value: ''};
      const name = taken ? title(taken.action) : 'Its own job';
      return {
        number: String(index + 1).padStart(2, '0'),
        type: `Button ${index + 1}`,
        title: name,
        tone: keyTone(index),
        label: detail.label,
        value: detail.value,
        shape: 'button',
        aria: `Editing ${capName(index)} for this menu: ${name}. ${detail.label} ${detail.value}`,
      };
    },
    view: () => panelView(job()),
    thumbKey: () => null,
    // A claim can be emptied: the button then goes back to its ordinary deck job.
    clearable: true,
    askedBy: () => ({label: capName(index)}),
    setAction: (value, label) => (value ? fxClaimAction(menuName, index, value) : fxSetClaim(menuName, index, null)),
    setIcon: () => null,
    setMode: hold => fxClaimMode(menuName, index, hold),
    createAction: async ({label, slug, keys}) => {
      const created = actionFromSpec({id: uniqueId(slug), label, kind: 'keys', keys});
      if (!(await commit(() => state.actions.push(created)))) return;
      await fxSetClaim(menuName, index, {action: created.id, mode: 'tap'});
    },
  };
}

// The panel for a dial on the Menus page, beside the same panel a menu's buttons use.
// It answers for the whole dial - one row per part, in the order the parts happen - so
// the drawing can stay a drawing and the words can live where the setting is made.
function menusDialTarget(menuName, control) {
  const entry = FX_DIALS.find(candidate => candidate.control === control) || FX_DIALS[0];
  const dial = fxDialParts(entry.control);
  const claim = slot => (((fxMenu(menuName) || {}).dials || {})[slot] || null);
  const job = () => {
    const used = dial.slots.filter(part => claim(part.slot)).length;
    return {
      value: used ? `${used} of ${dial.slots.length} set` : null,
      label: used ? 'Set' : 'Not set',
      current: used ? `${used} of ${dial.slots.length} set` : null,
      meta: '',
      icon: null,
      actionId: null,
      noun: 'dial',
      screen: false,
      held: false,
      canHold: false,
    };
  };
  return {
    token: `menu-dial:${menuName}:${entry.control}`,
    card: () => {
      const used = dial.slots.filter(part => claim(part.slot)).length;
      return {
        number: '◎',
        type: 'Dial',
        title: entry.name,
        tone: 'tone-teal',
        label: used ? 'Set' : 'Not set',
        value: used === dial.slots.length ? `every part set (${dial.slots.length})`
          : used ? `${used} of ${dial.slots.length} parts set`
          : dial.slots.length ? 'nothing set yet' : 'nothing to set',
        shape: 'dial',
        aria: `${entry.name}, while this menu is open: ${fxDialsSaid(entry.control, (fxMenu(menuName) || {}).dials || {})}.`,
      };
    },
    view: () => panelView(job()),
    thumbKey: () => null,
    contextHTML: () => `<p class="panel-context">${esc(dial.context)}</p>`,
    // One row per part of this dial.  The left dial's parts are stated rather than
    // offered: it is the way back out of a menu, so there is nothing here to set.
    slots: () => [
      ...dial.slots.map(part => {
        const actionId = claim(part.slot);
        return {
          slot: part.slot,
          label: part.way,
          hint: `Choose what this menu puts on ${entry.name.toLowerCase()}, ${part.way.toLowerCase()}, while it is open.`,
          value: actionId ? title(actionId) : '',
          meta: fxActionMeta(actionId),
        };
      }),
      ...dial.fixed.map(part => ({
        slot: part.slot,
        label: part.way,
        fixed: true,
        value: part.value,
        note: part.note,
      })),
    ],
    // A claim can be emptied: that part of the dial then goes back to its ordinary job.
    clearable: true,
    askedBy: () => ({label: entry.name}),
    setAction: (value, label, slot) => (value ? fxClaimDialAction(menuName, slot, value) : fxSetDialClaim(menuName, slot, null)),
    setIcon: () => null,
    setMode: () => null,
    createAction: async ({label, slug, keys}) => {
      const created = actionFromSpec({id: uniqueId(slug), label, kind: 'keys', keys});
      if (!(await commit(() => state.actions.push(created)))) return;
      await fxSetDialClaim(menuName, dial.slots.length ? dial.slots[0].slot : entry.control, created.id);
    },
  };
}

// A claim is one of the menu's three slots: index 0, 1, 2 for left, middle, right.
async function fxSetClaim(menuName, index, slot) {
  await commit(() => {
    const menu = fxMenu(menuName);
    if (!menu) return;
    const claims = (menu.controls || [null, null, null]).slice();
    claims[index] = slot;
    menu.controls = claims;
  });
  render();
}

// What a menu takes of a dial is a table of slot -> action, and a slot left out of it is
// a part of the dial that keeps its ordinary job.  Writing null empties the slot, which
// is how a claim is given back.
async function fxSetDialClaim(menuName, slot, actionId) {
  await commit(() => {
    const menu = fxMenu(menuName);
    if (!menu) return;
    const claims = Object.assign({}, menu.dials || {});
    if (actionId) claims[slot] = actionId; else delete claims[slot];
    menu.dials = claims;
  });
  render();
}

async function fxClaimDialAction(menuName, slot, specId) {
  const actionId = await fxEnsureAction(specId);
  if (!actionId) { setStatus('error', 'That action could not be created.'); return; }
  await fxSetDialClaim(menuName, slot, actionId);
}

async function fxClaimAction(menuName, index, specId) {
  const actionId = await fxEnsureAction(specId);
  if (!actionId) { setStatus('error', 'That action could not be created.'); return; }
  // A fresh claim fires once; the panel's Response row is how it is held instead.
  await fxSetClaim(menuName, index, {action: actionId, mode: 'tap'});
}

async function fxClaimMode(menuName, index, hold) {
  const claim = (fxMenu(menuName) || {}).controls?.[index];
  if (!claim) return;
  await fxSetClaim(menuName, index, {action: claim.action, mode: hold ? 'hold' : 'tap'});
}

// Mounted after the markup above is in the page, because that is where the panel lives.
function fxMountMenusPanel() {
  const root = document.querySelector('#fxMenusRoot [data-fx-menupanel]');
  if (!root) return;
  mountButtonPanel(root, fxSlotKey !== null && fxCapKey === null && fxDialControl === null
    ? menusButtonTarget(fxMenuName, fxSlotKey)
    : fxCapKey !== null
      ? menusCapTarget(fxMenuName, fxCapKey)
      : fxDialControl !== null
        ? menusDialTarget(fxMenuName, fxDialControl)
        : menusButtonTarget(fxMenuName, fxSlotKey));
}

// The panel that answers for a menu button is the deck's own component, mounted twice:
// once in the Menus page's inspector and once in the column under the deck's chain.
// Both mounts are menusButtonTarget() told which button to answer for, so the two pages
// speak the same words in the same order by construction rather than by care.
// The shortcut editor, the tools strip and the picture picker are the component's own
// now: it fills them where they live, for whichever button it is answering for.  The
// page supplies only what a pick or a shortcut *means*, through its target.


// ── the deck's chain ───────────────────────────────────────────────────────
function fxChainPlan() {
  const levels = [];
  let plain = null;
  if (fxChainClosed) return {levels, plain};
  const path = [selectedKey, ...fxDeep.map(step => step.key)];
  let menuName = fxMenuByKey(path[0]);
  if (!menuName) return {levels, plain};
  for (let depth = 1; depth < path.length + 1; depth += 1) {
    const menu = fxMenu(menuName);
    if (!menu) break;
    levels.push({menu, key: path[depth - 1], depth});
    if (depth === path.length) break;
    const item = fxItem(menu, path[depth]);
    if (!item || !item.menu) { plain = {menu: menu.name, key: path[depth]}; break; }
    menuName = item.menu;
  }
  return {levels, plain};
}

function renderDeckChain() {
  const host = $('#fxChain');
  if (!host) return;
  const {levels, plain} = fxChainPlan();
  fxChainButton = plain;
  const mirror = $('#deckMirrorPanel');
  const deckStage = $('#deck .stage');
  // Choosing a different key brings the chain back: putting it away applies to
  // the key you were looking at, not to every key from then on.
  if (selectedKey !== fxChainKey) { fxChainKey = selectedKey; fxChainClosed = false; }
  // Put away (or nothing to show): the page is exactly the default deck view.
  if (!levels.length) {
    host.hidden = true;
    host.innerHTML = '';
    if (mirror) mirror.hidden = false;
    if (deckStage) deckStage.classList.remove('fx-chain-open');
    fxHideButtonPanel();
    return;
  }
  host.hidden = false;
  if (mirror) mirror.hidden = true;
  if (deckStage) deckStage.classList.add('fx-chain-open');
  const chips = ['Deck', ...levels.map(level => level.menu.title)];
  host.innerHTML = `
    <div class="fx-path">
      ${chips.map((label, index) => `${index ? '<span class="fx-step-sep">›</span>' : ''}
        <button class="fx-step${index === chips.length - 1 ? ' on' : ''}" type="button" data-fx-step="${index}">${esc(label)}</button>`).join('')}
      <span class="fx-hint" style="margin-left:auto">press a button that opens a menu and it appears below</span>
    </div>
    <div class="fx-chain">
      ${levels.map((level, index) => `${fxArrowHTML(index === 0 ? `key ${level.key + 1} opens ${esc(level.menu.title)}` : `key ${level.key + 1} opens ${esc(level.menu.title)}`, index)}
        ${fxLevelHTML(level, index, levels)}`).join('')}
    </div>`;
  fxPaintChain();
  fxPositionArrows(levels);
  // The right-hand side answers for whatever was pressed last.
  if (plain) fxShowButtonPanel(plain.menu, plain.key);
  else fxHideButtonPanel();
}

function fxArrowHTML(caption, index) {
  return `<div class="fx-arrow" data-fx-arrow="${index}">
    <div class="fx-elbow" data-fx-elbow="${index}"><i></i><u></u></div>
    <span class="fx-cap" data-fx-cap="${index}">${caption}</span></div>`;
}

function fxLevelHTML(level, index, levels) {
  const items = fxItems(level.menu);
  const count = items.length;
  const next = levels[index + 1];
  return `<div class="fx-level" data-fx-level="${index}">
    <div class="fx-lvlhead">
      <span class="fx-dot"></span>
      <span class="fx-who">${esc(level.menu.title)}</span>
      <span class="fx-tag by">opened by key ${level.key + 1}</span>
      <span class="fx-tag">${count} ${count === 1 ? 'button' : 'buttons'}${count ? '' : ' · blank keys'}</span>
      <span class="fx-spacer"></span>
      <button class="fx-link" type="button" data-fx-edit="${esc(level.menu.name)}">Edit in Menus</button>
      <button class="fx-back" type="button" data-fx-back="${index}">← back</button>
    </div>
    <div class="fx-order">
      ${FX_KEYS.map(key => fxSlotHTML(level.menu, key, next && next.key === key)).join('')}
    </div>
  </div>`;
}

function fxSlotHTML(menu, key, leadsDeeper) {
  const item = fxItem(menu, key);
  // The highlight belongs to the level that owns the selection.  Matching on the
  // button number alone lit up the same number in the level above: selecting
  // Playback's button 2 highlighted Youtube's button 2 (Undo).  A level above marks
  // the one item that leads into the open level, in a softer tint - it is the way
  // through, not the button you are on.
  const selected = Boolean(fxChainButton && fxChainButton.menu === menu.name && fxChainButton.key === key);
  const on = selected ? ' on' : (leadsDeeper ? ' on via' : '');
  if (!item) {
    return `<button class="fx-slot empty${on}" type="button" data-fx-slot="${menu.name}:${key}">
      <span class="fx-idx">${key + 1}</span><span class="fx-name">Empty</span></button>`;
  }
  // The picture the deck will paint on this key: the assigned icon when there is
  // one, and otherwise the words the deck draws on the tile - the same face a
  // top-level key shows, so a menu button reads the same way at either level.
  const face = fxFaceSrc(item, key);
  const pic = face ? `<span class="fx-pic"><img src="${esc(face)}" alt="" width="40" height="40"></span>` : '';
  return `<button class="fx-slot${on}" type="button" data-fx-slot="${menu.name}:${key}">
    <span class="fx-idx">${key + 1}</span>
    ${item.menu ? '<span class="fx-opens">menu ▸</span>' : ''}
    ${pic}
    <span class="fx-name">${esc(item.label)}</span></button>`;
}

// Arrows are drawn from the control that was pressed, not from the edge of a card:
// the elbow is placed under that control's own centre once layout has happened.
function fxPositionArrows(levels) {
  requestAnimationFrame(() => {
    levels.forEach((level, index) => {
      const arrow = document.querySelector(`[data-fx-arrow="${index}"]`);
      const elbow = document.querySelector(`[data-fx-elbow="${index}"]`);
      const cap = document.querySelector(`[data-fx-cap="${index}"]`);
      if (!arrow || !elbow) return;
      const source = index === 0
        ? document.querySelector(`#deckKeys button[data-key="${level.key}"]`)
        : document.querySelector(`[data-fx-level="${index - 1}"] [data-fx-slot$=":${level.key}"]`);
      const bounds = arrow.getBoundingClientRect();
      const anchor = source ? source.getBoundingClientRect() : {left: bounds.left + bounds.width / 2, width: 0};
      const x = anchor.left + anchor.width / 2 - bounds.left;
      elbow.style.left = `${Math.max(8, Math.min(bounds.width - 8, x))}px`;
      if (cap) cap.style.left = `${Math.max(8, Math.min(bounds.width - 120, x + 12))}px`;
    });
  });
}

function fxPaintChain() {
  // Nothing to paint: the chain is plain DOM.  Kept as the one place a future
  // live mirror (what the deck is actually showing) would hook in.
}

// ── the right-hand side while a chain is open ──────────────────────────────
// The chain's panel is the same component again, mounted on the button that was pressed
// last.  The deck's own panel gives that column up while the chain is showing: they are
// one component in two mounts, and only one of them is ever visible.
function fxShowButtonPanel(menuName, key) {
  const deckPanel = $('#deckPanel');
  if (deckPanel) deckPanel.hidden = true;
  mountButtonPanel($('#fxButtonPanel'), menusButtonTarget(menuName, key));
}

function fxHideButtonPanel() {
  unmountButtonPanel($('#fxButtonPanel'));
}

// ── changes to the draft ───────────────────────────────────────────────────
function fxLabelForAction(actionId) {
  const candidate = action(actionId);
  const words = candidate ? title(actionId) : title(actionId);
  return String(words).slice(0, 32) || 'Button';
}

async function fxEnsureAction(specId) {
  const existing = state.actions.find(candidate => candidate.id === specId);
  if (existing) return existing.id;
  const spec = readyMadeAction(specId);
  if (!spec) return null;
  const fresh = {...spec, id: uniqueId(specId)};
  state.actions.push(fresh);
  return fresh.id;
}

async function fxSetButtonAction(menuName, key, specId) {
  const actionId = await fxEnsureAction(specId);
  if (!actionId) { setStatus('error', 'That action could not be created.'); return; }
  await commit(() => {
    const menu = fxMenu(menuName);
    if (!menu) return;
    const item = fxItem(menu, key);
    if (item) { item.action = actionId; delete item.menu; item.label = fxLabelForAction(actionId); return; }
    menu.items = [...fxItems(menu), {key, label: fxLabelForAction(actionId), action: actionId}];
  });
  render();
}

// A menu button is removed by taking it off the menu.  This used to delete the item's
// label, action, menu and icon and leave the item itself behind, which is not a state the
// deck file accepts: the parser wants every button to have a label, and the editor wants
// every button to either run an action or open a menu.  So "Nothing yet" left the menu
// invalid, and the next thing Mark touched got the refusal instead of the change.
//
// The item simply goes.  The menu pads its own keys, so a key with no item is an empty
// key - which is what a cleared button is.
async function fxClearButton(menuName, key) {
  await commit(() => {
    const menu = fxMenu(menuName);
    if (!menu) return;
    menu.items = fxItems(menu).filter(item => item.key !== key);
  });
  render();
}

async function fxSetButtonMenu(menuName, key, target) {
  await commit(() => {
    const menu = fxMenu(menuName);
    const targetMenu = fxMenu(target);
    if (!menu || !targetMenu) return;
    const label = String(targetMenu.title).slice(0, 32);
    const item = fxItem(menu, key);
    if (item) { item.menu = target; delete item.action; item.label = label; return; }
    menu.items = [...fxItems(menu), {key, label, menu: target}];
  });
  render();
}


async function fxSetIcon(menuName, key, reference) {
  await commit(() => {
    const menu = fxMenu(menuName);
    const item = menu ? fxItem(menu, key) : null;
    if (!item) return;
    if (reference) item.icon = reference; else delete item.icon;
  });
  render();
}

async function fxCreateMenu(name) {
  const title = String(name || '').trim().slice(0, 32);
  if (!title) return;
  const taken = new Set(fxMenus().map(menu => menu.name));
  let slug = slugify(title);
  let candidate = slug, number = 2;
  while (taken.has(candidate)) candidate = `${slug.slice(0, 28)}-${number++}`;
  fxMenuName = candidate;
  fxSlotKey = 0;
  fxCapKey = null; fxDialControl = null;
  fxNewMenuOpen = false;
  await commit(() => { state.menus = [...fxMenus(), {name: candidate, title, items: []}]; });
  render();
}

async function fxRenameMenu(name, title) {
  const trimmed = String(title || '').trim().slice(0, 32);
  fxRenaming = null;
  if (!trimmed) { render(); return; }
  await commit(() => { const target = fxMenu(name); if (target) target.title = trimmed; });
  render();
}

// Deleting a menu has to leave the configuration valid: the buttons that opened
// it go, the actions that pointed at it go, and any deck key using one of those
// actions goes back to being unassigned.  A dangling reference would save as an
// invalid config, so it is cleaned up here rather than at save time.
async function fxDeleteMenu(name) {
  const menu = fxMenu(name);
  if (!menu) return;
  fxDeleting = null;
  const doomed = state.actions.filter(candidate => candidate.kind === 'menu' && candidate.menu === name).map(candidate => candidate.id);
  await commit(() => {
    state.menus = fxMenus().filter(candidate => candidate.name !== name).map(candidate => ({
      ...candidate,
      items: fxItems(candidate).filter(item => item.menu !== name),
    }));
    state.actions = state.actions.filter(candidate => !doomed.includes(candidate.id));
    state.bindings = state.bindings.map(id => (doomed.includes(id) ? null : id));
  });
  fxMenuName = fxMenus().length ? fxMenus()[0].name : null;
  fxSlotKey = fxDialControl = null;
  fxCapKey = null;
  fxDeep = [];
  render();
}

async function fxTryAction(actionId) {
  try {
    const response = await fetch('/api/action/test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action: actionId}),
    });
    const reply = await response.json();
    if (reply && reply.ok) {
      setStatus('saved', `“${title(actionId)}” was sent to macOS. Nothing happened on screen? A shortcut macOS keeps for itself never fires from a deck — the deck is still working.`);
    } else {
      setStatus('error', `“${title(actionId)}” was not sent: ${(reply && reply.reason) || 'the daemon gave no reason'}`);
    }
  } catch (error) {
    setStatus('error', `The daemon could not be asked to try that action: ${error}`);
  }
}

// ── wiring ─────────────────────────────────────────────────────────────────
// Pressing a key on the drawing always asks that key what it opens: putting the
// chain away is a statement about the key you were looking at, not a setting.
document.addEventListener('click', event => {
  const key = event.target.closest && event.target.closest('#deckKeys button[data-key]');
  if (!key) return;
  fxChainClosed = false;
  fxDeep = [];
}, true);

function wireFlow() {
  const menus = $('#fxMenusRoot');
  if (menus && !menus.dataset.fxWired) {
    menus.dataset.fxWired = '1';
    menus.addEventListener('click', fxMenusClick);
    menus.addEventListener('submit', event => {
      event.preventDefault();
      const field = $('#fxNewMenuTitle');
      if (event.target.matches('[data-fx-newmenuform]')) { fxCreateMenu(field ? field.value : ''); return; }
      const renaming = event.target.closest('[data-fx-renameform]');
      if (renaming) { fxRenameMenu(renaming.dataset.fxRenameform, $('#fxRenameTitle').value); }
    });
  }
  const chain = $('#fxChain');
  if (chain && !chain.dataset.fxWired) {
    chain.dataset.fxWired = '1';
    chain.addEventListener('click', fxChainClick);
  }
  // The panels wire themselves: each mount handles its own clicks and its own picture
  // picker, so this page never has to know which panel a pick came from.
  fxWired = true;
}

function fxMenusClick(event) {
  const hit = target => event.target.closest(target);
  // Everything inside the panel - Edit, the three rows, their lists and the tools -
  // belongs to the panel component, which handles its own clicks.
  const menuRow = hit('[data-fx-menu]');
  if (menuRow) { fxMenuName = menuRow.dataset.fxMenu; fxSlotKey = null; fxCapKey = null; fxDialControl = null; render(); return; }
  if (hit('[data-fx-newmenu]')) { fxNewMenuOpen = true; render(); const field = $('#fxNewMenuTitle'); if (field) field.focus(); return; }
  if (hit('[data-fx-cancelrename]')) { fxRenaming = null; render(); return; }
  if (hit('[data-fx-canceldelete]')) { fxDeleting = null; render(); return; }
  const confirmDelete = hit('[data-fx-confirmdelete]');
  if (confirmDelete) { fxDeleteMenu(confirmDelete.dataset.fxConfirmdelete); return; }
  if (hit('[data-fx-cancelnew]')) { fxNewMenuOpen = false; render(); return; }
  // The drawing carries the same data attributes everywhere it is mounted.
  const face = hit('#fxDeckMount [data-key]');
  // One control at a time: choosing a tile is choosing away from a button under them.
  if (face) { fxSlotKey = +face.dataset.key; fxCapKey = null; fxDialControl = null; render(); return; }
  const cap = hit('#fxDeckMount [data-cap]');
  if (cap) { fxCapKey = +cap.dataset.cap; fxSlotKey = null; fxDialControl = null; render(); return; }
  const dial = hit('#fxDeckMount [data-dial]');
  if (dial) { fxDialControl = dial.dataset.control; fxSlotKey = null; fxCapKey = null; render(); return; }
  const rename = hit('[data-fx-renamemenu]');
  if (rename) {
    fxRenaming = rename.dataset.fxRenamemenu; fxDeleting = null; render();
    const field = $('#fxRenameTitle'); if (field) { field.focus(); field.select(); }
    return;
  }
  const remove = hit('[data-fx-deletemenu]');
  if (remove) { fxDeleting = remove.dataset.fxDeletemenu; fxRenaming = null; render(); return; }
}

function fxChainClick(event) {
  const hit = target => event.target.closest(target);
  const step = hit('[data-fx-step]');
  if (step) {
    const index = +step.dataset.fxStep;
    if (index === 0) { fxChainClosed = true; fxDeep = []; render(); return; }
    fxDeep = fxDeep.slice(0, Math.max(0, index - 1));
    render();
    return;
  }
  const back = hit('[data-fx-back]');
  if (back) {
    const index = +back.dataset.fxBack;
    if (index === 0) { fxChainClosed = true; fxDeep = []; render(); return; }
    fxDeep = fxDeep.slice(0, index - 1);
    render();
    return;
  }
  const edit = hit('[data-fx-edit]');
  if (edit) { fxMenuName = edit.dataset.fxEdit; fxSlotKey = null; fxCapKey = null; fxDialControl = null; showView('menus'); render(); return; }
  const slot = hit('[data-fx-slot]');
  if (slot) {
    const [menuName, key] = slot.dataset.fxSlot.split(':');
    const index = fxChainPlan().levels.findIndex(level => level.menu.name === menuName);
    if (index < 0) return;
    fxDeep = [...fxDeep.slice(0, index), {key: +key}];
    render();
    return;
  }
}
