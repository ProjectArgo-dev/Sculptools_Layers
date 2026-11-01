# Credits

**Sculptools: Layers** is a fork of the original **Sculpt Layers** add-on by
[mifth](https://superhivemarket.com/products/sculpt-layers), released — like the original — under the GNU General Public License v3.0 or later.

This project starts from Sculpt Layers and takes it in its own direction. My heartfelt thanks to mifth for the great work the whole
thing rests on; *Sculptools: Layers* is an independent continuation and is
neither affiliated with nor endorsed by him.

Maintained by **ProjectArgo**.


## Modifications

This is a modified version of Sculpt Layers, not the original.

### 2024 — mifth: the original *Sculpt Layers*

The layer system this add-on is built on:

* Layers stored as per-vertex offsets on a hidden base object, each with its
own opacity and visibility, with Multires support.
* Add, remove, reorder, save, merge, duplicate and collapse layers.
* SubD / UnSubD of the layers (Subdivide, Simple, Linear).
* Erasing part of a layer with a sculpt mask.
* Switching and duplicating layer data between objects.

### 2026 — ProjectArgo: *Sculptools: Layers*

Renamed and rebuilt as a Blender 4.5+ extension, tested on Blender 4.5
through 5.2.

**Added:**

* Automatic saving: strokes go into the active layer by themselves when you
switch layers, leave Sculpt Mode or save the file, so there is no Save Layer
button.
* Invert Layer and Mirror Layer (symmetrize or flip, by axis and source side).
* Shift+click on a layer's eye to isolate it.
* Bake to Shape Keys, one key per layer or a single combined pose, including
from Multires.
* Sync Multires, to adopt a Multires subdivided outside the add-on.
* Clean Up Unused Data.
* A popup opacity slider that opens under the cursor and applies the value
once you stop dragging, with each change undone in one step.
* Layer data that follows the mesh: Shift+D copies the layers and Alt+D shares
them, and removing layers from one object never deletes them for the objects
still using them.
* A link to the layer data that survives renaming its collection, and a
format version with migration for the stored data.
* Guards that refuse layer edits when the object is hidden, or when Dyntopo, a
remesh or an out-of-sync Multires would make them unusable, and that restore
modifiers, shape keys and Multires levels when an operation fails.

**Reworked:**

* Erasing: the Morph Brush, which removes the active layer gradually as you
paint with Blender's Layer brush, with or without Multires.
* Sculpting on a hidden layer or one at opacity 0: the strokes are stored in
that layer instead of being refused.
* Duplicate Layer, which names the copy the Blender way and places it right
above the original.
* The layer-data picker, now a menu that refuses incompatible data and saves
the current work before switching.
* Undo, so that each layer operation takes a single Ctrl+Z.
* Shape keys: layers now write to the Basis key, so they work on meshes that
have shape keys.

**Removed:** Save Base, the manual Save Layer button (replaced by automatic
saving) and the conversion of data from older versions.

Copyright (C) 2024 mifth
Copyright (C) 2026 ProjectArgo

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE) for the full text.

