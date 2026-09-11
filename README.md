# Vamaren Stock Checker

A Windows desktop app that checks a Magic: The Gathering decklist against
[Vamaren TCG](https://vamaren.tcgplayerpro.com/)'s live inventory.

![version](https://img.shields.io/github/v/release/JohnEugeo/vamaran_stock_checker)

## Features

- Paste any decklist (or import from Moxfield, Archidekt, MTGGoldfish,
  Aetherhub) and see instantly which cards the store has in stock
- Card images from Scryfall, with set-specific art (`1 Sol Ring (C21)`)
  and automatic fallback to the printing that is actually in stock
- Text and image display modes, dark theme
- Mark cards "added to cart" in-app, then open each store product page
  in your browser to buy
- Self-updating: the app checks GitHub daily and updates itself in
  one click

## Download

Grab `VamarenStockChecker.exe` from the
[latest release](https://github.com/JohnEugeo/vamaran_stock_checker/releases/latest).
No installation required.

## Building from source

```
pip install -r requirements.txt pyinstaller
pyinstaller VamarenStockChecker.spec --noconfirm
```

Or run directly: `python commander_deck_builder.py`
(requires Python 3.10+; deck imports from Moxfield/Aetherhub additionally
need `playwright install chromium`).

## License

[MIT](LICENSE)
