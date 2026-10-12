# Scene descriptions in the pixls dataset

`scripts/dataset.py describe` gives every in-scope photo a
description for choosing tone-test photos: Exposure, Highlights, Shadows,
Whites, Blacks, Contrast, Clarity, Texture and Dehaze, and their parity with
Camera Raw. It runs locally (mlx-vlm, `mlx-community/Qwen3-VL-8B-Instruct-4bit`
by default, about 10 s a photo on an M1 Pro); no API or tokens.

Two sources, each used for what it can judge:

- **What is in the scene** comes from a vision model looking at the camera's
  embedded JPEG preview at 768 px. A model can't read undeveloped raw data.
- **How much light there is** comes from the raw data (`stats`): the preview's
  tone curve hides the raw's headroom and clips what the raw still holds, so
  every tone field is derived from the measured numbers.

Photos of one camera whose previews look alike (difference hash within
`--same` bits, 6 by default, or three times that when taken within a minute of
each other) are one scene shot in several raw modes: the model
describes the first, the others copy its scene fields with
`same_scene_as` set, and each keeps tone fields from its own raw numbers.

## From the vision model

- `subject`: one concrete line on what is in the picture.
- `scene`: landscape, cityscape, architecture, interior, portrait, people,
  street, night, closeup, animal, still-life, product, vehicle, sky, test-chart,
  color-checker, junk (nothing to see at all: a lens cap, a blank frame, a
  blurred ceiling; a plain but real scene is not junk).
- `setting`: indoor, outdoor, mixed, unknown.
- `light`: direct-sun, overcast, open-shade, golden-hour, blue-hour,
  night-artificial, tungsten, fluorescent-led, mixed, flash, studio, unknown.
- `sky`: none, small, large (over a quarter of the frame).
- `haze`: none, light, strong.
- `backlit`: the main subject is darker than what is behind it.
- `light_sources`: the sun, lamps, neon or strong specular highlights in frame.
- `small_dark_in_bright`: small dark areas inside brighter surroundings, which
  Shadows treats differently from large dark areas.
- `large_dark_areas`: dark regions over a large part of the frame.
- `skin`: people's skin at a useful size.
- `texture`: none, some, lots.
- `smooth_gradients`: large smooth gradients where banding or halos would show.
- `saturated_colors`: strongly saturated areas.
- `quality_flags`: blurry, motion-blur, black-frame, lens-cap, overexposed,
  underexposed, heavy-noise, heavy-vignetting.
- `tone_notes`: one sentence on where the shadows and highlights are.

## From the raw numbers

EV values are scene luminance below the sensor's white level (0 = clipping)
with the camera's white balance: `ev_p01` … `ev_p999` percentiles, `spread_ev`
= `ev_p99 − ev_p01`, `clipped` (share of photosites within 1% of their channel's white level:
saturation often sits just under the nominal level),
`black` (share under −14 EV), `dark_area` and `bright_area` (share of the frame
3 EV under or 2 EV over the median).

- `key`: low when `ev_p50` < −7, high when > −3, else mid. This is where the
  photographer placed the scene in the raw, which is what Exposure tests need.
- `contrast`: low when `spread_ev` < 6, high when > 9, else normal.
- `tone_tests`, 0 (not useful), 1 (shows the control) or 2 (a strong test).
  Test charts, color checkers and junk frames are 0 except Exposure 1 for charts.
  - exposure: 2 when nothing clips, `black` < 1% and `spread_ev` is 5–10; else 1.
  - highlights: 2 when `ev_p99` > −1.5 or `bright_area` > 10%, with `clipped`
    < 0.5%; 1 when `ev_p99` > −3.
  - whites: 2 when `clipped` ≥ 0.05%, `ev_p999` > −0.3 or light sources are in
    frame; 1 when `ev_p999` > −1.5.
  - shadows: 2 when `dark_area` > 15%, or dark areas (large or small inside
    bright) with `spread_ev` > 7; 1 when `dark_area` > 3% or backlit.
  - blacks: 2 when `ev_p01` is over 6 EV under the median and `black` < 2%;
    1 when over 4 EV under.
  - contrast: 2 when `spread_ev` is 6–10 and `clipped` < 0.5%; else 1.
  - clarity: 2 for landscapes, cityscapes, architecture, street, portraits,
    people and interiors with some texture; else 1.
  - texture: texture none, some, lots → 0, 1, 2.
  - dehaze: haze none, light, strong → 0, 1, 2.
- `parity_use`: useless for junk, black frames and lens caps; limited for charts,
  blurred, noisy or badly exposed frames, or more than 5% clipped; else good.
  `parity_reason` says which.

The thresholds are first guesses. After changing them, `describe --rederive`
recomputes the tone fields from the stored scene fields and `stats` without
running the model again.
