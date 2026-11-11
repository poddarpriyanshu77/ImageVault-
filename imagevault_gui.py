#!/usr/bin/env python3
"""
ImageVault GUI - Tkinter front-end for imagevault.py
----------------------------------------------------
Run:  python imagevault_gui.py

Workflow: choose folders -> Preview (nothing is changed) -> review tables ->
Apply (asks for confirmation) -> Undo if needed.
Scanning and file operations run in a background thread so the window never freezes.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import imagevault as iv

try:
    from PIL import Image, ImageTk
    THUMBS = True
except ImportError:  # thumbnails are optional
    THUMBS = False


class ImageVaultApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("ImageVault - Photo Metadata and Album Organizer")
        root.geometry("1180x720")
        root.minsize(980, 600)

        self.q: queue.Queue = queue.Queue()
        self.state: dict | None = None          # result of the last preview
        self.thumb_ref = None                   # keep a reference or Tk drops the image

        self.src_var = tk.StringVar()
        self.dst_var = tk.StringVar()
        self.layout_var = tk.StringVar(value="date")
        self.mode_var = tk.StringVar(value="copy")
        self.dup_var = tk.StringVar(value="separate")
        self.summary_var = tk.StringVar(value="Choose a source and destination folder, then click Preview.")
        self.status_var = tk.StringVar(value="Ready")

        self._build_ui()
        for v in (self.src_var, self.dst_var, self.layout_var, self.mode_var, self.dup_var):
            v.trace_add("write", self._invalidate)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.LabelFrame(self.root, text="1. Folders")
        top.pack(fill="x", padx=10, pady=(10, 4))
        top.columnconfigure(1, weight=1)
        for row, (label, var) in enumerate((("Source (photos):", self.src_var),
                                            ("Destination (albums):", self.dst_var))):
            ttk.Label(top, text=label).grid(row=row, column=0, sticky="w", **pad)
            ttk.Entry(top, textvariable=var).grid(row=row, column=1, sticky="ew", **pad)
            ttk.Button(top, text="Browse...", command=lambda v=var: self._browse(v)).grid(row=row, column=2, **pad)

        opt = ttk.LabelFrame(self.root, text="2. Options")
        opt.pack(fill="x", padx=10, pady=4)
        ttk.Label(opt, text="Album layout:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(opt, textvariable=self.layout_var, values=iv.LAYOUTS, state="readonly",
                     width=12).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(opt, text="Mode:").grid(row=0, column=2, sticky="e", **pad)
        ttk.Radiobutton(opt, text="Copy (safe)", variable=self.mode_var, value="copy").grid(row=0, column=3, **pad)
        ttk.Radiobutton(opt, text="Move", variable=self.mode_var, value="move").grid(row=0, column=4, **pad)
        ttk.Label(opt, text="Duplicates:").grid(row=0, column=5, sticky="e", **pad)
        ttk.Radiobutton(opt, text="Put in _Duplicates", variable=self.dup_var, value="separate").grid(row=0, column=6, **pad)
        ttk.Radiobutton(opt, text="Skip", variable=self.dup_var, value="skip").grid(row=0, column=7, **pad)

        act = ttk.LabelFrame(self.root, text="3. Actions")
        act.pack(fill="x", padx=10, pady=4)
        self.btn_preview = ttk.Button(act, text="Preview (no changes)", command=self.on_preview)
        self.btn_apply = ttk.Button(act, text="Apply", command=self.on_apply, state="disabled")
        self.btn_save = ttk.Button(act, text="Save report...", command=self.on_save_report, state="disabled")
        self.btn_undo = ttk.Button(act, text="Undo last run", command=self.on_undo)
        for b in (self.btn_preview, self.btn_apply, self.btn_save, self.btn_undo):
            b.pack(side="left", **pad)

        ttk.Label(self.root, textvariable=self.summary_var, wraplength=1120,
                  font=("TkDefaultFont", 10, "bold")).pack(fill="x", padx=12, pady=4)

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=4)
        nb = ttk.Notebook(paned)
        paned.add(nb, weight=4)

        # Operations tab
        self.ops = self._make_tree(nb, "Operations", ("action", "file", "destination", "reason"),
                                   ("Action", "File", "Destination", "Reason"), (100, 170, 320, 220))
        self.ops.tag_configure("dup", foreground="#b45f06")
        self.ops.tag_configure("skip", foreground="#888888")
        self.ops.bind("<<TreeviewSelect>>", self._on_select_op)
        # Albums tab
        self.albums = self._make_tree(nb, "Albums", ("album", "count"), ("Album folder", "Files"), (380, 80))
        # Duplicates tab
        self.dups = self._make_tree(nb, "Duplicates", ("size", "path"), ("Size", "Path"), (90, 480), show_tree=True)
        self.dups.tag_configure("orig", foreground="#2e7d32")
        # Warnings tab
        wf = ttk.Frame(nb)
        nb.add(wf, text="Warnings")
        self.warn_text = tk.Text(wf, wrap="word", height=8)
        self.warn_text.pack(fill="both", expand=True)

        # Details / thumbnail pane
        right = ttk.LabelFrame(paned, text="Selected photo")
        paned.add(right, weight=1)
        self.thumb = ttk.Label(right, text="Select a row in\nOperations to see\na thumbnail", anchor="center")
        self.thumb.pack(fill="x", padx=8, pady=8)
        self.details = ttk.Label(right, text="", justify="left", wraplength=250)
        self.details.pack(fill="x", padx=8)

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(4, 10))
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=160)
        self.progress.pack(side="left")
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=10)

    def _make_tree(self, nb, title, cols, heads, widths, show_tree=False):
        frame = ttk.Frame(nb)
        nb.add(frame, text=title)
        tree = ttk.Treeview(frame, columns=cols, show="tree headings" if show_tree else "headings")
        if show_tree:
            tree.column("#0", width=230)
            tree.heading("#0", text="Group / file")
        for c, h, w in zip(cols, heads, widths):
            tree.heading(c, text=h)
            tree.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return tree

    # ------------------------------------------------------------- helpers
    def _browse(self, var: tk.StringVar):
        folder = filedialog.askdirectory(title="Choose folder")
        if folder:
            var.set(folder)

    def _invalidate(self, *_):
        """Any change to folders/options makes the preview stale."""
        if self.state is not None:
            self.state = None
            self.btn_apply.config(state="disabled")
            self.btn_save.config(state="disabled")
            self.status_var.set("Settings changed - click Preview again.")

    def _set_busy(self, busy: bool, msg: str = ""):
        state = "disabled" if busy else "normal"
        for b in (self.btn_preview, self.btn_undo):
            b.config(state=state)
        if busy:
            self.btn_apply.config(state="disabled")
            self.progress.start(12)
            self.status_var.set(msg)
        else:
            self.progress.stop()
            self.btn_apply.config(state="normal" if self.state else "disabled")
            self.btn_save.config(state="normal" if self.state else "disabled")

    def _run(self, work, done, busy_msg):
        """Run `work` in a thread; call `done(result)` on the Tk thread when finished."""
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
        for i, item in enumerate(plan):
            tag = "skip" if item.action == "skip-duplicate" else ("dup" if "duplicate" in item.reason else "")
            self.ops.insert("", "end", iid=str(i), tags=(tag,),
                            values=(item.action, item.src.name, str(item.dst) if item.dst else "-", item.reason))
        for album, count in rep["albums"].items():
            self.albums.insert("", "end", values=(album, count))
        for n, group in enumerate(result["groups"].values(), 1):
            ordered = sorted(group, key=lambda r: (r.taken, str(r.path)))
            parent = self.dups.insert("", "end", text=f"Group {n} ({len(group)} identical files)",
                                      values=(iv.human_size(group[0].size), ""), open=True)
            for j, r in enumerate(ordered):
                self.dups.insert(parent, "end", text="original (kept)" if j == 0 else "duplicate",
                                 values=(iv.human_size(r.size), str(r.path)), tags=("orig",) if j == 0 else ())
        self.warn_text.delete("1.0", "end")
        msgs = rep["warnings"] + [f"ERROR: {e}" for e in rep["errors"]]
        self.warn_text.insert("end", "\n".join(msgs) if msgs else "No warnings.")

        types = ", ".join(f"{k}: {v}" for k, v in sorted(rep["by_type"].items()))
        self.summary_var.set(
            f"{rep['images_found']} images ({iv.human_size(rep['total_size'])}) | {types} | "
            f"{rep['duplicate_files']} duplicate(s) wasting {iv.human_size(rep['wasted_space'])} | "
            f"{len(rep['albums'])} album folder(s) | PREVIEW ONLY - nothing has been changed")
        self.status_var.set("Preview ready. Review the tabs, then click Apply.")

    def _on_select_op(self, _event=None):
        sel = self.ops.selection()
        if not sel or not self.state:
            return
        item = self.state["plan"][int(sel[0])]
        rec = self.state["records"].get(str(item.src))
        if rec:
            dims = f"{rec.width} x {rec.height}" if rec.width else "unknown"
            self.details.config(text=(f"{rec.path.name}\n\nTaken: {rec.taken:%d %b %Y %H:%M}\n"
                                      f"Date from: {rec.date_source}\nCamera: {rec.camera}\n"
                                      f"Type: {rec.file_type}\nSize: {iv.human_size(rec.size)}\n"
                                      f"Dimensions: {dims}"))
        self.thumb_ref = None
        if THUMBS:
            try:
                with Image.open(item.src) as img:
                    img.thumbnail((260, 260))
                    self.thumb_ref = ImageTk.PhotoImage(img.copy())
                self.thumb.config(image=self.thumb_ref, text="")
                return
            except Exception:
                pass
        self.thumb.config(image="", text="(thumbnail not available)")

    # --------------------------------------------------------------- apply
    def on_apply(self):
        if not self.state:
            return
        plan, dest = self.state["plan"], self.state["dest"]
        n = sum(1 for p in plan if p.dst)
        mode = self.mode_var.get()
        warn = "\n\nMove mode removes files from the source folder (Undo can restore them)." if mode == "move" else ""
        if not messagebox.askyesno("Confirm", f"{mode.capitalize()} {n} file(s) into:\n{dest}\n\nContinue?{warn}"):
            self.status_var.set("Cancelled. No files were changed.")
            return
        self._run(lambda: iv.execute_plan(plan, dest), self._after_apply, f"{mode.capitalize()}ing files...")

    def _after_apply(self, result):
        done, failed = result
        self.state = None            # preview is stale after changing files
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
        self._run(lambda: iv.undo_last_run(Path(dst)),
                  lambda n: (self.status_var.set(f"Undo complete: {n} operation(s) reverted."),
                             messagebox.showinfo("ImageVault", f"Reverted {n} operation(s).")),
                  "Reverting last run...")

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
