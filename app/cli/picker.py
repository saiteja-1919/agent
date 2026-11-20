"""The local file picker: how the office selects the fee workbook.

Veda is run from a Windows desktop, so the ordinary way to choose a file is a
real file dialog. That is attempted first, and it is attempted *lazily* - only
when this function is called - so importing the module on a machine without a
display (a service, an SSH session, a test) never touches Tk.

When there is no display the picker degrades honestly: it says so and asks for
a path to be pasted, rather than failing with a Tkinter error the office would
not understand.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

#: What the picker offers. The format is sniffed from the content anyway
#: (section 9.2), so these filters are a convenience, never a guarantee.
WORKBOOK_TYPES = [
    ("Fee workbooks", "*.xlsx *.xls *.csv"),
    ("Excel", "*.xlsx *.xls"),
    ("CSV", "*.csv"),
    ("All files", "*.*"),
]


class PickerCancelled(Exception):
    """The operator closed the dialog without choosing a file."""


def _dialog_path(title: str, filetypes) -> Optional[str]:
    """Open a native file dialog. Returns ``None`` when there is no display."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:  # noqa: BLE001 - Tkinter is optional on some installs
        return None

    try:
        root = tk.Tk()
    except Exception:  # noqa: BLE001 - no display, or Tk cannot start
        return None

    try:
        root.withdraw()
        root.attributes("-topmost", True)  # the dialog must not hide behind Veda
        chosen = filedialog.askopenfilename(title=title, filetypes=filetypes)
        return str(chosen) if chosen else None
    except Exception:  # noqa: BLE001 - a broken display must not crash the CLI
        return None
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001 - best effort
            pass


def _prompt_path(title: str) -> Optional[str]:
    """No display: ask for a path to be pasted. Ctrl-D or blank cancels."""
    sys.stdout.write(f"{title}\n")
    sys.stdout.write("  (paste the full path of the file, then press Enter)\n> ")
    sys.stdout.flush()
    try:
        answer = sys.stdin.readline()
    except Exception:  # noqa: BLE001 - a closed stdin is a cancel
        return None
    answer = (answer or "").strip().strip('"').strip("'")
    return answer or None


def pick_workbook(preferred: Optional[Path] = None) -> Path:
    """Return the fee workbook to import, or raise :class:`PickerCancelled`.

    ``preferred`` short-circuits the dialog entirely, which is what the CLI uses
    for ``--file`` and what the tests use to stay non-interactive.
    """
    if preferred is not None:
        path = Path(preferred).expanduser()
        if not path.is_file():
            raise PickerCancelled(f"{path} is not a file")
        return path

    chosen = _dialog_path("Choose the fee workbook", WORKBOOK_TYPES)
    if chosen is None:
        chosen = _prompt_path("Choose the fee workbook")
    if not chosen:
        raise PickerCancelled("no file was chosen")

    path = Path(chosen).expanduser()
    if not path.is_file():
        raise PickerCancelled(f"{path} is not a file")
    return path.resolve()


def pick_folder(preferred: Optional[Path] = None, *, title: str = "Choose a folder") -> Path:
    """The same idea for an output folder."""
    if preferred is not None:
        path = Path(preferred).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        chosen = filedialog.askdirectory(title=title)
        if chosen:
            return Path(chosen)
    except Exception:  # noqa: BLE001 - fall through to the default
        pass
    return Path.cwd()
