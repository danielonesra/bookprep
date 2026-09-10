"""
project.py — in-memory + on-disk project state for the BookPrep desktop app.

A Project owns a working directory on disk (raw rendered page PNGs) and an
ordered list of Page records. All page-management operations (add, delete,
duplicate, move/reorder) operate on that list; the on-disk raw images are
addressed by a stable page uid so reordering never touches the filesystem.
"""

import os
import json
import uuid
import shutil
import tempfile
from dataclasses import dataclass, field, asdict
from typing import Optional

import cv2
import numpy as np
import img2pdf
from PIL import Image

import processing as proc
from pdf_render_worker import render_pdf_pages_isolated

DEFAULT_SETTINGS = {
    "trim_preset": "6x9",
    "trim_w_in": 6.0,
    "trim_h_in": 9.0,
    "dpi": 600,
    "margin_top_in": 0.75,
    "margin_bottom_in": 0.75,
    "margin_outer_in": 0.6,
    "margin_inner_in": 0.85,
    "mirror_margins": True,
    "align": "top",
    "contrast": 1.4,
    "threshold_mode": "adaptive",
    "fixed_threshold": 180,
    "thicken_px": 1,
    "despeckle_level": "normal",
    # ScanTailor-Advanced-style binarization (used by threshold_mode values
    # other than "adaptive"/"fixed" — see processing.LOCAL_THRESHOLD_METHODS)
    "window_divisor": 12,
    "binarize_k": 0.34,
    "binarize_delta": 0.0,
    "binarize_lower": 1,
    "binarize_upper": 254,
    "savgol_enabled": False,
    "savgol_window": 7,
    "savgol_degree": 2,
    "morph_smoothing": False,
    "ref_text_width_in": 0,
    # auto-detect (crop-box) tuning — see processing.detect_text_bbox
    "detect_min_area_frac": 0.0003,
    "detect_cluster_gap_frac": 0.025,
    "detect_column_vgap_frac": 0.10,
    "detect_column_overlap_frac": 0.4,
    "detect_pad_frac": 0.01,
    "detect_threshold_method": "otsu",
    "detect_border_strip_frac": 0.03,
}

DETECT_SETTING_KEYS = (
    "detect_min_area_frac", "detect_cluster_gap_frac", "detect_column_vgap_frac",
    "detect_column_overlap_frac", "detect_pad_frac", "detect_threshold_method",
    "detect_border_strip_frac",
)

TRIM_SIZES_IN = dict(proc.TRIM_SIZES_IN)


@dataclass
class Page:
    uid: str                     # stable id, used as the raw-image filename
    src_file: str                # original filename this page came from
    src_page: int                # page index within that source file
    src_dpi: float
    src_width_in: float
    src_height_in: float
    bbox: Optional[list] = None       # manual crop override [x,y,w,h]
    auto_bbox: Optional[list] = None  # auto-detected crop
    rotation: int = 0                 # 0/90/180/270
    deskew_angle: float = 0.0         # fine-tune rotation in degrees, applied after `rotation`
    bw_override: Optional[dict] = None  # per-page override of contrast/threshold/thicken/despeckle
    bw_disabled: bool = False           # skip B&W processing entirely; keep the page as scanned
    align_override: Optional[str] = None  # "top"/"center"/"bottom", or None to inherit global
    size_override: Optional[dict] = None  # {"mode": "relative"|"absolute", "value": float}
    has_retouch: bool = False             # manual paint touch-ups exist -- see Project.retouch_path()
    is_blank: bool = False                # render as a plain blank page: no crop, no thresholding
                                           # at all -- for pages whose scanned content is genuinely
                                           # blank/near-blank and confuses cropping/thresholding

    def active_bbox(self):
        return self.bbox or self.auto_bbox

    def has_bbox(self):
        return self.active_bbox() is not None


class Project:
    def __init__(self, work_dir=None):
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="bookprep_")
        os.makedirs(self.raw_dir, exist_ok=True)
        self.pages: list[Page] = []
        self.settings = dict(DEFAULT_SETTINGS)

    # ---- paths -----------------------------------------------------
    @property
    def raw_dir(self):
        return os.path.join(self.work_dir, "raw")

    def raw_path(self, uid):
        return os.path.join(self.raw_dir, f"{uid}.png")

    @property
    def retouch_dir(self):
        return os.path.join(self.work_dir, "retouch")

    def retouch_path(self, uid):
        return os.path.join(self.retouch_dir, f"{uid}.png")

    def retouch_rect_path(self, uid):
        """Sidecar file recording the content-block rect (x, y, w, h) --
        see processing.compose_page(..., return_rect=True) -- that was
        in effect when this page's retouch mask was last saved. Lets
        render_final() detect a margin/size change since then and
        reposition the mask via processing.remap_retouch_mask() instead
        of applying it at its original, now-stale, coordinates."""
        return os.path.join(self.retouch_dir, f"{uid}.rect.json")

    # ---- loading source files ---------------------------------------
    def add_pdf(self, pdf_path, insert_at=None, render_dpi=400, progress_cb=None):
        """Render every page of a PDF and insert as new Page entries.
        insert_at: index to insert before (None = append at end).
        Rendering happens in an isolated subprocess (see
        pdf_render_worker.py), which streams each page straight to
        self.raw_dir as it's rendered rather than returning pixel data —
        a crash in the PDF library can't take the whole app down with it,
        and a long book never has to be held in memory all at once."""
        filename = os.path.basename(pdf_path)
        results = render_pdf_pages_isolated(
            pdf_path, self.raw_dir, target_dpi=render_dpi, progress_cb=progress_cb
        )
        new_pages = [
            Page(
                uid=r["uid"], src_file=filename, src_page=i,
                src_dpi=r["src_dpi"], src_width_in=r["src_width_in"],
                src_height_in=r["src_height_in"],
            )
            for i, r in enumerate(results)
        ]
        if insert_at is None:
            self.pages.extend(new_pages)
        else:
            self.pages[insert_at:insert_at] = new_pages
        return new_pages

    def add_image(self, image_path, insert_at=None, assumed_dpi=400):
        """Add a single already-rasterized image (e.g. a touched-up PNG
        the user wants to drop back in) as one page."""
        img = cv2.imread(image_path, cv2.IMREAD_UNCHANGED)
        if img is None:
            raise ValueError(f"Could not read image: {image_path}")
        if img.ndim == 3 and img.shape[2] == 4:
            img = img[:, :, :3]  # drop alpha -- pages are always opaque
        # Preserve the source's actual color depth rather than forcing
        # everything to 3-channel BGR: a grayscale/monochrome source
        # image (common even when saved in an RGB-capable format like
        # PNG) shouldn't cost 3x the disk space and export size for
        # zero extra information -- see collapse_redundant_grayscale().
        img = proc.collapse_redundant_grayscale(img)
        with Image.open(image_path) as pil:
            dpi = pil.info.get("dpi", (assumed_dpi, assumed_dpi))[0] or assumed_dpi
        h, w = img.shape[:2]
        uid = uuid.uuid4().hex[:12]
        cv2.imwrite(self.raw_path(uid), img)
        page = Page(
            uid=uid, src_file=os.path.basename(image_path), src_page=0,
            src_dpi=dpi, src_width_in=w / dpi, src_height_in=h / dpi,
        )
        if insert_at is None:
            self.pages.append(page)
        else:
            self.pages.insert(insert_at, page)
        return page

    # ---- page management ---------------------------------------------
    def delete_pages(self, indices):
        indices = sorted(set(indices), reverse=True)
        for i in indices:
            page = self.pages.pop(i)
            try:
                os.remove(self.raw_path(page.uid))
            except OSError:
                pass

    def duplicate_page(self, index):
        src = self.pages[index]
        new_uid = uuid.uuid4().hex[:12]
        shutil.copyfile(self.raw_path(src.uid), self.raw_path(new_uid))
        dup = Page(**{**asdict(src), "uid": new_uid})
        self.pages.insert(index + 1, dup)
        return dup

    def move_pages(self, indices, target_index):
        """Move the pages at `indices` (list of ints, any order) so they
        end up as a contiguous block starting at `target_index`, preserving
        their relative order. target_index is interpreted against the
        list *after* the moved pages are removed."""
        indices = sorted(set(indices))
        moving = [self.pages[i] for i in indices]
        remaining = [p for i, p in enumerate(self.pages) if i not in indices]
        target_index = max(0, min(target_index, len(remaining)))
        remaining[target_index:target_index] = moving
        self.pages = remaining

    def reorder_by_uid(self, uid_order):
        """Reorder self.pages to match the given list of uids exactly
        (used after a drag-and-drop reorder in the UI list widget)."""
        by_uid = {p.uid: p for p in self.pages}
        self.pages = [by_uid[u] for u in uid_order if u in by_uid]

    # ---- detection -----------------------------------------------------
    def _quadrant_rotated_image(self, page: Page):
        """Raw scanned image with only the coarse 90/180/270 rotation
        applied (no fine deskew) — this is the coordinate space that
        deskew detection and correction both operate in."""
        img = cv2.imread(self.raw_path(page.uid), cv2.IMREAD_UNCHANGED)
        if img.ndim == 3 and img.shape[2] == 4:
            img = img[:, :, :3]  # drop alpha -- pages are always opaque
        if page.rotation == 90:
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        elif page.rotation == 180:
            img = cv2.rotate(img, cv2.ROTATE_180)
        elif page.rotation == 270:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return img

    def get_raw_image(self, page: Page):
        img = self._quadrant_rotated_image(page)
        if page.deskew_angle:
            img = proc.rotate_image(img, page.deskew_angle)
        return img

    # ---- manual touch-up (paint) layer -------------------------------
    def get_retouch_mask(self, page: Page):
        """
        Returns the page's manual touch-up mask (single-channel uint8,
        proc.RETOUCH_NONE=no edit / 0=forced black / 255=forced white),
        or None if the page has no touch-ups. The mask is sized to match
        that page's fully-composed output at whatever trim/DPI settings
        were in effect when it was painted -- render_final() checks the
        shape still matches before applying it, since changing trim size
        or DPI after painting would make a saved mask meaningless.

        Note this is the raw, as-saved mask -- still in the coordinate
        frame it was painted in. If margins/size have changed since,
        the caller needs get_retouch_rect() + proc.remap_retouch_mask()
        to reposition it; render_final() does this automatically.
        """
        if not page.has_retouch:
            return None
        path = self.retouch_path(page.uid)
        if not os.path.exists(path):
            return None
        mask = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
        return mask

    def get_retouch_rect(self, page: Page):
        """Returns the (x, y, w, h) content-block rect that was in
        effect when this page's retouch mask was last saved, or None if
        there's no sidecar (e.g. a mask saved before this feature
        existed -- render_final() falls back to applying the mask
        unshifted in that case, same as it always did)."""
        path = self.retouch_rect_path(page.uid)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r") as f:
                rect = json.load(f)
            return tuple(rect)
        except (OSError, ValueError, TypeError):
            return None

    def backfill_legacy_retouch_rects(self):
        """
        One-time self-heal for retouch masks that predate content-rect
        tracking (see HANDOFF.md §4.20): a page can have `has_retouch`
        True and a mask file on disk, but no `.rect.json` sidecar,
        because it was painted (or last saved) before that sidecar
        existed. Without it, render_final() has no idea what content
        rect the mask lines up with, and falls back to applying it
        unshifted -- reproducing the exact "touch-ups drift when
        margins change" bug for any pre-existing work, even though
        newly-painted touch-ups track correctly.

        There's no way to recover the TRUE original rect after the
        fact. Best-effort recovery: adopt whatever content rect the
        CURRENT settings produce as ground truth, on the assumption
        that nothing has moved the content between "mask was painted"
        and "this method runs" -- true if this runs immediately after
        loading a project whose masks predate the sidecar and before
        any further margin/size edits, which is why main_window.py
        calls this right after Project.load(), before anything else
        gets a chance to touch settings. Once adopted, future margin
        changes are tracked correctly from that point on; only a
        margin change that happened *before* this method got to run
        (e.g. earlier in a session that started before this feature
        existed) would be missed, and would need a repaint to recover.
        """
        for page in self.pages:
            if not page.has_retouch or self.get_retouch_rect(page) is not None:
                continue
            try:
                _, content_rect = self.render_final(page, also_return_rect=True)
            except Exception:
                continue
            with open(self.retouch_rect_path(page.uid), "w") as f:
                json.dump(list(content_rect), f)

    def save_retouch_mask(self, page: Page, mask, content_rect=None):
        """Persist a touch-up mask to disk. A mask that's entirely
        proc.RETOUCH_NONE (no actual edits) is treated the same as
        clearing it, so an empty paint layer doesn't linger as a file.

        content_rect: the (x, y, w, h) content-block rect -- see
        processing.compose_page(..., return_rect=True) -- in effect at
        save time (normally whatever the most recent preview render
        used). Saved alongside the mask so a later margin/size change
        can be detected and the mask repositioned to match; see
        get_retouch_rect() and processing.remap_retouch_mask()."""
        if mask is None or not np.any(mask != proc.RETOUCH_NONE):
            self.clear_retouch_mask(page)
            return
        os.makedirs(self.retouch_dir, exist_ok=True)
        cv2.imwrite(self.retouch_path(page.uid), mask)
        page.has_retouch = True
        if content_rect is not None:
            with open(self.retouch_rect_path(page.uid), "w") as f:
                json.dump(list(content_rect), f)

    def clear_retouch_mask(self, page: Page):
        path = self.retouch_path(page.uid)
        if os.path.exists(path):
            os.remove(path)
        rect_path = self.retouch_rect_path(page.uid)
        if os.path.exists(rect_path):
            os.remove(rect_path)
        page.has_retouch = False

    def rotate_page(self, page: Page, delta=90):
        page.rotation = (page.rotation + delta) % 360
        page.bbox = None
        page.auto_bbox = None

    def autodetect_deskew(self, page: Page):
        img = self._quadrant_rotated_image(page)
        bbox = page.active_bbox()
        if bbox is None:
            # No crop set yet (neither manual nor auto) -- run the same
            # crop-detection "Auto-detect text block" uses, with the
            # same tunable detect_* settings, so deskew has a sensible
            # region to work with. Without this, a large uniform-colored
            # background/border outside the actual page (e.g. the black
            # backdrop in a photo of a physical open book, or a scanner
            # bed margin) can dominate the row-projection signal the
            # algorithm relies on, since it's a much bigger, higher-
            # contrast structure than the text lines it's meant to be
            # measuring -- that background is virtually always already
            # axis-aligned in the raw scan/photo (only the page inside
            # it is skewed), so without a crop the detector ends up
            # correctly finding "zero rotation best aligns the
            # background" and completely misses the page's actual text
            # skew. Saved as page.auto_bbox -- visible in the UI and
            # reused elsewhere, not just a hidden internal computation
            # -- but a manual crop set afterward still takes precedence
            # (active_bbox() checks page.bbox first).
            bbox = proc.detect_text_bbox(img, **self._detect_kwargs())
            if bbox is not None:
                page.auto_bbox = list(bbox)
        detect_img = proc.crop_image(img, bbox) if bbox else img
        angle = proc.detect_skew_angle(detect_img)
        page.deskew_angle = angle

        # Re-run crop detection now that the page is straightened, for a
        # tighter fit -- a tilted block of text needs a bigger axis-
        # aligned box to fully contain it than the same block once
        # deskewed, so the crop found before straightening is generally
        # looser than necessary. Only ever updates auto_bbox, never a
        # manual override (active_bbox() already prefers page.bbox, so
        # this has no effect on actual rendering when a manual crop is
        # set -- it just keeps the "best automatic guess" fresh in case
        # the manual crop is cleared later).
        straightened = self.get_raw_image(page)  # re-reads with the angle just set above
        refined_bbox = proc.detect_text_bbox(straightened, **self._detect_kwargs())
        if refined_bbox is not None:
            page.auto_bbox = list(refined_bbox)

        return angle

    def autodetect_deskew_all(self, progress_cb=None):
        for i, page in enumerate(self.pages):
            self.autodetect_deskew(page)
            if progress_cb:
                progress_cb(i + 1, len(self.pages))

    def set_deskew_angle(self, page: Page, angle: float):
        page.deskew_angle = round(float(angle), 1)

    def _detect_kwargs(self):
        s = self.settings
        return dict(
            pad_frac=s["detect_pad_frac"],
            min_area_frac=s["detect_min_area_frac"],
            cluster_gap_frac=s["detect_cluster_gap_frac"],
            column_vgap_frac=s["detect_column_vgap_frac"],
            column_overlap_frac=s["detect_column_overlap_frac"],
            threshold_method=s.get("detect_threshold_method", "otsu"),
            border_strip_frac=s.get("detect_border_strip_frac", 0.03),
        )

    def autodetect(self, page: Page):
        img = self.get_raw_image(page)
        bbox = proc.detect_text_bbox(img, **self._detect_kwargs())
        page.auto_bbox = list(bbox) if bbox else None
        page.bbox = None
        return page.auto_bbox

    def autodetect_all(self, progress_cb=None):
        for i, page in enumerate(self.pages):
            self.autodetect(page)
            if progress_cb:
                progress_cb(i + 1, len(self.pages))

    def run_autodetect(self, pages, do_crop=True, do_deskew=False, progress_cb=None):
        """Run auto-crop and/or auto-deskew over an explicit list of
        pages (all pages, or just a selection -- see the "Auto-detect…"
        dialog in main_window.py). When both are requested, this is
        crop -> deskew -> (deskew's own internal) re-crop per page: run
        autodetect() (a fresh auto-crop) first if do_crop, then
        autodetect_deskew() if do_deskew -- which itself auto-crops
        first if no crop is active yet (see its docstring) and
        re-detects a tighter crop afterward regardless, so requesting
        deskew alone already gets the full crop-deskew-recrop sequence
        without do_crop needing to be set too.
        """
        for i, page in enumerate(pages):
            if do_crop:
                self.autodetect(page)
            if do_deskew:
                self.autodetect_deskew(page)
            if progress_cb:
                progress_cb(i + 1, len(pages))

    def set_reference_from(self, page: Page):
        bbox = page.active_bbox()
        if not bbox:
            raise ValueError("This page has no crop box set yet.")
        width_in = bbox[2] / page.src_dpi
        self.settings["ref_text_width_in"] = round(width_in, 4)
        return self.settings["ref_text_width_in"]

    def set_size_override(self, pages, override: Optional[dict]):
        """override: {"mode": "relative", "value": <multiplier, e.g. 1.15
        for 115%>} or {"mode": "absolute", "value": <target width in
        inches>}, or None to go back to automatic sizing. Applied on top
        of whatever the automatic calculation (size-reference or native)
        would otherwise produce — see compose_page() in processing.py."""
        for page in pages:
            page.size_override = dict(override) if override else None

    def set_bw_override(self, pages, override: Optional[dict]):
        """override: dict with contrast/threshold_mode/fixed_threshold/
        thicken_px/despeckle, plus any of the ScanTailor-Advanced
        binarization keys (window_divisor/binarize_k/binarize_delta/
        binarize_lower/binarize_upper/savgol_*/morph_smoothing), or None
        to clear and inherit global settings."""
        for page in pages:
            page.bw_override = dict(override) if override else None

    def set_bw_disabled(self, pages, disabled: bool):
        """When disabled, a page skips contrast/threshold/thicken/despeckle
        entirely and keeps its original scanned appearance (grayscale or
        color, whatever it was rendered as) — only cropping, resolution
        normalization, and margin placement still apply, so it still fits
        into the book's layout."""
        for page in pages:
            page.bw_disabled = disabled

    def set_align_override(self, pages, align):
        """align: "top"/"center"/"bottom", or None to inherit the
        project's global vertical alignment setting."""
        for page in pages:
            page.align_override = align

    # ---- rendering the final composed page -----------------------------
    def render_final(self, page: Page, index_in_book=None, also_return_rect=False):
        if page.is_blank:
            # No crop, no thresholding, not even reading the raw scan --
            # a page marked blank is just a plain white page at whatever
            # size every other page in the book is. This exists because
            # thresholding a genuinely blank/near-blank scan can produce
            # garbage (scanner noise/grain gets picked up as "content"
            # with nothing real to threshold against), and there's no
            # crop region to speak of either -- see mark_page_blank() in
            # main_window.py.
            s = self.settings
            dpi = s["dpi"]
            trim_w_px = int(round(s["trim_w_in"] * dpi))
            trim_h_px = int(round(s["trim_h_in"] * dpi))
            blank = np.full((trim_h_px, trim_w_px), 255, dtype=np.uint8)
            rect = (0, 0, trim_w_px, trim_h_px)
            return (blank, rect) if also_return_rect else blank

        img = self.get_raw_image(page)
        bbox = page.active_bbox()
        crop = proc.crop_image(img, bbox) if bbox else img

        s = self.settings

        # Final settings (margin mirroring for book gutters, per-page align
        # override) need to be resolved BEFORE sizing, since content-area
        # size depends on which margin ends up as "inner" vs "outer".
        settings = dict(s)
        if page.align_override:
            settings["align"] = page.align_override
        if s.get("mirror_margins") and index_in_book is not None:
            # Standard book pagination: page 1 is a recto (right-hand) page,
            # so odd page numbers are always right-hand and even are always
            # left-hand -- this is universal in Western book printing and
            # is what Lulu (and any other printer) expects the interior PDF
            # to already follow. index_in_book is 0-based, so page number
            # = index_in_book + 1; a left-hand (verso) page is therefore an
            # ODD index (0-based) -- i.e. index 1, 3, 5... = pages 2, 4, 6.
            # On a left-hand page the spine is on its RIGHT, so that's
            # where the (larger) inner/gutter margin needs to end up.
            is_left_page = (index_in_book % 2 == 1)
            if is_left_page:
                settings["margin_inner_in"], settings["margin_outer_in"] = (
                    s["margin_outer_in"], s["margin_inner_in"]
                )

        if page.bw_disabled:
            processed = crop
            binarize = False
            presized = False
        else:
            bw_settings = dict(s)
            if page.bw_override:
                bw_settings.update(page.bw_override)

            # Resize the still-grayscale/color crop to its final target
            # size FIRST, then binarize at that (final, output-DPI)
            # resolution -- rather than binarizing at the crop's native
            # resolution and having compose_page resize the already-binary
            # result afterward. The latter needs a smooth interpolation
            # (cubic) for any real upscale, which smears a crisp binary
            # edge into a gray ramp that then has to be snapped back to
            # 0/255 -- introducing an edge wobble that gets worse the
            # bigger the upscale factor. Confirmed directly against a real
            # scan (see compute_target_size()'s docstring and HANDOFF.md).
            ch, cw = crop.shape[:2]
            target_w_px, target_h_px = proc.compute_target_size(
                cw, ch, page.src_dpi, settings, size_override=page.size_override)
            resized_crop = proc.resize_for_target(crop, target_w_px, target_h_px)

            processed = proc.enhance_bw(
                resized_crop, contrast=bw_settings["contrast"], threshold_mode=bw_settings["threshold_mode"],
                fixed_threshold=bw_settings["fixed_threshold"], thicken_px=bw_settings["thicken_px"],
                despeckle_level=bw_settings.get("despeckle_level", "normal"), dpi=s["dpi"],
                window_divisor=bw_settings.get("window_divisor", 12),
                binarize_k=bw_settings.get("binarize_k", 0.34),
                binarize_delta=bw_settings.get("binarize_delta", 0.0),
                binarize_lower=bw_settings.get("binarize_lower", 1),
                binarize_upper=bw_settings.get("binarize_upper", 254),
                savgol_enabled=bw_settings.get("savgol_enabled", False),
                savgol_window=bw_settings.get("savgol_window", 7),
                savgol_degree=bw_settings.get("savgol_degree", 2),
                morph_smoothing=bw_settings.get("morph_smoothing", False),
            )
            binarize = True
            presized = True

        final, content_rect = proc.compose_page(processed, page.src_dpi, settings, binarize=binarize,
                                                 size_override=page.size_override, presized=presized,
                                                 return_rect=True)

        retouch_mask = self.get_retouch_mask(page)
        if retouch_mask is not None and final.ndim == 2 and retouch_mask.shape == final.shape:
            # Manual paint touch-ups, applied last so they show up
            # regardless of threshold method/smoothing settings. Shape
            # mismatch (trim size or DPI changed since painting) means
            # the saved mask no longer lines up with this page's pixels
            # -- silently skip rather than distort or crash; the mask
            # file is left on disk in case the user reverts the setting
            # that caused the mismatch.
            #
            # Shape match alone isn't enough, though: a margin (or
            # per-page size-override) change leaves the canvas the same
            # size but moves/rescales *where the content sits* on it
            # (see compose_page's x_off/y_off). The mask was painted
            # against wherever the content was AT THE TIME, so if that's
            # moved since, remap the mask onto the new content rect
            # before applying it -- otherwise the touch-up silently
            # drifts off the spot it was meant to cover.
            old_rect = self.get_retouch_rect(page)
            if old_rect is None:
                # Legacy mask with no rect sidecar at all (predates this
                # tracking, or reached us through some path other than
                # open_project()'s proactive backfill_legacy_retouch_rects()
                # -- see that method's docstring for the full story).
                # Apply unshifted this one time, exactly like before this
                # feature existed, and adopt *this* render's content
                # rect as the new baseline so any margin change from
                # here on is tracked correctly instead of repeating this
                # gap indefinitely.
                try:
                    with open(self.retouch_rect_path(page.uid), "w") as f:
                        json.dump(list(content_rect), f)
                except OSError:
                    pass
            elif old_rect != content_rect:
                retouch_mask = proc.remap_retouch_mask(retouch_mask, old_rect, content_rect, final.shape)
            final = proc.apply_retouch(final, retouch_mask)
        return (final, content_rect) if also_return_rect else final

    # ---- export ----------------------------------------------------------
    def export_pngs(self, out_dir, progress_cb=None):
        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for i, page in enumerate(self.pages):
            final = self.render_final(page, index_in_book=i)
            final = proc.collapse_redundant_grayscale(final)
            path = os.path.join(out_dir, f"page_{i + 1:04d}.png")
            cv2.imwrite(path, final, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            paths.append(path)
            if progress_cb:
                progress_cb(i + 1, len(self.pages))
        return paths

    def export_pdf(self, pdf_path, progress_cb=None):
        image_paths = self._export_images_for_pdf(
            os.path.join(self.work_dir, "export_tmp"), progress_cb=progress_cb)
        dpi = self.settings["dpi"]
        layout_fun = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
        pdf_bytes = img2pdf.convert(image_paths, layout_fun=layout_fun)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return pdf_path

    def _export_images_for_pdf(self, out_dir, progress_cb=None):
        """Like export_pngs(), but used only as an internal step of
        export_pdf() -- NOT the user-facing "Export PNGs (for GIMP)"
        feature (that one, export_pngs(), must keep writing real .png
        files people can open and edit in GIMP).

        A binarized B&W page is, by construction, pure black-and-white
        (see enhance_bw()/compose_page(): every pixel is exactly 0 or
        255) -- but writing it as an 8-bit-per-pixel grayscale PNG (what
        cv2.imwrite does for a uint8 array, and what export_pngs() uses)
        still spends a full byte per pixel before compression. Since this
        is genuinely 1-bit-per-pixel information, encoding it as CCITT
        Group 4 -- the fax/scanner encoding built specifically for
        bilevel text pages, and universally supported by PDF readers and
        print houses (it's the standard format for scanned book
        interiors) -- is a lossless re-encoding of the exact same pixels
        that comes out 5-10x+ smaller for real scanned text (measured:
        ~140KB/page as 8bpp PNG vs ~25KB/page as G4 TIFF on a synthetic
        test page; real text compresses even better than that synthetic
        case, since G4 specifically exploits the horizontal/vertical run
        patterns typical of text glyphs). This is what was inflating a
        152-page book to 285MB.

        Non-bilevel pages (color, or grayscale from a per-page
        "B&W disabled" override) aren't touched by this -- they're still
        written as PNGs, same as export_pngs() does, since Group 4 only
        applies to pure bilevel images.
        """
        os.makedirs(out_dir, exist_ok=True)
        paths = []
        for i, page in enumerate(self.pages):
            final = self.render_final(page, index_in_book=i)
            # A page carried through in color (B&W processing off) may
            # still be genuinely grayscale underneath -- e.g. imported
            # from a PDF whose rasterizer always renders RGB regardless
            # of the source's actual color depth (this is exactly what
            # was inflating pages kept in their original tone: R==G==B
            # on every single pixel, but stored as 3-channel RGB, so 3x
            # the necessary data before compression even starts). This
            # check is a strict, lossless equality test -- it only
            # collapses to grayscale when doing so changes nothing.
            final = proc.collapse_redundant_grayscale(final)
            is_bilevel = (final.ndim == 2
                          and np.array_equal(np.unique(final), np.array([0, 255], dtype=np.uint8)))
            if is_bilevel:
                path = os.path.join(out_dir, f"page_{i + 1:04d}.tiff")
                Image.fromarray(final).convert("1").save(path, compression="group4")
            else:
                path = os.path.join(out_dir, f"page_{i + 1:04d}.png")
                cv2.imwrite(path, final, [cv2.IMWRITE_PNG_COMPRESSION, 3])
            paths.append(path)
            if progress_cb:
                progress_cb(i + 1, len(self.pages))
        return paths

    @staticmethod
    def build_pdf_from_folder(folder, pdf_path, dpi):
        """Assemble a PDF from a folder of sequentially named PNGs — used
        for the round trip where the user has touched up the exported
        pages in GIMP and wants them re-assembled."""
        files = sorted(
            f for f in os.listdir(folder)
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff"))
        )
        if not files:
            raise ValueError("No image files found in that folder.")
        paths = [os.path.join(folder, f) for f in files]
        layout_fun = img2pdf.get_fixed_dpi_layout_fun((dpi, dpi))
        pdf_bytes = img2pdf.convert(paths, layout_fun=layout_fun)
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)
        return pdf_path

    # ---- persistence -------------------------------------------------
    def save(self, path):
        data = {
            "settings": self.settings,
            "pages": [asdict(p) for p in self.pages],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load(self, path):
        with open(path) as f:
            data = json.load(f)
        # merge over defaults so projects saved before a settings key was
        # added (e.g. the auto-detect tuning options) still load cleanly
        self.settings = dict(DEFAULT_SETTINGS)
        self.settings.update(data["settings"])
        # migrate the old boolean (pre-dropdown) auto-detect threshold
        # setting, if this project predates detect_threshold_method
        old_adaptive = data["settings"].get("detect_use_adaptive_threshold")
        if "detect_threshold_method" not in data["settings"] and old_adaptive:
            self.settings["detect_threshold_method"] = "adaptive"
        # migrate the old boolean (pre-connected-component) despeckle
        # setting, if this project predates despeckle_level
        if "despeckle_level" not in data["settings"] and "despeckle" in data["settings"]:
            self.settings["despeckle_level"] = "normal" if data["settings"]["despeckle"] else "off"
        self.pages = [Page(**p) for p in data["pages"]]

    def cleanup(self):
        shutil.rmtree(self.work_dir, ignore_errors=True)
