// The deck, drawn once.
//
// Every page that shows the deck shows the same object, so it is drawn by the same
// code: six LCD keys in two rows of three, the three round buttons beneath them, one
// large dial at the right with two smaller dials below it.  That is the arrangement on the hardware and the reason this file exists
// — the Menus page used to draw its own deck, flatter and with the dials in a stack,
// and the two drifted apart.
//
// A page supplies only what the drawing cannot know:
//
//   view.faces[i]  { art, label, badge, tint, empty, selected }  what that key shows
//   view.caps[i]   { selected, claimed, aria, title }            state of that button
//   view.dials[i]  { selected, claimed, aria, title }            state of that dial
//   view.labels    whether the dials' names are drawn beside them
//   view.aria      group names, which differ by page ("while this menu is open")
//
// The class names and data attributes are the ones the pages already wire - `.key` with
// `data-key` inside `#deckKeys`, `.button-cap` with `data-cap`, `.dial` with
// `data-dial` - so a page keeps its own click handling and this file stays about looks.

const DECK_DRAW_CAPS = [
  { control: 'button6', name: 'Button 1 of 3, left', short: 'left' },
  { control: 'button8', name: 'Button 2 of 3, middle', short: 'middle' },
  { control: 'button7', name: 'Button 3 of 3, right', short: 'right' },
];

const DECK_DRAW_DIALS = [
  { control: 'top', big: true, short: 'Big' },
  { control: 'bottom_left', big: false, short: 'Left' },
  { control: 'bottom_right', big: false, short: 'Right' },
];

function drawEsc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// One LCD key: a black frame with the lit face recessed inside it, the way the keys sit
// on the deck. The face carries the picture and the name, because that is what the deck
// itself puts on the key - anything longer than that belongs in the panel beside it.
function deckKeyHTML(face, index) {
  const key = face || {};
  const pieces = [`<span class="deck-num">${String(index + 1).padStart(2, '0')}</span>`];
  if (key.badge) pieces.push(`<span class="deck-badge">${drawEsc(key.badge)}</span>`);
  const face_html = [
    `<span class="deck-top">${pieces.join('')}</span>`,
    key.art ? `<img class="key-art" src="${drawEsc(key.art)}" alt="" width="26" height="26">` : '',
    `<span class="deck-name">${drawEsc(key.label || 'Empty')}</span>`,
  ].join('');
  const classes = ['key'];
  if (key.selected) classes.push('selected');
  if (key.art) classes.push('has-art');
  if (key.empty) classes.push('empty');
  return `<button class="${classes.join(' ')}" type="button" data-key="${index}"
      aria-pressed="${key.selected ? 'true' : 'false'}"
      title="${drawEsc(key.title || key.label || '')}">
      <span class="deck-well"><span class="deck-face"${key.tint ? ` style="--face:${drawEsc(key.tint)}"` : ''}>${face_html}</span></span></button>`;
}

// The three buttons under the keys: each one in its own shallow well, drawn as the
// matte domed button it is. A dot means this menu has taken it, the ring means it is
// the one being set.
function deckCapsHTML(view) {
  const caps = view.caps || [];
  const buttons = DECK_DRAW_CAPS.map((cap, index) => {
    const state = caps[index] || {};
    const classes = ['button-cap'];
    if (state.claimed) classes.push('claimed');
    if (state.selected) classes.push('selected');
    return `<span class="deck-well deck-well-cap">
      <button class="${classes.join(' ')}" type="button" data-control="${cap.control}" data-cap="${index}"
        aria-pressed="${state.selected ? 'true' : 'false'}"
        title="${drawEsc(state.title || cap.name)}" aria-label="${drawEsc(state.aria || cap.name)}"></button>
    </span>`;
  }).join('');
  return `<div class="deck-caps" role="group" aria-label="${drawEsc(view.capsLabel || 'The three buttons under the LCD keys')}">${buttons}</div>`;
}

// The dials where they sit on the deck: the large one at the right, the two small ones
// below it. A knob is drawn as a knob - knurled edge, turned face - because a slot's
// worth of words per part once said a great deal and showed nothing.
function deckDialsHTML(view) {
  const dials = view.dials || [];
  const knob = (entry, index) => {
    const state = dials[index] || {};
    // Which dial it is travels on data-control; the size class is all the drawing needs,
    // because the pair's order is what places the two small ones.
    const classes = ['dial', entry.big ? 'dial-large' : 'dial-small'];
    if (state.claimed) classes.push('claimed');
    if (state.selected) classes.push('selected');
    const label = view.labels
      ? `<small class="deck-dial-name">${drawEsc(entry.short)}</small>`
      : '';
    return `<span class="deck-dial-holder${state.claimed ? ' claimed' : ''}">
      <button class="${classes.join(' ')}" type="button" data-control="${entry.control}" data-dial="${index}"
        aria-pressed="${state.selected ? 'true' : 'false'}"
        title="${drawEsc(state.title || '')}" aria-label="${drawEsc(state.aria || entry.control)}">
        <i class="dial-marker" aria-hidden="true"></i><i class="dial-cue" aria-hidden="true"></i></button>${label}</span>`;
  };
  const [big, ...small] = DECK_DRAW_DIALS;
  return `<div class="deck-dials" role="group" aria-label="${drawEsc(view.dialsLabel || 'The dials')}">
      ${knob(big, 0)}
      <div class="deck-dial-pair">${small.map((entry, index) => knob(entry, index + 1)).join('')}</div>
    </div>`;
}

// The body itself, with everything in its place on it.
function deckBodyHTML(view) {
  const settings = view || {};
  const faces = settings.faces || [];
  return `<div class="deck-body" role="img" aria-label="${drawEsc(settings.aria || 'AKP03 landscape control deck')}">
      <div class="deck-left">
        <div class="deck-keyfield" id="deckKeys" aria-label="Six editable LCD keys">
          ${faces.map((face, index) => deckKeyHTML(face, index)).join('')}
        </div>
        ${deckCapsHTML(settings)}
      </div>
      <div class="deck-right">
        ${deckDialsHTML(settings)}
      </div>
    </div>`;
}

// Draw the deck into a mount. The page keeps its own click handling: the buttons carry
// the data attributes it already reads.
function mountDeckDrawing(root, view) {
  if (!root) return null;
  root.innerHTML = deckBodyHTML(view);
  return root;
}
