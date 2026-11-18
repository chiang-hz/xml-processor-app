"""Read the source's automatic print breaks from desktop Excel on Windows."""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import NamedTemporaryFile


_READ_PAGE_BREAKS = r"""
$ErrorActionPreference = 'Stop'
$excel = New-Object -ComObject Excel.Application
try {
    $excel.DisplayAlerts = $false
    $excel.EnableEvents = $false
    $excel.AutomationSecurity = 3
    $book = $excel.Workbooks.Open($env:XML_EXCEL_PAGINATION_FILE, 0, $true)
    try {
        $sheet = $book.Worksheets.Item($env:XML_EXCEL_PAGINATION_SHEET)
        if ($sheet.Visible -ne -1) { $sheet.Visible = -1 }
        $sheet.Activate()
        $sheet.DisplayPageBreaks = $true
        $ends = @($sheet.HPageBreaks | ForEach-Object { $_.Location.Row - 1 })
        ConvertTo-Json -InputObject $ends -Compress
    } finally {
        $book.Close($false)
    }
} finally {
    $excel.Quit()
}
"""


def read_excel_page_ends(source_bytes, sheet_name):
    """Return one-based last rows before horizontal breaks, or None if unavailable."""
    powershell = shutil.which('powershell.exe') if os.name == 'nt' else None
    if not powershell:
        return None

    path = None
    try:
        with NamedTemporaryFile(suffix='.xlsx', delete=False) as temp:
            path = Path(temp.name)
            temp.write(source_bytes)
        script = base64.b64encode(_READ_PAGE_BREAKS.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(
            [powershell, '-NoProfile', '-NonInteractive', '-EncodedCommand', script],
            env={**os.environ, 'XML_EXCEL_PAGINATION_FILE': str(path),
                 'XML_EXCEL_PAGINATION_SHEET': sheet_name},
            creationflags=subprocess.CREATE_NO_WINDOW,
            capture_output=True, check=True, timeout=30, encoding='utf-8', errors='replace',
        )
        return sorted(set(json.loads(result.stdout)))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
