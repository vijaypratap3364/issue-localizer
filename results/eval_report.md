# Issue Localizer -- Evaluation Report

**Generated:** 2026-07-24 02:57 UTC  
**Model:** `gemini-3.5-flash-lite`  
**Repo under test:** [python-pillow/Pillow](https://github.com/python-pillow/Pillow)  
**Dataset:** `data/eval_dataset.jsonl` -- 80 examples (73 scored, 7 failed to run due to API errors)

## Headline numbers

| Metric | Value |
|---|---|
| Precision (macro-avg) | 0.740 |
| Recall (macro-avg) | 0.553 |
| F1 (macro-avg) | 0.591 |
| Avg. tool-call turns per example | 5.5 (cap: 6) |
| Full-hit rate | 31.5% (23/73) |

## Results by category

| Category | Count | % of scored | Precision | Recall | F1 | Avg turns |
|---|---|---|---|---|---|---|
| Complete miss | 10 | 13.7% | 0.00 | 0.00 | 0.00 | 5.4 |
| Partial hit | 40 | 54.8% | 0.84 | 0.43 | 0.55 | 5.7 |
| Full hit | 23 | 31.5% | 0.89 | 1.00 | 0.93 | 5.1 |

- **Complete miss**: None of the ground-truth changed files appeared anywhere in the predictions.
- **Partial hit**: The issue has multiple ground-truth files and only some were predicted.
- **Full hit**: Every ground-truth file was predicted (precision may still be <1 if extra wrong files were also predicted).

## Observed patterns

- Complete misses involve more ground-truth files on average (3.3 vs 1.4 for full hits) -- cross-file changes are harder to fully localize.
- Complete misses use more tool-call turns on average (5.4 vs 5.1 for full hits) -- more investigation doesn't correlate with a correct answer here; the agent isn't running out of budget, it's running out of leads.
- Partial hits involve more ground-truth files on average (3.6 vs 1.4 for full hits) -- as expected, finding *some but not all* changed files is a multi-file-change problem.

## Failure analysis

### Complete miss (10)

| Issue | Predicted | Actual (✓ = predicted) | Turns |
|---|---|---|---|
| Missing TCL license in third-party licenses | LICENSE | wheels/dependency_licenses/TCL_TK.txt | 6 |
| Pillow built with libjpeg-turbo 3.0.0 errors with _imaging.so: undefined symbol: jpeg12_write_raw_data | src/_imaging.c<br>src/libImaging/JpegEncode.c | setup.py | 6 |
| Converting animated webp images to animated gifs creates corrupt files | src/PIL/WebPImagePlugin.py<br>src/_webp.c<br>src/PIL/GifImagePlugin.py | Tests/test_imagepalette.py<br>src/PIL/ImagePalette.py | 6 |
| `image.paste(image, box=(x, y))` is unexpected if both images are identical and `y` is positive | src/PIL/Image.py<br>src/libImaging/Imaging.h | Tests/test_image_paste.py<br>src/libImaging/Paste.c | 3 |
| Seven tests fail with freetype 2.14.0 or newer | src/_imagingft.c<br>src/PIL/ImageFont.py | .github/workflows/macos-install.sh<br>Tests/images/colr_bungee.png<br>Tests/images/colr_bungee_mask.png<br>Tests/images/colr_bungee_older.png<br>Tests/test_imagedraw.py<br>Tests/test_imagefont.py<br>Tests/test_imagefontctl.py<br>winbuild/build_prepare.py | 6 |
| Wheel: Loading zstd-compressed TIFF fails, even with imagecodecs | src/PIL/TiffImagePlugin.py | .github/workflows/wheels-dependencies.sh<br>wheels/dependency_licenses/ZSTD.txt | 6 |
| Reduce AVIF wheel size? | depends/install_libavif.sh | .github/workflows/wheels-dependencies.sh<br>.github/workflows/wheels.yml<br>Tests/check_wheel.py<br>docs/releasenotes/11.3.0.rst<br>wheels/dependency_licenses/AOM.txt<br>wheels/dependency_licenses/DAV1D.txt<br>wheels/dependency_licenses/LIBAVIF.txt<br>wheels/dependency_licenses/LIBYUV.txt<br>winbuild/build_prepare.py | 5 |
| ImageGrab.grabclipboard raises ValueError: cannot read this XPM file | src/PIL/ImageGrab.py<br>Tests/test_imagegrab.py | Tests/images/hopper_bpp2.xpm<br>Tests/images/hopper_rgb.xpm<br>Tests/test_file_xpm.py<br>docs/handbook/image-file-formats.rst<br>src/PIL/XpmImagePlugin.py | 4 |
| pillow 11.2.1 FTBFS on fedora-{34,...,39}, debian-bookworm | src/PIL/__init__.py | setup.py | 6 |
| CR2 / TIFF image loads with incorrectly applied orientation / rotation | src/PIL/TiffImagePlugin.py<br>Tests/test_file_tiff.py | Tests/test_file_libtiff.py<br>src/libImaging/TiffDecode.c | 6 |

### Partial hit (40)

| Issue | Predicted | Actual (✓ = predicted) | Turns |
|---|---|---|---|
| cur file saved as a png has black pixels where it needs to be transparent | src/PIL/CurImagePlugin.py<br>Tests/test_file_cur.py | Tests/images/mask_1.cur<br>Tests/images/mask_L.cur<br>**Tests/test_file_cur.py** ✓<br>**src/PIL/CurImagePlugin.py** ✓<br>src/libImaging/Convert.c | 6 |
| Add scale option to ImageGrab.grab function | src/PIL/ImageGrab.py<br>Tests/test_imagegrab.py<br>docs/reference/ImageGrab.rst | **Tests/test_imagegrab.py** ✓<br>**docs/reference/ImageGrab.rst** ✓<br>docs/releasenotes/12.3.0.rst<br>**src/PIL/ImageGrab.py** ✓ | 4 |
| rounded_rectangle with radius > 50% and single corner | src/PIL/ImageDraw.py<br>Tests/test_imagedraw.py | **Tests/test_imagedraw.py** ✓<br>docs/reference/ImageDraw.rst<br>**src/PIL/ImageDraw.py** ✓ | 4 |
| Refactor Pyroma test to be non-runtime | .pre-commit-config.yaml | .ci/install.sh<br>.github/workflows/macos-install.sh<br>**.pre-commit-config.yaml** ✓<br>Makefile<br>Tests/test_image_access.py<br>Tests/test_pyroma.py<br>pyproject.toml<br>tox.ini | 6 |
| Support monochrome AVIF saving and loading | src/_avif.c<br>src/PIL/AvifImagePlugin.py<br>Tests/test_file_avif.py | **Tests/test_file_avif.py** ✓<br>docs/handbook/image-file-formats.rst<br>**src/PIL/AvifImagePlugin.py** ✓<br>**src/_avif.c** ✓ | 5 |
| getsize_multiline doesn't take into account characters that extend below the baseline | src/PIL/ImageFont.py | Tests/images/rectangle_surrounding_text.png<br>Tests/oss-fuzz/fuzzers.py<br>Tests/test_font_pcf.py<br>Tests/test_font_pcf_charsets.py<br>Tests/test_imagedraw.py<br>Tests/test_imagedraw2.py<br>Tests/test_imagefont.py<br>Tests/test_imagefontctl.py<br>docs/deprecations.rst<br>docs/reference/ImageDraw.rst<br>docs/reference/ImageFont.rst<br>docs/releasenotes/9.2.0.rst<br>src/PIL/ImageDraw.py<br>src/PIL/ImageDraw2.py<br>**src/PIL/ImageFont.py** ✓ | 6 |
| Unable to convert TIFF to JPEG file | src/PIL/TiffImagePlugin.py | Tests/images/separate_planar_extra_samples.tiff<br>Tests/test_file_libtiff.py<br>**src/PIL/TiffImagePlugin.py** ✓ | 5 |
| Image.open() fails on a JPEG2000 image | src/PIL/Jpeg2KImagePlugin.py | Tests/test_file_jpeg2k.py<br>**src/PIL/Jpeg2KImagePlugin.py** ✓ | 5 |
| Bitmap missing for glyph | src/PIL/ImageFont.py<br>src/_imagingft.c | Tests/test_font_crash.py<br>**src/_imagingft.c** ✓ | 6 |
| XDGViewer and GmDisplayViewer fail to show images | src/PIL/ImageShow.py | docs/reference/ImageShow.rst<br>**src/PIL/ImageShow.py** ✓ | 5 |
| Image.open() fails with struct.error with Photoshop .psd files | src/PIL/PsdImagePlugin.py | Tests/images/negative_top_left_layer.psd<br>Tests/test_file_psd.py<br>**src/PIL/PsdImagePlugin.py** ✓<br>src/PIL/_binary.py | 6 |
| `rounded_rectangle` produces black vertical line in edge case | src/PIL/ImageDraw.py<br>Tests/test_imagedraw.py | Tests/images/imagedraw_rounded_rectangle_radius.png<br>**Tests/test_imagedraw.py** ✓<br>**src/PIL/ImageDraw.py** ✓ | 6 |
| apng duration returns float instead of int | src/PIL/PngImagePlugin.py<br>Tests/test_file_apng.py | **Tests/test_file_apng.py** ✓<br>docs/handbook/image-file-formats.rst<br>**src/PIL/PngImagePlugin.py** ✓ | 6 |
| Segfaults when using the 39C3 font from Chaos Communication congress | src/_imagingft.c<br>Tests/test_imagefont.py | Tests/fonts/AdobeVFPrototypeDuplicates.ttf<br>Tests/fonts/LICENSE.txt<br>**Tests/test_imagefont.py** ✓<br>src/PIL/ImageFont.py<br>**src/_imagingft.c** ✓ | 6 |
| Todo: Docs for ImageMorph | docs/reference/index.rst<br>src/PIL/ImageMorph.py | docs/reference/ImageMorph.rst<br>**src/PIL/ImageMorph.py** ✓ | 6 |
| "ValueError: tile cannot extend outside image" for JPEG files exported from macOS Photos app | src/PIL/MpoImagePlugin.py<br>src/PIL/JpegImagePlugin.py | Tests/images/frame_size.mpo<br>Tests/images/sugarshack_frame_size.mpo<br>Tests/test_file_mpo.py<br>**src/PIL/JpegImagePlugin.py** ✓ | 6 |
| Overly large PA palette | src/PIL/Image.py | Tests/test_image.py<br>Tests/test_image_convert.py<br>**src/PIL/Image.py** ✓<br>src/_imaging.c | 5 |
| PNG iCCP chunk profile compression type verification seems wrong | src/PIL/PngImagePlugin.py | Tests/images/unknown_compression_method.png<br>Tests/test_file_png.py<br>**src/PIL/PngImagePlugin.py** ✓ | 6 |
| Issue with PIL.Image.fromarray(..., mode="1") returning a transposed image | src/PIL/Image.py<br>Tests/test_image_array.py | **Tests/test_image_array.py** ✓<br>docs/deprecations.rst<br>docs/releasenotes/11.3.0.rst<br>**src/PIL/Image.py** ✓ | 6 |
| Image.alpha_composite does not work with images of type "LA" | src/libImaging/AlphaComposite.c<br>Tests/test_image.py | **Tests/test_image.py** ✓<br>src/PIL/Image.py<br>**src/libImaging/AlphaComposite.c** ✓ | 6 |
| Image.histogram() gives wrong result on images of mode "LA" | src/PIL/Image.py<br>Tests/test_image_histogram.py | **Tests/test_image_histogram.py** ✓<br>src/libImaging/Histo.c | 6 |
| Parallel build is no longer parallel | setup.py | .ci/requirements-mypy.txt<br>docs/installation/building-from-source.rst<br>pyproject.toml<br>**setup.py** ✓ | 6 |
| RGB/BGR confusion | src/PIL/Image.py<br>Tests/test_image_putdata.py | docs/handbook/concepts.rst<br>docs/reference/ImageDraw.rst<br>docs/reference/PixelAccess.rst<br>**src/PIL/Image.py** ✓ | 6 |
| Inconsistent `Image.open(str)` | src/PIL/Image.py | Tests/test_image.py<br>**src/PIL/Image.py** ✓ | 6 |
| `ValueError: image has no palette` when show GIF | src/PIL/GifImagePlugin.py<br>Tests/test_file_gif.py | Tests/images/no_palette_with_transparency_after_rgb.gif<br>**Tests/test_file_gif.py** ✓<br>**src/PIL/GifImagePlugin.py** ✓ | 6 |
| TIFF save options are not applied to appended images | src/PIL/TiffImagePlugin.py<br>Tests/test_file_tiff.py | Tests/test_file_mpo.py<br>**Tests/test_file_tiff.py** ✓<br>src/PIL/Image.py<br>src/PIL/MpoImagePlugin.py<br>**src/PIL/TiffImagePlugin.py** ✓ | 6 |
| Multiline ttb text is not supported | src/PIL/ImageText.py<br>Tests/test_imagefontctl.py | Tests/images/test_combine_multiline_ttb.png<br>**Tests/test_imagefontctl.py** ✓<br>src/PIL/ImageDraw.py | 6 |
| AttributeError: 'Image' object has no attribute 'encoderinfo'. Did you mean: 'encoderconfig'? | src/PIL/Image.py<br>src/PIL/JpegImagePlugin.py | Tests/test_file_mpo.py<br>**src/PIL/Image.py** ✓ | 6 |
| Text justify doesn't work with middle and right anchor (and last line is also always justified) | src/PIL/ImageText.py<br>Tests/test_imagefont.py | Tests/images/multiline_text_justify_anchor.png<br>**Tests/test_imagefont.py** ✓<br>src/PIL/ImageDraw.py | 6 |
| Use multi-phase initialisation (PEP 489) | src/_imagingtk.c<br>src/_webp.c | src/_avif.c<br>src/_imaging.c<br>src/_imagingcms.c<br>src/_imagingft.c<br>src/_imagingmath.c<br>src/_imagingmorph.c<br>**src/_imagingtk.c** ✓<br>**src/_webp.c** ✓ | 6 |
| Loaded PCX image has smeared result | src/PIL/PcxImagePlugin.py<br>src/libImaging/PcxDecode.c | Tests/images/p_4_planes.pcx<br>Tests/test_file_pcx.py<br>**src/libImaging/PcxDecode.c** ✓ | 6 |
| `Image.getexif()` errors on n04532106_1553.JPEG from ImageNet | src/PIL/Image.py<br>Tests/test_file_jpeg.py | Tests/test_image.py<br>**src/PIL/Image.py** ✓ | 6 |
| Segfault when seeking to position 0 from any position in a TIFF image and reading the numpy array | src/PIL/TiffImagePlugin.py | Tests/test_tiff_crashes.py<br>**src/PIL/TiffImagePlugin.py** ✓<br>src/map.c | 6 |
| ImageDraw.polygon is very slow when width > 1 | src/PIL/ImageDraw.py<br>Tests/test_imagedraw.py | **src/PIL/ImageDraw.py** ✓<br>src/_imaging.c<br>src/libImaging/Draw.c<br>src/libImaging/Imaging.h | 6 |
| Unpickle Image serialized with older version of Pillow | src/PIL/ImageFile.py | Tests/test_pickle.py<br>**src/PIL/ImageFile.py** ✓ | 6 |
| Create a list of Pillow plugins in the documentation | docs/handbook/third-party-plugins.rst | docs/handbook/appendices.rst<br>**docs/handbook/third-party-plugins.rst** ✓ | 6 |
| Performance when reading TIFF file with small tiles | src/PIL/TiffImagePlugin.py<br>src/PIL/ImageFile.py | Tests/test_imagefile.py<br>**src/PIL/ImageFile.py** ✓ | 6 |
| Image.save() causes images created by Image.frombuffer() to stop reflecting changes in that buffer | src/PIL/Image.py | Tests/test_image.py<br>**src/PIL/Image.py** ✓ | 5 |
| Windows application screenshot | src/display.c<br>src/PIL/ImageGrab.py | Tests/test_imagegrab.py<br>docs/reference/ImageGrab.rst<br>docs/releasenotes/11.2.0.rst<br>**src/PIL/ImageGrab.py** ✓<br>**src/display.c** ✓ | 4 |
| BmpImagePlugin - Unsupported 32bpp DIBs with Alpha | src/PIL/BmpImagePlugin.py<br>Tests/test_bmp_reference.py | Tests/test_file_bmp.py<br>**src/PIL/BmpImagePlugin.py** ✓ | 5 |

### Failed to run (7)

| Issue | Error |
|---|---|
| Tests/test_file_avif.py::TestFileAvif::test_write_rgb fails on riscv64 | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |
| `py.typed` present, but not all methods have types | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |
| Missing Apache-2.0 notice for IcoImagePlugin | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |
| Some TIFF files are identified as FLI/FLC files | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |
| Saving and loading int16 images to PNG format is causing data loss | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |
| `Image.fromarrow` minimal example does not work | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |
| Change in behavior with JPEG Images in 11.2.0 | Gemini API: exceeded 5 retries due to rate limiting/server errors. Last error: 429: {   "error": {     "code": 429,     "message": "You exceeded your current quota, please check your plan and billing |

## All scored examples

| # | Issue | Category | Precision | Recall | F1 | Turns |
|---|---|---|---|---|---|---|
| 0 | cur file saved as a png has black pixels where it needs to be transparent | Partial hit | 1.00 | 0.40 | 0.57 | 6 |
| 1 | Missing TCL license in third-party licenses | Complete miss | 0.00 | 0.00 | 0.00 | 6 |
| 2 | [BUG] Reference leak: tag_type new ref from PyDict_GetItemRef never DECREF'd in PyImaging_LibTiffEncoderNew | Full hit | 1.00 | 1.00 | 1.00 | 3 |
| 3 | path_subscript hardcodes slice length to 4 instead of self->count | Full hit | 1.00 | 1.00 | 1.00 | 5 |
| 4 | Reference leak of seq in set_value_to_item macro on nested sequence error | Full hit | 1.00 | 1.00 | 1.00 | 3 |
| 5 | Use-after-release of Py_buffer in _prepare_lut_table | Full hit | 0.50 | 1.00 | 0.67 | 5 |
| 6 | Add scale option to ImageGrab.grab function | Partial hit | 1.00 | 0.75 | 0.86 | 4 |
| 7 | rounded_rectangle with radius > 50% and single corner | Partial hit | 1.00 | 0.67 | 0.80 | 4 |
| 8 | Refactor Pyroma test to be non-runtime | Partial hit | 1.00 | 0.12 | 0.22 | 6 |
| 9 | Support monochrome AVIF saving and loading | Partial hit | 1.00 | 0.75 | 0.86 | 5 |
| 11 | KeyError: 'JPEG' when saving RGB images to PDF without explicit format argument | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 12 | getsize_multiline doesn't take into account characters that extend below the baseline | Partial hit | 1.00 | 0.07 | 0.12 | 6 |
| 13 | test_grab_x11 failure when no X session is running | Full hit | 1.00 | 1.00 | 1.00 | 3 |
| 14 | Missing libm linkage for _imagingmath extension causes undefined symbol errors | Full hit | 1.00 | 1.00 | 1.00 | 4 |
| 15 | FreeTypeFont is not thread-safe with free threading | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 16 | Unable to convert TIFF to JPEG file | Partial hit | 1.00 | 0.33 | 0.50 | 5 |
| 17 | Image.open() fails on a JPEG2000 image | Partial hit | 1.00 | 0.50 | 0.67 | 5 |
| 18 | Transparent images don't seem to be handled correctly | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 19 | Bitmap missing for glyph | Partial hit | 0.50 | 0.50 | 0.50 | 6 |
| 20 | XDGViewer and GmDisplayViewer fail to show images | Partial hit | 1.00 | 0.50 | 0.67 | 5 |
| 21 | Image.open() fails with struct.error with Photoshop .psd files | Partial hit | 1.00 | 0.25 | 0.40 | 6 |
| 22 | `rounded_rectangle` produces black vertical line in edge case | Partial hit | 1.00 | 0.67 | 0.80 | 6 |
| 23 | Pillow built with libjpeg-turbo 3.0.0 errors with _imaging.so: undefined symbol: jpeg12_write_raw_data | Complete miss | 0.00 | 0.00 | 0.00 | 6 |
| 25 | Converting animated webp images to animated gifs creates corrupt files | Complete miss | 0.00 | 0.00 | 0.00 | 6 |
| 26 | apng duration returns float instead of int | Partial hit | 1.00 | 0.67 | 0.80 | 6 |
| 27 | Segfaults when using the 39C3 font from Chaos Communication congress | Partial hit | 1.00 | 0.40 | 0.57 | 6 |
| 28 | Todo: Docs for ImageMorph | Partial hit | 0.50 | 0.50 | 0.50 | 6 |
| 29 | test_separate_tables failure with Pillow 11.2.1 | Full hit | 0.50 | 1.00 | 0.67 | 4 |
| 30 | Transparent 1-bit PNG images treated as opaque (alpha component 255) | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 32 | `image.paste(image, box=(x, y))` is unexpected if both images are identical and `y` is positive | Complete miss | 0.00 | 0.00 | 0.00 | 3 |
| 33 | Make PDF output reproducible | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 34 | ImageOps.expand removes the alpha channel of palettes | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 35 | EPS Image conversion fails after upgrading to 11.2.1 / 11.3.0 from 10.4.0 | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 36 | "ValueError: tile cannot extend outside image" for JPEG files exported from macOS Photos app | Partial hit | 0.50 | 0.25 | 0.33 | 6 |
| 37 | Overly large PA palette | Partial hit | 1.00 | 0.25 | 0.40 | 5 |
| 39 | Seven tests fail with freetype 2.14.0 or newer | Complete miss | 0.00 | 0.00 | 0.00 | 6 |
| 40 | PNG iCCP chunk profile compression type verification seems wrong | Partial hit | 1.00 | 0.33 | 0.50 | 6 |
| 41 | Issue with PIL.Image.fromarray(..., mode="1") returning a transposed image | Partial hit | 1.00 | 0.50 | 0.67 | 6 |
| 42 | Wheel: Loading zstd-compressed TIFF fails, even with imagecodecs | Complete miss | 0.00 | 0.00 | 0.00 | 6 |
| 43 | ZeroDivisionError in ImageStat | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 44 | Is there a way to delete only the GPS data rather than all of the EXIF data? | Full hit | 1.00 | 1.00 | 1.00 | 5 |
| 45 | Image.alpha_composite does not work with images of type "LA" | Partial hit | 1.00 | 0.67 | 0.80 | 6 |
| 46 | Image.histogram() gives wrong result on images of mode "LA" | Partial hit | 0.50 | 0.50 | 0.50 | 6 |
| 47 | Parallel build is no longer parallel | Partial hit | 1.00 | 0.25 | 0.40 | 6 |
| 48 | Pillow 11.3.0 iOS wheel contains dynamically linked libjpeg reference | Full hit | 0.50 | 1.00 | 0.67 | 6 |
| 49 | RGB/BGR confusion | Partial hit | 0.50 | 0.25 | 0.33 | 6 |
| 50 | Reduce AVIF wheel size? | Complete miss | 0.00 | 0.00 | 0.00 | 5 |
| 51 | Inconsistent `Image.open(str)` | Partial hit | 1.00 | 0.50 | 0.67 | 6 |
| 52 | getiptcinfo() fails to get iptc metadata for TIFF images | Full hit | 1.00 | 1.00 | 1.00 | 4 |
| 53 | `ValueError: image has no palette` when show GIF | Partial hit | 1.00 | 0.67 | 0.80 | 6 |
| 54 | TIFF save options are not applied to appended images | Partial hit | 1.00 | 0.40 | 0.57 | 6 |
| 55 | Multiline ttb text is not supported | Partial hit | 0.50 | 0.33 | 0.40 | 6 |
| 56 | MPO encoding then decoding does not work | Full hit | 1.00 | 1.00 | 1.00 | 5 |
| 57 | AttributeError: 'Image' object has no attribute 'encoderinfo'. Did you mean: 'encoderconfig'? | Partial hit | 0.50 | 0.50 | 0.50 | 6 |
| 58 | Text justify doesn't work with middle and right anchor (and last line is also always justified) | Partial hit | 0.50 | 0.33 | 0.40 | 6 |
| 59 | BmpImagePlugin save palette (in "1" mode) sets a "should probably be 0" value to 255... | Full hit | 0.50 | 1.00 | 0.67 | 6 |
| 60 | ImageGrab.grabclipboard raises ValueError: cannot read this XPM file | Complete miss | 0.00 | 0.00 | 0.00 | 4 |
| 61 | Use multi-phase initialisation (PEP 489) | Partial hit | 1.00 | 0.25 | 0.40 | 6 |
| 62 | show method caused an error on macOS when app is packed with PyInstaller | Full hit | 1.00 | 1.00 | 1.00 | 6 |
| 64 | Loaded PCX image has smeared result | Partial hit | 0.50 | 0.33 | 0.40 | 6 |
| 65 | Unanticipated UnicodeDecodeError raised during call to PIL.Image.open | Full hit | 1.00 | 1.00 | 1.00 | 4 |
| 66 | `Image.getexif()` errors on n04532106_1553.JPEG from ImageNet | Partial hit | 0.50 | 0.50 | 0.50 | 6 |
| 67 | Segfault when seeking to position 0 from any position in a TIFF image and reading the numpy array | Partial hit | 1.00 | 0.33 | 0.50 | 6 |
| 68 | ImageDraw.polygon is very slow when width > 1 | Partial hit | 0.50 | 0.25 | 0.33 | 6 |
| 70 | pillow 11.2.1 FTBFS on fedora-{34,...,39}, debian-bookworm | Complete miss | 0.00 | 0.00 | 0.00 | 6 |
| 71 | Unpickle Image serialized with older version of Pillow | Partial hit | 1.00 | 0.50 | 0.67 | 6 |
| 72 | Create a list of Pillow plugins in the documentation | Partial hit | 1.00 | 0.50 | 0.67 | 6 |
| 73 | Performance when reading TIFF file with small tiles | Partial hit | 0.50 | 0.50 | 0.50 | 6 |
| 74 | Image.save() causes images created by Image.frombuffer() to stop reflecting changes in that buffer | Partial hit | 1.00 | 0.50 | 0.67 | 5 |
| 76 | Image.fromarray silently fails with floating-point input | Full hit | 0.50 | 1.00 | 0.67 | 6 |
| 77 | Windows application screenshot | Partial hit | 1.00 | 0.40 | 0.57 | 4 |
| 78 | BmpImagePlugin - Unsupported 32bpp DIBs with Alpha | Partial hit | 0.50 | 0.50 | 0.50 | 5 |
| 79 | CR2 / TIFF image loads with incorrectly applied orientation / rotation | Complete miss | 0.00 | 0.00 | 0.00 | 6 |

