"""Real, hidden Tk widgets with synthetic text and an in-memory save callback only."""

import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import pytest

from tooling.experiments.model_selection import credential_window


def drive(action):
    original = tk.Tk

    def hidden_window():
        window = original()
        window.withdraw()
        window.after_idle(lambda: action(window))
        # Do not leave a test stuck on a failed assertion in a Tk callback.
        window.after(2000, window.destroy)
        return window

    tk.Tk = hidden_window


def widgets(window):
    return window.winfo_children()[0].winfo_children()


def masked_entry():
    writes, observed = [], []

    def action(window):
        children = widgets(window)
        entry = next(w for w in children if isinstance(w, ttk.Entry))
        button = next(w for w in children if isinstance(w, ttk.Button))
        observed.append(entry.cget("show"))
        entry.insert(0, "SYNTHETIC-NOT-A-KEY")
        button.invoke()
        observed.extend([entry.get(), str(entry.cget("state"))])
        observed.append(any("保存成功" in w.cget("text") for w in children if isinstance(w, ttk.Label)))
        button.invoke()

    drive(action)
    assert credential_window(writes.append)
    assert writes == ["SYNTHETIC-NOT-A-KEY"]
    assert observed == ["*", "", "disabled", True]


def empty_cancel():
    writes = []

    def action(window):
        next(w for w in widgets(window) if isinstance(w, ttk.Button)).invoke()
        window.destroy()

    drive(action)
    assert not credential_window(writes.append)
    assert writes == []


def save_error():
    observed = []

    def failing_save(value):
        raise OSError(value)

    def action(window):
        children = widgets(window)
        next(w for w in children if isinstance(w, ttk.Entry)).insert(0, "SYNTHETIC-SECRET")
        next(w for w in children if isinstance(w, ttk.Button)).invoke()
        observed.extend(w.cget("text") for w in children if isinstance(w, ttk.Label))
        window.destroy()

    drive(action)
    assert not credential_window(failing_save)
    assert any("保存失败" in text for text in observed)
    assert all("SYNTHETIC" not in text for text in observed)


@pytest.mark.parametrize("scenario", ["masked_entry", "empty_cancel", "save_error"])
def test_credential_window_in_separate_tcl_process(scenario):
    # Each GUI invocation is a fresh application, matching the real launcher.
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), scenario],
        check=False, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "SYNTHETIC" not in result.stdout + result.stderr


if __name__ == "__main__":
    globals()[sys.argv[1]]()
