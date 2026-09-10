"""
processing.py — core image pipeline for BookPrep.

Responsibilities:
  1. Render PDF pages to high-res raster images (fitz/PyMuPDF).
  2. Auto-detect the text block on a page (content bounding box), the way
     ScanTailor does, so scans with uneven borders / different source
     trim sizes can be cropped down to "just the text".
  3. Normalize resolution: every source PDF may have been scanned at a
     different DPI / physical page size. We re-scale each page's detected
     text block to a common physical size (e.g. text block is always
     X inches wide) before laying it onto the final trim size, so the body
     text reads the same point-size throughout the finished book.
  4. Enhance: grayscale -> contrast stretch -> binarize -> thicken strokes,
     similar to what Google Books / Lulu-ready scans look like.
  5. Compose the final print-ready page: a white canvas at the target trim
     size and DPI, with margins, and the processed text block placed in it.

All pixel-space functions operate on numpy arrays (OpenCV convention,
uint8, single channel or BGR).
"""

import io
import os
import json
import uuid
import time
import numpy as np
import cv2
import fitz  # PyMuPDF
from PIL import Image


# ----------------------------------------------------------------------
# 1. PDF -> page images
# ----------------------------------------------------------------------

def render_pdf_pages_to_files(pdf_path, out_dir, target_dpi=400, progress_cb=None):
    """
    Render every page of a PDF directly to individual PNG files in
    out_dir, one page at a time — never holding more than a single
    page's full-resolution pixel data in memory.

    This matters a lot for anything but very short books: the naive
    approach of rendering every page into one big in-memory list (as
    render_pdf_pages() below does) means a several-hundred-page book at
    400dpi can easily add up to multiple gigabytes held simultaneously —
    and when that whole list then has to be pickled and piped across a
    process boundary (this is called from an isolated subprocess; see
    pdf_render_worker.py), that multi-gigabyte serialization is exactly
    the kind of thing that grinds a machine to a halt and eventually
    crashes. Streaming to disk page-by-page keeps peak memory roughly
    constant regardless of how long the book is.

    Returns a list of dicts, one per page, in page order:
        {"uid": <new unique id>, "src_dpi":, "src_width_in":, "src_height_in":}
    Each page's PNG is saved at os.path.join(out_dir, f"{uid}.png").
    progress_cb(i, n), if given, is called after each page (1-indexed).
    """
    os.makedirs(out_dir, exist_ok=True)
    doc = fitz.open(pdf_path)
    results = []
    try:
        n = len(doc)
        for i in range(n):
            page = doc[i]
            rect = page.rect  # in points, 72 pt = 1 inch
            width_in = rect.width / 72.0
            height_in = rect.height / 72.0

            zoom = target_dpi / 72.0
            max_dim_in = max(width_in, height_in, 0.01)
            # cap so neither pixel dimension exceeds ~6000px, regardless
            # of how physically large the page is
            capped_zoom = 6000.0 / (max_dim_in * 72.0)
            zoom = min(zoom, capped_zoom)

            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
            actual_dpi = pix.width / width_in if width_in > 0 else target_dpi

            uid = uuid.uuid4().hex[:12]
            out_path = os.path.join(out_dir, f"{uid}.png")
            # PyMuPDF always rasterizes through RGB regardless of the
            # source PDF's actual color depth, so a page that's really
            # monochrome or grayscale (the common case for text-only
            # book pages, even from a color-capable source PDF) would
            # otherwise get stored at 3x its necessary size for the rest
            # of the project's lifetime -- raw working files, every
            # preview render, and (if B&W processing is ever turned off
            # for that page) the final export too. Check directly rather
            # than assume: if every pixel has R==G==B, save as single-
            # channel grayscale instead; this is a strict equality check,
            # so a page that's already collapsed this way is bit-for-bit
            # recoverable as if it had stayed RGB -- nothing is lost.
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            collapsed = collapse_redundant_grayscale(arr)
            if collapsed.ndim == 2:
                cv2.imwrite(out_path, collapsed)
            else:
                pix.save(out_path)

            results.append({
                "uid": uid,
                "src_dpi": actual_dpi,
                "src_width_in": width_in,
                "src_height_in": height_in,
            })

            # drop references before the next iteration so the previous
            # page's pixel buffer can be freed rather than accumulating
            pix = None
            page = None

            if progress_cb:
                progress_cb(i + 1, n)
    finally:
        doc.close()
    return results


def render_pdf_pages(pdf_path, target_dpi=400, max_dpi=600):
    """
    Render every page of a PDF to a numpy BGR image, all held in memory
    at once. Convenient for small/one-off use (tests, scripts), but NOT
    what the app uses for adding books — see render_pdf_pages_to_files()
    above, which streams to disk instead of accumulating every page's
    pixels in one list. For a long book this function's memory use grows
    with page count and can become very large.

    Returns a list of dicts:
        { "image": np.ndarray (BGR), "src_dpi": float,
          "src_width_in": float, "src_height_in": float }

    src_dpi is recovered from the page's native point size vs the pixel
    size we chose to render at — this is what lets us later normalize
    pages that came from scans of different original resolutions.
    """
    doc = fitz.open(pdf_path)
    pages = []
    for page in doc:
        rect = page.rect  # in points, 72 pt = 1 inch
        width_in = rect.width / 72.0
        height_in = rect.height / 72.0

        # Don't render absurdly large pages past a sane pixel ceiling
        zoom = target_dpi / 72.0
        max_dim_in = max(width_in, height_in, 0.01)
        capped_zoom = 6000.0 / (max_dim_in * 72.0)
        zoom = min(zoom, capped_zoom)

        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, colorspace=fitz.csRGB, alpha=False)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
            pix.height, pix.width, pix.n
        )
        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)

        actual_dpi = pix.width / width_in if width_in > 0 else target_dpi

        pages.append({
            "image": img_bgr,
            "src_dpi": actual_dpi,
            "src_width_in": width_in,
            "src_height_in": height_in,
        })
    doc.close()
    return pages


# ----------------------------------------------------------------------
# 2. Auto text-block detection
# ----------------------------------------------------------------------

def _merge_boxes(boxes, gap_x, gap_y):
    """Repeatedly merge any two boxes whose bounds (expanded by gap_x/gap_y)
    overlap, until no more merges are possible. boxes: list of
    [x0, y0, x1, y1, area]. Returns the resulting list of merged clusters."""
    boxes = [list(b) for b in boxes]
    changed = True
    while changed:
        changed = False
        for i in range(len(boxes)):
            if boxes[i] is None:
                continue
            for j in range(i + 1, len(boxes)):
                if boxes[j] is None:
                    continue
                a, b = boxes[i], boxes[j]
                overlap = not (
                    a[0] - gap_x > b[2] or b[0] - gap_x > a[2] or
                    a[1] - gap_y > b[3] or b[1] - gap_y > a[3]
                )
                if overlap:
                    boxes[i] = [
                        min(a[0], b[0]), min(a[1], b[1]),
                        max(a[2], b[2]), max(a[3], b[3]),
                        a[4] + b[4],
                    ]
                    boxes[j] = None
                    changed = True
        boxes = [b for b in boxes if b is not None]
    return boxes


def _vertical_gap(a, b):
    if a[3] < b[1]:
        return b[1] - a[3]
    if b[3] < a[1]:
        return a[1] - b[3]
    return 0


def _horizontal_overlap_frac(a, b):
    left = max(a[0], b[0])
    right = min(a[2], b[2])
    if right <= left:
        return 0.0
    narrower = min(a[2] - a[0], b[2] - b[0])
    return (right - left) / narrower if narrower > 0 else 0.0


def _merge_same_column(clusters, min_overlap_frac, max_vgap):
    """Bridge clusters that sit in the same horizontal column (e.g. two
    paragraphs either side of a chapter break, or a drop-cap opening)
    even when the gap between them is too large for _merge_boxes — but
    only when they substantially share the same horizontal extent, so a
    corner watermark or an off-column stamp still doesn't get pulled in
    just because it happens to be within some fixed distance."""
    clusters = [list(c) for c in clusters]
    changed = True
    while changed:
        changed = False
        for i in range(len(clusters)):
            if clusters[i] is None:
                continue
            for j in range(i + 1, len(clusters)):
                if clusters[j] is None:
                    continue
                a, b = clusters[i], clusters[j]
                if (_vertical_gap(a, b) <= max_vgap and
                        _horizontal_overlap_frac(a, b) >= min_overlap_frac):
                    clusters[i] = [
                        min(a[0], b[0]), min(a[1], b[1]),
                        max(a[2], b[2]), max(a[3], b[3]),
                        a[4] + b[4],
                    ]
                    clusters[j] = None
                    changed = True
        clusters = [c for c in clusters if c is not None]
    return clusters


def detect_text_bbox(img_bgr, pad_frac=0.01, min_area_frac=0.0003,
                      cluster_gap_frac=0.025, column_vgap_frac=0.10,
                      column_overlap_frac=0.4, threshold_method="otsu",
                      border_strip_frac=0.03):
    """
    Detect the bounding box of the main body-text block on a scanned
    page, ignoring scanner borders, punch holes, and noise speckles —
    and, importantly, ignoring things that aren't *part of* the body
    text: a page number, a "Digitized by Google" watermark, a library
    stamp, a corner smudge. These are extremely common on real-world
    scans and are usually spatially separate from the actual text block,
    so simply unioning every surviving contour's bounding box (as a
    naive implementation would) lets a single stray mark blow the crop
    out to include it.

    Strategy (ScanTailor-style, with two-pass clustering):
      - grayscale, threshold (inverted: ink = white) — one of the same
        methods used for the final B&&W conversion (threshold_method:
        "otsu" by default, "adaptive", or any of sauvola/wolf/fox/window/
        bradley/grad/edgeplus/blurdiv/edgediv — see enhance_bw()'s
        docstring for what each does). Local methods (Sauvola, Wolf, etc.)
        can help on scans with uneven lighting or a gradient/shadowed page
      - strip a border margin (border_strip_frac) to remove scanner-bed
        edges and binding/gutter shadows before they can be mistaken for
        content — see the note below on why this matters
      - morphological close with a wide horizontal+vertical kernel to
        fuse individual glyphs/lines into solid paragraph blobs
      - drop tiny contours (speckle noise, punch holes) — min_area_frac
      - pass 1: cluster the remaining contours by simple proximity
        (cluster_gap_frac), so ordinary word/line gaps merge into
        per-paragraph blobs
      - pass 2: bridge clusters that share the same horizontal column
        (column_overlap_frac) even across a larger gap than pass 1 allows
        (column_vgap_frac) — e.g. a chapter-break gap or a drop-cap
        opening — matched on horizontal overlap, not raw distance, which
        is what lets it skip a watermark or stamp that happens to sit in
        a similar vertical position but a different horizontal one
      - pick the cluster with the greatest actual ink-pixel count as the
        body text block, and discard the rest
      - bbox = that cluster's bounds, padded by pad_frac

    Real scans vary a lot, so this is tuned to be a good starting point
    rather than perfect on every page — the app's manual crop-box editing
    (per page or in bulk) is there for outliers.

    Why border_strip_frac matters (and defaults to 3%, not a sliver):
    a scanner-bed edge or a binding/gutter shadow on a thick book forms a
    dark border or gradient hugging the page edges. Otsu's threshold
    happily classifies that as "ink" too, and once morphological closing
    bridges it to the real text, cv2.findContours returns ONE contour
    whose bounding box is nearly the whole page. Worse, cv2.contourArea()
    on that contour reports close to the full page area (it doesn't
    account for the contour being a mostly-hollow frame/ring shape, not a
    solid blob) — so even ranking by area doesn't save you, because the
    border "wins" on paper despite having very little actual ink. That's
    why area-ranking below uses real ink-pixel counts (from the
    pre-closing threshold mask) rather than cv2.contourArea: it's the fix
    for that specific failure mode, not just a style preference. Scans
    with a thick, dark surrounding border/shadow (common on non-Google,
    non-pre-cropped color scans) need border_strip_frac raised enough to
    physically separate that border from the text before closing ever
    runs — density-based ranking alone can't fix it if they're already
    one connected blob at the pixel level.

    Parameters (all as a fraction of page width/height unless noted):
      pad_frac: extra margin added around the final detected box
      min_area_frac: contours with fewer ink pixels than this (as a
        fraction of page area) are discarded as noise before clustering
        even starts — raise this to ignore small stray marks outright
      cluster_gap_frac: pass-1 merge distance (word/line/paragraph gaps)
      column_vgap_frac: pass-2 merge distance for same-column content —
        raise this to bridge bigger gaps (chapter breaks), but it also
        makes the algorithm more willing to sweep in something distant
        that happens to align with the text column
      column_overlap_frac: how much horizontal overlap (0-1) two blobs
        need for pass 2 to consider them "the same column"
      threshold_method: which thresholding method to use before contour-
        finding — "otsu" (default, single global cutoff), "adaptive"
        (locally-adaptive, helps on unevenly-lit scans), or any of the
        ScanTailor Advanced local methods also offered for the final
        B&&W conversion ("sauvola", "wolf", "fox", "window", "bradley",
        "grad", "edgeplus", "blurdiv", "edgediv"). If Otsu is misreading
        a scan's ink/paper split, matching whichever method works well
        for that scan's final B&&W output is a reasonable thing to try
        here too.
      border_strip_frac: width (as a fraction of the page's shorter
        dimension) of the outer margin zeroed out before contour-finding,
        to keep scanner-bed edges and binding shadows out of the running
        entirely. Raise this if a scan has an unusually wide dark border
        or gutter shadow that's still getting swept into the detected
        box; lower it if genuine content sits very close to the physical
        page edge and is being clipped.

    Returns (x, y, w, h) in pixel coordinates of img_bgr, or None if no
    content was found (blank page).
    """
    h_img, w_img = img_bgr.shape[:2]
    gray = img_bgr if img_bgr.ndim == 2 else cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    # Slight blur to suppress scan grain before thresholding
    gray_blur = cv2.GaussianBlur(gray, (3, 3), 0)
    if threshold_method == "adaptive":
        block = max(15, (min(gray_blur.shape) // 12) | 1)  # odd
        thresh = cv2.adaptiveThreshold(
            gray_blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, block, 10,
        )
    elif threshold_method == "otsu":
        _, thresh = cv2.threshold(
            gray_blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU
        )
    elif threshold_method in LOCAL_THRESHOLD_METHODS:
        window_px = max(15, (min(gray_blur.shape) // 12) | 1)
        bw = binarize_advanced(gray_blur, threshold_method, window_px=window_px, k=0.34)
        thresh = cv2.bitwise_not(bw)  # binarize_advanced: ink=0; contour-finding wants ink=255
    else:
        raise ValueError(f"unknown threshold_method: {threshold_method!r}")

    # Remove scanner-bed edges / binding shadows before they can be
    # mistaken for content (see docstring above for why this needs to be
    # wide enough to physically separate a real border/shadow from text,
    # not just shave off a sliver)
    edge = max(2, int(border_strip_frac * min(h_img, w_img)))
    thresh[:edge, :] = 0
    thresh[-edge:, :] = 0
    thresh[:, :edge] = 0
    thresh[:, -edge:] = 0

    # Fuse text into paragraph/line blobs
    kernel_w = max(15, w_img // 60)
    kernel_h = max(8, h_img // 150)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, kernel_h))
    closed = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(
        closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
    )

    min_area = min_area_frac * w_img * h_img
    boxes = []
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        # Real ink-pixel count within this contour's bounding box, not
        # cv2.contourArea(c) -- contourArea reports close to the full
        # bounding-box area for a hollow/frame-shaped contour (a border,
        # a decorative rule, a table's outer line), which would otherwise
        # let a mostly-empty frame outscore genuinely dense body text.
        ink_pixels = int(cv2.countNonZero(thresh[y:y + h, x:x + w]))
        if ink_pixels < min_area:
            continue
        boxes.append([x, y, x + w, y + h, ink_pixels])

    if not boxes:
        return None

    gap_x = int(cluster_gap_frac * w_img)
    gap_y = int(cluster_gap_frac * h_img)
    clusters = _merge_boxes(boxes, gap_x, gap_y)
    clusters = _merge_same_column(
        clusters, min_overlap_frac=column_overlap_frac,
        max_vgap=column_vgap_frac * h_img,
    )

    x0, y0, x1, y1, _ = max(clusters, key=lambda c: c[4])

    pad_x = int(pad_frac * w_img)
    pad_y = int(pad_frac * h_img)
    x0 = max(0, x0 - pad_x)
    y0 = max(0, y0 - pad_y)
    x1 = min(w_img, x1 + pad_x)
    y1 = min(h_img, y1 + pad_y)

    return (x0, y0, x1 - x0, y1 - y0)


# ----------------------------------------------------------------------
# 3 & 4. Enhance: contrast, binarize, thicken
# ----------------------------------------------------------------------
#
# Binarization methods below are ports of the algorithms in ScanTailor
# Advanced's imageproc/Binarize.cpp (the ScanTailor-Advanced/scantailor-
# advanced "vigri" fork, which adds Fox/Window/Bradley/Grad/EdgeDiv on top
# of the classic Otsu/Sauvola/Wolf trio). Formulas match that source as
# closely as practical; window-edge handling uses cv2's border replication
# instead of ScanTailor's clipped-window integral images, which gives
# near-identical results without needing a custom integral-image class.

LOCAL_THRESHOLD_METHODS = (
    "sauvola", "wolf", "fox", "window", "bradley", "grad",
    "edgeplus", "blurdiv", "edgediv",
)


def _local_mean_std(gray_f32, win_w, win_h):
    """Local mean and standard deviation over a win_w x win_h window,
    via box filtering (fast, O(n), edge-replicated)."""
    mean = cv2.boxFilter(gray_f32, -1, (win_w, win_h), borderType=cv2.BORDER_REPLICATE)
    sqmean = cv2.boxFilter(gray_f32 * gray_f32, -1, (win_w, win_h), borderType=cv2.BORDER_REPLICATE)
    var = np.clip(sqmean - mean * mean, 0, None)
    std = np.sqrt(var)
    return mean, std


def _binarize_sauvola(gray_f32, win_w, win_h, k, delta):
    mean, std = _local_mean_std(gray_f32, win_w, win_h)
    frac_s = std / 128.0
    frac_d = delta / 128.0
    threshold = mean * (1.0 - k * (1.0 - (frac_s + frac_d)))
    return gray_f32 < threshold


def _binarize_wolf(gray_f32, win_w, win_h, lower, upper, k, delta):
    mean, std = _local_mean_std(gray_f32, win_w, win_h)
    min_gray = float(gray_f32.min())
    max_dev = float(std.max()) or 1.0
    frac_sn = std / max_dev
    frac_d = delta / 128.0
    base = mean - min_gray
    threshold = base * (1.0 - k * (1.0 - (frac_sn + frac_d))) + min_gray
    return (gray_f32 < lower) | ((gray_f32 <= upper) & (gray_f32 < threshold))


def _binarize_fox(gray_f32, win_w, win_h, lower, upper, k, delta):
    mean, _ = _local_mean_std(gray_f32, win_w, win_h)
    min_gray = float(gray_f32.min())
    di = gray_f32 - mean
    deviation = di / (256.0 - di)
    max_dev = float(deviation.max())
    if max_dev <= 0.0:
        max_dev = 1.0
    frac_sn = deviation / max_dev
    frac_d = delta / 128.0
    base = mean - min_gray
    threshold = base * (1.0 - k * 0.5 * (1.0 - (frac_sn + frac_d))) + min_gray
    return (gray_f32 < lower) | ((gray_f32 <= upper) & (gray_f32 < threshold))


def _binarize_window(gray_f32, win_w, win_h, lower, upper, k, delta):
    mean, std = _local_mean_std(gray_f32, win_w, win_h)
    mean_full = float(gray_f32.mean())
    dev_max = float(std.max())
    dev_min = float(std.min())
    dev_range = dev_max - dev_min
    coefw = k * 3.0
    md = (mean + 1.0 - delta) / (mean_full + std + 1.0)
    kdm = (2.0 * mean_full + 1.0) / (std + 1.0)
    kds = (std - dev_min) / dev_range if dev_range > 0 else np.ones_like(std)
    kd = 1.0 + kdm * kds
    threshold = mean * (1.0 - coefw * md / kd)
    return (gray_f32 < lower) | ((gray_f32 <= upper) & (gray_f32 < threshold))


def _binarize_bradley(gray_f32, win_w, win_h, k, delta):
    mean, _ = _local_mean_std(gray_f32, win_w, win_h)
    threshold = mean * (1.0 - k) if k < 1.0 else np.zeros_like(mean)
    return gray_f32 < (threshold + delta)


def _binarize_grad(gray_f32, win_w, win_h, lower, upper, k, delta):
    mean, _ = _local_mean_std(gray_f32, win_w, win_h)
    mean = mean + delta
    g = np.abs(mean - gray_f32)
    sum_g = float(g.sum())
    gvalue = float((gray_f32 * g).sum() / sum_g) if sum_g > 0 else 127.5
    mean_grad = gvalue * (1.0 - k)
    threshold = mean_grad + mean * k
    return (gray_f32 < lower) | ((gray_f32 <= upper) & (gray_f32 < threshold))


def _edgediv_prefilter(gray_f32, win_w, win_h, kep, kbd):
    """EdgePlus / BlurDiv / EdgeDiv: local-contrast prefilter, thresholded
    with a plain Otsu cut afterwards. EdgePlus = kep>0 only, BlurDiv =
    kbd>0 only, EdgeDiv = both (matches ScanTailor's binarizeEdgeDiv)."""
    mean, _ = _local_mean_std(gray_f32, win_w, win_h)
    retval = gray_f32.copy()
    if kep > 0.0:
        edge = (retval + 1.0) / (mean + 1.0) - 0.5
        edgeplus = gray_f32 * edge
        retval = kep * edgeplus + (1.0 - kep) * gray_f32
    if kbd > 0.0:
        edgeinv = (mean + 1.0) / (retval + 1.0) - 0.5
        edgenorm = kbd * edgeinv + (1.0 - kbd)
        retval = np.where(edgenorm > 0.0, retval / edgenorm, retval)
    return np.clip(retval, 0, 255).astype(np.uint8)


def binarize_advanced(gray, method, window_px=35, k=0.34, delta=0.0,
                       lower_bound=1, upper_bound=254):
    """
    Threshold a grayscale uint8 image using one of the ScanTailor Advanced
    local/global methods. Returns a uint8 0/255 image (ink=0, bg=255).

    method: one of LOCAL_THRESHOLD_METHODS ("sauvola", "wolf", "fox",
        "window", "bradley", "grad", "edgeplus", "blurdiv", "edgediv").
    window_px: local window size in pixels (odd; used by every method
        except plain global Otsu).
    k: the method's sensitivity coefficient (meaning differs per method —
        see ScanTailor's Binarize.cpp for the exact formulas).
    delta: small additive/threshold-shift fine-tune, 0 = off.
    lower_bound / upper_bound: for wolf/fox/window/grad, gray levels
        outside [lower_bound, upper_bound] are forced black/left alone
        respectively (guards against pure black/white regions).
    """
    win = max(3, window_px | 1)  # odd
    gray_f32 = gray.astype(np.float32)

    if method == "sauvola":
        black = _binarize_sauvola(gray_f32, win, win, k, delta)
    elif method == "wolf":
        black = _binarize_wolf(gray_f32, win, win, lower_bound, upper_bound, k, delta)
    elif method == "fox":
        black = _binarize_fox(gray_f32, win, win, lower_bound, upper_bound, k, delta)
    elif method == "window":
        black = _binarize_window(gray_f32, win, win, lower_bound, upper_bound, k, delta)
    elif method == "bradley":
        black = _binarize_bradley(gray_f32, win, win, k, delta)
    elif method == "grad":
        black = _binarize_grad(gray_f32, win, win, lower_bound, upper_bound, k, delta)
    elif method in ("edgeplus", "blurdiv", "edgediv"):
        kep = k if method in ("edgeplus", "edgediv") else 0.0
        kbd = k if method in ("blurdiv", "edgediv") else 0.0
        prefiltered = _edgediv_prefilter(gray_f32, win, win, kep, kbd)
        otsu_t, _ = cv2.threshold(prefiltered, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        black = prefiltered.astype(np.float32) < (otsu_t + delta)
    else:
        raise ValueError(f"unknown binarization method: {method!r}")

    bw = np.where(black, 0, 255).astype(np.uint8)
    return bw


def _savgol_kernel_1d(window, polyorder):
    """1D Savitzky-Golay smoothing kernel (odd window, centered), derived
    via least-squares polynomial fit — avoids a scipy dependency."""
    window = max(3, window | 1)
    half = window // 2
    x = np.arange(-half, half + 1, dtype=np.float64)
    A = np.vstack([x ** p for p in range(polyorder + 1)]).T
    # Coefficients that reproduce the fitted polynomial's value at x=0.
    pseudo_inv = np.linalg.pinv(A)
    kernel = pseudo_inv[0]
    return kernel.astype(np.float32)


def savgol_smooth_gray(gray, window=7, polyorder=2):
    """
    Savitzky-Golay smoothing applied separably (rows then columns).
    Removes scan grain while preserving edges better than a Gaussian
    blur of similar strength — used as an optional pre-binarization step.
    """
    window = max(3, window | 1)
    polyorder = min(polyorder, window - 1)
    kernel = _savgol_kernel_1d(window, polyorder)
    gray_f32 = gray.astype(np.float32)
    smoothed = cv2.filter2D(gray_f32, -1, kernel.reshape(1, -1), borderType=cv2.BORDER_REPLICATE)
    smoothed = cv2.filter2D(smoothed, -1, kernel.reshape(-1, 1), borderType=cv2.BORDER_REPLICATE)
    return np.clip(smoothed, 0, 255).astype(np.uint8)


def morphological_smoothing(bw):
    """
    Smooth jagged pixel-stair-stepping on binarized letter edges (open
    then close with a small structuring element) for a crisper printed
    look — applied after thresholding, before despeckle/thicken.
    """
    inv = cv2.bitwise_not(bw)  # ink -> white for morphology
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    inv = cv2.morphologyEx(inv, cv2.MORPH_OPEN, kernel)
    inv = cv2.morphologyEx(inv, cv2.MORPH_CLOSE, kernel)
    return cv2.bitwise_not(inv)


DESPECKLE_PRESETS = {
    # (min_relative_parent_weight, pixels_to_sqdist, big_object_threshold)
    # at a reference DPI of 300 -- ported from ScanTailor Advanced's
    # core/Despeckle.cpp Settings::get(). All three scale with actual DPI.
    "cautious": dict(min_relative_parent_weight=0.125, pixels_to_sqdist=10.0 ** 2, big_object_threshold=7),
    "normal": dict(min_relative_parent_weight=0.175, pixels_to_sqdist=6.5 ** 2, big_object_threshold=12),
    "aggressive": dict(min_relative_parent_weight=0.225, pixels_to_sqdist=3.5 ** 2, big_object_threshold=17),
}


def despeckle_components(bw, dpi=300, level="normal"):
    """
    ScanTailor-Advanced-style despeckle: connected components at least
    `big_object_threshold` pixels wide or tall are kept outright. Every
    other (small) component is kept ONLY if it's close enough to an
    already-kept component of comparable-or-larger size -- this is what
    lets legitimate small marks (accents, dots on i's/j's, punctuation,
    apostrophes) survive because they sit right next to real text, while
    isolated dust specks and smudges far from any real content get
    removed regardless of their exact size. Kept components can "rescue"
    other small components in turn, so this repeats until nothing new
    gets kept.

    This replaces a much simpler approach (a small morphological
    opening), which can only ever remove specks smaller than its kernel
    -- real scan dust/smudges are frequently a few pixels across, bigger
    than that kernel, and would survive untouched. Confirmed by a user
    comparing BookPrep's output to ScanTailor Advanced's on the same
    page/algorithm: identical stray smudges were still present after a
    kernel-only despeckle but are correctly removed by this approach.

    bw: uint8 0/255 image (ink=0, background=255).
    dpi: the image's actual DPI -- every threshold below is tuned at a
        reference DPI of 300 and scales linearly from there, matching
        ScanTailor's own DPI-aware behavior (its thresholds are in
        pixels at whatever DPI the page happens to be scanned/output at).
    level: "off" (no-op), "cautious", "normal" (default -- matches
        ScanTailor Advanced's own default), or "aggressive".
    """
    if level == "off" or level is None:
        return bw
    if level not in DESPECKLE_PRESETS:
        raise ValueError(f"unknown despeckle level: {level!r}")

    preset = DESPECKLE_PRESETS[level]
    dpi_factor = max(dpi, 1) / 300.0
    big_object_threshold = max(1, round(preset["big_object_threshold"] * dpi_factor))
    min_relative_parent_weight = preset["min_relative_parent_weight"] * dpi_factor
    pixels_to_sqdist = preset["pixels_to_sqdist"]
    VERTICAL_SCALE_SQ = 4.0  # matches ScanTailor: vertical distance weighted 2x

    ink = (bw == 0).astype(np.uint8)
    num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    if num_labels <= 1:
        return bw  # nothing but background

    if num_labels > 8000:
        # A real scanned page of text -- even a dense, small-type page --
        # doesn't produce anywhere close to this many separate connected
        # components (confirmed on realistic rendered-text test pages:
        # a few hundred to a couple thousand). This many almost certainly
        # means the input is dominated by fine noise/texture rather than
        # discrete specks-versus-glyphs, which isn't what this algorithm
        # is designed to sort through.
        #
        # This cap was originally set at 25000 based on a back-of-envelope
        # memory estimate, but that turned out to badly underestimate how
        # slow (not just memory-hungry) the rescue loop below gets as
        # component count climbs -- confirmed directly: a 24752-component
        # adversarial test image (noise-heavy, not real text) slipped
        # under that cap and still took long enough, across the 8-round
        # rescue loop, to look indistinguishable from a genuine hang in
        # a background worker thread (traced with faulthandler to prove
        # it was still executing, not actually stuck). 8000 leaves a wide
        # safety margin over any realistic text page while catching this
        # class of input well before it gets slow. The wall-clock budget
        # below is a second line of defense in case some other input
        # shape slips past a purely count-based cap.
        return bw

    x = stats[:, 0].astype(np.float64)
    y = stats[:, 1].astype(np.float64)
    w = stats[:, 2].astype(np.float64)
    h = stats[:, 3].astype(np.float64)
    area = stats[:, 4].astype(np.float64)
    x2, y2 = x + w, y + h

    kept = np.maximum(w, h) >= big_object_threshold
    kept[0] = False  # background (label 0) isn't real content

    if not kept.any():
        # Nothing in the whole image reaches the "definitely real content"
        # size threshold -- normal text at a correctly-matched DPI always
        # has some connected strokes/ligatures that clear this bar, so
        # this means something is off (DPI badly mismatched to the
        # image's actual resolution, unusually thin/light text, or a
        # near-blank/all-noise image). With no seed component to rescue
        # from, every component would be removed, wiping the page to
        # blank -- clearly worse than doing nothing, so skip despeckling
        # rather than risk silently deleting all content.
        return bw

    CHUNK = 2000  # bounds peak memory to CHUNK * len(kept_idx) regardless
    # of the total candidate count, instead of one candidates x kept
    # matrix that scales quadratically with page content.
    deadline = time.monotonic() + 5.0  # hard wall-clock budget -- see cap note above

    for _ in range(8):  # repeat until nothing new gets rescued
        if time.monotonic() > deadline:
            break
        kept_idx = np.nonzero(kept)[0]
        cand_idx = np.nonzero(~kept)[0]
        cand_idx = cand_idx[cand_idx != 0]
        if len(kept_idx) == 0 or len(cand_idx) == 0:
            break

        newly_kept_list = []
        for start in range(0, len(cand_idx), CHUNK):
            batch = cand_idx[start:start + CHUNK]

            # bounding-box-to-bounding-box squared distance (0 if
            # overlapping), batch candidates as rows, currently-kept
            # components as columns
            dx = np.maximum(np.maximum(x[batch, None] - x2[None, kept_idx],
                                        x[None, kept_idx] - x2[batch, None]), 0.0)
            dy = np.maximum(np.maximum(y[batch, None] - y2[None, kept_idx],
                                        y[None, kept_idx] - y2[batch, None]), 0.0)
            sqdist = dx * dx + VERTICAL_SCALE_SQ * dy * dy

            cand_area = area[batch][:, None]
            max_allowed_sqdist = pixels_to_sqdist * cand_area
            size_ok = area[None, kept_idx] >= (min_relative_parent_weight * cand_area)
            eligible = (sqdist <= max_allowed_sqdist) & size_ok

            newly_kept_list.append(batch[eligible.any(axis=1)])

        newly_kept = np.concatenate(newly_kept_list) if newly_kept_list else np.array([], dtype=int)
        if len(newly_kept) == 0:
            break
        kept[newly_kept] = True

    keep_mask = np.isin(labels, np.nonzero(kept)[0])
    return np.where(keep_mask, 0, 255).astype(np.uint8)


def enhance_bw(img_bgr, contrast=1.4, threshold_mode="adaptive",
               fixed_threshold=180, thicken_px=0, despeckle_level="normal", dpi=300,
               window_divisor=12, binarize_k=0.34, binarize_delta=0.0,
               binarize_lower=1, binarize_upper=254,
               savgol_enabled=False, savgol_window=7, savgol_degree=2,
               morph_smoothing=False):
    """
    Convert a (cropped) page image to crisp black-on-white print art.

    contrast: >1 steepens the tone curve around midgray before
              thresholding (helps faint/uneven scans).
    threshold_mode: "adaptive" or "fixed" (the original two modes, kept
              for backwards compatibility with existing projects), or one
              of the ScanTailor Advanced methods: "otsu", "sauvola",
              "wolf", "fox", "window", "bradley", "grad", "edgeplus",
              "blurdiv", "edgediv". See binarize_advanced() for what each
              does; sauvola/wolf are the best general-purpose choices for
              uneven or faint scans.
    fixed_threshold: cutoff used only by "fixed" mode.
    window_divisor: local window size = image's shorter side / this
              value (matches how "adaptive" mode already picks its block
              size) — used by every mode except "otsu"/"fixed". Smaller
              divisor = larger, more global window.
    binarize_k: sensitivity coefficient for the ScanTailor Advanced
              methods (meaning differs per method).
    binarize_delta: small additive threshold fine-tune for those methods.
    binarize_lower / binarize_upper: gray-level bounds used by
              wolf/fox/window/grad to guard pure black/white regions.
    savgol_enabled: apply Savitzky-Golay smoothing to the grayscale image
              before thresholding — removes scan grain while preserving
              edges better than a blur.
    savgol_window / savgol_degree: Savitzky-Golay filter parameters.
    thicken_px: dilate black strokes by this many pixels (0 = off).
              This mimics the bolder, more legible text Google Books
              produces from its B&W reprocessing.
    despeckle_level: "off", "cautious", "normal" (default), or
              "aggressive" — removes small isolated ink specks left over
              from scan grain/dust, using ScanTailor Advanced's
              connected-component approach (see despeckle_components()):
              small marks near real text (accents, punctuation, dots on
              i's) survive; isolated specks far from anything don't,
              regardless of their exact size.
    dpi: the image's actual DPI — despeckle_level's thresholds are
              DPI-aware (tuned at a 300 DPI reference and scaled from
              there), so this should be the real output DPI, not a guess.
    morph_smoothing: smooth jagged letter-edge pixel steps after
              binarizing (open+close), before despeckle/thicken.

    Returns a single-channel uint8 image, 0/255 only (pure B&W).
    """
    gray = img_bgr if img_bgr.ndim == 2 else cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)

    if contrast != 1.0:
        mean = 128.0
        g = gray.astype(np.float32)
        g = (g - mean) * contrast + mean
        gray = np.clip(g, 0, 255).astype(np.uint8)

    if savgol_enabled and threshold_mode not in ("adaptive", "fixed"):
        gray = savgol_smooth_gray(gray, savgol_window, savgol_degree)

    if threshold_mode == "adaptive":
        block = max(15, (min(gray.shape) // 12) | 1)  # odd
        bw = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY, block, 10
        )
    elif threshold_mode == "fixed":
        _, bw = cv2.threshold(gray, fixed_threshold, 255, cv2.THRESH_BINARY)
    elif threshold_mode == "otsu":
        _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    elif threshold_mode in LOCAL_THRESHOLD_METHODS:
        window_px = max(15, (min(gray.shape) // max(1, window_divisor)) | 1)
        bw = binarize_advanced(
            gray, threshold_mode, window_px=window_px, k=binarize_k,
            delta=binarize_delta, lower_bound=binarize_lower, upper_bound=binarize_upper,
        )
    else:
        raise ValueError(f"unknown threshold_mode: {threshold_mode!r}")

    if morph_smoothing:
        bw = morphological_smoothing(bw)

    if despeckle_level and despeckle_level != "off":
        bw = despeckle_components(bw, dpi=dpi, level=despeckle_level)

    if thicken_px > 0:
        inv = cv2.bitwise_not(bw)  # text -> white for dilation
        ksize = 2 * thicken_px + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ksize, ksize))
        inv = cv2.dilate(inv, kernel, iterations=1)
        bw = cv2.bitwise_not(inv)

    return bw


# ----------------------------------------------------------------------
# 5. Resolution normalization + page composition
# ----------------------------------------------------------------------

IN_TO_PT = 72.0

TRIM_SIZES_IN = {
    "5x8":     (5.0, 8.0),
    "5.25x8":  (5.25, 8.0),
    "5.5x8.5": (5.5, 8.5),
    "6x9":     (6.0, 9.0),
    "6.14x9.21": (6.14, 9.21),
    "7x10":    (7.0, 10.0),
    "8x10":    (8.0, 10.0),
    "8.5x11":  (8.5, 11.0),
}


def _scale_to_fit_exactly(w, h, max_w, max_h):
    """Scale (w, h) so it exactly touches max_w/max_h in whichever
    dimension is the tighter fit, preserving aspect ratio."""
    scale = min(max_w / w, max_h / h)
    return max(1, int(round(w * scale))), max(1, int(round(h * scale)))


def _shrink_to_fit(w, h, max_w, max_h):
    """Like _scale_to_fit_exactly, but only shrinks — leaves (w, h)
    alone if it already fits within max_w/max_h."""
    if w > max_w or h > max_h:
        return _scale_to_fit_exactly(w, h, max_w, max_h)
    return w, h


def compute_target_size(cw, ch, src_dpi, settings, size_override=None):
    """
    Pure sizing math, extracted from compose_page() so a caller can
    resize a *grayscale/color* crop to the correct final pixel size
    BEFORE binarizing it, instead of binarizing at the crop's native
    resolution and resizing the already-binary result afterward.

    Why this matters: resizing an already-binary (0/255) image with a
    smooth interpolation (cv2.INTER_CUBIC, needed for any real upscale)
    creates intermediate gray values at every edge, which then have to be
    snapped back to pure black/white with a fixed cutoff. That snap-back
    doesn't reproduce the original crisp edge — it introduces a
    wobble/staircase whose severity scales with the upscale factor.
    Confirmed directly: on a real scanned letter, wobble measured ~0.44
    at native resolution, climbing to ~1.35 as the exact same crop was
    upscaled 4x with this resize-then-rethreshold approach — a real user
    comparison against ScanTailor Advanced's output on the same page
    surfaced this (ScanTailor resizes/thresholds in the other order and
    its edges stayed essentially straight, wobble ~0.26, regardless of
    scale). See HANDOFF.md for the full investigation.

    Returns (target_w_px, target_h_px) -- see compose_page()'s docstring
    for what every parameter means; this is exactly the sizing decision
    compose_page makes internally, just available standalone.
    """
    dpi = settings["dpi"]
    trim_w_px = int(round(settings["trim_w_in"] * dpi))
    trim_h_px = int(round(settings["trim_h_in"] * dpi))
    margin_top_px = int(round(settings["margin_top_in"] * dpi))
    margin_bottom_px = int(round(settings["margin_bottom_in"] * dpi))
    margin_inner_px = int(round(settings["margin_inner_in"] * dpi))
    margin_outer_px = int(round(settings["margin_outer_in"] * dpi))
    content_w_px = trim_w_px - margin_inner_px - margin_outer_px
    content_h_px = trim_h_px - margin_top_px - margin_bottom_px
    if content_w_px <= 0 or content_h_px <= 0:
        raise ValueError("Margins leave no room for content at this trim size.")

    cw, ch = int(cw), int(ch)

    ref_w_in = settings.get("ref_text_width_in") or 0
    if ref_w_in > 0 and src_dpi > 0:
        crop_w_in = cw / src_dpi
        target_w_px = max(1, int(round(cw * (ref_w_in / crop_w_in) * (dpi / src_dpi))))
        target_h_px = max(1, int(round(ch * (target_w_px / cw))))
    else:
        target_w_px = max(1, int(round(cw * (dpi / src_dpi)))) if src_dpi else cw
        target_h_px = max(1, int(round(ch * (dpi / src_dpi)))) if src_dpi else ch

    if ref_w_in > 0:
        auto_w_px, auto_h_px = _shrink_to_fit(target_w_px, target_h_px, content_w_px, content_h_px)
    else:
        auto_w_px, auto_h_px = _scale_to_fit_exactly(target_w_px, target_h_px, content_w_px, content_h_px)

    if size_override:
        mode = size_override.get("mode")
        value = size_override.get("value") or 1.0
        if mode == "absolute":
            target_w_px = max(1, int(round(value * dpi)))
            target_h_px = max(1, int(round(ch * (target_w_px / cw)))) if cw else auto_h_px
        else:  # "relative"
            target_w_px = max(1, int(round(auto_w_px * value)))
            target_h_px = max(1, int(round(auto_h_px * value)))
        target_w_px, target_h_px = _shrink_to_fit(target_w_px, target_h_px, content_w_px, content_h_px)
    else:
        target_w_px, target_h_px = auto_w_px, auto_h_px

    return target_w_px, target_h_px


def resize_for_target(img, target_w_px, target_h_px):
    """
    Resize a grayscale/color (continuous-tone, NOT yet binarized) crop to
    an exact target pixel size, picking area-decimation for shrinking and
    cubic for growing -- the same interpolation choice compose_page used
    to make internally, just applied to continuous-tone data so a
    subsequent binarization step sees clean, correctly-scaled tone
    instead of an already-binary image that needs to be smeared and
    snapped back to 0/255.
    """
    ch, cw = img.shape[:2]
    interp = cv2.INTER_AREA if target_w_px < cw else cv2.INTER_CUBIC
    return cv2.resize(img, (target_w_px, target_h_px), interpolation=interp)


RETOUCH_NONE = 128  # sentinel gray value meaning "no manual edit here"


def apply_retouch(final_img, retouch_mask):
    """
    Composite a manual touch-up mask onto a finished (post-compose_page)
    page image -- the last step before a page is considered done, so
    manual edits always show up regardless of which threshold method or
    smoothing settings were used to get there.

    final_img: the fully-composed page, single-channel uint8 (0-255).
    retouch_mask: single-channel uint8, same shape as final_img.
        RETOUCH_NONE (128) = no edit at this pixel (leave final_img as
        is); 0 = force black; 255 = force white. Any other value is
        treated as "no edit" too, as a defensive default.

    Returns a new uint8 array; does not modify final_img in place.
    """
    black = retouch_mask == 0
    white = retouch_mask == 255
    out = final_img.copy()
    out[black] = 0
    out[white] = 255
    return out


def remap_retouch_mask(mask, old_rect, new_rect, canvas_shape):
    """
    Reposition (and, in margin-fill sizing mode, rescale) a full-canvas
    retouch mask when the content block it was painted over has moved
    to a different spot on the page canvas.

    Root cause this fixes: a saved retouch mask lines up with the page
    CANVAS (fixed size = trim size), not with the content block placed
    on it. A margin edit changes compose_page()'s x_off/y_off (and, in
    margin-fill mode, the content's scale too -- see compute_placement()
    and compose_page()'s docstring on size_override), so the content
    slides/scales under a mask that never moved. Before this existed,
    apply_retouch() just stamped the mask back at its original absolute
    pixel coordinates, so any touch-up meant to cover a specific spot
    in the scanned content would visibly drift off that spot the moment
    margins changed -- reported as "annotations move when I change
    margins."

    old_rect/new_rect: (x, y, w, h) content-block rects as returned by
        compose_page(..., return_rect=True) -- old_rect is whatever was
        in effect when the mask was last saved, new_rect is what the
        current settings produce.
    canvas_shape: shape of the canvas being rendered now (normally the
        same size as `mask`, since trim size itself doesn't change with
        margins, but kept explicit rather than assumed).

    Maps every mask pixel through the affine transform that carries
    old_rect onto new_rect, so a stroke painted over a specific letter
    stays over that letter. Pixels the transform pulls in from outside
    the mask are filled with RETOUCH_NONE (no-op), not left undefined.
    Returns `mask` unchanged if the rects already match.
    """
    if old_rect is None or tuple(old_rect) == tuple(new_rect):
        return mask
    old_x, old_y, old_w, old_h = old_rect
    new_x, new_y, new_w, new_h = new_rect
    if old_w <= 0 or old_h <= 0:
        return mask
    sx = new_w / old_w
    sy = new_h / old_h
    M = np.array([
        [sx, 0.0, new_x - old_x * sx],
        [0.0, sy, new_y - old_y * sy],
    ], dtype=np.float64)
    h, w = canvas_shape[:2]
    return cv2.warpAffine(
        mask, M, (w, h), flags=cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT, borderValue=float(RETOUCH_NONE),
    )


def compute_placement(target_w_px, target_h_px, settings):
    """
    Given a crop already sized to (target_w_px, target_h_px) pixels,
    compute where compose_page() will place it on the trim-size canvas.

    Factored out of compose_page() so other code -- specifically,
    retouch-mask repositioning when margins change, see
    remap_retouch_mask() and Project.render_final() -- can ask "where
    would this land" without re-deriving the placement math in a second
    place and risking the two copies drifting apart.

    Returns (x_off, y_off, trim_w_px, trim_h_px).
    """
    dpi = settings["dpi"]
    trim_w_px = int(round(settings["trim_w_in"] * dpi))
    trim_h_px = int(round(settings["trim_h_in"] * dpi))

    margin_top_px = int(round(settings["margin_top_in"] * dpi))
    margin_bottom_px = int(round(settings["margin_bottom_in"] * dpi))
    margin_inner_px = int(round(settings["margin_inner_in"] * dpi))
    margin_outer_px = int(round(settings["margin_outer_in"] * dpi))

    content_w_px = trim_w_px - margin_inner_px - margin_outer_px
    content_h_px = trim_h_px - margin_top_px - margin_bottom_px

    x_off = margin_inner_px + (content_w_px - target_w_px) // 2
    align = settings.get("align", "top")
    if align == "center":
        y_off = margin_top_px + (content_h_px - target_h_px) // 2
    elif align == "bottom":
        y_off = margin_top_px + (content_h_px - target_h_px)
    else:
        y_off = margin_top_px

    x_off = max(0, min(x_off, trim_w_px - target_w_px))
    y_off = max(0, min(y_off, trim_h_px - target_h_px))
    return x_off, y_off, trim_w_px, trim_h_px


def compose_page(crop_img, src_dpi, settings, binarize=True, size_override=None, presized=False,
                  return_rect=False):
    """
    Place a processed (cropped) text-block image onto a full print-ready
    page canvas at the target trim size, applying margins and resolution
    normalization.

    settings is a dict with:
      trim_w_in, trim_h_in   - target trim size in inches
      dpi                    - output raster DPI (e.g. 600 for B&W line art)
      margin_top_in, margin_bottom_in, margin_outer_in, margin_inner_in
      ref_text_width_in      - the physical width every page's text block
                                should be scaled to (this is what makes
                                pages scanned at different resolutions
                                look uniform). If None/0, the crop is
                                instead sized from src_dpi directly (see
                                below). Either way this is just the
                                *automatic* baseline — size_override, if
                                given, takes precedence (see below).
      align                  - "top", "center", or "bottom" vertically
                                within the content area
      mirror_margins         - if True, inner/outer margins flip on
                                even/odd pages (book gutter); handled by
                                caller passing the right inner/outer.

    binarize: if True (the default), the resized crop is snapped back to
      pure black/white after resizing (cleans up anti-aliasing artifacts
      from resizing an already-binary image). Set False to preserve
      smooth/continuous tone — used when B&W processing has been skipped
      for a page (see enhance_bw's caller in project.py) so the page
      keeps its original grayscale or color appearance instead of being
      forced to pure black and white.

    size_override: {"mode": "relative", "value": <multiplier>} to scale
      the automatic size up/down (e.g. value=1.15 for 115%), or
      {"mode": "absolute", "value": <width in inches>} to set an exact
      target width directly (height follows the crop's own pixel aspect
      ratio, same as the automatic calculation does). None means no
      override — use the automatic size as-is. An override still gets
      clamped down if it would overflow the content area, but — unlike
      the automatic sizing — is never grown to fill extra space; an
      explicit per-page choice shouldn't be second-guessed by the
      margin-driven auto-fill behavior described below.

    presized: if True, crop_img is ALREADY at the exact size
      compute_target_size(cw, ch, src_dpi, settings, size_override) would
      return for its OWN (pre-resize) dimensions — i.e. the caller
      resized a grayscale/color crop to the target size and binarized it
      at that resolution, instead of binarizing at native resolution and
      letting compose_page resize the already-binary result. In that
      case compose_page skips its own resize/rethreshold entirely and
      just places crop_img on the canvas as-is. This is what
      Project.render_final() does now — see compute_target_size()'s
      docstring for why. Leave this False for any caller that still
      wants compose_page to do the resize itself (e.g. the bw_disabled/
      "keep original tone" path, where there's no binary-image-resize
      artifact to worry about since binarize=False there anyway).

    crop_img may be single-channel (grayscale/binary) or 3-channel BGR;
    the output canvas matches whichever it's given.

    return_rect: if True, return (canvas, (x_off, y_off, target_w_px,
      target_h_px)) instead of just canvas -- the content block's
      position/size on the canvas, exactly as compute_placement() would
      report it. Used by Project.render_final() to detect when a margin
      (or size-override) change has moved the content, so a saved
      retouch mask can be repositioned to match instead of silently
      drifting off the content it was painted over -- see
      remap_retouch_mask().

    Returns a uint8 numpy array, the full output page (same channel count
    as crop_img), or (array, rect) if return_rect is True.
    """
    dpi = settings["dpi"]
    trim_w_px = int(round(settings["trim_w_in"] * dpi))
    trim_h_px = int(round(settings["trim_h_in"] * dpi))

    is_color = crop_img.ndim == 3
    canvas_shape = (trim_h_px, trim_w_px, crop_img.shape[2]) if is_color else (trim_h_px, trim_w_px)
    canvas = np.full(canvas_shape, 255, dtype=np.uint8)

    margin_inner_px = int(round(settings["margin_inner_in"] * dpi))
    margin_outer_px = int(round(settings["margin_outer_in"] * dpi))
    margin_top_px = int(round(settings["margin_top_in"] * dpi))
    margin_bottom_px = int(round(settings["margin_bottom_in"] * dpi))
    content_w_px = trim_w_px - margin_inner_px - margin_outer_px
    content_h_px = trim_h_px - margin_top_px - margin_bottom_px
    if content_w_px <= 0 or content_h_px <= 0:
        raise ValueError("Margins leave no room for content at this trim size.")

    ch, cw = crop_img.shape[:2]

    if presized:
        # Caller already resized (and, if applicable, binarized) crop_img
        # to the correct target size -- nothing left to compute or resize.
        target_w_px, target_h_px = cw, ch
        resized = crop_img
    else:
        target_w_px, target_h_px = compute_target_size(cw, ch, src_dpi, settings, size_override)
        interp = cv2.INTER_AREA if target_w_px < cw else cv2.INTER_CUBIC
        resized = cv2.resize(crop_img, (target_w_px, target_h_px), interpolation=interp)
        if binarize:
            # resizing a pure B&W image can introduce gray edges; re-binarize.
            # (Prefer calling render_final with presized=True instead of
            # relying on this path when possible -- see compute_target_size's
            # docstring for why binarizing-then-resizing is worse than
            # resizing-then-binarizing.)
            _, resized = cv2.threshold(resized, 200, 255, cv2.THRESH_BINARY)
        # else: leave the resize as smooth continuous tone — this is the
        # "keep the original scan as-is" path

    # --- placement --- (single source of truth: compute_placement())
    x_off, y_off, _, _ = compute_placement(target_w_px, target_h_px, settings)

    canvas[y_off:y_off + target_h_px, x_off:x_off + target_w_px] = resized
    if return_rect:
        return canvas, (x_off, y_off, target_w_px, target_h_px)
    return canvas


def detect_skew_angle(img_bgr, angle_range=6.0, coarse_step=0.5, fine_step=0.1):
    """
    Estimate the skew angle of scanned text using the projection-profile
    method: binarize the page, then for a range of candidate rotation
    angles, rotate and sum ink pixels per row. The angle that makes text
    lines most sharply horizontal produces the row-sum profile with the
    highest variance (crisp peaks at each line of text, near-zero between
    lines); a skewed page smears ink across many rows and flattens that
    variance out.

    Returns the angle (degrees) that should be applied to straighten the
    page — i.e. rotate the image by +angle to correct it.
    """
    gray = img_bgr if img_bgr.ndim == 2 else cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

    h, w = bw.shape
    scale = min(1.0, 900.0 / max(h, w))
    if scale < 1.0:
        bw = cv2.resize(bw, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    bh, bw_w = bw.shape
    center = (bw_w / 2.0, bh / 2.0)

    def score(angle):
        M = cv2.getRotationMatrix2D(center, angle, 1.0)
        rotated = cv2.warpAffine(bw, M, (bw_w, bh), flags=cv2.INTER_NEAREST, borderValue=0)
        row_sums = rotated.sum(axis=1).astype(np.float64)
        return float(np.var(row_sums))

    def search(lo, hi, step):
        best_angle, best_score = 0.0, -1.0
        a = lo
        while a <= hi + 1e-9:
            s = score(a)
            if s > best_score:
                best_score, best_angle = s, a
            a += step
        return best_angle

    coarse = search(-angle_range, angle_range, coarse_step)
    fine = search(coarse - coarse_step, coarse + coarse_step, fine_step)
    return round(fine, 1)


def rotate_image(img, angle_deg, border_value=None):
    """Rotate an image by an arbitrary angle around its center, keeping
    the same canvas size and filling new corners with white (or 0 for
    single-channel). Used for both automatic and manual deskew."""
    if angle_deg == 0:
        return img
    h, w = img.shape[:2]
    if border_value is None:
        border_value = 255 if img.ndim == 2 else (255, 255, 255)
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
    return cv2.warpAffine(
        img, M, (w, h), flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_CONSTANT, borderValue=border_value,
    )


# ----------------------------------------------------------------------
def collapse_redundant_grayscale(img):
    """If img is 3-channel but every pixel has R==G==B exactly (i.e. it's
    genuinely grayscale/monochrome content that's just being carried
    around as 3 identical channels), return a single-channel view
    instead. This is a strict, lossless check -- bit-for-bit identical
    once collapsed, not a quality tradeoff -- and matters because a
    scanned book page that's actually grayscale (or a page for which B&W
    processing was intentionally left off, so it keeps its original
    tone) can end up stored as 3-channel RGB purely because it passed
    through a renderer/pipeline stage that defaults to RGB (e.g. PDF
    import always rasterizes via a fixed RGB colorspace regardless of
    the source's actual color depth). Storing 3 redundant channels costs
    3x the space for zero extra information, both on disk mid-project
    and in exported PDFs/PNGs.

    Returns img unchanged if it's already single-channel, or if the
    channels actually differ anywhere (genuine color content).
    """
    if img.ndim != 3:
        return img
    if np.array_equal(img[:, :, 0], img[:, :, 1]) and np.array_equal(img[:, :, 1], img[:, :, 2]):
        return img[:, :, 0]
    return img


# ----------------------------------------------------------------------
# Utility: encode/decode helpers used by the Flask layer
# ----------------------------------------------------------------------

def to_png_bytes(img):
    """img: BGR or single-channel numpy array -> PNG bytes."""
    if img.ndim == 2:
        pil = Image.fromarray(img, mode="L")
    else:
        pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def crop_image(img, bbox):
    x, y, w, h = [int(v) for v in bbox]
    ih, iw = img.shape[:2]
    x = max(0, min(x, iw - 1))
    y = max(0, min(y, ih - 1))
    w = max(1, min(w, iw - x))
    h = max(1, min(h, ih - y))
    return img[y:y + h, x:x + w]
