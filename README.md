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

## Detection guidance

The technique leaves a specific set of artifacts at the file, endpoint,
and gateway layers. Below is a starting set for defenders.

### File-level detection

A PDF+ZIP polyglot can be identified by scanning the full byte stream of
a PDF attachment, not just the header.

**YARA rule:**

```yara
rule PDF_ZIP_Polyglot
{
    meta:
        description = "Detects a file that is both a PDF and a ZIP"
        author      = "Vali Rassouli Chokharpan"
        reference   = "https://example.com/janus-file"

    strings:
        $pdf_header = { 25 50 44 46 2D }          // %PDF-
        $zip_eocd   = { 50 4B 05 06 }              // PK\x05\x06
        $zip_local  = { 50 4B 03 04 }              // PK\x03\x04

    condition:
        $pdf_header at 0 and
        $zip_eocd in (filesize - 65557 .. filesize) and
        $zip_local in (filesize - 65557 .. filesize)
}
```

Key points:

- `%PDF-` must be at offset 0.
- The ZIP EOCD record must be in the trailing window (last 64 KB, per the ZIP spec).
- The presence of a local file header (`PK\x03\x04`) inside the same window confirms real ZIP content, not just a stray signature.

### Endpoint detection

The launcher stage produces a distinct process chain and file pattern.

| Artifact | What to monitor |
| :--- | :--- |
| Base64 staging file | A `.b64` file written to `%TEMP%` under a randomized name |
| `certutil -decode` | `certutil.exe` invoked with `-decode` and a `%TEMP%` source and destination |
| Launcher execution | `cmd.exe` started from an archive extraction directory (e.g., `7zG.exe`, `WinRAR.exe`, `explorer.exe`) |
| Payload drop | An `.exe` written to the same folder as the batch file, followed by `start "" /b` |
| PDF and EXE in the same folder | A `.pdf` and an `.exe` appearing in the same directory within a short window |

**Sysmon queries (example):**

```
Event ID 1: Process Create
  Image ends with certutil.exe
  CommandLine contains "-decode"

Event ID 11: File Create
  TargetFilename contains "\AppData\Local\Temp\"
  TargetFilename ends with ".b64"
```

**CrowdStrike LogScale (example):**

```
#event_simpleName=ProcessRollup2
| Image = /\\certutil\.exe$/i
| CommandLine = /-decode/i
| table([ComputerName, UserName, ParentBaseFileName, CommandLine])
```

### Gateway detection

For email security teams, the checks that matter are:

1. **Full-body scanning of PDF attachments.** Do not stop at the PDF structure. Scan the entire attachment for a trailing ZIP EOCD record.
2. **Nested archive detection.** If the file contains both a PDF header and a ZIP EOCD, flag it as a polyglot regardless of the declared MIME type.
3. **Extension mismatch.** A file whose declared type is `application/pdf` but whose body contains ZIP local headers is suspicious. This is the same class of detection used for Office macros and OLE objects.
4. **Archive-to-EXE chain.** If an email leads to an attachment that produces an `.exe` after extraction, escalate even if the initial file was allowed.

### Detection gaps to be aware of

The technique is designed to slip past scanners that only inspect the
header. Common misses:

- PDF parsers that follow the cross-reference table and stop at `%%EOF` — they never see the appended ZIP.
- File-type fingerprinting based on the first four bytes only.
- Signature engines that look for known-bad hashes — the polyglot has a unique hash per build because the PDF content is randomized.

If your scanner shows "PDF" as the file type and does not inspect trailing bytes, this technique will pass through.

### Detection tuning notes

- Legitimate PDFs can contain `PK\x03\x04` bytes by coincidence, especially inside compressed streams. Require the ZIP EOCD record to be near the end of the file (last 64 KB) for a high-confidence match.
- Some email signatures and PDF/A conformance markers also produce trailing bytes. The YARA rule above reduces false positives by requiring `%PDF-` at offset 0.
- Whitelist any internal tooling that intentionally produces PDFs with appended data (rare but it exists).

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
