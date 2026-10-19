# AKP03 Control Deck — visual direction prototype

A self-contained browser prototype for the AKP03 Control Deck editor. It explores a warm, desktop-first workspace with a six-key physical deck, menu/page builder, contextual inspector, and readable inline action choices.

This is **sample/draft UI data only**. It does not connect to the daemon, device, configuration, or hardware.

## Run

From the repository root:

```bash
cd design-prototype
python3 -m http.server 8000
```

Open <http://localhost:8000> in a desktop browser, ideally at a 1366 × 768 viewport. Stop the server with `Ctrl-C` when finished.

## Click-through

- Use **Deck**, **Menus**, and **Actions** in the left rail.
- Select a physical LCD key to update its inspector.
- In **Menus**, select a page in the Menu tree, a page tab, or a content tile.
- The right-side inline picker follows the selected tile; navigation positions are visibly reserved for Back and Home.
