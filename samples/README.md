# Sample statements

Dummy PDFs, one or more for every layout kosh can read. **Every name, number and amount is invented.** They are built
by `python -m samples.generate` from the synthetic layouts in `tests/`, and `tests/test_samples.py` parses each one and
compares it with `manifest.json`, so a sample that stops parsing fails the build.

| Folder | Parser | What is there |
|---|---|---|
| `bank/` | `icici` | monthly combined statement (savings, PPF, linked FD), the same encrypted, and the statement you request from net banking for a period |
| `cards/` | `icici_card`, `hdfc_card` (two cards), `axis_card`, `tata_card` (encrypted) | one statement per issuer |
| `nps/` | `nps` | monthly (schemes stacked, schemes side by side) and the annual statement from the NPS portal |

Consolidated mutual-fund statements (CAS) are read by the `casparser` library and have no sample here: a real CAS
cannot be faked convincingly, so it is covered by tests that build the parsed data directly.

Encrypted samples list their password in `manifest.json`. They are fake passwords for fake files.

## Adding a layout (a new bank, card issuer, or a variant of one we have)

1. Write the parser in `finance/parsers/` and register it (see an existing one such as `axis_card.py`).
2. Build a **synthetic** statement that reproduces the layout: add a builder beside the others in `tests/` (`pdffix.py`,
   `cardfix.py`, `npsfix.py`). Never copy a real statement, not even with the numbers edited: names, account numbers,
   merchants and amounts all give a person away.
3. Add it to `samples/generate.py` with `add(folder, name, parser, pdf, about=...)` and run `python -m samples.generate`.
   That writes the PDF and its manifest entry, and refuses to if the parser rejects it.
4. Run `python -m pytest`. Commit the PDF, the manifest and the code together.

A PDF you made some other way works too: put it in the right folder, run
`python -m samples.describe path/to.pdf --parser <name> [--password X]` and paste the printed entry into
`manifest.json`. Only do this if you are certain the file holds no real data.
