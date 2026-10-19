/* The picture library, and the one picture picker.
   Both the Deck page and the Menus page read this file: the library is fetched
   once from /api/icons, and the picker is rendered from here. Add a picture to a
   pack — or draw one yourself and drop it in — and it appears on both pages with
   no second change. The only thing that differs per page is what a pick *means*,
   which is the callback each page hands to wireIconPicker(). */

const PACK_PREFIX = 'pack:';
const ICON_ACTION_ID = /^[a-z0-9][a-z0-9-]*$/;

let artworkLibrary = null;

// The headings, in the order the daemon lists them.  A library that predates the
// sections still works: everything files under one heading.
function librarySections() {
  const sent = artworkLibrary && Array.isArray(artworkLibrary.sections) ? artworkLibrary.sections : [];
  if (sent.length) return sent;
  return [{id: 'own', name: 'Custom sets'}];
}

// The library is cached so a panel can render without waiting.  Anything that
// changes it — a picture added, a set made or renamed — happens on the Icons page,
// so a caller that must not miss such a change passes {fresh: true} and reads it
// again.  A picker opening on the copy read when the page loaded would not show a
// picture added a minute ago.
async function loadLibrary({fresh = false} = {}) {
  if (artworkLibrary && !fresh) return artworkLibrary;
  const response = await fetch('/api/icons');
  if (!response.ok) throw Error('The icon library could not be read.');
  artworkLibrary = await response.json();
  return artworkLibrary;
}

async function refreshLibrary() {
  return loadLibrary({fresh: true});
}

function libraryIcons() {
  if (!artworkLibrary || !artworkLibrary.packs) return [];
  return artworkLibrary.packs.flatMap(pack => pack.icons);
}

function libraryPackOf(reference) {
  if (!artworkLibrary || !artworkLibrary.packs) return '';
  const pack = artworkLibrary.packs.find(candidate =>
    (candidate.icons || []).some(icon => icon.reference === reference));
  return pack ? pack.name : '';
}

function artworkPreviewSrc(reference, key) {
  if (!reference) return null;
  if (reference.startsWith(PACK_PREFIX)) {
    // The route is /api/icons/<pack>/<name>.png, so the separator must stay a
    // literal slash: encoding the whole reference as one segment 404s it.
    const [pack, ...name] = reference.slice(PACK_PREFIX.length).split('/');
    return `/api/icons/${encodeURIComponent(pack)}/${name.map(encodeURIComponent).join('/')}.png`;
  }
  // A key keeping the file it already has: show the deck's own picture.
  return `/api/screen/${encodeURIComponent(key)}.png`;
}

function artworkLabel(reference) {
  if (!reference) return null;
  if (!reference.startsWith(PACK_PREFIX)) return 'The icon already on this key';
  const wanted = reference.slice(PACK_PREFIX.length);
  const icon = libraryIcons().find(candidate => candidate.reference === reference);
  return icon ? icon.label : wanted;
}

// A library icon carries the action it was drawn for.  The general pack names an
// action id ("cut"); the macOS pack describes a shortcut ("cmd + n"), which is
// not an id and must never be compared with one.
function iconActionId(reference) {
  if (!reference || !reference.startsWith(PACK_PREFIX)) return null;
  const icon = libraryIcons().find(candidate => candidate.reference === reference);
  const action = icon && icon.action;
  return typeof action === 'string' && ICON_ACTION_ID.test(action) ? action : null;
}

// One picker for both pages, so the two panels cannot drift apart again.
//
// The pictures are grouped under the same headings the action list uses - the
// names and their order come from the daemon with the library, so the two rows
// cannot disagree.  There is deliberately no search box: the sets that shipped with
// the app hold a couple of dozen pictures each, and those are quicker to scan than
// to type at.
//
// Which sets are open is remembered here rather than in the markup.  The panel
// re-renders whenever the library changes, and a fold that snapped shut while
// someone was picking through it would lose their place.
let openIconSets = new Set();

function iconPickerHTML({selected}) {
  if (!artworkLibrary) return '<p class="lib-note">Loading the icons…</p>';
  const icons = libraryIcons().filter(icon => icon && icon.reference);
  const sections = librarySections();
  const shippedIds = new Set(artworkLibrary.builtin || []);
  const tile = icon => {
    const pack = libraryPackOf(icon.reference);
    const title = `${icon.label || icon.reference}${pack ? ' — ' + pack : ''}`;
    return `<button type="button" class="lib-pic${selected === icon.reference ? ' on' : ''}"
      data-lib-ref="${esc(icon.reference)}" title="${esc(title)}">
      <img src="${esc(icon.preview || artworkPreviewSrc(icon.reference, 0))}" alt="" width="44" height="44"></button>`;
  };
  const noPicture = `<button type="button" class="lib-pic plain${selected ? '' : ' on'}" data-lib-ref=""
      title="No icon — show the words"><span class="lib-aa" aria-hidden="true">Aa</span></button>`;
  const groups = sections
    .map(section => ({id: section.id, name: section.name,
      builtin: shippedIds.has(section.id),
      icons: icons.filter(icon => icon.section === section.id)}))
    .filter(group => group.icons.length);
  // Each set is one block: a heading and its pictures.  The separator is drawn
  // between blocks, so a line can never trail a group with nothing after it.
  //
  // A set folds.  With a library pack in the list an open set is a page of scrolling
  // before the next one starts, so every set arrives closed and opens on the one the
  // person is actually hunting through.  "No icon" is not a set — it is one tile and
  // the way back to words — so it stays open.
  const foldKey = group => (group.id === 'own-sets' ? 'own-sets' : group.id);
  const openOf = group => openIconSets.has(foldKey(group));
  const caret = open => `<span class="lib-caret" aria-hidden="true">${open ? '▾' : '▸'}</span>`;
  const row = group => {
    const open = openOf(group);
    return `<div class="lib-block">
      <button type="button" class="lib-heading lib-toggle" data-lib-set="${esc(group.id)}"
        aria-expanded="${open ? 'true' : 'false'}">
        ${caret(open)}
        <span class="lib-set-name">${esc(group.name)}</span>
        <span class="lib-count">${group.icons.length}</span>
      </button>
      <div class="lib-grid"${open ? '' : ' hidden'}>${group.icons.map(tile).join('')}</div>
    </div>`;
  };
  const plainBlock = (name, inner) => `<div class="lib-block">
      <p class="lib-heading">${esc(name)}</p>
      <div class="lib-grid">${inner}</div>
    </div>`;
  // The sets that shipped with the app stand on their own.  Everything the person
  // added sits behind one row, so the first screen is the deck's own glyphs and one
  // line for the library, however many sets that library turned into.  A single set
  // of their own is not a collection and gets no wrapper.
  const shipped = groups.filter(group => group.builtin);
  const own = groups.filter(group => !group.builtin);
  const ownCount = own.reduce((total, group) => total + group.icons.length, 0);
  const ownPart = own.length >= 2
    ? (() => {
      const group = {id: 'own-sets', name: 'Your sets', icons: []};
      const open = openOf(group);
      return `<div class="lib-block">
      <button type="button" class="lib-heading lib-toggle lib-group" data-lib-group="own-sets"
        aria-expanded="${open ? 'true' : 'false'}">
        ${caret(open)}
        <span class="lib-set-name">Your sets</span>
        <span class="lib-count">${ownCount}</span>
      </button>
      <div class="lib-nest"${open ? '' : ' hidden'}>${own.map(row).join('')}</div>
    </div>`;
    })()
    : own.map(row).join('');
  return `
    ${plainBlock('No icon', noPicture)}
    ${shipped.map(row).join('')}
    ${ownPart}`;
}

// Wiring that belongs with the markup above. onPick(null) means "no picture".
function wireIconPicker(root, {onPick}) {
  if (!root || typeof onPick !== 'function') return;
  root.querySelectorAll('.lib-toggle').forEach(heading => {
    heading.onclick = () => {
      const body = heading.nextElementSibling;
      const open = heading.getAttribute('aria-expanded') === 'true';
      const key = heading.dataset.libGroup || heading.dataset.libSet || '';
      if (open) openIconSets.delete(key); else if (key) openIconSets.add(key);
      heading.setAttribute('aria-expanded', open ? 'false' : 'true');
      if (body) body.hidden = open;
      const caret = heading.querySelector('.lib-caret');
      if (caret) caret.textContent = open ? '▸' : '▾';
    };
  });
  root.querySelectorAll('.lib-pic').forEach(button => {
    // The button is handed on as well, so the caller can tell which panel the pick
    // came from: this one picker renders in two panels at once.
    button.onclick = () => onPick(button.dataset.libRef || null, button);
  });
}
