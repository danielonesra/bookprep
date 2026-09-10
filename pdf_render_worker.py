"""
pdf_render_worker.py — runs PDF rendering (PyMuPDF/fitz) in an isolated
subprocess instead of a background thread inside the GUI process.

Why isolated at all: MuPDF's C library is not reliably safe to call from
a plain worker thread (e.g. a QThreadPool thread) inside a larger
application — under some environments (observed under WSL2) a call into
it from a background thread can segfault the entire process instead of
raising a catchable Python exception, since a native crash happens below
the interpreter and can't be caught by try/except. A segfault in a
*child* process, on the other hand, just terminates that child cleanly;
the parent gets a normal, catchable error and stays alive.

Why the child streams pages to disk instead of returning them: the
straightforward approach — render every page into memory, return the
whole list — means the entire rendered book has to be pickled and piped
across the process boundary in one shot. For anything but a short book,
that can be gigabytes of raw pixel data held in both processes at once
(rendering + serializing in the child, deserializing in the parent),
which is exactly the kind of thing that grinds a machine to a halt and
eventually crashes or is killed for using too much memory. Instead the
child writes each page straight to its final location on disk as it's
rendered and only sends back small per-page metadata (dpi, physical
size, a generated id) — the parent never receives any pixel data at all.

The pool is started once, eagerly, from the main thread very early in
startup (see main.py) — deliberately *before* any QThreadPool workers
exist, since forking a new process while other threads are already
running can itself be fragile.
"""

import os
import multiprocessing
import concurrent.futures
from concurrent.futures.process import BrokenProcessPool

import fitz  # PyMuPDF
import processing as proc

_executor = None


def init_executor(max_workers=1):
    """Call once, early, from the main thread (see main.py)."""
    global _executor
    if _executor is None:
        _executor = concurrent.futures.ProcessPoolExecutor(max_workers=max_workers)
        # Touch the pool immediately so the worker process is forked now,
        # while startup is still simple, rather than lazily on first use.
        _executor.submit(_noop).result(timeout=30)
    return _executor


def _noop():
    return True


def _render_worker(pdf_path, out_dir, target_dpi):
    # Executes in the child process — completely separate memory space,
    # completely separate fitz/MuPDF state, from the GUI process. Streams
    # each page to out_dir as it's rendered (see render_pdf_pages_to_files).
    return proc.render_pdf_pages_to_files(pdf_path, out_dir, target_dpi=target_dpi)


def render_pdf_pages_isolated(pdf_path, out_dir, target_dpi=400, timeout=1800, progress_cb=None):
    """
    Render a PDF's pages, in the isolated subprocess, streaming each page
    directly to out_dir (see render_pdf_pages_to_files — this is what
    avoids holding the whole book in memory). Returns the same lightweight
    per-page metadata list that function returns; no pixel data crosses
    the process boundary.

    Progress is inferred by polling out_dir for new files rather than via
    an explicit IPC channel — simpler and more robust than trying to pass
    a multiprocessing.Queue through a ProcessPoolExecutor (which doesn't
    support that; Queues can only be shared via direct process inheritance,
    not pickled through submit()).

    Raises a plain RuntimeError with a clear message (instead of crashing
    the app) if the subprocess dies or times out.
    """
    global _executor
    if _executor is None:
        init_executor()

    # Cheap: just reads the page count, doesn't render anything.
    n_total = None
    try:
        doc = fitz.open(pdf_path)
        n_total = len(doc)
        doc.close()
    except Exception:
        pass  # if this fails, the real render call below will raise a clear error

    os.makedirs(out_dir, exist_ok=True)
    existing_files = set(os.listdir(out_dir))

    try:
        future = _executor.submit(_render_worker, pdf_path, out_dir, target_dpi)
    except BrokenProcessPool:
        _executor = None
        init_executor()
        future = _executor.submit(_render_worker, pdf_path, out_dir, target_dpi)

    elapsed = 0.0
    poll_interval = 0.25
    try:
        while True:
            if progress_cb and n_total:
                try:
                    new_count = len(set(os.listdir(out_dir)) - existing_files)
                except OSError:
                    new_count = 0
                progress_cb(min(new_count, n_total), n_total)

            try:
                return future.result(timeout=poll_interval)
            except concurrent.futures.TimeoutError:
                elapsed += poll_interval
                if elapsed > timeout:
                    future.cancel()
                    raise RuntimeError(
                        f"Rendering '{pdf_path}' took too long and was aborted "
                        f"(timeout {timeout}s). Very large or high-page-count "
                        "PDFs may need to be split into smaller files."
                    )
                continue
    except BrokenProcessPool as e:
        _executor = None  # pool is dead — rebuild it next call
        raise RuntimeError(
            f"PDF rendering crashed while processing '{pdf_path}'. "
            "This usually means the PDF is unusually large, malformed, or "
            "uses a feature that trips up the PDF renderer. Try re-saving "
            "or optimizing the PDF (e.g. with Ghostscript or a PDF repair "
            "tool), or splitting it into smaller files, then add it again."
        ) from e


def shutdown_executor():
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
        _executor = None
