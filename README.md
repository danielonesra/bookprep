# BookPrep — native desktop edition

A native Qt (PySide6) desktop app for turning scanned book PDFs — Google
Books downloads, library scans, whatever — into one clean, uniform,
print-ready PDF for Lulu. This replaces the earlier browser/Flask version:
same processing engine underneath, but everything runs as a real desktop
app with no HTTP round-trips, so page-flipping and crop editing are
instant instead of sluggish.

## Setup (WSL2 / Ubuntu)

Requires Python 3.10+ and a GUI-capable WSL2 (Windows 11's WSLg gives you
this out of the box; on Windows 10 WSL2 you'd need an X server like
VcXsrv/X410 and `export DISPLAY=...` first).

```bash
cd bookprep-native
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

That's it — a native window opens. No browser, no server, no localhost.

If you hit a missing system library error from Qt on a minimal WSL2/Ubuntu
image, install the usual Qt runtime deps:
```bash
sudo apt update && sudo apt install -y libgl1 libegl1 libxkbcommon0 \
  libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 \
  libxcb-randr0 libxcb-render-util0 libxcb-xinerama0
```

## What's new vs. the browser version

- **Native Qt UI** — page rendering, cropping, and preview all happen
  in-process; nothing is re-encoded to PNG and sent over HTTP just to
  flip a page. Preview updates run on a background thread pool so the
  UI never freezes, even while a big image is being processed.
- **Page management**: multi-select **Delete**, **Duplicate**, **Move
  up/down**, and drag-and-drop reordering directly in the page list —
  for assembling a book out of pages from several different scans, or
  fixing page order, or dropping in a replacement scan of a bad page.
  **Add scans…** / **Add images…** insert right after the currently
  selected page (or at the end if nothing's selected).
- **Page list scroll and zoom.** The mouse wheel scrolls the page list as
  usual; hold **Ctrl** and scroll to zoom the thumbnails larger or
  smaller instead — handy for getting a better look at a lot of pages at
  once, or shrinking them down to see more of the book at a glance.
- **Right-click context menu** on the page list. Select any number of
  pages and right-click for: Delete, Duplicate, Move page(s) to…,
  Auto-detect text block, Reset crop, Rotate 90°, Auto-deskew, Set deskew
  angle, and Black & white processing overrides — all applied to the
  whole selection at once. **Move page(s) to…** (also in the toolbar)
  opens a small dialog to move the selection before or after a specific
  page number, rather than one step at a time. Select exactly one page
  to also get **Add page before…** / **Add page after…** (any mix of
  images or PDFs) and **Add pdf before…** / **Add pdf after…** (PDF
  files only, for when you specifically want to insert another scanned
  book's pages at that spot), for inserting a replacement or missing
  scan at a specific spot in the book.
- **Deskew**: a projection-profile algorithm (rotate through candidate
  angles, pick the one that makes text lines line up into the sharpest
  horizontal bands) auto-detects and corrects small scan skew — click
  **Auto-deskew** in the editor toolbar, or **Auto-deskew** in the
  context menu to run it on several pages at once. For manual fine-tuning
  there's a degrees spinbox (tenths of a degree, up/down arrows) next to
  it — turning it rotates the live preview immediately, and the context
  menu's **Set deskew angle…** applies one exact value to a whole
  selection.

  Auto-deskew detects skew *within* the page's crop region, not the
  whole raw scan — if a crop hasn't been set yet (manually or via
  **Auto-detect text block**), it runs that same crop detection first,
  using whatever detection settings are currently configured. This
  matters because a raw scan often includes more than just the page —
  a scanner bed edge, a binding shadow, or (especially for a photo of a
  physical open book) a solid-colored backdrop around it — and that
  surrounding material is a much bigger, higher-contrast shape than the
  text lines deskew is actually trying to measure. Left uncropped, deskew
  tends to "correct" against that surrounding shape instead (which is
  usually already straight in the photo) and finds essentially no skew
  at all, even on a visibly crooked page. If that happens, check
  **Detect: border strip** in the right pane — raising it tells crop
  detection to ignore a wider margin around the edge before it starts
  looking for content, which is exactly the fix for a wide photo
  backdrop or an unusually deep binding shadow. Auto-deskew also
  re-detects the crop a second time after straightening — a tilted
  block of text needs a bigger axis-aligned box to fully contain it
  than the same block once level, so the crop found afterward is
  generally tighter than the one found (or already set) beforehand.
- **Blank page.** For a page whose scanned content is genuinely blank or
  near-blank, cropping and thresholding can both behave badly — there's
  no real content to find a crop box around, and thresholding a mostly
  featureless scan can pick up scanner noise/grain as "content" and
  produce speckled garbage instead of a clean page. **Blank page** in the
  editor toolbar sidesteps both entirely: the page renders as a plain
  white page at the book's normal trim size, with no crop region and no
  thresholding applied at all, regardless of whatever the raw scan
  actually contains. If clicked by accident, **Reset crop** (also clears
  any crop region, same as before) undoes it and makes the page
  croppable again.
- **Resizable, zoomable editor and preview.** The divider between the
  crop editor and the "Proof preview" pane is a movable splitter — drag
  it up or down to give either one more room. Both panes are fully
  zoomable/pannable: the mouse wheel scrolls, **Ctrl+wheel zooms**
  in/out (anchored under the cursor), click-drag empty space to pan
  around, or use the **−** / **+** buttons, **Fit** (whole page in
  view), and **100%** (actual pixel size) buttons above each one.
  Zooming the crop editor doesn't change the crop box's coordinates —
  it's a pure viewing aid, so you can zoom in to place a precise crop on
  fine detail without the box itself shifting. Your zoom level is
  preserved when you flip between pages, so if you zoom into a recurring
  problem spot (e.g. a corner that
  needs checking on every page) it stays put as you page through.
- **Per-page black & white overrides**: the right panel's contrast /
  threshold / thicken / despeckle settings apply to the whole book by
  default, but if some pages need different treatment (a faint batch of
  scans next to an already-crisp one), select those pages, right-click →
  **Black & white processing…**, and set values just for them. Pages with
  an override show "custom B&W" in the page list; a **Clear black & white
  override** entry appears in the context menu once any selected page has
  one, to revert it back to the book's global settings.
- **Turn off B&W processing entirely for selected pages.** Some pages —
  a color plate, an illustration, anything that just looks better as
  Google (or whatever the source) originally scanned it — shouldn't be
  forced through contrast/threshold/thicken/despeckle at all. The same
  **Black & white processing…** dialog has a checkbox for this: "Turn off
  B&W processing for these pages." Note that turning contrast down and
  thicken to 0 does *not* achieve this on its own — those pages still get
  binarized to pure black and white by the threshold step, which has no
  "off" position of its own; this checkbox is what actually bypasses
  binarization and keeps the page's original grayscale or color tone.
  Cropping, resolution normalization, and margin placement still apply,
  so the page still lines up with the rest of the book — only the tonal
  processing is skipped. These pages show "B&W off" in the page list.

  A page kept this way automatically stays at whatever color depth it
  actually has — color, grayscale, or monochrome — rather than being
  forced to a fixed format. A source that's genuinely just black text
  (the common case even for pages from a color-capable PDF) is detected
  and stored efficiently regardless of the format it arrived in; a page
  with real color content (an illustration, a photo) keeps its full
  color untouched. This matters most for file size: a book imported
  from a source whose pages are already monochrome (e.g. many Google
  Books PDFs) stays that compact through both the working project and
  the exported PDF, instead of every page being bloated to full color
  behind the scenes and only caught at the very end.

  Sometimes a page is really just black-and-white text but ends up
  carrying incidental grayscale anyway — e.g. it had a light watermark
  that you cropped out or painted over, but the page is still encoded
  as continuous-tone underneath. For that case, use **Preset: flatten
  to monochrome (no enhancements)** in the same dialog: it sets Otsu
  global auto-threshold with contrast, thicken, despeckle, and
  smoothing all off (and turns "keep the scan exactly as-is" back off,
  since the preset needs actual thresholding to run). This collapses
  the page to true black-and-white without reshaping the text — Otsu
  just finds the natural split point between "black-ish" and
  "white-ish" pixels and snaps to it, so if the page is already pure
  black and white this changes nothing at all; if it's carrying
  redundant grayscale, this is what actually removes it.
- **Per-page vertical alignment.** The main "Vertical align" setting
  (Top/Center/Bottom) applies to the whole book, but a specific page can
  override it — right-click a selection → **Vertical alignment** submenu
  → pick Top, Center, or Bottom, or "Use global default" to go back to
  inheriting the book-wide setting. Useful for an odd page whose content
  block is a different height than the rest (a short poem, a plate with
  a caption) where the book's usual alignment looks off just for that
  one page. Overridden pages show "align: bottom" (or top/center) in the
  page list.
- **Margins control content size directly when no size reference is set.**
  Reduce margins and the content grows to fill the extra room (touching
  the page edges at zero margins); increase them and it shrinks to fit —
  both directions now work, not just shrinking. This only applies when
  you haven't set a size reference; a size reference is a deliberately
  *fixed* physical width meant to hold steady across the whole book, so
  margins can still shrink it if it doesn't fit but won't grow it further
  (growing it would quietly undermine the point of setting one).
- **Per-page size override.** Right-click a selection → **Page size…**
  for a page (or batch) that still looks a bit off — too small, too
  large, or added from a different book with different typography.
  Choose **Relative** (a percentage of whatever the automatic size would
  otherwise be, e.g. 115%) or **Absolute** (an exact target width in
  inches), via a radio toggle in the dialog. Either way it's still only
  ever shrunk to fit the margins, never grown past them. A **Clear page
  size override** entry appears in the context menu once any selected
  page has one. Overridden pages show "size: 115%" or "size: 4.25in" in
  the page list.
- **Export PNGs (for GIMP)** — writes every processed page as a
  sequential `page_0001.png`, `page_0002.png`, … file to a folder you
  choose, *before* PDF assembly, so you can open them in GIMP for manual
  touch-up (spot-clean a scan artifact, patch a torn page, whatever).
- **Build PDF from PNG folder…** — once you've touched pages up in GIMP
  (editing them in place, or saving over the exported files), point this
  at that folder and it assembles the final print PDF from exactly what's
  there, at your configured DPI. This is the other half of the GIMP
  round-trip: **Export PNGs → edit in GIMP → Build PDF from PNG folder**.
- **Save project / Open project** — saves your crop boxes, rotations,
  deskew angles, per-page B&W overrides, page order, and all settings
  (plus copies of the rendered source pages) to disk so you can pick a
  book back up later. Saving writes a `yourproject.json` file plus a
  sibling `yourproject.json_data/` folder — keep them together.

## Workflow

1. **Add scans (PDF)…** for one or more source PDFs. Pages are inserted
   in the order you pick them.
2. **Auto-detect…** for a first-pass crop box (and optionally a
   straightening pass) on every page, or just a selection — see below.
3. Step through with ‹ Prev / Next › (above the page list on the left).
   Straighten any crooked scans first — click **Auto-deskew** (first
   button in the editor toolbar above the crop view), or nudge the
   degrees spinbox by hand — then fix any bad crops by dragging the red
   box (drag the body to move it, drag a corner handle to resize both
   edges at once, or an edge handle to resize just that one side).
   **⟳ Rotate 90°** for sideways scans (clears that page's crop, since
   the coordinate space changes). **Blank page** for a genuinely blank
   scan that's giving crop/threshold detection trouble — see above.
4. Use the page list on the left — click, shift-click, or ctrl-click to
   select several pages, then **right-click** for a menu of actions to
   run on the whole selection: delete, duplicate, move to a specific
   page, auto-detect, clear crop, rotate, deskew, or set a custom black
   & white treatment. Select exactly one page to also get **Add page
   before…** / **Add page after…** and **Add pdf before…** / **Add pdf
   after…**, for slotting in a missing or replacement scan. Drag pages
   in the list to reorder — handy when stitching a book together from
   multiple source PDFs whose pages need interleaving.
5. Pick a clean, representative page and click **Set as size reference** —
   every page's text block gets rescaled to match its physical width, so
   mixed-DPI source scans read as one consistent size in the final book.
   If you'd rather not use a size reference at all, that's fine too —
   with none set, a page's content simply scales to fill the margins
   exactly (shrinking or growing as you adjust them), so at zero margins
   it touches the page edges. If a page still looks a bit off after
   automatic sizing (or you've just added pages from a different book
   with different typography), right-click → **Page size…** to nudge it
   by a percentage or set an exact width, per page or in bulk.
6. Set **trim size** and **margins** in the right panel — pick one of
   the standard presets, or **Custom…** for an exact width/height in
   inches if your book doesn't match any of them — and tune
   **contrast / threshold / stroke thickening** — the proof preview updates
   automatically (debounced ~250ms after you stop adjusting). If a
   handful of pages need different treatment than the rest of the book,
   select them and use the context menu's **Black & white processing…**
   instead of changing the global settings.
7. For small manual fixes (a stray speck the auto-cleanup missed, a
   broken letter stroke, a smudge), use **✏ Touch-up mode** right below
   the proof preview: toggle it on, pick a brush size (down to a single
   pixel) and **Paint black** / **Paint white**, then paint directly on
   the preview at whatever zoom level you're at — painting always works
   in actual page pixels regardless of zoom, so a 1px brush is a genuine
   single output pixel. Touch-ups are a separate layer on top of the
   algorithmic conversion: they survive any later change to contrast,
   threshold method, or smoothing settings, and show up in exported
   PDFs/PNGs too. Switching pages keeps each page's touch-ups separate.
   Touch-ups stay anchored to the content they were painted on, not to
   a fixed spot on the page — so if you paint over a specific mark and
   later adjust the margins, the touch-up moves and rescales along with
   the content instead of drifting off it.
   **Clear touch-ups** removes all manual edits on the current page
   (with a confirmation — though like everything else here, it can be
   undone). Touch-up layers are saved alongside the project file, in
   the same `_data` folder as the page images — if you copy or move a
   saved project, bring that whole folder with it or touch-ups (like
   page images) won't come along.

   For fixes bigger than a brush stroke (patching a torn corner with a
   clean patch of page from elsewhere, repeating a design element,
   covering a large stain), use **⬚ Select region** next to Touch-up
   mode: drag a rectangle over the source area, then **Copy**. Click
   **Paste** to arm a floating, semi-transparent preview of the copied
   patch that follows your cursor — click anywhere to stamp it down.
   Paste stays armed after each stamp, so you can drop the same patch
   in several places in a row without re-copying; press **Escape** or
   right-click to stop pasting. Like brush touch-ups, pasted patches
   land on the same touch-up layer (so they survive re-processing and
   are saved with the project) and work in real page pixels regardless
   of zoom.

   **↶ Undo** (or **Ctrl+Z**) steps back through paint strokes, paste
   stamps, and Clear touch-ups, one action at a time, up to the last 15
   changes on the current page. It's specific to the touch-up layer —
   it won't undo crop/margin/threshold settings — and each page keeps
   its own separate undo history, which resets when you switch pages
   (so you can't undo into a different page's edits).
8. Export:
   - **Export PDF…** for the final print-ready file, straight to Lulu.
     B&W pages are encoded with CCITT Group 4 compression (the standard
     lossless encoding for bilevel/fax-style scans) rather than a plain
     grayscale image, which keeps these files far smaller — typically
     several times smaller than a naive export — with zero quality loss,
     since a binarized page is genuinely 1-bit-per-pixel data either way.
     Pages where B&W processing is disabled export at whatever color
     depth they actually are (grayscale content stays grayscale, color
     content stays color) rather than being padded out to a fixed
     format, so this stays compact too.
   - **Export PNGs (for GIMP)…** if you want to hand-touch-up individual
     pages in a full image editor (more room to work than the built-in
     tool — cloning, layers, etc.), then **Build PDF from PNG folder…**
     once you're done.

## Notes

- **Gutter side follows standard book pagination**: with "Mirror
  inner/outer on facing pages" on, the first page (page 1) is treated as
  a recto (right-hand) page with its gutter on the left, the second as
  verso (left-hand) with its gutter on the right, and so on alternating —
  matching how Lulu (and printers generally) expect the interior PDF to
  already be laid out, since a book's page 1 is conventionally always a
  right-hand page when it's opened.
- DPI: 600 is the safe default for pure black & white / line-art
  interiors on Lulu; 300 is usually fine for text-only books and renders
  faster.
- **Threshold modes**: the "Adaptive" and "Fixed" modes are the original
  two options. Beyond those, the threshold-mode dropdown offers the
  local/global binarization methods from ScanTailor Advanced (ports of
  its `imageproc/Binarize.cpp`, including the Fox/Window/Bradley/Grad/
  EdgeDiv methods from the actively-maintained
  ScanTailor-Advanced/scantailor-advanced fork):
  - **Otsu** — a single global cutoff, auto-computed for the whole page.
    Good for clean, evenly-lit scans.
  - **Sauvola** — local mean + local standard deviation. The standard
    choice for scanned text: it holds the threshold near the local mean
    in flat, low-contrast regions (rejecting paper grain/noise) while
    dropping it near text edges (preserving faint strokes). Usually the
    best first thing to try if Adaptive isn't cutting it.
  - **Wolf** — a Sauvola variant normalized against the image's true
    darkest pixel and largest local contrast, instead of a fixed
    constant. Helps when there's a genuine deep-black ink color to
    calibrate against.
  - **Fox** — a Wolf variant using a different local-contrast measure,
    aimed at scans with bleed-through (text from the back of the page
    showing through).
  - **Window** (Bataineh) — a more elaborate blend of local and global
    statistics; more tuning knobs, marginal gains over Sauvola/Wolf.
  - **Bradley** — the simplest local-mean threshold (integral-image
    adaptive thresholding); similar in spirit to Adaptive mode but with
    a percentage-based offset instead of a flat one, and no
    local-contrast awareness.
  - **Grad** ("Gradient Snip") — blends a global gradient-weighted mean
    with the local mean. Suited to clean, evenly-lit scans.
  - **EdgePlus / BlurDiv / EdgeDiv** — not thresholds in their own right;
    they sharpen local contrast before running a plain Otsu cutoff.
    EdgeDiv runs both prefilters together.

  When a non-Adaptive/Fixed/Otsu mode is selected, extra controls appear:
  **window size** (as a divisor of the page's shorter dimension — smaller
  divisor = larger, more global window), **sensitivity (k)** (meaning
  varies by method — see `processing.py`'s docstrings for the exact
  formulas), **threshold shift** (a small additive fine-tune), and for
  Wolf/Fox/Window/Grad a **lower bound** guarding pure-black regions.
  Two more checkboxes are always available: **Savitzky-Golay smoothing**
  (denoises the grayscale image before thresholding, preserving edges
  better than a blur) and **morphological smoothing** (cleans up jagged
  pixel-stair-stepping on letter edges after thresholding, before
  despeckle/thicken). A **"Reset to defaults"** button at the bottom of
  the panel puts all of the above (contrast through despeckle) back to
  their factory defaults in one click.
- **Despeckle** (in the same panel) removes small isolated ink specks
  left over from scan dust/grain, without touching legitimate small
  marks (accents, dots on i's/j's, punctuation) that sit close to real
  text — it only removes specks that are both small *and* isolated.
  Four levels: Off, Cautious, Normal (default), Aggressive.
- Pages are resized to their final print size *before* being converted
  to black & white, not after — binarizing first and resizing the
  already-binary result afterward (which is what a naive implementation
  would do) smears crisp edges into a gray ramp on any real upscale,
  which then has to be snapped back to pure black/white and doesn't
  reproduce the original crisp edge. This is handled correctly as
  shipped. If you still see edge roughness on some pages, the biggest
  lever left is how much your source scan gets upscaled to reach your
  project's output DPI — a page scanned at a low native resolution and
  stretched a lot to reach a high output DPI will show more roughness
  than the same page scanned at higher resolution to begin with, largely
  independent of threshold method or smoothing settings.
- Everything happens locally on your machine — nothing is uploaded
  anywhere.
- **PDF rendering runs in an isolated subprocess, streaming pages
  straight to disk.** PyMuPDF's underlying C library (MuPDF) isn't
  reliably safe to call from a plain background thread, and under some
  environments (this has been seen under WSL2) that can crash the whole
  app with a segfault instead of a normal, catchable error — so adding a
  PDF renders it in a separate OS process; if a specific PDF ever trips
  that up, you'll get a clean error dialog naming the file instead of the
  app going down. That subprocess also writes each page straight to disk
  as it's rendered rather than collecting the whole book in memory and
  handing it all back at once — for a long book (100+ pages) at a decent
  DPI, holding every page's full-resolution pixels simultaneously (and
  then having to serialize all of it across the process boundary) can add
  up to multiple gigabytes, which is what used to cause the whole machine
  to grind to a halt and eventually crash on longer books. Peak memory
  now stays roughly constant regardless of how long the book is, and the
  progress dialog shows real per-page progress instead of an
  indeterminate spinner while it works.
- **Auto-detect crops to the body text block specifically**, not just
  "everything with ink on the page." It clusters nearby marks together
  (bridging normal word/line/paragraph/chapter gaps) and picks the
  largest resulting cluster, so an isolated watermark, library stamp, or
  page number elsewhere on the page doesn't drag the crop out to include
  it. It's tuned to be a good starting point on typical scans, not
  flawless on every one — the per-page and bulk crop-box editing is there
  for outliers it gets wrong.

  **Auto-detect…** in the toolbar opens a small dialog rather than just
  running once over everything: choose **All pages** or **Selected
  pages** (only enabled when something's actually selected), and check
  **Auto-crop**, **Auto-deskew**, or both. Auto-deskew already includes
  its own crop-then-recrop pass either side of straightening (see
  Deskew, above), so checking it alone still gets a full
  crop-deskew-recrop sequence per page even with Auto-crop left
  unchecked — Auto-crop here is for running an *additional*, independent
  fresh crop pass, e.g. re-cropping without touching straightening on
  pages that are already level.

  If it's consistently over- or under-including on a particular batch
  of scans, the **"Auto-detect
  (crop) tuning"** section in the right panel exposes the knobs behind
  it:
  - **Ignore marks smaller than** — anything smaller than this (as a
    fraction of page area) is discarded as noise before it's even
    considered. Raise this to make stray marks (stamps, punch holes,
    page numbers) get ignored outright, regardless of where they sit.
  - **Line/paragraph gap tolerance** — how close two bits of ink need to
    be to count as the same paragraph.
  - **Chapter-break / stray-mark distance** — how big a gap can still get
    bridged when two blobs share the same horizontal column (e.g. across
    a chapter opening). Raising this bridges bigger legitimate gaps, but
    also makes the algorithm more willing to sweep in something distant
    that happens to line up with the text column.
  - **Column alignment strictness** — how precisely two blobs need to
    line up horizontally to count as "the same column" for the rule
    above.
  - **Crop padding** — a small margin added around the final box.
  - **Threshold method** — which of the same B&&W algorithms (Otsu,
    Adaptive, Sauvola, Wolf, Fox, Window, Bradley, Grad, EdgePlus,
    BlurDiv, EdgeDiv) Auto-detect uses internally to tell ink from page
    before it looks for the text block. Defaults to Otsu. If Otsu is
    misreading a scan's ink/paper split — grabbing the whole page,
    grabbing nothing, or anything in between — try whichever method
    works well for that scan's final B&&W output; there's no reason the
    two need to match, but they often will.
  - **Border/shadow strip width** — how wide a margin around the page
    edge gets zeroed out before any content is even considered (up to
    45%, for scans with a very wide border or shadow), to keep
    scanner-bed edges and binding/gutter shadows out of the running
    entirely. This is the one to raise if Auto-detect grabs the *entire
    page* instead of just the text — which is exactly what a dark
    scanner-bed border or a thick book's gutter shadow will do if it's
    not stripped wide enough: it gets misread as "ink," and once it's
    bridged to the real text by the clustering step, the whole page
    becomes one blob. This is much more common on scans that aren't
    already clean and pre-cropped (i.e. not Google Books-style scans) —
    raise this first if Auto-detect works fine on clean B&&W scans but
    keeps grabbing the whole page on scans from another source. If
    raising it stops it from grabbing the whole page but the crop still
    isn't tight around the text, the **"Ignore marks smaller than"** and
    **"Crop padding"** sliders above are the next ones to check.

  A **Reset to defaults** button is there if tuning goes sideways.
  Changing these only affects *future* auto-detect runs — pages that
  already have a crop box set (auto or manual) aren't touched
  retroactively.
- Only reprint material you actually have the right to reprint (public
  domain works, your own scans, properly licensed content) — this is also
  required by Lulu's own terms.

## Project structure

```
bookprep-native/
  main.py             entry point
  main_window.py       the whole UI (page list, crop editor, settings, export)
  crop_item.py          interactive movable/resizable crop rectangle
  project.py            page data model: add/delete/duplicate/move, rendering, export
  processing.py          core image pipeline (render, detect, enhance, compose) — unchanged
  worker.py               background-thread helper so long operations don't freeze the UI
  requirements.txt
```
