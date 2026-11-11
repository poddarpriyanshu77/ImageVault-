#!/usr/bin/env python3
"""
ImageVault GUI (modern Tkinter edition)
---------------------------------------
Run:  python imagevault_gui.py        (keep it next to imagevault.py)

Left sidebar  : choose folders, layout, mode and duplicate handling, then run actions.
Main area     : summary cards, charts, photo gallery, operations table, albums,
                duplicate groups and warnings. Nothing changes on disk until you
                press Apply and confirm. Undo reverses the last run.
Scanning and file operations run in a background thread, so the window never freezes.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import imagevault as iv

try:
    from PIL import Image, ImageTk
    THUMBS = True
except ImportError:  # thumbnails are optional
    THUMBS = False

# ------------------------------------------------------------------ palette
SIDEBAR, SIDE_CARD, SIDE_TXT, SIDE_MUTED = "#161b2e", "#232a45", "#e8ebf7", "#8b93b8"
BG, CARD, TEXT, MUTED, LINE = "#f3f5fb", "#ffffff", "#1d2340", "#6b7399", "#e3e7f3"
ACCENT, ACCENT_D = "#5b6cff", "#4757e6"
GREEN, GREEN_D, ORANGE, RED, TEAL = "#1fb57a", "#179a68", "#f08a24", "#e5484d", "#14a5c9"
TILE_W, MAX_TILES = 150, 60
FONT = "TkDefaultFont"


def short(text: str, n: int) -> str:
    return text if len(text) <= n else "…" + text[-(n - 1):]


def short_head(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def pick_font(root) -> str:
    avail = set(tkfont.families(root))
    for f in ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Ubuntu", "Cantarell", "DejaVu Sans", "Arial"):
        if f in avail:
            return f
    return "TkDefaultFont"


# ------------------------------------------------------------ custom widgets
class FlatButton(tk.Label):
    """A flat, colored button that looks the same on Windows, macOS and Linux."""

    def __init__(self, master, text, command, bg, hover=None, fg="white", disabled_bg="#2b3354", **kw):
        super().__init__(master, text=text, bg=bg, fg=fg, cursor="hand2", pady=11, padx=14,
                         font=(FONT, 10, "bold"), **kw)
        self.cmd, self.base, self.hover = command, bg, hover or bg
        self.fg0, self.dis_bg, self.enabled = fg, disabled_bg, True
        self.bind("<Enter>", lambda e: self.enabled and self.config(bg=self.hover))
        self.bind("<Leave>", lambda e: self.config(bg=self.base if self.enabled else self.dis_bg))
        self.bind("<Button-1>", lambda e: self.enabled and self.cmd())

    def set_enabled(self, on: bool):
        self.enabled = on
        self.config(bg=self.base if on else self.dis_bg, fg=self.fg0 if on else "#6a7399",
                    cursor="hand2" if on else "arrow")


class Chips(tk.Frame):
    """Segmented selector (like radio buttons, but prettier)."""

    def __init__(self, master, items, var, cols=2):
        super().__init__(master, bg=SIDEBAR)
        self.var, self.labels = var, {}
        for i, (text, val) in enumerate(items):
            lb = tk.Label(self, text=text, font=(FONT, 9, "bold"), pady=7, cursor="hand2")
            lb.grid(row=i // cols, column=i % cols, sticky="ew", padx=2, pady=2)
            lb.bind("<Button-1>", lambda e, v=val: var.set(v))
            self.labels[val] = lb
        for c in range(cols):
            self.columnconfigure(c, weight=1, uniform="chip")
        var.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def refresh(self):
        for val, lb in self.labels.items():
            on = self.var.get() == val
            lb.config(bg=ACCENT if on else SIDE_CARD, fg="white" if on else SIDE_MUTED)


class BarChart(tk.Canvas):
    """Tiny horizontal bar chart drawn on a Canvas."""

    def __init__(self, master, color):
        super().__init__(master, bg=CARD, highlightthickness=0, height=190)
        self.data, self.color = [], color
        self.bind("<Configure>", lambda e: self.redraw())

    def set_data(self, data):
        self.data = list(data)[:6]
        self.redraw()

    def redraw(self):
        self.delete("all")
        w, h = max(self.winfo_width(), 200), max(self.winfo_height(), 100)
        if not self.data:
            self.create_text(w / 2, h / 2, text="No data yet", fill=MUTED, font=(FONT, 10))
            return
        top = max(v for _, v in self.data) or 1
        row = min(34, (h - 8) / len(self.data))
        lw = 112
        for i, (name, val) in enumerate(self.data):
            y = 6 + i * row
            self.create_text(0, y + row / 2, text=short_head(name, 15), anchor="w", fill=TEXT, font=(FONT, 9))
            bw = max((w - lw - 48) * val / top, 3)
            self.create_rectangle(lw, y + 5, lw + bw, y + row - 5, fill=self.color, outline="")
            self.create_text(lw + bw + 8, y + row / 2, text=str(val), anchor="w", fill=MUTED, font=(FONT, 9, "bold"))


# ----------------------------------------------------------------------- app
class ImageVaultApp:
    def __init__(self, root: tk.Tk):
        global FONT
        self.root = root
        FONT = pick_font(root)
        root.title("ImageVault - Photo Metadata and Album Organizer")
        root.geometry("1300x800")
        root.minsize(1120, 700)
        root.configure(bg=BG)

        self.q: queue.Queue = queue.Queue()
        self.state: dict | None = None
        self.detail_ref = None
        self.g_refs, self.g_tiles, self.g_token = [], [], 0

        self.src_var, self.dst_var = tk.StringVar(), tk.StringVar()
        self.layout_var = tk.StringVar(value="date")
        self.mode_var = tk.StringVar(value="copy")
        self.dup_var = tk.StringVar(value="separate")
        self.summary_var = tk.StringVar(value="Choose a source and destination folder, then click Preview.")
        self.status_var = tk.StringVar(value="Ready")

        self._style()
        self._build_sidebar()
        self._build_main()
        for v in (self.src_var, self.dst_var, self.layout_var, self.mode_var, self.dup_var):
            v.trace_add("write", self._invalidate)

    # ---------------------------------------------------------------- styles
    def _style(self):
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=TEXT, rowheight=30,
                    borderwidth=0, font=(FONT, 9))
        s.configure("Treeview.Heading", background="#eceffa", foreground=TEXT, relief="flat",
                    font=(FONT, 9, "bold"), padding=(8, 8))
        s.map("Treeview", background=[("selected", "#e1e5ff")], foreground=[("selected", TEXT)])
        s.map("Treeview.Heading", background=[("active", "#e1e5fa")])
        s.configure("TNotebook", background=BG, borderwidth=1, bordercolor=LINE, lightcolor=BG,
                    darkcolor=BG, tabmargins=(0, 0, 0, 0))
        s.configure("TNotebook.Tab", background="#e4e8f6", foreground=MUTED, padding=(15, 9),
                    font=(FONT, 10, "bold"), borderwidth=1, bordercolor=LINE, lightcolor="#e4e8f6",
                    darkcolor="#e4e8f6")
        s.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)],
              lightcolor=[("selected", CARD)], darkcolor=[("selected", CARD)])
        s.configure("Accent.Horizontal.TProgressbar", troughcolor="#e4e8f6", background=ACCENT,
                    borderwidth=0, thickness=8)
        s.configure("Vertical.TScrollbar", background="#d8dcf0", troughcolor=CARD, borderwidth=0, arrowsize=0)

    # --------------------------------------------------------------- sidebar
    def _build_sidebar(self):
        side = tk.Frame(self.root, bg=SIDEBAR, width=300)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        pad = {"padx": 20}

        logo = tk.Frame(side, bg=SIDEBAR)
        logo.pack(fill="x", pady=(24, 18), **pad)
        badge = tk.Canvas(logo, width=44, height=44, bg=SIDEBAR, highlightthickness=0)
        badge.create_oval(2, 2, 42, 42, fill=ACCENT, outline="")
        badge.create_rectangle(13, 15, 31, 29, fill="white", outline="")
        badge.create_polygon(13, 29, 20, 21, 25, 26, 28, 23, 31, 29, fill=ACCENT_D, outline="")
        badge.pack(side="left")
        tk.Label(logo, text="ImageVault", bg=SIDEBAR, fg="white", font=(FONT, 17, "bold")).pack(anchor="w", padx=12)
        tk.Label(logo, text="Photo & album organizer", bg=SIDEBAR, fg=SIDE_MUTED, font=(FONT, 9)).pack(anchor="w", padx=12)

        # bottom actions first so they are never pushed off-screen
        actions = tk.Frame(side, bg=SIDEBAR)
        actions.pack(side="bottom", fill="x", pady=(0, 20), **pad)
        self.btn_preview = FlatButton(actions, "Preview  (no changes)", self.on_preview, ACCENT, ACCENT_D)
        self.btn_apply = FlatButton(actions, "Apply organization", self.on_apply, GREEN, GREEN_D)
        self.btn_save = FlatButton(actions, "Save report...", self.on_save_report, SIDE_CARD, "#2d3658", fg=SIDE_TXT)
        self.btn_undo = FlatButton(actions, "Undo last run", self.on_undo, SIDE_CARD, "#43283a", fg="#ff9aa0")
        for b in (self.btn_preview, self.btn_apply, self.btn_save, self.btn_undo):
            b.pack(fill="x", pady=3)
        self.btn_apply.set_enabled(False)
        self.btn_save.set_enabled(False)

        self._folder_picker(side, "SOURCE FOLDER", self.src_var)
        self._folder_picker(side, "DESTINATION FOLDER", self.dst_var)
        self._section(side, "ALBUM LAYOUT")
        Chips(side, [("By date", "date"), ("By type", "type"), ("By camera", "camera"), ("Date + type", "date-type")],
              self.layout_var, cols=2).pack(fill="x", **pad)
        self._section(side, "MODE")
        Chips(side, [("Copy (safe)", "copy"), ("Move", "move")], self.mode_var, cols=2).pack(fill="x", **pad)
        self._section(side, "DUPLICATES")
        Chips(side, [("Separate folder", "separate"), ("Skip them", "skip")], self.dup_var, cols=2).pack(fill="x", **pad)

    def _section(self, parent, text):
        tk.Label(parent, text=text, bg=SIDEBAR, fg=SIDE_MUTED, font=(FONT, 8, "bold")).pack(
            anchor="w", padx=22, pady=(16, 5))

    def _folder_picker(self, parent, title, var):
        self._section(parent, title)
        row = tk.Frame(parent, bg=SIDE_CARD)
        row.pack(fill="x", padx=20)
        shown = tk.StringVar(value="Not selected")
        lbl = tk.Label(row, textvariable=shown, bg=SIDE_CARD, fg=SIDE_TXT, font=(FONT, 9), anchor="w", padx=10)
        lbl.pack(side="left", fill="x", expand=True, ipady=9)
        btn = tk.Label(row, text="Browse", bg=ACCENT, fg="white", font=(FONT, 9, "bold"), padx=12, cursor="hand2")
        btn.pack(side="right", fill="y")
        btn.bind("<Button-1>", lambda e: self._browse(var))
        btn.bind("<Enter>", lambda e: btn.config(bg=ACCENT_D))
        btn.bind("<Leave>", lambda e: btn.config(bg=ACCENT))
        var.trace_add("write", lambda *_: shown.set(short(var.get(), 28) if var.get() else "Not selected"))

    # ------------------------------------------------------------------ main
    def _build_main(self):
        main = tk.Frame(self.root, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        head = tk.Frame(main, bg=BG)
        head.pack(fill="x", padx=24, pady=(20, 8))
        tk.Label(head, text="Preview & Results", bg=BG, fg=TEXT, font=(FONT, 20, "bold")).pack(anchor="w")
        tk.Label(head, textvariable=self.summary_var, bg=BG, fg=MUTED, font=(FONT, 10), wraplength=900,
                 justify="left").pack(anchor="w", pady=(2, 0))

        cards = tk.Frame(main, bg=BG)
        cards.pack(fill="x", padx=24, pady=8)
        self.cards = {}
        for i, (key, cap, color) in enumerate((("images", "Images found", ACCENT), ("dups", "Duplicates", ORANGE),
                                               ("wasted", "Space wasted", RED), ("albums", "Album folders", GREEN))):
            f = tk.Frame(cards, bg=CARD, highlightbackground=LINE, highlightthickness=1)
            f.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))
            cards.columnconfigure(i, weight=1, uniform="card")
            tk.Frame(f, bg=color, width=5).pack(side="left", fill="y")
            inner = tk.Frame(f, bg=CARD)
            inner.pack(side="left", fill="both", expand=True, padx=14, pady=10)
            tk.Label(inner, text=cap.upper(), bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w")
            val = tk.Label(inner, text="-", bg=CARD, fg=TEXT, font=(FONT, 22, "bold"))
            val.pack(anchor="w")
            sub = tk.Label(inner, text=" ", bg=CARD, fg=MUTED, font=(FONT, 9))
            sub.pack(anchor="w")
            self.cards[key] = (val, sub)

        # status bar (packed before the expanding body)
        bar = tk.Frame(main, bg=CARD, highlightbackground=LINE, highlightthickness=1)
        bar.pack(side="bottom", fill="x")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=150, style="Accent.Horizontal.TProgressbar")
        self.progress.pack(side="left", padx=14, pady=8)
        tk.Label(bar, textvariable=self.status_var, bg=CARD, fg=MUTED, font=(FONT, 9)).pack(side="left")

        body = tk.Frame(main, bg=BG)
        body.pack(fill="both", expand=True, padx=24, pady=(8, 14))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.nb = ttk.Notebook(body)
        self.nb.grid(row=0, column=0, sticky="nsew")
        self._build_overview()
        self._build_gallery()
        self.ops = self._tree("Operations", ("action", "file", "destination", "reason"),
                              ("Action", "File", "Goes to (inside destination)", "Reason"), (90, 160, 230, 190))
        self.ops.tag_configure("dup", foreground=ORANGE)
        self.ops.tag_configure("skip", foreground="#9aa0bd")
        self.ops.bind("<<TreeviewSelect>>", self._on_select_op)
        self.albums = self._tree("Albums", ("album", "count"), ("Album folder", "Files"), (420, 90))
        self.dups = self._tree("Duplicates", ("size", "path"), ("Size", "Path"), (90, 520), tree_col=True)
        self.dups.tag_configure("orig", foreground=GREEN)
        wf = tk.Frame(self.nb, bg=CARD)
        self.nb.add(wf, text="Warnings")
        self.warn_text = tk.Text(wf, wrap="word", bg=CARD, fg=TEXT, relief="flat", font=(FONT, 10), padx=14, pady=12)
        self.warn_text.pack(fill="both", expand=True)

        # details panel
        det = tk.Frame(body, bg=CARD, width=270, highlightbackground=LINE, highlightthickness=1)
        det.grid(row=0, column=1, sticky="ns", padx=(12, 0))
        det.pack_propagate(False)
        tk.Label(det, text="SELECTED PHOTO", bg=CARD, fg=MUTED, font=(FONT, 8, "bold")).pack(anchor="w", padx=16, pady=(14, 8))
        box = tk.Frame(det, bg="#eceffa", width=238, height=200)
        box.pack(padx=16)
        box.pack_propagate(False)
        self.d_img = tk.Label(box, text="Click a photo in the\nGallery or Operations tab", bg="#eceffa",
                              fg=MUTED, font=(FONT, 9))
        self.d_img.pack(expand=True)
        self.d_name = tk.Label(det, text="", bg=CARD, fg=TEXT, font=(FONT, 11, "bold"), wraplength=235, justify="left")
        self.d_name.pack(anchor="w", padx=16, pady=(12, 4))
        self.d_meta = tk.Label(det, text="", bg=CARD, fg=MUTED, font=(FONT, 9), justify="left", anchor="w")
        self.d_meta.pack(anchor="w", padx=16)

    def _build_overview(self):
        f = tk.Frame(self.nb, bg=CARD)
        self.nb.add(f, text="Overview")
        self.charts = {}
        spec = (("types", "Photos by type", ACCENT, 0, 0, 1, 130),
                ("dates", "Date source", TEAL, 0, 1, 1, 130),
                ("albums", "Largest albums", GREEN, 1, 0, 2, 190))
        for key, title, color, r, c, span, height in spec:
            box = tk.Frame(f, bg=CARD)
            box.grid(row=r, column=c, columnspan=span, sticky="nsew", padx=20, pady=(16, 4))
            tk.Label(box, text=title, bg=CARD, fg=TEXT, font=(FONT, 11, "bold")).pack(anchor="w", pady=(0, 6))
            ch = BarChart(box, color)
            ch.config(height=height)
            ch.pack(fill="both", expand=True)
            self.charts[key] = ch
        f.columnconfigure(0, weight=1, uniform="ch")
        f.columnconfigure(1, weight=1, uniform="ch")
        tk.Label(f, text="Tip: the Gallery tab shows thumbnails; duplicates are tagged in orange.",
                 bg=CARD, fg=MUTED, font=(FONT, 9), wraplength=560, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", padx=20, pady=8)

    def _build_gallery(self):
        f = tk.Frame(self.nb, bg=CARD)
        self.nb.add(f, text="Gallery")
        self.g_canvas = tk.Canvas(f, bg=CARD, highlightthickness=0)
        sb = ttk.Scrollbar(f, orient="vertical", command=self.g_canvas.yview)
        self.g_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.g_canvas.pack(side="left", fill="both", expand=True)
        self.g_inner = tk.Frame(self.g_canvas, bg=CARD)
        self.g_win = self.g_canvas.create_window((0, 0), window=self.g_inner, anchor="nw")
        self.g_inner.bind("<Configure>", lambda e: self.g_canvas.configure(scrollregion=self.g_canvas.bbox("all")))
        self.g_canvas.bind("<Configure>", self._relayout_gallery)
        self.g_canvas.bind("<Enter>", lambda e: self._bind_wheel(True))
        self.g_canvas.bind("<Leave>", lambda e: self._bind_wheel(False))
        self.g_msg = tk.Label(self.g_inner, text="Thumbnails appear here after you click Preview.", bg=CARD,
                              fg=MUTED, font=(FONT, 10), pady=40, padx=30)
        self.g_msg.grid(row=0, column=0)

    def _tree(self, title, cols, heads, widths, tree_col=False):
        frame = tk.Frame(self.nb, bg=CARD)
        self.nb.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=cols, show="tree headings" if tree_col else "headings")
        if tree_col:
            tree.column("#0", width=240)
            tree.heading("#0", text="Group / file", anchor="w")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h, anchor="w")
            tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return tree

    # --------------------------------------------------------------- gallery
    def _bind_wheel(self, on):
        if on:
            self.root.bind_all("<MouseWheel>", self._wheel)
            self.root.bind_all("<Button-4>", self._wheel)
            self.root.bind_all("<Button-5>", self._wheel)
        else:
            for ev in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.root.unbind_all(ev)

    def _wheel(self, e):
        step = -1 if (getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0) else 1
        self.g_canvas.yview_scroll(step * 2, "units")

    def _relayout_gallery(self, _e=None):
        w = max(self.g_canvas.winfo_width(), TILE_W)
        self.g_canvas.itemconfigure(self.g_win, width=w)
        cols = max(1, w // TILE_W)
        for i, tile in enumerate(self.g_tiles):
            tile.grid(row=i // cols, column=i % cols, padx=4, pady=4)

    def _fill_gallery(self, records: dict, dup_paths: set):
        self.g_token += 1
        token = self.g_token
        for t in self.g_tiles:
            t.destroy()
        self.g_tiles, self.g_refs = [], []
        self.g_msg.grid_remove()
        if not THUMBS:
            self.g_msg.config(text="Install Pillow to see thumbnails:  pip install Pillow")
            self.g_msg.grid()
            return
        items = sorted(records.values(), key=lambda r: (r.taken, str(r.path)))[:MAX_TILES]

        def batch(start):
            if token != self.g_token:
                return
            for r in items[start:start + 6]:
                self._add_tile(r, str(r.path) in dup_paths)
            self._relayout_gallery()
            if start + 6 < len(items):
                self.root.after(8, batch, start + 6)

        batch(0)

    def _add_tile(self, r, is_dup: bool):
        tile = tk.Frame(self.g_inner, bg=CARD, padx=4, pady=4, cursor="hand2")
        box = tk.Frame(tile, bg="#eceffa", width=132, height=110)
        box.pack_propagate(False)
        box.pack()
        photo = None
        try:
            with Image.open(r.path) as im:
                im = im.convert("RGB")
                im.thumbnail((128, 106))
                photo = ImageTk.PhotoImage(im)
                self.g_refs.append(photo)
        except Exception:
            pass
        img = tk.Label(box, image=photo, bg="#eceffa") if photo else tk.Label(box, text="no preview", bg="#eceffa", fg=MUTED, font=(FONT, 8))
        img.pack(expand=True)
        name = tk.Label(tile, text=short_head(r.path.name, 18), bg=CARD, fg=TEXT, font=(FONT, 8, "bold"))
        name.pack(pady=(4, 0))
        sub = tk.Label(tile, text="DUPLICATE" if is_dup else f"{r.taken:%d %b %Y}", bg=CARD,
                       fg=ORANGE if is_dup else MUTED, font=(FONT, 8, "bold" if is_dup else "normal"))
        sub.pack()
        for w in (tile, box, img, name, sub):
            w.bind("<Button-1>", lambda e, p=r.path: self._show_details(p))
        self.g_tiles.append(tile)

    # ------------------------------------------------------------- helpers
    def _browse(self, var):
        folder = filedialog.askdirectory(title="Choose folder")
        if folder:
            var.set(folder)

    def _invalidate(self, *_):
        if self.state is not None:
            self.state = None
            self.btn_apply.set_enabled(False)
            self.btn_save.set_enabled(False)
            self.status_var.set("Settings changed - click Preview again.")

    def _set_busy(self, busy: bool, msg: str = ""):
        self.btn_preview.set_enabled(not busy)
        self.btn_undo.set_enabled(not busy)
        if busy:
            self.btn_apply.set_enabled(False)
            self.btn_save.set_enabled(False)
            self.progress.start(12)
            self.status_var.set(msg)
        else:
            self.progress.stop()
            self.progress["value"] = 0
            self.btn_apply.set_enabled(bool(self.state))
            self.btn_save.set_enabled(bool(self.state))

    def _run(self, work, done, busy_msg):
        self._set_busy(True, busy_msg)

        def target():
            try:
                self.q.put(("ok", done, work()))
            except iv.ImageVaultError as exc:
                self.q.put(("err", None, str(exc)))
            except Exception as exc:  # last-resort guard so the GUI never hangs
                self.q.put(("err", None, f"Unexpected error: {exc}"))

        threading.Thread(target=target, daemon=True).start()
        self.root.after(100, self._poll)

    def _poll(self):
        try:
            kind, done, result = self.q.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        if kind == "ok":
            done(result)
            self._set_busy(False)
        else:
            self.state = None
            self._set_busy(False)
            self.status_var.set("Error")
            messagebox.showerror("ImageVault", result)

    # ------------------------------------------------------------- preview
    def on_preview(self):
        src, dst = self.src_var.get().strip(), self.dst_var.get().strip()
        if not src or not dst:
            messagebox.showwarning("ImageVault", "Please choose both a source and a destination folder.")
            return
        layout, mode, dup = self.layout_var.get(), self.mode_var.get(), self.dup_var.get()

        def work():
            source, dest = iv.validate_paths(Path(src), Path(dst))
            records, skipped, errors = iv.scan_directory(source, exclude=dest)
            if not records:
                return None
            groups, herr = iv.find_duplicates(records)
            plan = iv.build_plan(records, groups, dest, layout, mode, dup)
            report = iv.build_report(source, dest, records, skipped, groups, plan, errors + herr, layout, mode)
            return {"dest": dest, "records": {str(r.path): r for r in records},
                    "groups": groups, "plan": plan, "report": report}

        self._run(work, self._show_preview, "Scanning photos and checking for duplicates...")

    def _show_preview(self, result):
        if result is None:
            self.state = None
            self.summary_var.set("No images found in the source folder.")
            self.status_var.set("Nothing to do")
            return
        self.state = result
        rep, plan = result["report"], result["plan"]

        for tree in (self.ops, self.albums, self.dups):
            tree.delete(*tree.get_children())
        dup_paths = set()
        for i, item in enumerate(plan):
            is_dup = "duplicate" in item.reason
            if is_dup:
                dup_paths.add(str(item.src))
            tag = "skip" if item.action == "skip-duplicate" else ("dup" if is_dup else "")
            self.ops.insert("", "end", iid=str(i), tags=(tag,),
                            values=(item.action, item.src.name,
                                    str(item.dst.relative_to(result["dest"])) if item.dst else "-", item.reason))
        for album, count in rep["albums"].items():
            self.albums.insert("", "end", values=(album, count))
        for n, group in enumerate(result["groups"].values(), 1):
            ordered = sorted(group, key=lambda r: (r.taken, str(r.path)))
            parent = self.dups.insert("", "end", text=f"Group {n}  ({len(group)} identical files)",
                                      values=(iv.human_size(group[0].size), ""), open=True)
            for j, r in enumerate(ordered):
                self.dups.insert(parent, "end", text="original (kept)" if j == 0 else "duplicate",
                                 values=(iv.human_size(r.size), str(r.path)), tags=("orig",) if j == 0 else ())
        self.warn_text.delete("1.0", "end")
        msgs = rep["warnings"] + [f"ERROR: {e}" for e in rep["errors"]]
        self.warn_text.insert("end", "\n".join(msgs) if msgs else "No warnings. Everything looks good.")

        # cards
        c = self.cards
        c["images"][0].config(text=str(rep["images_found"]))
        c["images"][1].config(text=iv.human_size(rep["total_size"]) + " in total")
        c["dups"][0].config(text=str(rep["duplicate_files"]))
        c["dups"][1].config(text=f"in {rep['duplicate_groups']} group(s)")
        c["wasted"][0].config(text=iv.human_size(rep["wasted_space"]))
        c["wasted"][1].config(text="could be reclaimed")
        c["albums"][0].config(text=str(len(rep["albums"])))
        c["albums"][1].config(text=f"layout: {rep['layout']}")
        # charts
        names = {"exif": "EXIF metadata", "filename": "File name", "file-modified": "File modified date"}
        self.charts["types"].set_data(sorted(rep["by_type"].items(), key=lambda kv: -kv[1]))
        self.charts["dates"].set_data(sorted(((names.get(k, k), v) for k, v in rep["date_sources"].items()),
                                             key=lambda kv: -kv[1]))
        self.charts["albums"].set_data(sorted(rep["albums"].items(), key=lambda kv: -kv[1]))
        self._fill_gallery(result["records"], dup_paths)

        self.summary_var.set("Preview only - no files have been changed. Review the tabs, then click Apply.")
        extra = f" (gallery shows first {MAX_TILES})" if len(result["records"]) > MAX_TILES else ""
        self.status_var.set(f"Preview ready: {len(plan)} planned operation(s){extra}")
        self.nb.select(0)

    # --------------------------------------------------------------- details
    def _on_select_op(self, _e=None):
        sel = self.ops.selection()
        if sel and self.state:
            self._show_details(self.state["plan"][int(sel[0])].src)

    def _show_details(self, path: Path):
        rec = self.state["records"].get(str(path)) if self.state else None
        self.detail_ref = None
        if THUMBS:
            try:
                with Image.open(path) as im:
                    im = im.convert("RGB")
                    im.thumbnail((234, 196))
                    self.detail_ref = ImageTk.PhotoImage(im)
            except Exception:
                pass
        if self.detail_ref:
            self.d_img.config(image=self.detail_ref, text="")
        else:
            self.d_img.config(image="", text="No preview available")
        self.d_name.config(text=path.name)
        if rec:
            dims = f"{rec.width} x {rec.height}" if rec.width else "unknown"
            self.d_meta.config(text=(f"Taken     {rec.taken:%d %b %Y, %H:%M}\nDate from  {rec.date_source}\n"
                                     f"Camera    {rec.camera}\nType        {rec.file_type}\n"
                                     f"Size         {iv.human_size(rec.size)}\nDimensions {dims}"))

    # --------------------------------------------------------------- apply
    def on_apply(self):
        if not self.state:
            return
        plan, dest = self.state["plan"], self.state["dest"]
        n, mode = sum(1 for p in plan if p.dst), self.mode_var.get()
        warn = "\n\nMove mode removes files from the source folder (Undo can restore them)." if mode == "move" else ""
        if not messagebox.askyesno("Confirm", f"{mode.capitalize()} {n} file(s) into:\n{dest}\n\nContinue?{warn}"):
            self.status_var.set("Cancelled. No files were changed.")
            return
        self._run(lambda: iv.execute_plan(plan, dest), self._after_apply, f"{mode.capitalize()}ing files...")

    def _after_apply(self, result):
        done, failed = result
        self.state = None
        self.summary_var.set(f"Finished: {len(done)} file(s) processed, {len(failed)} failed. Use Undo to revert.")
        self.status_var.set("Done")
        text = f"{len(done)} file(s) processed successfully."
        if failed:
            text += f"\n\n{len(failed)} failed:\n" + "\n".join(failed[:10])
        messagebox.showinfo("ImageVault", text)

    # ---------------------------------------------------------------- undo
    def on_undo(self):
        dst = self.dst_var.get().strip()
        if not dst:
            messagebox.showwarning("ImageVault", "Choose the destination folder of the run you want to undo.")
            return
        if not messagebox.askyesno("Undo", f"Revert the last run in:\n{dst}?"):
            return

        def done(n):
            self.status_var.set(f"Undo complete: {n} operation(s) reverted.")
            self.summary_var.set("Last run reverted.")
            messagebox.showinfo("ImageVault", f"Reverted {n} operation(s).")

        self._run(lambda: iv.undo_last_run(Path(dst)), done, "Reverting last run...")

    # -------------------------------------------------------------- report
    def on_save_report(self):
        if not self.state:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".json", title="Save report",
            filetypes=[("JSON", "*.json"), ("CSV (operations)", "*.csv"), ("Text", "*.txt")])
        if not path:
            return
        try:
            iv.save_report(self.state["report"], Path(path))
            self.status_var.set(f"Report saved: {path}")
        except iv.ImageVaultError as exc:
            messagebox.showerror("ImageVault", str(exc))


def main():
    root = tk.Tk()
    ImageVaultApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
