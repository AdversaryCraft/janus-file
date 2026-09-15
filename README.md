# Janus File

A PDF that is also a ZIP. Companion PoC for the article
**"Why Gmail Blocks Your EXE (And How I Sent One Anyway)"**.

One file, two formats. Gmail sees a PDF and applies PDF scanning logic.
7-Zip, WinRAR, and every other standard archive tool see a ZIP and extract
its contents. Both readings are correct.

> **Authorized testing only.** Only run this against systems you own or
> have explicit written permission to test. Do not use it to send
> unsolicited attachments to anyone.

---

## What it does

The generated file starts with `%PDF-` and ends with a valid ZIP
end-of-central-directory record (`PK\x05\x06`). Depending on what opens
it:

| Opened with | Sees |
| :--- | :--- |
| Any PDF viewer | A bank statement with a balance and an instruction note |
| 7-Zip / WinRAR / Explorer | A ZIP containing `Statement_Launcher.bat`, `payload.exe`, and `README.txt` |

The detailed statement PDF is not stored as a separate file in the ZIP.
It is base64-encoded inside `Statement_Launcher.bat` and materializes in
`%TEMP%` only when the launcher runs.

---

## Why it works

Email scanners classify attachments by reading the first few bytes of the
file. They see `%PDF-` and apply PDF scanning logic, which does not look
past the PDF structure. The ZIP portion — appended after the `%%EOF`
marker — is never treated as an archive, so its contents are never
inspected.

The trick is in the ZIP offsets. After concatenating the PDF and the ZIP,
every offset in the ZIP central directory has to be shifted by the length
of the PDF. Without that patch, most extractors fail. With it, the file
is genuinely dual-natured.

---

## Installation

Requires Python 3.8 or later.

```bash
pip install reportlab
```

---

## Usage

```bash
python bank_stmt_polyglot.py -p payload.exe -o statement.pdf
```

### Options

| Flag | Description |
| :--- | :--- |
| `-p`, `--payload` | File to embed in the ZIP (required) |
| `-o`, `--output` | Output filename, e.g. `statement.pdf` (required) |
| `--bank` | Bank name shown in the PDF (default: `Adversary Craft Bank`) |
| `--name` | Account holder name (default: `Rajesh Kumar`) |
| `--theme` | Base color for the document (see Themes below) |

### Examples

```bash
# Default navy theme
python bank_stmt_polyglot.py -p payload.exe -o statement.pdf

# Named theme preset
python bank_stmt_polyglot.py -p payload.exe -o statement.pdf --theme red

# Custom hex color
python bank_stmt_polyglot.py -p payload.exe -o statement.pdf --theme "#1b5e20"

# Custom bank name (spaces allowed, use double quotes)
python bank_stmt_polyglot.py -p payload.exe -o statement.pdf \
    --bank "First National Bank" --theme purple
```

### Themes

All colors are derived from a single base color.

| Name | Hex |
| :--- | :--- |
| `navy` | `#0a2e5c` |
| `blue` | `#004c8f` |
| `orange` | `#f58220` |
| `red` | `#8b0000` |
| `crimson` | `#a6192e` |
| `maroon` | `#97144d` |
| `green` | `#1b5e20` |
| `teal` | `#00518f` |
| `purple` | `#4a148c` |
| `indigo` | `#22409a` |

You can also pass any `#RRGGBB` value directly.

---

## Verifying the output

After generation, confirm the file is valid in both formats.

**As a PDF:**
```bash
start statement.pdf
```
Opens in the default PDF viewer with the expected theme and balance.

**As a ZIP:**
```bash
7z l statement.pdf
```
Expected listing:
```
Statement_Launcher.bat
payload.exe
README.txt
```

**Full chain (in a lab):**
```bash
7z x statement.pdf -oextracted
cd extracted
Statement_Launcher.bat
```
The detailed statement opens, and the payload runs in the background.

---

## How the launcher works

`Statement_Launcher.bat` contains a base64-encoded copy of the detailed
statement PDF. When executed it:

1. Writes the base64 blob to a temp file in `%TEMP%`
2. Reconstructs the PDF with `certutil -decode`
3. Deletes the base64 staging file
4. Opens the PDF in the default viewer
5. Runs `payload.exe` silently with `start "" /b`

The launcher does not write outside of `%TEMP%`.

---

## Repository structure

```
janus-file/
├── README.md
├── LICENSE
├── bank_stmt_polyglot.py
├── detections/
│   ├── pdf_zip_polyglot.yar
│   └── certutil_decode.yml
└── examples/
    └── hello.exe
```

The `detections/` folder holds the YARA rule and a Sigma-style YAML stub
for the launcher stage. Copy them into your detection pipeline as needed.

---

## Requirements

- Python 3.8+
- [ReportLab](https://pypi.org/project/reportlab/) for PDF generation
- 7-Zip or WinRAR on the test host to extract the archive portion

---

## License

MIT. See [LICENSE](LICENSE) for the exact text.

---

## Disclaimer

This is research code. It exists to help defenders understand a specific
class of email attachment scanner bypass. Running it against systems you
do not own or have written permission to test is illegal in most
jurisdictions and is not what this project is for.
