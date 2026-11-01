# SPDX-FileCopyrightText: 2024 mifth
# SPDX-FileCopyrightText: 2026 ProjectArgo
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Part of "Sculptools: Layers", a fork of the Sculpt Layers add-on by mifth,
# modified by ProjectArgo since 2026. See CREDITS.md.

import contextlib
import sys
import time
import traceback

import bpy
import numpy as np

SL_ATTRIB_NAME = 'SLayer_'  # Default attribute name
SL_NAME = 'Layer_'  # Default layer name
SL_BASE_OBJ_NAME = '_SL_B_'
# NOTE: `sculptLayers_parent_coll` and `sculptLayers_version` used to be spelled
# out here as PARENT_COLL_NAME / VERSION_NAME, for `obj.keys()` lookups. Both are
# registered RNA properties, not custom props, so those lookups never matched;
# the code accesses them as plain attributes now and the constants are gone.

# Morph session markers, set as custom properties on the temporary dense copy
# the user paints on (SL_MorphBrush / SL_ApplyMorph in layers_base). A copy CAN
# be saved in a .blend mid-session, so these are persisted names: never rename.
MORPH_OBJ_SUFFIX = '_SL_Morph'
MORPH_SRC_PROP = 'sculptLayers_morph_src'      # original object name
MORPH_COLL_PROP = 'sculptLayers_morph_coll'    # parent collection name
MORPH_INDEX_PROP = 'sculptLayers_morph_index'  # active layer index at session start
MORPH_OPACITY_PROP = 'sculptLayers_morph_opacity'          # its opacity then
MORPH_LAYER_BRUSH_PROP = 'sculptLayers_morph_layer_brush'  # Layer brush as found
MORPH_PREV_BRUSH_PROP = 'sculptLayers_morph_prev_brush'    # brush to give back
# The copy's shape at session start, in an attribute of our own: Blender's
# `.sculpt_persistent_co` is overwritten by the Layer brush's "Set Persistent
# Base" button, so Apply must never read P0 from there.
MORPH_P0_ATTR = 'sculptLayers_morph_p0'
# Blender's persistent-base attributes, read by the Layer brush at stroke time.
PERSIST_CO_ATTR = '.sculpt_persistent_co'
PERSIST_NO_ATTR = '.sculpt_persistent_no'
PERSIST_DISP_ATTR = '.sculpt_persistent_disp'
# What a Morph session sets on the Layer brush (and therefore saves first).
# Direction ADD is load-bearing: SUBTRACT without Ctrl overshoots the target
# and brings the layer back inverted (measured 2026-10-06, r = 2.0).
MORPH_BRUSH_SETTINGS = {"use_persistent": True, "height": 1.0, "direction": 'ADD'}
# The strength a Morph session STARTS the Layer brush at (the user's call,
# 2026-10-06: at 1.0 the erase leaves hard edges). Set once and given back at
# the end; never enforced, so it can be changed mid-session.
MORPH_START_STRENGTH = 0.5

# Isolate state for the layer list's eye column (Shift+Click). A plain boolean
# IDProperty on the BASE object, so it follows the layer data: saved in the
# .blend, restored by undo, carried along by Link Data. Its only effect is which
# action the NEXT Shift+Click performs, so a stale value costs one extra click
# and can never corrupt layer data -- no invalidation on add/remove/move.
ISOLATE_PROP = 'sculptLayers_isolate'

# Top collection that holds every object's layer-data sub-collection.
SCULPT_COLL_NAME = 'LAYERS_DATA'
# Pre-1.3.5 name; adopted/renamed to SCULPT_COLL_NAME on the first mutating use.
SCULPT_COLL_NAME_LEGACY = 'SCULPT_LAYERS'

# Addon version (single source of truth, no circular imports)
ADDON_VERSION = (1, 0, 0)
ADDON_VERSION_STR = str(ADDON_VERSION)


# ----------------------------------------------------------------------
# Layer-data format version
# ----------------------------------------------------------------------
# ADDON_VERSION above is the add-on's public version. This is a SEPARATE integer
# describing the SHAPE of the data stored in the .blend, and it moves only when
# that shape actually changes -- so a patch release never makes an older build
# cry wolf about a file it can read perfectly.
#
# A plain IDProperty on the BASE object, like sculptLayers_mr_levels /
# _cage_verts / _isolate. It follows the data (saved in the .blend, restored by
# undo, carried by object.copy(), which is what Duplicate Data relies on) and it
# stays readable while the add-on is DISABLED -- precisely the state Blender
# passes through during an update. A registered RNA property would be invisible
# to obj.keys() and would leave the namespace on unregister().
#
# One stamp per DATASET, not per object: Link Data points several objects at one
# base object, so a stamp on the sculpted object would mean N stamps for one set
# of layers, free to diverge.
#
# Semantics: "the format this data is in NOW", not "created with". Exactly two
# things write it -- NewBaseObject at creation, and migrate_layer_data() after
# each successful step. A MISSING stamp means 1: for files written before this
# existed the fallback IS the migration, the same shape as CAGE_VERTS_PROP and
# as parent_collection()'s legacy-name fallback.
DATA_FORMAT_VERSION = 1
DATA_VERSION_PROP = "sculptLayers_data_version"


def data_format_version(base_obj):
    """READ-ONLY. The format version *base_obj*'s layer data is in, or None.

    Safe from poll()/draw(). A missing stamp reads as 1 (pre-versioning data),
    and so does a corrupted one: re-running a migration is idempotent, whereas
    skipping one is not, so the oldest format is the safe way to be wrong."""
    if base_obj is None:
        return None
    try:
        return int(base_obj.get(DATA_VERSION_PROP, 1))
    except (TypeError, ValueError):
        return 1


def data_format_newer(base_obj):
    """READ-ONLY. True when this layer data was written by a NEWER add-on.

    Drives the panel's advisory note only. It deliberately does NOT feed
    layers_blocked_reason(): the decision (user, 2026-08-17) is to warn and let
    the user work. What makes that safe is migrate_layer_data() leaving data
    newer than itself strictly alone."""
    v = data_format_version(base_obj)
    return v is not None and v > DATA_FORMAT_VERSION


# Ordered migration steps: {from_version: fn(base_obj) -> None}, each raising the
# data by exactly ONE version. EMPTY at format 1, and that is the point -- what
# ships today is not a migration but the guarantee that every file leaving from
# now on carries a reliable stamp, so a future step always knows where it starts.
#
# RULE for any step added here: it must be ADDITIVE -- write the new
# representation, never delete the old one. That is what set_parent_collection()
# already does (pointer AND legacy name), and it is what replaces an explicit
# backup of the layer data: an older build keeps reading its own fields, so a
# downgrade stays survivable by construction.
_MIGRATIONS = {}


def migrate_layer_data(base_obj):
    """EXECUTE-ONLY. Bring *base_obj*'s layer data up to DATA_FORMAT_VERSION.

    Idempotent, which is what makes it harmless to call on every mutating
    operation -- and mutation_guard does exactly that. Never call it from
    poll()/draw() (side effects on every redraw) or from load_post (writing to
    objects there marks a freshly opened .blend as modified)."""
    v = data_format_version(base_obj)
    if v is None or v > DATA_FORMAT_VERSION:
        # No owner, or data NEWER than us -- left strictly alone. This is the
        # line that makes the panel's "warn, don't block" note safe: an older
        # build must never try to repair a format it does not know, and must not
        # stamp it either.
        return
    while v < DATA_FORMAT_VERSION:
        step = _MIGRATIONS.get(v)
        if step is None:
            print(f"Sculptools: Layers has no migration from data format {v}; "
                  "the layer data is left as it is.")
            return
        step(base_obj)
        v += 1
        # Stamped after EVERY step, not once at the end: if step 2 of 3 raises,
        # the data is coherently at format 2 rather than at 1 with half a
        # migration applied on top of it.
        base_obj[DATA_VERSION_PROP] = v
    # HEAL a missing or corrupted stamp on data that is already current. Without
    # this, a .blend written before the stamp existed reads as format 1 through
    # the fallback and never actually acquires one -- so the guarantee this whole
    # mechanism exists for ("every file carries a reliable stamp") would hold
    # only for datasets created from scratch. Reached on the no-migration path
    # too, where it is a no-op because the value already matches.
    if base_obj.get(DATA_VERSION_PROP) != DATA_FORMAT_VERSION:
        base_obj[DATA_VERSION_PROP] = DATA_FORMAT_VERSION


def getBaseObject(parent_col):
    if parent_col.objects:
        return parent_col.objects[0]
    return None


# ----------------------------------------------------------------------
# The layer-data link
# ----------------------------------------------------------------------
# An object points at its layer-data collection TWICE:
#   * `sculptLayers_parent_coll_ptr` -- a real Collection pointer, and the source
#     of truth. Blender remaps it on rename and sets it to None when the
#     collection is removed, so it can never dangle. Measured on 4.5 and 5.2
#     (dev_probe_coll_pointer.py), together with: it survives a save/load round
#     trip, and object.copy() carries it -- which Duplicate Data relies on.
#   * `sculptLayers_parent_coll` -- the legacy NAME. It is persisted in every
#     .blend the add-on has ever written, so it keeps being written and read.
#
# The name ALONE was a data-loss path, measured 2026-08-12: renaming a
# `<obj>_DATA_<n>` collection in the Outliner -- a tidy-up gesture, nothing more
# -- detached the layers in silence (poll_has_layers went False and the panel
# fell back to its empty state), and from then on `sl.cleanup_data` reported that
# collection as unused and offered to delete the user's layers. Resolving the
# pointer first makes a rename invisible instead.
#
# MIGRATION: a file written before this change carries only the name.
# parent_collection() falls back to it, so nothing breaks, and the pointer is
# written the next time anything links (sl.add_layer / sl.link_data / Duplicate
# Data). Files are deliberately NOT healed from `load_post`: writing to objects
# there would mark a freshly opened .blend as modified, and the fallback already
# makes such a file work.

def parent_collection(active_obj):
    """READ-ONLY. The object's layer-data collection, or None.

    Safe from poll()/draw(). Prefers the rename-proof pointer and falls back to
    the legacy name for files that predate it. This is the ONLY way to resolve
    the link -- never index `bpy.data.collections` with the name directly, or a
    renamed collection reads as "no layers"."""
    if active_obj is None:
        return None
    coll = active_obj.sculptLayers_parent_coll_ptr
    if coll is not None:
        return coll
    name = active_obj.sculptLayers_parent_coll
    return bpy.data.collections.get(name) if name else None


def set_parent_collection(active_obj, coll):
    """Link -- or, with coll=None, unlink -- the object's layer data.
    EXECUTE-ONLY (it writes two properties).

    Always writes BOTH: the pointer is what survives a rename, the name is what
    existing .blend files and any older build read. `sculptLayers_parent_coll` is
    a registered RNA StringProperty, so it cannot be `del`eted -- "" clears it."""
    active_obj.sculptLayers_parent_coll_ptr = coll
    active_obj.sculptLayers_parent_coll = coll.name if coll is not None else ""


def data_users(coll):
    """READ-ONLY. Every object whose layer data is *coll*; [] for None.

    Safe from poll()/draw(). Resolved through parent_collection(), so a name-only
    link from an older file counts exactly like a pointer -- the rule
    get_unused_data_collections() applies too, so the two can never disagree
    about whether a dataset is in use.

    Sharing is a normal state, not an error: sl.link_data creates it on purpose,
    and a plain Shift+D creates it in silence, because object.copy() carries the
    pointer (measured 2026-10-01, dev_probe_audit.py T2)."""
    if coll is None:
        return []
    return [o for o in bpy.data.objects if parent_collection(o) == coll]


def mesh_group(obj):
    """READ-ONLY. *obj* plus every object sharing BOTH its mesh and its layer
    data -- its Alt+D copies. [] for None.

    The group is the unit layer data belongs to: one mesh cannot fall behind
    itself, so objects sharing a mesh can share a dataset safely, and every
    operation that changes WHICH dataset an object uses must move the whole
    group (two datasets driving one mesh would overwrite each other)."""
    if obj is None:
        return []
    coll = parent_collection(obj)
    return [o for o in bpy.data.objects
            if o.data == obj.data and parent_collection(o) == coll]


def other_data_users(obj):
    """READ-ONLY. The objects using *obj*'s layer data through a DIFFERENT mesh
    -- a Shift+D copy not yet edited, or data linked before 2026-10-01.

    These are the ones an edit made through *obj* would leave behind, which is
    why ensure_own_data() splits them off before any write, and why an object
    LEAVING the data (Collapse, last-layer remove) must not delete it while any
    remain."""
    if obj is None:
        return []
    return [o for o in data_users(parent_collection(obj)) if o.data != obj.data]


def link_mesh_group(obj, coll):
    """EXECUTE-ONLY. Point *obj* and its whole mesh group at *coll*; None
    unlinks them. The group is resolved before anything is written."""
    for o in mesh_group(obj):
        set_parent_collection(o, coll)


def copy_layer_data(context, obj, base_obj):
    """EXECUTE-ONLY. A new data collection, named after *obj*, holding a full
    copy of *base_obj*: object, mesh, layer attributes, layer list and every
    stamp on it. Links nothing -- the caller decides who uses it."""
    sc_coll = GetSculptCollection(context, True)
    new_coll = CreateParentCollection(context, obj, sc_coll)
    context.scene.sculptLayers_scene_id += 1
    new_base = base_obj.copy()
    new_base.data = base_obj.data.copy()
    # Named after the new collection, not left as Blender's copy name: the
    # copy came out as `Ball_SL_B_2.001` inside `Ball_DATA_2` (seen in the
    # 2026-10-05 usage pass), a suffix that says nothing about which dataset
    # it is.
    new_base.name = base_object_name(context, obj, new_coll)
    new_base.data.name = new_base.name
    new_coll.objects.link(new_base)
    return new_coll


def ensure_own_data(context, obj, force=False, writer=None):
    """EXECUTE-ONLY. Before anything writes *obj*'s layer data: if another MESH
    uses that data too, give *obj* and its mesh group a copy of their own.
    Returns the new collection, or None when nothing was copied. With
    force=True it copies regardless (Duplicate Data).

    Layer data follows the mesh, as in Blender: Shift+D copies the mesh, so it
    must not keep sharing the layers. It did, because object.copy() carries the
    link, and the two objects then diverged -- every edit rebuilt only the
    active object's mesh, and the other object's next commit subtracted layers
    its mesh had never received. Measured 2026-10-01 (dev_probe_shared_stale.py,
    4.5 + 5.2): the copy's new layer came back holding the original's stroke
    alone, or was subtracted into a DIFFERENT layer. Splitting before the FIRST
    write means two meshes can never diverge -- every divergence starts with a
    write. Objects sharing one mesh (Alt+D) never split: the same probe reads
    0.000 for them, Multires included.

    The splitting side keeps the data as it stands. The one exception is an
    opacity a script assigned (the property path): RNA has already stored it on
    the SHARED base, but no mesh shows it until its deferred rebuild runs. It
    belongs to whoever queued it -- *writer* True/False says whether that is
    this group, None reads it off the pending record -- and the other side gets
    back the values its meshes were built with (`_prev_opacity`).

    It also runs the lazy MIGRATION, first, so that a copy is made from data
    already in the current format. Every writer has to come through here --
    directly, via mutation_guard or via apply_layer_opacity, which
    dev_test_data_follows_mesh.py section I enforces -- so this is what gives
    the writers outside mutation_guard (the active-layer switch, Move,
    Duplicate Data, Apply Morph, both opacity paths) the migration they used to
    skip. Idempotent: mutation_guard migrating just before costs nothing."""
    _migrate_on_entry(obj)
    coll = parent_collection(obj)
    if coll is None:
        return None
    if not force and not other_data_users(obj):
        return None
    base_obj = getBaseObject(coll)
    if base_obj is None:
        return None
    group = mesh_group(obj)
    new_coll = copy_layer_data(context, obj, base_obj)
    for o in group:
        set_parent_collection(o, new_coll)
    _hand_over_pending(base_obj, getBaseObject(new_coll),
                       {o.name for o in group}, writer)
    return new_coll


def _hand_over_pending(old_base, new_base, names, writer):
    """Keep the two queued opacity changes valid across a split.

    The records name their base object; a group that just moved must have its
    records follow it, or flush_live_opacity() drops the user's value as stale
    and _run_opacity_rebuild() rebuilds from the wrong data. Then, if a
    script-assigned value is in flight on the old data, it stays with its
    writer and the other side is restored (see ensure_own_data)."""
    global _pending_opacity, _pending_live_opacity
    pend = _pending_opacity
    if pend is not None and pend[0] in names and pend[1] == old_base.name:
        _pending_opacity = (pend[0], new_base.name, pend[2])
    live = _pending_live_opacity
    if live is not None and live[0] in names and live[1] == old_base.name:
        _pending_live_opacity = (live[0], new_base.name, live[2], live[3])
    if writer is None:
        if pend is None or pend[1] != old_base.name:
            return  # no script-assigned value in flight on this data
        writer = pend[0] in names
    _restore_prev_opacities(old_base if writer else new_base)


def _restore_prev_opacities(base_obj):
    """Put every layer back to the opacity its meshes were built with. The 1.0
    fallback is the property default, the same rule the commit uses."""
    global _in_opacity_update
    for sl_i in base_obj.sculptLayers:
        prev = sl_i.get("_prev_opacity", 1.0)
        if sl_i.layer_opacity != prev:
            _in_opacity_update = True
            try:
                sl_i.layer_opacity = prev
            finally:
                _in_opacity_update = False


def get_morph_mask_source(obj):
    """If *obj* is an active Morph-mask temp object, return the name of the
    original source object it was spawned from; otherwise None.

    READ-ONLY: safe to call from poll()/draw() (no scene mutation)."""
    if obj is None:
        return None
    return obj.get(MORPH_SRC_PROP) or None


def _write_point_attr(mesh, name, data_type, key, values):
    """Create-or-replace a POINT attribute of *mesh* and fill it."""
    att = mesh.attributes.get(name)
    if att is not None and (att.data_type != data_type or att.domain != 'POINT'):
        mesh.attributes.remove(att)
        att = None
    if att is None:
        att = mesh.attributes.new(name, data_type, 'POINT')
    att.data.foreach_set(key, values)


def _read_point_attr(mesh, name, width):
    """READ-ONLY. A POINT attribute as a flat float32 array, or None."""
    att = mesh.attributes.get(name)
    if att is None or att.domain != 'POINT':
        return None
    out = np.empty(len(mesh.vertices) * width, dtype=np.float32)
    att.data.foreach_get('vector' if width == 3 else 'value', out)
    return out


def arm_morph_session(tmp_obj, src_obj, coll_name, base_obj, idx):
    """Write a Morph session onto the temporary copy *tmp_obj*: its markers, P0
    (the copy's shape now) and the Layer brush's persistent base, armed the
    INVERTED way -- co = P0, no = target - P0, disp = 0 -- so a brush with
    height 1 places a point at P0 + (target - P0) * disp, and disp, which grows
    with the brush strength and clamps at 1, is the erased fraction.

    The plain arming (co = target, height 0) erases ~95% per stroke at ANY
    strength; this one grades -- ~6% per stroke at 0.1, 84-94% at 1.0, never
    past the target, never sideways (measured 2026-10-06, 4.5.13 + 5.2.2,
    dev_gui_morph_persistent_layer.py). Object or Sculpt Mode, plain mesh.
    The caller guarantees the point counts match. Returns the target."""
    me = tmp_obj.data
    n = len(me.vertices)
    p0 = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get('co', p0)
    target = layer_stack_coords(base_obj, exclude_index=idx)
    _write_point_attr(me, MORPH_P0_ATTR, 'FLOAT_VECTOR', 'vector', p0)
    _write_point_attr(me, PERSIST_CO_ATTR, 'FLOAT_VECTOR', 'vector', p0)
    _write_point_attr(me, PERSIST_NO_ATTR, 'FLOAT_VECTOR', 'vector', target - p0)
    if me.attributes.get(PERSIST_DISP_ATTR) is not None:
        _write_point_attr(me, PERSIST_DISP_ATTR, 'FLOAT', 'value',
                          np.zeros(n, dtype=np.float32))
    tmp_obj[MORPH_SRC_PROP] = src_obj.name
    tmp_obj[MORPH_COLL_PROP] = coll_name
    tmp_obj[MORPH_INDEX_PROP] = idx
    tmp_obj[MORPH_OPACITY_PROP] = float(base_obj.sculptLayers[idx].layer_opacity)
    me.update()
    return target


def apply_morph_delta(tmp_obj, base_obj, idx):
    """Add what changed on the Morph copy to layer *idx*:
    delta += (P1 - P0) / opacity_at_start. P1 is read off the copy's mesh, so
    call it in Object Mode.

    Untouched points stay bit-identical (their change is exactly 0). A low
    opacity amplifies the stored change x1/opacity -- the rule for every
    stroke on a dimmed layer (CLAUDE.md: by design), so the layer shows after
    Apply exactly what the copy showed.

    Returns the largest absolute change (0.0 when nothing moved), or None --
    nothing written -- when the copy carries no P0 (a session started by an
    older build), the point counts differ or the recorded opacity is 0."""
    me = tmp_obj.data
    n = len(base_obj.data.vertices)
    if len(me.vertices) != n:
        return None
    p0 = _read_point_attr(me, MORPH_P0_ATTR, 3)
    if p0 is None:
        return None
    opacity = float(tmp_obj.get(MORPH_OPACITY_PROP, 1.0))
    if opacity <= 0.0:
        return None
    change = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get('co', change)
    change -= p0
    biggest = float(np.abs(change).max()) if n else 0.0
    if biggest == 0.0:
        return 0.0
    if opacity != 1.0:
        change /= np.float32(opacity)
    att = base_obj.data.attributes[base_obj.sculptLayers[idx].layer_attrib_name]
    d = np.empty(n * 3, dtype=np.float32)
    att.data.foreach_get('vector', d)
    d += change
    att.data.foreach_set('vector', d)
    return biggest


def morph_session_base(tmp_obj):
    """READ-ONLY. (base object, layer index) of the data a Morph copy edits,
    resolved through its SOURCE's link -- never through the collection name
    the session recorded (Link / Duplicate Data can have moved it).
    (None, None) when either is gone."""
    src = bpy.data.objects.get(tmp_obj.get(MORPH_SRC_PROP) or "")
    coll = parent_collection(src) if src is not None else None
    base_obj = getBaseObject(coll) if coll is not None else None
    idx = tmp_obj.get(MORPH_INDEX_PROP)
    if base_obj is None or idx is None or not 0 <= idx < len(base_obj.sculptLayers):
        return None, None
    return base_obj, idx


def resync_morph_disp(tmp_obj):
    """Bring the Layer brush's per-point factor back in step with the copy's
    positions. A sculpt undo restores POSITIONS but not
    `.sculpt_persistent_disp` (measured 2026-10-06, 4.5.13 + 5.2.2,
    dev_gui_morph_persistent_undo.py): without this, the stroke after a Ctrl+Z
    re-applied the undone erase in full, even at strength 0.01.
    disp = (pos - P0) . no / |no|^2, clamped to [0, 1].

    Also re-arms co / no when they no longer match P0 -- the Layer brush's own
    "Set Persistent Base" button overwrites them.

    Writes ONLY the copy's persistent-base attributes: never layer data, never
    positions, no mode switch, no undo push. Returns True if it wrote."""
    me = tmp_obj.data
    n = len(me.vertices)
    p0 = _read_point_attr(me, MORPH_P0_ATTR, 3)
    if p0 is None:
        return False
    wrote = False
    co = _read_point_attr(me, PERSIST_CO_ATTR, 3)
    no = _read_point_attr(me, PERSIST_NO_ATTR, 3)
    if co is None or no is None or not np.array_equal(co, p0):
        base_obj, idx = morph_session_base(tmp_obj)
        if base_obj is None or len(base_obj.data.vertices) != n:
            return False
        no = layer_stack_coords(base_obj, exclude_index=idx) - p0
        _write_point_attr(me, PERSIST_CO_ATTR, 'FLOAT_VECTOR', 'vector', p0)
        _write_point_attr(me, PERSIST_NO_ATTR, 'FLOAT_VECTOR', 'vector', no)
        wrote = True
    pos = np.empty(n * 3, dtype=np.float32)
    me.vertices.foreach_get('co', pos)
    no3 = no.reshape(-1, 3).astype(np.float64)
    off = (pos - p0).reshape(-1, 3).astype(np.float64)
    nn = (no3 * no3).sum(1)
    disp = np.where(nn > 1e-20, (off * no3).sum(1) / np.maximum(nn, 1e-30), 0.0)
    disp = np.clip(disp, 0.0, 1.0).astype(np.float32)
    old = _read_point_attr(me, PERSIST_DISP_ATTR, 1)
    if (old is None and not disp.any()) or (old is not None and np.array_equal(old, disp)):
        if wrote:
            me.update()
        return wrote
    _write_point_attr(me, PERSIST_DISP_ATTR, 'FLOAT', 'value', disp)
    me.update()
    return True


def base_object_name(context, active_obj, parent_col):
    """READ-ONLY. The name a new base object gets: its data collection's, with
    `_SL_B_` in place of `_DATA_` -- `Ball_DATA_2` holds `Ball_SL_B_2`.

    The number used to come from `sculptLayers_scene_id` AFTER the caller had
    already spent it on the collection, so every dataset read `Ball_DATA_0` >
    `Ball_SL_B_1`, and Duplicate Data's copy kept Blender's `.001` on top. A
    collection whose name has no `_DATA_` (renamed by the user before its base
    went missing) falls back to the old rule. Nothing parses these names; they
    are for the person reading the Outliner."""
    head, sep, tail = parent_col.name.rpartition('_DATA_')
    if sep:
        return head + SL_BASE_OBJ_NAME + tail
    return (active_obj.name + SL_BASE_OBJ_NAME +
            str(context.scene.sculptLayers_scene_id))


def NewBaseObject(context, active_obj, parent_col, multires_mod):
    base_obj_name = base_object_name(context, active_obj, parent_col)
    # create a new mesh from the active mesh, named after the base object it
    # will belong to -- it was a bare "mesh", so Blender File listed mesh,
    # mesh.001, mesh.002... with nothing saying whose layers each one held.
    new_mesh = bpy.data.meshes.new(base_obj_name)
    work_mesh = active_obj.data
    if multires_mod:
        work_mesh = multires_mod[2]

    new_mesh.vertices.add(len(work_mesh.vertices))

    # get co from active mesh
    np_active_verts = np.zeros(len(work_mesh.vertices) * 3, dtype=np.float32)
    work_mesh.vertices.foreach_get('co', np_active_verts)

    # set co from active mesh
    new_mesh.vertices.foreach_set('co', np_active_verts)

    base_obj = bpy.data.objects.new(base_obj_name, new_mesh)
    parent_col.objects.link(base_obj)

    base_obj["sculptLayers_mr_levels"] = (
        multires_mod[0].total_levels if multires_mod else 0
    )
    # The single creation point for layer data, so the single place that stamps
    # the format it is being created in.
    base_obj[DATA_VERSION_PROP] = DATA_FORMAT_VERSION

    return base_obj


# ----------------------------------------------------------------------
# Crash safety for the mutating operators
# ----------------------------------------------------------------------
# Every operator that edits layers walks the same path: leave Sculpt Mode, mute
# the shape keys, push the Multires to its top level while switching the other
# modifiers off, build a temporary object, do the numpy work, then put all four
# back. Each explicit `return {'CANCELLED'}` already restores them by hand -- but
# an EXCEPTION did not. A raise inside multires_subdivide / multires_reshape
# (the two ops most likely to fail) left the user with muted shape keys,
# modifiers switched off, a Multires stuck at the wrong level and `_TMP_*`
# objects littering the scene -- silent, and easy to mistake for corrupted work.
#
# `mutation_guard` closes that hole WITHOUT touching the happy path: it only
# acts when an exception escapes, and it re-raises afterwards so Blender still
# prints the traceback and the failure is never swallowed.


class _MutationState:
    """Records what a mutating operator changed, so a failure can undo it.

    The operator fills the fields in as it goes; anything still unset is simply
    skipped on rollback. `track()` registers temporary datablocks for deletion.
    """
    __slots__ = ("active_obj", "context", "context_mode",
                 "shapekeys", "multires", "other_mods", "temp_ids", "undo_steps")

    def __init__(self, context, active_obj, context_mode):
        self.context = context
        self.active_obj = active_obj
        self.context_mode = context_mode
        self.shapekeys = []      # ShapeKeysOff() result
        self.multires = None     # GetMultires() tuple
        self.other_mods = None   # OtherModsOff() dict, when used without Multires
        self.temp_ids = []
        self.undo_steps = []

    def track(self, datablock):
        """Register a temporary object/mesh to remove if the operator fails.
        Returns *datablock* so it can wrap a creation expression."""
        self.temp_ids.append(datablock)
        return datablock

    def on_failure(self, fn):
        """Register an extra rollback step, run (in reverse) before the standard
        restore. For state that is neither a datablock nor one of the four
        fields above -- e.g. sl.subdivide_layers adding a Multires modifier to an
        object that had none, which a failure must not leave behind."""
        self.undo_steps.append(fn)

    def rollback(self):
        """Put everything back. Never raises: it runs while another exception is
        already in flight, and a secondary failure must not mask the original --
        so every step is isolated and a partial rollback still beats none."""
        for fn in reversed(self.undo_steps):
            try:
                fn()
            except Exception:
                pass

        for db in reversed(self.temp_ids):
            # May already be gone (or a dead StructRNA) if the operator failed
            # after its own cleanup -- that is exactly what the guard tolerates.
            try:
                if isinstance(db, bpy.types.Object):
                    mesh = db.data
                    bpy.data.objects.remove(db, do_unlink=True)
                    if isinstance(mesh, bpy.types.Mesh) and mesh.users == 0:
                        bpy.data.meshes.remove(mesh)
                elif isinstance(db, bpy.types.Mesh) and db.users == 0:
                    bpy.data.meshes.remove(db)
            except Exception:
                pass

        # Same order as the manual restore sequence on the happy path:
        # multires level -> other modifiers -> shape keys -> mode.
        if self.multires:
            try:
                self.multires[0].levels = self.multires[1]
            except Exception:
                pass
            try:
                OtherModsOn(self.multires)
            except Exception:
                pass
        if self.other_mods:
            try:
                for mod in self.other_mods.values():
                    mod.show_viewport = True
            except Exception:
                pass
        try:
            ShapeKeysOn(self.active_obj, self.shapekeys)
        except Exception:
            pass
        try:
            # The operator may have left another object active (the temp ones
            # get selected for multires_reshape), so point the context back at
            # ours before asking for a mode change.
            if self.context.view_layer.objects.active is not self.active_obj:
                self.context.view_layer.objects.active = self.active_obj
            if self.active_obj.mode != self.context_mode:
                bpy.ops.object.mode_set(mode=self.context_mode, toggle=False)
        except Exception:
            pass


def _migrate_on_entry(active_obj):
    """EXECUTE-ONLY. Migrate *active_obj*'s layer data, if it has any.

    None-safe at both hops: an unlinked object returns here, and an empty parent
    collection yields base_obj None, which migrate_layer_data() ignores."""
    coll = parent_collection(active_obj)
    if coll is None:
        return
    migrate_layer_data(getBaseObject(coll))


@contextlib.contextmanager
def mutation_guard(context, active_obj, context_mode):
    """Restore the scene if the wrapped operator body raises, then re-raise.

    Also the add-on's single LAZY MIGRATION point. _migrate_on_entry() runs
    here, because this is the one place that already executes at the start of
    every mutating operator -- so coverage is automatic, including for operators
    added later, which by convention must use this guard. It is idempotent and
    costs microseconds (a parent_collection() lookup, a dict.get and an integer
    compare) against the 200-400 ms multires_reshape spends on the same click.
    It runs INSIDE the try, so a failing migration is rolled back and re-raised
    rather than escaping unhandled.

    For the same reason it is where shared layer data is SPLIT: ensure_own_data()
    runs right after the migration, before the body resolves the base object or
    pushes its undo step -- so the body only ever writes the object's own data,
    and the split rides inside the state the undo step records.

    Otherwise still a no-op on success: the operators keep their explicit
    restore code, and the rollback only covers the exception path they never
    handled."""
    state = _MutationState(context, active_obj, context_mode)
    try:
        _migrate_on_entry(active_obj)
        ensure_own_data(context, active_obj)
        yield state
    except Exception:
        print("Sculptools: Layers is rolling back the scene after a failed operator:")
        traceback.print_exc()
        state.rollback()
        raise


def ShapeKeysOff(active_obj: bpy.types.Object):
    shapes_mute = []
    if active_obj.data.shape_keys:
        for i, shape_key in enumerate(active_obj.data.shape_keys.key_blocks):
            if i != 0:
                if shape_key.value != 0 and not shape_key.mute:
                    shapes_mute.append(i)
                    shape_key.mute = True
    return shapes_mute


def ShapeKeysOn(active_obj: bpy.types.Object, shapekeys_mutes: list):
    if active_obj.data.shape_keys:
        for i, shape_key in enumerate(active_obj.data.shape_keys.key_blocks):
            if i in shapekeys_mutes:
                shape_key.mute = False


# ----------------------------------------------------------------------
# Live vertex positions
# ----------------------------------------------------------------------
# On a plain mesh the live coordinates are `mesh.vertices`. The moment shape
# keys exist that stops being true: the reference ("Basis") key shadows the mesh
# data, so writes to `mesh.vertices` are silently invisible and reads return
# stale coordinates (measured on 5.2 -- writing the mesh moved nothing, writing
# the reference key moved everything).
#
# The layer stack always describes the object's NEUTRAL shape, and ShapeKeysOff()
# mutes every pose key so the evaluated result collapses onto exactly that
# reference key. That key is therefore where layers must read from and write to.
# Route every non-Multires vertex read/write through the three helpers below;
# with no shape keys they are exactly the old `mesh.vertices` access.

def GetLiveCoordsOwner(active_obj):
    """Return the collection holding the object's live vertex positions, i.e.
    what foreach_get/foreach_set('co', ...) must target."""
    keys = active_obj.data.shape_keys
    if keys is not None and keys.reference_key is not None:
        return keys.reference_key.data
    return active_obj.data.vertices


def ReadLiveCoords(active_obj, verts_count):
    """Live vertex positions as a flat (verts_count * 3,) float32 array."""
    np_co = np.empty(verts_count * 3, dtype=np.float32)
    GetLiveCoordsOwner(active_obj).foreach_get('co', np_co)
    return np_co


def WriteLiveCoords(active_obj, np_co):
    """Write a flat float32 coordinate array onto the live vertex positions."""
    GetLiveCoordsOwner(active_obj).foreach_set('co', np_co)


def basis_key_active(active_obj):
    """READ-ONLY. True when a sculpt can safely be captured into a layer:
    either the object has no shape keys, or the active shape key IS the
    reference (Basis) key.

    Sculpt strokes land in the ACTIVE shape key (that is why sculpt tools abort
    on a locked active key). Layers read and write the reference key, so with a
    pose key active the strokes would never be picked up -- and folding them
    into the neutral shape would be wrong anyway, since they belong to the pose.

    Safe to call from poll()/draw(): no scene mutation, tolerates None and
    non-mesh objects."""
    keys = getattr(getattr(active_obj, "data", None), "shape_keys", None)
    if keys is None:
        return True
    ref = keys.reference_key
    if ref is None:
        return True
    return active_obj.active_shape_key == ref


# Per-layer opacities the VISIBLE MESH currently reflects, when they differ from
# the ones stored on the properties. Set only for the length of the opacity
# commit (see _run_opacity_rebuild): there the properties already hold the NEW
# values while the mesh still shows the old ones, and sl.save_layer has to divide
# by what the mesh shows. Passing them like this rather than writing the old
# values back onto the properties keeps the rollback invisible to Blender's undo,
# which used to snapshot it. None means "read the properties", the normal case.
_capture_opacities = None


def set_capture_opacities(layer_values):
    """Install (or clear, with None) the opacities the capture should divide by.

    Always call with None in a `finally`: a leaked override would make every
    later capture divide by stale values."""
    global _capture_opacities
    _capture_opacities = None if layer_values is None else list(layer_values)


def effective_opacities(base_obj):
    """READ-ONLY. The per-layer opacities the visible mesh reflects. Normally
    just the property values; during an opacity commit, the previous ones. Falls
    back to the properties if the override does not match the layer count
    (defensive: layers could have changed under it)."""
    stored = [sl_i.layer_opacity for sl_i in base_obj.sculptLayers]
    if _capture_opacities is None:
        return stored
    if len(_capture_opacities) != len(base_obj.sculptLayers):
        return stored
    return list(_capture_opacities)


def layer_stack_coords(base_obj, exclude_index=None):
    """READ-ONLY. `base + sum(visible opacity_i * delta_i)` as a flat float32
    array -- the shape the visible mesh is rebuilt to -- optionally without
    one layer.

    UpdateMesh builds every rebuild from this, and the Morph session takes its
    target from it with the active layer excluded, so the two can never
    disagree. The arithmetic is UpdateMesh's, unchanged:
    dev_test_update_mesh_numpy.py pins it bit for bit."""
    verts_count = len(base_obj.data.vertices)

    # get co from base mesh
    np_base_verts = np.empty(verts_count * 3, dtype=np.float32)
    base_obj.data.vertices.foreach_get('co', np_base_verts)

    # ONE scratch buffer for every layer, not a fresh zeroed array per layer:
    # foreach_get overwrites all of it each time. With ApplyLayerOpacity's
    # scalar multiply this took a 1M-point, 8-layer rebuild from 354 to 239 ms
    # on 4.5 (340 -> 234 on 5.2), bit-identical -- measured 2026-10-01,
    # dev_probe_audit.py T6, pinned by dev_test_update_mesh_numpy.py.
    np_sl_i_verts = np.empty(verts_count * 3, dtype=np.float32)
    for i, sl_i in enumerate(base_obj.sculptLayers):
        if exclude_index is not None and i == exclude_index:
            continue
        if sl_i.visible:
            # get co from a sculpt layer (mesh)
            base_obj.data.attributes[sl_i.layer_attrib_name].data.foreach_get('vector', np_sl_i_verts)

            # calculate opacity for a layer
            opacity_val = sl_i.layer_opacity
            if opacity_val != 1.0:
                ApplyLayerOpacity(np_sl_i_verts, opacity_val, verts_count, False)

            np.add(np_base_verts, np_sl_i_verts, out=np_base_verts)
    return np_base_verts


# Return can be Points or None
def UpdateMesh(context, active_obj, multires_mod, do_return_point=None, exclude_index=None):
    parent_col = parent_collection(active_obj)
    base_obj = getBaseObject(parent_col)

    verts_count = len(base_obj.data.vertices)
    np_base_verts = layer_stack_coords(base_obj, exclude_index)

    # apply new co to Active Object
    if multires_mod:
        # create a temporary mesh
        tmp_mesh = bpy.data.meshes.new("_TMP_SCULPTLAYER")
        temp_obj = None
        # try/finally: multires_reshape can raise, and the temp object/mesh
        # would then stay in the scene forever (it is linked to the scene
        # collection, so the user sees it). The callers' mutation_guard cannot
        # clean these up -- they are created and owned entirely in here.
        try:
            tmp_mesh.vertices.add(verts_count)

            # set positions for temporary mesh
            tmp_mesh.vertices.foreach_set("co", np_base_verts)

            # create temporary object
            temp_obj = bpy.data.objects.new(active_obj.name + '_TMP_SCULPTLAYER', tmp_mesh)
            context.scene.collection.objects.link(temp_obj)

            bpy.ops.object.select_all(action='DESELECT')
            temp_obj.select_set(True)
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj

            # Reshape Mesh
            bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)
        finally:
            # Clean
            if temp_obj is not None:
                bpy.data.objects.remove(temp_obj, do_unlink=True)  # REMOVE OBJ
            bpy.data.meshes.remove(tmp_mesh)  # REMOVE MESH
            temp_obj = None
            tmp_mesh = None
    else:
        WriteLiveCoords(active_obj, np_base_verts)

    if do_return_point:
        return np_base_verts

    np_base_verts = None
    return None


# State for the opacity-slider auto-save (see UpdateLayerOpacity).
_last_opacity_autosave = 0.0
_OPACITY_AUTOSAVE_DEBOUNCE = 0.2  # seconds
_in_opacity_update = False

# ----------------------------------------------------------------------
# Deferred (coalesced) opacity rebuild
# ----------------------------------------------------------------------
# Seconds of stillness before the single rebuild runs. Long enough to swallow a
# whole drag, short enough to feel immediate on a click or an arrow-key nudge.
#
# It must also outlast a PAUSE inside a drag. Python cannot tell "hand held
# still" from "mouse released" -- the property callback is the only signal, and
# it stops in both cases -- so a pause longer than this fires the rebuild
# mid-drag. That is not just a wasted rebuild: the rebuild flips to Object Mode
# and back, which ends Blender's active button-edit session, so BLENDER commits
# the current value as an undo step and the rest of the drag becomes a second
# one. Result: two Ctrl+Z presses to get back. Measured in a real GUI on
# 2026-07-29 -- our rebuild itself pushes nothing (see the push_undo note in
# UpdateLayersValue); every step on this path is Blender's.
# 0.15 was too tight: a brief hesitation at an intermediate value split the undo.
# Raising this trades post-release latency for fewer split drags; it does not
# eliminate the split, it only raises the pause a user has to make to cause one.
_OPACITY_REBUILD_DELAY = 0.35
# (object name, base object name) of the pending rebuild, or None. Names, not
# references: a PropertyGroup pointer can go stale between the drag and the timer
# firing. WHICH slider moved is deliberately not recorded -- the rebuild is a
# full recompute from all current values, and the commit step restores every
# opacity (see _run_opacity_rebuild), so any mix of pending changes coalesces
# into one correct rebuild.
_pending_opacity = None
_last_opacity_request = 0.0
_opacity_timer_armed = False


def _queue_opacity_rebuild(active_obj, base_obj, commit=True):
    """Record that an opacity moved and (re)arm the trailing-edge timer.

    `commit=False` is the undo path: there the mesh must be re-derived to match
    the restored values, but nothing may be captured INTO a layer -- the mesh
    just came back from an undo, and committing it would write the state the
    user just undid into the stored delta. If a slider change is already queued,
    its commit wins: `commit` ORs, it never downgrades."""
    global _pending_opacity, _last_opacity_request, _opacity_timer_armed
    if _pending_opacity is not None and _pending_opacity[2]:
        commit = True
    _pending_opacity = (active_obj.name, base_obj.name, commit)
    _last_opacity_request = time.monotonic()
    if _opacity_timer_armed:
        return  # already waiting; it re-checks the deadline when it fires
    _opacity_timer_armed = True
    try:
        bpy.app.timers.register(_opacity_timer,
                                first_interval=_OPACITY_REBUILD_DELAY)
    except Exception as e:
        # Registration itself failed -- fall back to the old synchronous
        # behaviour rather than silently never updating the mesh. (Note this is
        # NOT the `--background` case: there register() succeeds and the timer
        # simply never fires, because there is no event loop. Headless callers
        # are expected to call flush_opacity_rebuild() themselves.)
        _opacity_timer_armed = False
        print(f"Sculptools: Layers has no opacity timer ({e}); "
              "rebuilding synchronously.")
        flush_opacity_rebuild()


def _opacity_timer():
    """Timer tick. Returns a new interval to keep waiting, or None when done."""
    global _opacity_timer_armed
    remaining = _OPACITY_REBUILD_DELAY - (time.monotonic() - _last_opacity_request)
    if remaining > 0.001:
        return remaining  # the value moved again -- keep waiting
    _opacity_timer_armed = False
    flush_opacity_rebuild()
    return None


def flush_opacity_rebuild():
    """Apply a pending opacity change NOW, if there is one.

    Called by the timer, and directly from `_on_save_pre` so a .blend is never
    written with a mesh that lags the opacity values. Tests call it too:
    `bpy.app.timers` never fire under `--background` (no event loop).

    Never raises -- it runs from a timer, where an exception would just be
    printed and the pending state left dirty forever."""
    global _pending_opacity
    pending, _pending_opacity = _pending_opacity, None
    if pending is None:
        return False
    try:
        return _run_opacity_rebuild(*pending)
    except Exception as e:
        print(f"Sculptools: Layers could not rebuild after an opacity change: {e}")
        return False


def discard_pending_opacity_rebuild():
    """Drop a queued rebuild without running it. Used on file load, where the
    objects it names belong to the file being replaced."""
    global _pending_opacity
    _pending_opacity = None


def _run_opacity_rebuild(obj_name, base_name, commit=True):
    """The work UpdateLayerOpacity used to do inline, once per settled change."""
    global _last_opacity_autosave

    context = bpy.context
    active_obj = bpy.data.objects.get(obj_name)
    base_obj = bpy.data.objects.get(base_name)
    # Everything may have moved on since the drag: object deleted, another one
    # selected, the layers removed. Re-validate rather than trust the snapshot.
    if (active_obj is None or base_obj is None
            or context.active_object is not active_obj
            or not base_obj.sculptLayers):
        return False

    # Data shared with another MESH is split before anything is written -- see
    # ensure_own_data(). RNA has already stored the new value on the SHARED
    # base; writer=True keeps it on this object's side and gives the other
    # users back the values their meshes were built with.
    if ensure_own_data(context, active_obj, writer=True) is not None:
        base_obj = getBaseObject(parent_collection(active_obj))

    # Commit any in-progress sculpt into the active layer before the opacity
    # recompute rebuilds the visible mesh from the stored deltas. Un-saved sculpt
    # strokes live only on the mesh (not yet in a layer delta), so without this
    # they would be discarded the moment the opacity slider is touched.
    #
    # The commit runs with EVERY layer's opacity restored to its previous value,
    # not just the slider that moved. save_layer reads all of them and divides
    # the captured difference by them, so it must see exactly the state the mesh
    # is still in. Committing at a new value cancels against the recompute and
    # the slider appears dead; and restoring only ONE of them is worse than
    # useless when two changed inside the same delay -- the capture then divides
    # by the new value of the other and the stored delta comes out scaled
    # (measured: a layer delta doubling from 1.0 to 2.0 when two sliders moved
    # back to back). Restoring all of them is what makes coalescing safe, which
    # is why the pending record does not track which slider moved.
    #
    # The fallback when `_prev_opacity` was never recorded is 1.0 -- the PROPERTY
    # DEFAULT, which is what the mesh was built with. It must not be the current
    # value: by the time this runs, RNA has already stored the new one, so
    # defaulting to it restores nothing and the capture divides by the new value
    # twice (measured: delta 1.0 -> 4.0).
    #
    # Because the whole drag collapses into ONE call, the recorded previous
    # values still hold what was in force before the drag started. The time
    # debounce is kept only to avoid a redundant commit when separate changes
    # land back to back.
    now = time.monotonic()
    if commit and now - _last_opacity_autosave > _OPACITY_AUTOSAVE_DEBOUNCE:
        layers = list(base_obj.sculptLayers)
        prev_values = [sl_i.get("_prev_opacity", 1.0) for sl_i in layers]
        # Hand the previous values to the capture instead of WRITING them back
        # onto the properties. Rolling the RNA properties back was visible to
        # Blender's undo: sl.save_layer pushes a step as its first act, so that
        # step snapshotted the rolled-back state -- and a single Ctrl+Z after two
        # settled drags landed on it, showing the value from before the FIRST
        # drag. Measured 2026-07-31 from a screen recording plus an instrumented
        # session: after the undo, the previous-value marker came back UNSET, a
        # state that existed only inside the first commit's rollback window.
        # Nothing observes the properties mid-commit now, so no snapshot can.
        set_capture_opacities(prev_values)
        try:
            saved = AutoSaveCurrentLayer(context)
        finally:
            set_capture_opacities(None)
        if saved:
            _last_opacity_autosave = now

    # No ShapeKeysOff/On pair here any more: UpdateLayersValue() mutes the pose
    # keys itself, inside a try/finally. The pair that used to sit around this
    # call had no finally, so a failed rebuild left the keys muted for good.
    #
    # No undo_push on this path. Blender already pushes a step for the slider
    # edit itself, and that step is taken when the value changes -- BEFORE this
    # deferred rebuild runs. Our own pushes then added a second step holding the
    # new value together with the not-yet-rebuilt mesh, so the first Ctrl+Z after
    # a drag landed on that inconsistent snapshot (the shape jumped back while
    # the number stayed put) and a second press was needed. Reported with video
    # on 2026-07-29. See UpdateLayersValue() for who still needs them (nobody,
    # today).
    UpdateLayersValue(context, push_undo=False)
    # Remember the values this rebuild settled on, so the next change can commit
    # against the matching mesh state (see the previous-opacity note above). All
    # of them, for the same reason the commit restores all of them.
    for sl_i in base_obj.sculptLayers:
        sl_i["_prev_opacity"] = sl_i.layer_opacity
    return True


# ----------------------------------------------------------------------
# Live preview for the opacity popup
# ----------------------------------------------------------------------
# The popup's slider writes the OPERATOR's `value`, which nothing else watches,
# so a movement there has to be turned into a rebuild by hand. Same trailing
# edge as the property path (_OPACITY_REBUILD_DELAY): one rebuild when the value
# settles, not one per mouse event.
#
# What it must NOT do is route through _queue_opacity_rebuild. That path lets
# sl.save_layer push its undo step AFTER the value changed and BEFORE the mesh
# is rebuilt, which is the entire defect the popup exists to fix. Going straight
# to apply_layer_opacity keeps the ordered push, so each settled value is one
# atomic step.
#
# The wait is NOT _OPACITY_REBUILD_DELAY. That 0.35 s is calibrated for the
# PANEL slider, where it has to outlast a pause inside a drag or Blender splits
# the undo (see the comment on that constant). The popup's slider is an operator
# property Blender never pushes for, so that constraint is gone here and the
# wait is tuned purely for latency.
_LIVE_OPACITY_DELAY = 0.12            # floor, in seconds
# Ceiling. Without one the wait grew with the cost, unbounded: one apply on an
# 18k-point Multires L1 base costs 1.47 s on 5.2 and 2.63 s on 4.5 (almost all
# of it multires_reshape, i.e. Blender's), so a settled drag or a typed value sat
# 3.0 / 5.3 s with nothing on screen before the freeze even began (measured with
# real input events, 2026-10-05). Past a second the wait stops buying anything:
# the popup is ONE undo step however many applies it runs
# (begin_live_opacity_session), so an apply during a long pause costs a freeze
# the user was not moving through anyway.
_LIVE_OPACITY_MAX_DELAY = 1.0
# How many rebuilds' worth of stillness to wait for. 1.0 would mean spending
# half of a paused drag frozen; 2.0 keeps it near a third and still settles fast.
_LIVE_COST_FACTOR = 2.0
_pending_live_opacity = None          # (obj name, base name, index, value)
_last_live_opacity_request = 0.0
_live_opacity_timer_armed = False
# (key, seconds) of the previous apply, or None. KEYED by the mesh it ran on
# (_live_cost_key): a global cost let a 7 ms apply on a plain mesh set the
# 0.12 s floor for the first drag after SubD, where an apply cost 2.6 s -- the
# 2026-08-03 "first drag" case through another door (measured 2026-10-05).
_live_cost = None
# True once the open popup has pushed its boundary undo step. ONE boundary per
# popup: see begin_live_opacity_session().
_live_boundary_pushed = False


def _live_cost_key(active_obj, base_obj):
    """READ-ONLY. What an apply's cost depends on: the object, the dataset, and
    the point count of the base (it changes with SubD / UnSubD, and the cost
    with it). Names, not references, like every other pending record here."""
    return (active_obj.name, base_obj.name, len(base_obj.data.vertices))


def _live_delay(key=None):
    """Seconds of stillness before the popup's value applies, for the mesh
    named by *key* (_live_cost_key).

    Driven by what the previous apply actually COST, because the cost is the
    whole problem. Measured with dev_probe_opacity_breakdown.py: one settled
    change is 2 ms without Multires but 261 / 295 / 390 ms at Multires
    L1 / L2 / L3 -- and that time is inside multires_reshape plus the two mode
    switches (180 ms + 147 ms at L3), i.e. Blender's, not our vertex math.
    Waiting less than the cost means the next freeze starts before the previous
    one is forgotten.

    The STARTING value matters as much as the formula: with nothing measured yet
    the wait is the panel slider's proven 0.35 s, not the floor. Starting at the
    floor made the very first drag on a Multires object the worst case a user
    can meet -- a ~390 ms freeze every 120 ms -- which is what was reported on
    2026-08-03.

    The cost is the previous apply's ON THIS MESH: a cost measured on another
    one says nothing (see _live_cost), so it counts as nothing measured. And the
    wait never exceeds _LIVE_OPACITY_MAX_DELAY, however expensive the apply.

    One rule, no branching on object type: wait a couple of rebuilds' worth."""
    if _live_cost is None or key is None or _live_cost[0] != key:
        return _OPACITY_REBUILD_DELAY
    return min(_LIVE_OPACITY_MAX_DELAY,
               max(_LIVE_OPACITY_DELAY, _LIVE_COST_FACTOR * _live_cost[1]))


_pending_live_key = None              # _live_cost_key of the pending value


def queue_live_opacity(base_obj, index, value):
    """Record a popup slider movement and (re)arm the trailing-edge timer."""
    global _pending_live_opacity, _last_live_opacity_request
    global _live_opacity_timer_armed, _pending_live_key
    active_obj = bpy.context.active_object
    if active_obj is None or base_obj is None:
        return
    _pending_live_opacity = (active_obj.name, base_obj.name,
                             int(index), float(value))
    _pending_live_key = _live_cost_key(active_obj, base_obj)
    _last_live_opacity_request = time.monotonic()
    if _live_opacity_timer_armed:
        return  # already waiting; it re-checks the deadline when it fires
    _live_opacity_timer_armed = True
    try:
        bpy.app.timers.register(_live_opacity_timer,
                                first_interval=_live_delay(_pending_live_key))
    except Exception as e:
        # Same fallback as _queue_opacity_rebuild: better a synchronous rebuild
        # than a slider that silently never applies.
        _live_opacity_timer_armed = False
        print(f"Sculptools: Layers has no opacity timer ({e}); "
              "applying synchronously.")
        flush_live_opacity()


def _live_opacity_timer():
    """Timer tick. Returns a new interval to keep waiting, or None when done."""
    global _live_opacity_timer_armed
    remaining = (_live_delay(_pending_live_key)
                 - (time.monotonic() - _last_live_opacity_request))
    if remaining > 0.001:
        return remaining  # the slider moved again -- keep waiting
    _live_opacity_timer_armed = False
    flush_live_opacity()
    return None


def flush_live_opacity():
    """Apply a queued popup slider value NOW, if there is one.

    Never raises -- it runs from a timer, where an exception would just be
    printed and the pending state left dirty forever. Tests must call it:
    `bpy.app.timers` never fire under `--background`."""
    global _pending_live_opacity, _live_cost
    pending, _pending_live_opacity = _pending_live_opacity, None
    if pending is None:
        return False
    obj_name, base_name, index, value = pending
    active_obj = bpy.data.objects.get(obj_name)
    base_obj = bpy.data.objects.get(base_name)
    # Everything may have moved on while the popup was open.
    if (active_obj is None or base_obj is None
            or bpy.context.active_object is not active_obj):
        return False
    # ... including WHICH data the object uses. The operators that switch it
    # (sl.link_data, sl.duplicate_layers) flush this value first -- see
    # flush_pending_opacity() -- so this only catches a route nobody foresaw.
    # Applying anyway edited a base the object no longer uses AND committed the
    # object's capture against that base's opacities (measured 2026-10-01: the
    # new dataset's delta halved). Dropped, not redirected: an index means
    # nothing on another base.
    coll = parent_collection(active_obj)
    if coll is None or getBaseObject(coll) != base_obj:
        return False
    # Keyed BEFORE the apply: it may split shared data onto a new base, and the
    # cost belongs to the mesh the user is dragging on.
    key = _live_cost_key(active_obj, base_obj)
    # perf_counter, not monotonic: on Windows/Python 3.11 (Blender 4.5)
    # monotonic has ~15.6 ms resolution, so a 6 ms rebuild measures as zero.
    started = time.perf_counter()
    global _live_boundary_pushed
    try:
        applied = apply_layer_opacity(bpy.context, base_obj, index, value,
                                      push_boundary=not _live_boundary_pushed)
        if applied:
            _live_boundary_pushed = True
        return applied
    except Exception as e:
        print(f"Sculptools: Layers could not apply the opacity: {e}")
        return False
    finally:
        # Feeds _live_delay(): the next wait on this mesh is never shorter than
        # this took (up to the ceiling).
        _live_cost = (key, time.perf_counter() - started)


def discard_pending_live_opacity():
    """Drop a queued popup value without applying it. Used on file load, where
    the objects it names belong to the file being replaced -- which also ends
    any popup session: the next apply pushes its own boundary."""
    global _pending_live_opacity, _live_boundary_pushed
    _pending_live_opacity = None
    _live_boundary_pushed = False


def begin_live_opacity_session():
    """SL_SetOpacity.invoke: a popup opened. Its first apply pushes the boundary
    undo step; later applies in the same popup do not.

    It used to be one boundary per APPLY, so a pause longer than _live_delay()
    inside one drag applied an intermediate value and left two steps after the
    stroke -- one Ctrl+Z back to 1.00, then an empty one (seen in the GUI,
    2026-10-01). One popup is one change, the way one drag of a native slider
    is one step. Skipping the push is safe because nothing else can push a step
    while the popup is open: measured the same day, Ctrl+Z is swallowed and the
    popup closes only on a click outside or Esc -- both calling
    end_live_opacity_session(), which applies what is waiting before the click
    can reach the mesh as a stroke, and then records the result."""
    global _live_boundary_pushed
    _live_boundary_pushed = False


def end_live_opacity_session():
    """SL_SetOpacity.cancel: the popup closed. Apply a waiting value NOW, still
    inside the session, record the result as an undo step, end the session.

    Now, not at the timer's next tick: the popup closes on a click, and a click
    on the mesh is the start of a stroke. Applied later, the commit would
    capture that stroke without a boundary between the two, and one Ctrl+Z
    would take back both. The armed timer then finds nothing to do.

    The closing push is what makes the popup undoable at all. Its applies run
    from a timer, outside any operator, so nothing recorded the state they
    left: measured in the 5.2 GUI on 2026-10-01 with the user driving, one
    popup change followed by Ctrl+Z moved the history back a step and restored
    NOTHING -- the value stayed 0.43 and the committed stroke stayed in the
    layer -- and the next press removed the stroke while the slider kept
    reading 0.43 over a mesh built at 1.00. The boundary before the first apply
    alone is not enough: an undo reverts what the steps recorded, and the apply
    came after the last one. (The 2026-08-01 measurement that passed was of the
    earlier dialog, whose operator closed with a step of its own.) With this
    push: Ctrl+Z -> 1.00 and the stroke, uncommitted; one empty press; the
    stroke goes -- and redo brings back the value and the committed stroke."""
    global _live_boundary_pushed
    applied = _live_boundary_pushed
    try:
        applied = flush_live_opacity() or applied
    finally:
        _live_boundary_pushed = False
    if applied:
        bpy.ops.ed.undo_push(message="Layer Opacity")
        # The newest step now holds the result: a popup opened right after
        # this one needs no boundary of its own (see boundary_undo_push()).
        _arm_step_current()


# ----------------------------------------------------------------------
# Redundant boundary steps
# ----------------------------------------------------------------------
# Every action pushes a boundary undo step before it changes anything, because
# undoing INTO a sculpt stroke never reloads Object data: without a global step
# holding the state before the action, the first Ctrl+Z after "stroke +
# action" restores nothing (CLAUDE.md, the Sculpt-step bullet). But when the
# newest undo step is ALREADY a global step holding the current state -- the
# end step of the previous add-on action, or the popup's "Layer Opacity", with
# nothing done since -- the boundary is a copy of it and costs a Ctrl+Z that
# changes nothing. Measured with real input events on 4.5 and 5.2
# (2026-10-06): two popups in a row took two presses to get from the second
# value back to the first, and three eye clicks alternated a real press and an
# empty one.
#
# "Nothing done since" is read off the depsgraph. A sculpt stroke (Draw, Mask),
# an undo and a redo all update the sculpted object; opening and closing the
# popup, moving the mouse, orbiting the view and switching brushes update
# nothing at all (measured, identical on both versions). So the state is ARMED
# when an add-on action ends with a global step, and any update of the object,
# its mesh or its layer base DISARMS it. The handler only clears a flag: it
# never writes data, so it is not the undo_post kind of mistake.
#
# Arming waits for the depsgraph to SETTLE: the first full pass of the event
# loop with no update at all. An action's own changes keep arriving after it
# returns -- measured on the eye, 2026-10-06: one more update of the object a
# whole pass after the operator, which disarmed a state armed on the first
# pass. The timer (interval 0, i.e. once per pass) therefore re-checks until a
# pass goes by quietly, and gives up after _STEP_ARM_MAX_PASSES -- a few
# milliseconds -- leaving the state disarmed, i.e. the old behaviour. What
# that leaves unseen is a stroke started, finished and quiet again inside
# those few passes after a click on the panel: faster than a hand can move
# from the sidebar to the mesh.
_step_current = None        # (object name, base name) while armed
_step_arm_queued = False    # the arming timer is registered
_step_arm_busy = False      # an update arrived since the timer last looked
_step_arm_passes = 0
_STEP_ARM_MAX_PASSES = 8


def _step_key(obj):
    """READ-ONLY. What an armed state is about: the object and the layer base
    it uses ("" when it has none)."""
    if obj is None:
        return None
    coll = parent_collection(obj)
    base = getBaseObject(coll) if coll is not None else None
    return (obj.name, base.name if base is not None else "")


def _is_wm_operator_frame(frame):
    """READ-ONLY. True when *frame* is the execute() or invoke() of one of this
    add-on's operators. Split out of _called_by_window_manager() so the rule
    can be tested with a stand-in frame."""
    if frame is None or frame.f_code.co_name not in ("execute", "invoke"):
        return False
    owner = frame.f_locals.get("self")
    return (isinstance(owner, bpy.types.Operator)
            and type(owner).__module__.startswith(__package__ + "."))


def _called_by_window_manager():
    """READ-ONLY. True when the OUTERMOST Python frame is one of this add-on's
    operators, i.e. Blender's window manager called it -- and will push the
    operator's end undo step when it finishes. A bpy.ops call from Python gets
    no end step (measured 2026-10-06), and an operator nested in another one
    finishes inside it; neither leaves the newest step holding its result."""
    frame = sys._getframe(1)
    while frame.f_back is not None:
        frame = frame.f_back
    return _is_wm_operator_frame(frame)


def _arm_step_current():
    """Arm once the depsgraph has settled (see the section comment)."""
    global _step_arm_queued, _step_arm_busy, _step_arm_passes
    # Busy from the start: the timer must see at least one whole pass after
    # this request before it may arm.
    _step_arm_busy = True
    _step_arm_passes = 0
    if _step_arm_queued:
        return
    _step_arm_queued = True
    try:
        bpy.app.timers.register(_step_arm_timer, first_interval=0.0)
    except Exception:
        # Without the timer the state simply never arms: every action keeps
        # pushing its boundary, which is the behaviour this only optimises.
        _step_arm_queued = False


def _step_arm_timer():
    """Once per pass of the event loop until a pass goes by with no depsgraph
    update; then arm on the active object."""
    global _step_current, _step_arm_queued, _step_arm_busy, _step_arm_passes
    if _step_arm_busy:
        _step_arm_busy = False
        _step_arm_passes += 1
        if _step_arm_passes <= _STEP_ARM_MAX_PASSES:
            return 0.0              # still settling: look again next pass
        _step_arm_queued = False    # never settled: stay disarmed
        return None
    _step_arm_queued = False
    try:
        _step_current = _step_key(bpy.context.view_layer.objects.active)
    except Exception:
        _step_current = None        # cannot tell: keep pushing boundaries
    return None


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph):
    """Disarm on any update of the armed object, its mesh or its layer base,
    and hold back a pending arming on any update at all. Reads the update list
    and sets flags -- nothing else, ever."""
    global _step_current, _step_arm_busy
    if _step_arm_queued:
        _step_arm_busy = True
    if _step_current is None:
        return
    obj_name, base_name = _step_current
    watched = set()
    for name in (obj_name, base_name):
        obj = bpy.data.objects.get(name) if name else None
        if obj is not None:
            watched.add(obj.as_pointer())
            if obj.data is not None:
                watched.add(obj.data.as_pointer())
    if not watched:
        _step_current = None
        return
    for update in depsgraph.updates:
        try:
            pointer = update.id.original.as_pointer()
        except (AttributeError, ReferenceError):
            continue
        if pointer in watched:
            _step_current = None
            return


def discard_step_current():
    """Forget an armed state. On file load: the steps belong to another file."""
    global _step_current
    _step_current = None


def boundary_undo_push(context):
    """The boundary undo step an action pushes before it changes anything --
    skipped when the newest step already holds the current state (see the
    section comment). Callers keep their own `mode != 'OBJECT'` guard: in
    Object Mode no boundary is pushed at all.

    Also arms the state for the NEXT action when the window manager will close
    this one with its end step. Returns True when it pushed."""
    global _step_current
    redundant = (_step_current is not None
                 and _step_current == _step_key(context.active_object))
    _step_current = None
    if not redundant:
        bpy.ops.ed.undo_push()
    if _called_by_window_manager():
        _arm_step_current()
    return not redundant


def flush_pending_opacity():
    """Apply BOTH queued opacity paths now: the property path's deferred rebuild
    and the popup's live value. Never raises (both flushes catch).

    For the operators about to change WHICH layer an index names (sl.move_layer,
    sl.remove_layer, sl.merge_layer) or WHICH base the object uses
    (sl.link_data, sl.duplicate_layers, sl.collapse_layers). The user set the
    value BEFORE clicking, so it lands first, on the layer it was set on.

    The popup's record names its layer by INDEX and waits 0.35 s -- up to ~0.8 s
    on a Multires L3 object -- before applying. Measured 2026-10-01
    (dev_probe_audit.py T1, 4.5 + 5.2): a move inside that window sent 0.3 meant
    for SLayer_0 to SLayer_1, a remove sent it to SLayer_2. Flushing rather than
    re-keying the record by layer name also keeps the value when its layer is
    merged away, and lets a merge bake the value the user actually set.

    Call it at the top of execute(), OUTSIDE mutation_guard: it switches modes
    and pushes an undo step of its own. NEVER from inside an opacity commit --
    an apply nested in _run_opacity_rebuild would overwrite its capture
    override."""
    flush_opacity_rebuild()
    flush_live_opacity()


def cancel_pending_timers():
    """Drop every queued opacity change AND deregister the two timers.

    Called from unregister(). Discarding the pending state is not enough:
    `bpy.app.timers` keeps a reference to the callback itself, so a registration
    OUTLIVES the add-on. Measured 2026-08-12 -- `bpy.app.timers.is_registered()`
    still returns True after `addon_disable`, and the timer then goes on to run
    `apply_layer_opacity` on behalf of an add-on the user has switched off: an
    undo push, two mode switches and a full mesh rebuild, out of nowhere.

    It is worse in the dev reload cycle (disable -> purge sys.modules -> enable):
    the armed timer stays bound to the OLD module's globals, so two copies of
    `_pending_*` exist at once and the one nobody can reach is the one that
    fires. `bpy.app.timers.unregister()` raises when the timer is not registered,
    hence the is_registered() guard."""
    global _opacity_timer_armed, _live_opacity_timer_armed, _step_arm_queued
    discard_pending_opacity_rebuild()
    discard_pending_live_opacity()
    discard_step_current()
    # A leaked capture override would make every later capture divide by stale
    # values; unregister is the last chance to clear it.
    set_capture_opacities(None)
    for fn in (_opacity_timer, _live_opacity_timer, _step_arm_timer):
        try:
            if bpy.app.timers.is_registered(fn):
                bpy.app.timers.unregister(fn)
        except Exception as e:
            print(f"Sculptools: Layers could not deregister {fn.__name__}: {e}")
    _opacity_timer_armed = False
    _live_opacity_timer_armed = False
    _step_arm_queued = False


def resolve_layer(context, index):
    """READ-ONLY. The active object's base object when *index* names a real
    layer on it, else None.

    Used by sl.set_opacity from both invoke() and execute(). Read-only, so it
    is also safe to call from poll() or draw()."""
    active_obj = context.active_object
    if active_obj is None:
        return None
    parent_col = parent_collection(active_obj)
    if parent_col is None:
        return None
    base_obj = getBaseObject(parent_col)
    if base_obj is None or index < 0 or index >= len(base_obj.sculptLayers):
        return None
    return base_obj


def apply_layer_opacity(context, base_obj, index, value, push_boundary=True):
    """Set one layer's opacity and rebuild the visible mesh as ONE undo step.

    THE ORDER IS LOAD-BEARING and was measured in the GUI on 2026-08-01 (three
    hypotheses, two falsified; see
    docs/superpowers/specs/2026-08-01-opacity-modal-operator-design.md):

      * The boundary undo step must be pushed BEFORE the value changes, while the
        value and the mesh still agree. Pushing it after the rebuild instead only
        swaps which of the two comes back first -- still two presses, still an
        inconsistent state in between.
      * Switching to Object Mode before the commit is what stops sl.save_layer
        from pushing a step of its own (its push is guarded on `mode != OBJECT`).
        That push, taken after the value has changed and before the mesh is
        rebuilt, is the whole defect. Suppressing it WITHOUT the boundary push
        above is worse than the bug: the undo then swallows the preceding sculpt
        stroke.

    push_boundary=False is for the popup's later applies only: one popup is ONE
    change, so only its first apply pushes (begin_live_opacity_session()).

    Returns True when the mesh was rebuilt, False when it refused."""
    global _last_opacity_autosave, _in_opacity_update

    active_obj = context.active_object
    if active_obj is None or base_obj is None:
        return False
    if index < 0 or index >= len(base_obj.sculptLayers):
        return False
    coll = parent_collection(active_obj)
    own_base = coll is not None and getBaseObject(coll) == base_obj

    # A rebuild queued by a script assignment would otherwise fire later against
    # a mesh this call is about to rebuild from different values.
    flush_opacity_rebuild()

    # Data shared with another MESH (a Shift+D copy not yet edited) is split
    # before anything is written -- see ensure_own_data(). Either the flush
    # above or this call may have moved the object to its own copy, which has
    # the same layers in the same order, so the index still holds.
    ensure_own_data(context, active_obj)
    if own_base:
        base_obj = getBaseObject(parent_collection(active_obj))

    value = max(0.0, min(1.0, float(value)))
    context_mode = active_obj.mode

    if context_mode != 'OBJECT':
        if push_boundary:
            boundary_undo_push(context)
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

    shapekeys_mutes = None
    sl = base_obj.sculptLayers[index]
    value_before = sl.layer_opacity
    try:
        prev_values = [sl_i.get("_prev_opacity", 1.0)
                       for sl_i in base_obj.sculptLayers]

        # Assigning an RNA property from Python fires its update callback even
        # for an unchanged value. Suppress it -- the rebuild happens here -- and
        # refresh `_prev_opacity` by hand below, since the suppressed callback is
        # what normally does it.
        _in_opacity_update = True
        try:
            sl.layer_opacity = value
        finally:
            _in_opacity_update = False

        # Commit any in-progress sculpt BEFORE the recompute discards it, and do
        # it against the PREVIOUS opacities -- that is the state the mesh is
        # still in. Only from Sculpt Mode, which is what AutoSaveCurrentLayer
        # enforced on the deferred path; we have already left it, so the
        # mode-agnostic AutoSaveOnModeExit is the right helper here.
        if context_mode == 'SCULPT':
            set_capture_opacities(prev_values)
            try:
                if AutoSaveOnModeExit(context):
                    _last_opacity_autosave = time.monotonic()
            finally:
                set_capture_opacities(None)

        shapekeys_mutes = ShapeKeysOff(active_obj)
        # push_undo=False: the boundary step above and Blender's own push at the
        # end of the calling operator are the only two steps this should produce.
        UpdateLayersValue(context, push_undo=False)
        for sl_i in base_obj.sculptLayers:
            sl_i["_prev_opacity"] = sl_i.layer_opacity
    except Exception:
        # A failure between the write and the rebuild would otherwise leave the
        # property holding the NEW value while the mesh still shows the OLD
        # shape -- exactly the inconsistency this function exists to prevent.
        # Put the value back, then re-raise: like mutation_guard, this is a
        # no-op on success and never swallows the failure.
        _in_opacity_update = True
        try:
            sl.layer_opacity = value_before
        finally:
            _in_opacity_update = False
        raise
    finally:
        if shapekeys_mutes is not None:
            ShapeKeysOn(active_obj, shapekeys_mutes)
        if active_obj.mode != context_mode:
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)

    return True


def _layer_location(context, sl):
    """READ-ONLY. Return (active_obj, base_obj, index) if the active object's
    layer data actually contains *sl*, else (None, None, -1).

    A layer lives on a hidden BASE object, not on the object being sculpted, so
    nothing structurally ties `self` in an update callback to
    `context.active_object`. From the panel they always match -- the slider is
    drawn from the active object's own data -- but the property is equally
    assignable from Python, from a driver, or while the context points at
    another object entirely, and then the callback would rebuild the WRONG
    object's mesh (or dereference None in ShapeKeysOff)."""
    active_obj = context.active_object
    if active_obj is None:
        return None, None, -1
    parent_col = parent_collection(active_obj)
    if parent_col is None:
        return None, None, -1
    base_obj = getBaseObject(parent_col)
    if base_obj is None:
        return None, None, -1
    for i, candidate in enumerate(base_obj.sculptLayers):
        if candidate == sl:
            return active_obj, base_obj, i
    return None, None, -1


def UpdateLayerOpacity(self, context):
    global _last_opacity_autosave, _in_opacity_update

    # Re-entrancy guard: the commit below temporarily rewrites self.layer_opacity,
    # which re-fires this callback. Ignore those internal writes.
    if _in_opacity_update:
        return

    active_obj, base_obj, index = _layer_location(context, self)
    if active_obj is None:
        # Not this object's layer (or no object at all): store the value and do
        # nothing else. The next rebuild driven by the real owner picks it up.
        return

    # Do NOT rebuild here. Dragging the slider fires this callback per mouse
    # event, and the rebuild is expensive: measured in the GUI on a Multires L2
    # object (7938-point base), one pass costs ~211 ms -- undo_push 5 + mode_set
    # OBJECT 60 + UpdateMesh/multires_reshape 107 + mode_set SCULPT 34 +
    # undo_push 5. A 12-event drag therefore ran 12 full rebuilds (4.4 s at
    # ~5 fps) and left an undo entry roughly every 0.1 of slider travel.
    #
    # Instead, note that something moved and let a trailing-edge timer do ONE
    # rebuild once the value stops changing. The commit-then-rebuild logic all
    # lives in _run_opacity_rebuild().
    _queue_opacity_rebuild(active_obj, base_obj)


def UpdateLayersValue(context, push_undo=True):
    # The bpy.ops.ed.undo_push() calls bracketing this function are LOAD-BEARING
    # for a caller that has NO boundary step of its own -- do not "clean them
    # up". Since 2026-10-01 no caller in the add-on is one: sl.link_data, the
    # last, pushes its own boundary before its commit and passes
    # push_undo=False, which took it from three boundary steps to one (GUI-
    # measured; dev_test_undo_boundary.py). A new caller without a boundary of
    # its own must keep the default.
    #
    # They were removed on 2026-07-29 on the strength of a background-mode
    # measurement that seemed to show they split one action
    # into two undo steps -- that measurement was an artefact: Blender reports
    # "Undo disabled at startup in background-mode (call ed.undo_push() to
    # explicitly initialize the undo-system)", so in `--background` the undo
    # stack only existed BECAUSE of these very calls. Re-measured in the GUI
    # with them removed, the add-on's actions produced no undo step at all and
    # Ctrl+Z jumped straight past them. See the note in CLAUDE.md.
    #
    # `push_undo=False` is for a caller that already has its boundary: the
    # opacity-slider path, where Blender pushes a step for the property edit
    # (see _run_opacity_rebuild()), apply_layer_opacity and sl.link_data, which
    # push their own.
    #
    # The rebuild runs under the SAME discipline as every operator's (whose
    # GetMultires() does this): pose shape keys muted, and the object's other
    # modifiers switched off around multires_reshape. Without it this function
    # reshaped the Multires against a different base than the one every capture
    # reads back. Measured 2026-10-01 (dev_probe_audit.py T4 / T5, 4.5 + 5.2):
    # with a Displace BEFORE the Multires one opacity round trip moved the stored
    # delta by 0.145 on a 0.084 delta, and an sl.link_data re-link with a pose
    # key at 1.0 baked the pose into the layer (drift 0.300 = the pose offset).
    # Both are 0.000 through any other rebuild. ShapeKeysOff() nests safely: a
    # caller that already muted the keys gets [] back here and keeps ownership.
    active_obj = context.active_object
    context_mode = active_obj.mode

    if active_obj.mode != 'OBJECT':
        if push_undo:
            bpy.ops.ed.undo_push()
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

    shapekeys_mutes = ShapeKeysOff(active_obj)
    multires_mod = None
    other_mods = {}
    # try/finally: a raise inside multires_reshape used to leave the Multires at
    # its top level, the caller's shape keys muted and the object in Object Mode.
    try:
        for mod in active_obj.modifiers:
            if mod.type == 'MULTIRES' and mod.total_levels != 0 and mod.show_viewport:
                multires_mod = (mod, mod.levels)
                mod.levels = mod.total_levels
                break
        if multires_mod:
            other_mods = OtherModsOff(active_obj)

        UpdateMesh(context, active_obj, multires_mod, False)
    finally:
        # Same order as the operators: multires level -> other modifiers ->
        # shape keys -> mode.
        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
        for mod in other_mods.values():
            mod.show_viewport = True
        ShapeKeysOn(active_obj, shapekeys_mutes)
        if active_obj.mode != context_mode:
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)

    active_obj.data.update()

    if context_mode != 'OBJECT' and push_undo:
        bpy.ops.ed.undo_push()


def _exclude_sculpt_layer_collection(context, sc_coll):
    """Mark the layer-data root collection as excluded in the current view
    layer. No-op if it can't be resolved here (defensive against multi-scene)."""
    lc = context.view_layer.layer_collection.children.get(sc_coll.name)
    if lc is not None:
        lc.exclude = True


def GetSculptCollection(context, do_create_collection):
    # get/add the layer-data root collection
    sc_coll = bpy.data.collections.get(SCULPT_COLL_NAME)
    if sc_coll is None:
        # Backward-compat: files made before 1.3.5 used SCULPT_COLL_NAME_LEGACY.
        # Adopt it, renaming to the new name only on the mutating path so the
        # read-only enum/UI callers stay side-effect free.
        legacy = bpy.data.collections.get(SCULPT_COLL_NAME_LEGACY)
        if legacy is not None:
            if do_create_collection:
                legacy.name = SCULPT_COLL_NAME
            sc_coll = legacy

    if sc_coll is not None:
        # Multi-scene safety: the collection may exist in bpy.data but be linked
        # only to another scene. Re-link it to the current scene so the
        # view_layer.layer_collection lookups below (and in CreateParentCollection)
        # don't raise KeyError. Only do this on the mutating path.
        if do_create_collection and sc_coll.name not in context.scene.collection.children:
            context.scene.collection.children.link(sc_coll)
            _exclude_sculpt_layer_collection(context, sc_coll)
        return sc_coll

    if do_create_collection:
        sc_coll = bpy.data.collections.new(SCULPT_COLL_NAME)
        context.scene.collection.children.link(sc_coll)
        _exclude_sculpt_layer_collection(context, sc_coll)
        return sc_coll

    return None


def CreateParentCollection(context, active_obj, sc_coll):
    parent_col = bpy.data.collections.new(active_obj.name + '_DATA_' + str(context.scene.sculptLayers_scene_id))
    sc_coll.children.link(parent_col)
    # Defensive: resolve the layer-collection chain via .get() so a stale/
    # multi-scene state can't raise KeyError while still excluding when possible.
    sc_lc = context.view_layer.layer_collection.children.get(sc_coll.name)
    if sc_lc is not None:
        child_lc = sc_lc.children.get(parent_col.name)
        if child_lc is not None:
            child_lc.exclude = True
    return parent_col


# Per point, in object units: below this a hidden / zero-opacity layer's
# capture is rebuild residue, not a stroke. That capture is ADDITIVE (see
# sl.save_layer), so residue kept would be added again on every commit. Right
# after a rebuild the mesh matches the stack to 1.7e-7 at most -- plain, and
# Multires L1 / L2 on a sphere and a grid, identical on 4.5 and 5.2
# (dev_probe_hidden_noise.py, 2026-10-05) -- so 1e-6 leaves a 6x margin and is
# still far below anything a brush does.
HIDDEN_STROKE_TOL = 1e-6


def ApplyLayerOpacity(np_layer_verts, opacity_val, verts_count, do_divide):
    """Scale (or, with do_divide, unscale) a flat float32 delta IN PLACE by one
    layer's opacity, and return it.

    One float32 scalar, not a per-vertex array: this used to build an
    np.full(verts_count) opacity array and broadcast it, which is the same
    float32 multiply bit for bit -- pinned against a verbatim copy of the old
    body by dev_test_update_mesh_numpy.py -- plus an allocation the size of the
    mesh on every call. `verts_count` is no longer needed and is kept only so
    the six call sites stay as they are."""
    if do_divide:
        np.divide(np_layer_verts, np.float32(opacity_val), out=np_layer_verts)
    else:
        np.multiply(np_layer_verts, np.float32(opacity_val), out=np_layer_verts)
    return np_layer_verts


# NOTE: the layer-data picker used to be a `Scene.sculptLayers_coll_list`
# EnumProperty fed by a GetCollections() items callback, assigned straight from
# the panel and acted on by a SetNewLayersData() update callback. All of it is
# gone, replaced by the `sl.link_data` operator + SL_MT_DataLink menu, because
# the pair had four measured defects (verified live 2026-07-29):
#   * no auto-commit -- switching data wiped the in-progress sculpt off the mesh
#     AND left it uncaptured in the layer being abandoned (pure data loss);
#   * no compatibility check -- a 266-vert sphere's data linked onto an 8-vert
#     cube in complete silence, since foreach_set only *reports* a size mismatch;
#   * being a SCENE property it could never follow the active object: it read
#     'None' even for an object that was linked, contradicting the label above it;
#   * assigning it with no active object raised AttributeError.
# The enum also needed a module-level list anchor to work around Blender's
# documented items-callback bug ("Python must keep a reference ... or Blender
# will misbehave or even crash"); a Menu draws plain operator buttons, so that
# hazard is gone with it. Do NOT reintroduce an update-callback-driven picker.


def GetMultires(context, active_obj, is_return_zero_lvl=False):
    multires_mod = None
    multires_found = None
    other_mods = {}

    for mod in active_obj.modifiers:
        if mod.type == 'MULTIRES' and mod.show_viewport:
            multires_found = mod
        else:
            if mod.show_viewport:
                other_mods[mod.name] = mod

    if multires_found:
        # switch off other modifiers
        if other_mods:
            for mod in other_mods.values():
                mod.show_viewport = False

        levels = active_obj.modifiers[multires_found.name].levels
        active_obj.modifiers[multires_found.name].levels = active_obj.modifiers[multires_found.name].total_levels

        depsgraph = context.evaluated_depsgraph_get()
        multires_mesh = depsgraph.objects[active_obj.name].data

        if multires_found.total_levels == 0 and is_return_zero_lvl:
            multires_mod = (multires_found, levels, multires_mesh, depsgraph, other_mods)
        elif multires_found.total_levels != 0:
            multires_mod = (multires_found, levels, multires_mesh, depsgraph, other_mods)
        else:
            # total_levels == 0 and the caller did not ask for the zero-level
            # tuple: we return None, so nobody will ever call OtherModsOn() or
            # restore `levels` on our behalf -- the caller sees no Multires and
            # takes the plain-mesh branch, and mutation_guard is handed a None
            # too. Undo here what we changed a few lines above, or the user's
            # other modifiers stay switched off in silence.
            multires_found.levels = levels
            for mod in other_mods.values():
                mod.show_viewport = True
    else:
        multires_mod = None

    return multires_mod


def OtherModsOn(multires_mod):
    if multires_mod and len(multires_mod) >= 5 and multires_mod[4]:
        for mod in multires_mod[4].values():
            mod.show_viewport = True


def OtherModsOff(active_obj):
    other_mods = {}
    for mod in active_obj.modifiers:
        if mod.type == 'MULTIRES' and mod.show_viewport:
            pass
        else:
            if mod.show_viewport:
                other_mods[mod.name] = mod
                mod.show_viewport = False
    return other_mods


def _remove_root_if_empty(context):
    """Remove the layer-data root collection once it holds no more DATA
    sub-collections, so it doesn't linger empty (and excluded) in the outliner."""
    sc_coll = GetSculptCollection(context, False)  # read-only lookup, never creates
    if sc_coll is not None and not sc_coll.children and not sc_coll.objects:
        bpy.data.collections.remove(sc_coll)


def get_unused_data_collections(context):
    """READ-ONLY. Return the list of layer-data collections under LAYERS_DATA
    that no object references -- orphans left by deleted objects, Duplicate Data,
    or reassignment (plus any empty ones).

    Safe to call from poll()/draw(): no scene mutation. The in-use set is built
    from all of `bpy.data.objects`, so data referenced by hidden objects or
    objects in other scenes is never reported as unused.

    It goes through parent_collection(), i.e. the POINTER first: a collection the
    user renamed in the Outliner is still in use, and reporting it as an orphan is
    how `sl.cleanup_data` used to offer to delete live layer data (measured
    2026-08-12). The legacy name is still honoured through the same helper, for
    objects linked before the pointer existed."""
    sc_coll = GetSculptCollection(context, False)  # read-only lookup, never creates
    if sc_coll is None:
        return []
    in_use = set()
    for o in bpy.data.objects:
        linked = parent_collection(o)
        if linked is not None:
            in_use.add(linked.name)
    return [coll for coll in sc_coll.children if coll.name not in in_use]


def RemoveSculptLayers(context, active_obj, base_obj=None):
    """Detach *active_obj* from its layer data, and delete the data (*base_obj*
    and its collection) only when no other object uses it. EXECUTE-ONLY.
    Returns how many OTHER objects still use the data -- 0 when it was deleted,
    or when there was nothing to delete.

    The callers are the ways an object LEAVES its layers: Collapse All and
    removing the last layer. They used to delete the data outright, and with the
    data shared that deleted every other object's layers too -- measured
    2026-10-01 on 4.5 and 5.2 (dev_probe_audit.py T2): Collapse All on a Shift+D
    copy left the ORIGINAL with no layers, and its legacy name pointing at a
    collection that no longer existed. Now the object only unlinks itself while
    anyone else uses the data; the last one out deletes it.

    "The object" is its whole MESH GROUP (its Alt+D copies, see mesh_group()):
    they are one mesh, so they leave together -- one left linked would keep a
    dataset driving a mesh the other one treats as plain."""
    parent_col = parent_collection(active_obj)
    group = mesh_group(active_obj)
    others = other_data_users(active_obj)
    if base_obj and not others:
        # Remove all data of layers
        base_object_mesh = base_obj.data
        bpy.data.objects.remove(base_obj, do_unlink=True)
        bpy.data.meshes.remove(base_object_mesh)
        if parent_col is not None:
            bpy.data.collections.remove(parent_col)

    # Clear the Collection link (pointer + legacy name -- see
    # set_parent_collection). Removing the collection above already nulls the
    # pointer; this covers the paths where nothing was removed.
    for o in group:
        set_parent_collection(o, None)

    # Drop the root collection too if this object's data was the last one under it.
    _remove_root_if_empty(context)
    return len(others)


# Hard cap on layers per object: only this many distinct SLayer_<n> attribute
# names exist (0..MAX_SCULPT_LAYERS-1), so this is the true maximum.
MAX_SCULPT_LAYERS = 100


def AddSculptLayer(base_obj):
    """Create a new layer attribute + list item on the base object.

    Returns the new layer, or None when the hard cap (MAX_SCULPT_LAYERS) is
    reached and no free SLayer_<n> name is left. Callers MUST handle None
    instead of creating a broken (nameless) layer."""
    sl_attrib_name = None

    # Add New Attrib to Base Layer
    for i in range(MAX_SCULPT_LAYERS):
        sl_attrib_name_tmp = SL_ATTRIB_NAME + str(i)
        if sl_attrib_name_tmp not in base_obj.data.attributes.keys():
            base_obj.data.attributes.new(name=sl_attrib_name_tmp, type='FLOAT_VECTOR', domain='POINT')
            sl_attrib_name = sl_attrib_name_tmp
            break

    if sl_attrib_name is None:
        # Every attribute slot is taken -> at the layer cap.
        return None

    # Add layer Item
    sl = base_obj.sculptLayers.add()
    sl.layer_attrib_name = sl_attrib_name
    sl.name = SL_NAME + str(i)

    return sl


def duplicate_layer_name(base_obj, name):
    """READ-ONLY. The name a copy of layer *name* gets, the way Blender names a
    duplicate: *name* without any `.NNN` it already ends in, plus the lowest
    `.001`, `.002`, ... no layer of *base_obj* uses. So a copy of `Layer_0` and
    a copy of `Layer_0.001` are both `Layer_0.002` once `Layer_0.001` exists.
    It was `Layer_0COPY` until 2026-10-05 (the user's choice, after the usage
    pass flagged it)."""
    stem, dot, number = name.rpartition('.')
    if not (dot and number.isdigit()):
        stem = name
    taken = {sl.name for sl in base_obj.sculptLayers}
    n = 1
    while "%s.%03d" % (stem, n) in taken:
        n += 1
    return "%s.%03d" % (stem, n)


def _auto_save_active_layer(context, log_label, boundary_pushed=False):
    """Shared implementation for the auto-save helpers.

    Saves the active layer if it exists -- hidden or at opacity 0 included, which
    sl.save_layer captures additively. Skips a hidden OBJECT and a non-Basis
    shape key. Returns True if the save was performed, False otherwise (never
    raises).
    *boundary_pushed* is handed to sl.save_layer -- see AutoSaveCurrentLayer."""
    active_obj = context.active_object
    if not active_obj:
        return False

    # A hidden object cannot be committed -- sl.save_layer refuses it through
    # layers_blocked_reason(), and calling it anyway only logs a failure. The
    # strokes stay on the mesh and are committed by the first auto-save after
    # the object is shown again. Measured 2026-10-05: on Multires this path
    # used to raise inside GetMultires and drop the user out of Sculpt Mode.
    if object_hidden(active_obj):
        print(f"Sculptools: Layers skipped the {log_label}: the object is hidden.")
        return False

    # Capturing from the wrong shape key would store a bogus delta: bail out
    # instead. The panel shows the same warning so the user isn't left guessing.
    if not basis_key_active(active_obj):
        print(f"Sculptools: Layers skipped the {log_label}: the active shape "
              "key is not the Basis, sculpt strokes land in the active key.")
        return False

    parent_col = parent_collection(active_obj)
    if parent_col is None:
        return False

    base_obj = getBaseObject(parent_col)

    if not base_obj or not base_obj.sculptLayers:
        return False

    current_index = base_obj.sculptLayers_index

    if current_index < 0 or current_index >= len(base_obj.sculptLayers):
        return False

    # A hidden or zero-opacity active layer is committed too, ADDITIVELY (see
    # sl.save_layer). It used to be skipped here in silence, and the strokes
    # then stayed on the mesh until the next commit stored them in whatever
    # layer was active by then -- divided by ITS opacity -- or a rebuild wiped
    # them (measured in the GUI on 4.5 and 5.2, 2026-10-05).
    try:
        bpy.ops.sl.save_layer(boundary_pushed=boundary_pushed)
        return True
    except Exception as e:
        print(f"Sculptools: Layers could not run the {log_label}: {e}")
        return False


def AutoSaveCurrentLayer(context, boundary_pushed=False):
    """Auto-save the currently active layer (only while in Sculpt Mode).

    boundary_pushed=True: the CALLER has just pushed its own boundary undo step
    and nothing has changed since, so sl.save_layer skips its push -- which
    would only repeat it, one more empty Ctrl+Z after every stroke (measured in
    the GUI, 2026-10-01; see SL_SaveLayer.boundary_pushed). Only an operator
    that pushes first may pass it. Every other caller -- the opacity commit
    above all -- keeps the push, which is its stroke/commit boundary."""
    active_obj = context.active_object
    # Skip if not in sculpt mode (avoid issues during init/load)
    if not active_obj or active_obj.mode != 'SCULPT':
        return False
    return _auto_save_active_layer(context, "auto-save", boundary_pushed)


def AutoSaveOnModeExit(context):
    """Auto-save the active layer when exiting Sculpt Mode.
    Unlike AutoSaveCurrentLayer this does not require being in Sculpt Mode,
    since it is called after the mode has already changed."""
    return _auto_save_active_layer(context, "auto-save on mode exit")


# ----------------------------------------------------------------------
# Mirror helpers
#
# All READ-ONLY and pure numpy: no bpy.ops, no scene state, nothing written.
# Design + measurements:
# docs/superpowers/specs/2026-08-14-mirror-invert-layer-design.md
# ----------------------------------------------------------------------

# Cell side of the spatial hash, as a fraction of the neutral mesh's
# bounding-box diagonal. On the user's real base object that is 3.4e-4 against
# a vertex spacing of ~0.04 -- two orders of magnitude apart, and the gap only
# widens as the Multires level rises, so a cell can never hold two distinct
# vertices that might be confused for one another.
MIRROR_TOL_FACTOR = 1e-4

# Below this centre-cell match rate the mesh simply is not symmetric on that
# axis. Measured 2026-08-14: 0.0000 on a half mesh (an object whose symmetry
# comes from a Mirror modifier) and 0.0106 on the wrong axis, against 0.9635 on
# a real symmetric base object -- so the threshold has two orders of magnitude
# of daylight on either side.
MIRROR_MIN_MATCH_RATE = 0.10

_MIRROR_CELL_OFFSETS = None


def _mirror_cell_offsets():
    """The centre cell and its 26 neighbours, nearest ring first."""
    global _MIRROR_CELL_OFFSETS
    if _MIRROR_CELL_OFFSETS is None:
        offs = np.array([(i, j, k) for i in (-1, 0, 1)
                         for j in (-1, 0, 1) for k in (-1, 0, 1)])
        _MIRROR_CELL_OFFSETS = offs[np.argsort(np.abs(offs).sum(1))]
    return _MIRROR_CELL_OFFSETS


def mirror_direction_id(axis, source):
    """('X', 'POSITIVE') -> 'POSITIVE_X'. The inverse of mirror_direction_axis.

    The panel keeps the axis and the source side as two separate toggles, while
    the operator keeps one six-value identifier -- Blender's own vocabulary, so
    a script or a keymap entry reads the same as the native Symmetrize.

    The split exists because the two halves are not equally meaningful: FLIP has
    no source side at all. Measured 2026-08-14 on 4.5: from a sculpt living only
    on +X, SYMMETRIZE gives 0 non-zero points with '-X to +X' (the empty side is
    the source, so it wipes the work) against 50 with '+X to -X', while FLIP
    gives bit-identical results either way. A single six-value widget therefore
    offered six choices for three behaviours, and the user met exactly that
    confusion in the panel.
    """
    return "%s_%s" % (source, axis)


def mirror_direction_axis(direction):
    """'POSITIVE_X' -> (axis index, source sign).

    The identifiers are Blender's own (the native symmetrize_direction enum)
    and their labels spell the semantics out: 'POSITIVE_X' reads "+X to -X", so
    the NAMED side is the SOURCE and the destination is the other one.
    """
    axis = {'X': 0, 'Y': 1, 'Z': 2}[direction[-1]]
    return axis, (1.0 if direction.startswith('POSITIVE') else -1.0)


def mirror_vertex_map(base_obj, axis, tol=None):
    """READ-ONLY. Pair every vertex of base_obj's mesh with its mirror partner
    across the local `axis` plane through the origin.

    Returns (partner, stats):
      partner  int64 array, len == len(base_obj.data.vertices). partner[i] is
               the index of i's mirror partner, -1 when none was found within
               the tolerance, and i itself for a vertex on the plane.
      stats    dict(matched, total, rate, rate_first_pass, tol, cells, bailed)

    Computed on the NEUTRAL base mesh: that is what the layer deltas are
    indexed against, and the only shape with any claim to symmetry -- the
    sculpt is asymmetric by definition, which is why the user is mirroring it.

    A spatial cell hash, fully vectorised. The centre cell alone matches
    everything on a truly symmetric mesh (negating a float is exact, so the
    keys agree bit for bit); the 26 neighbouring cells are scanned only for
    what is left, which makes the pairing a guarantee rather than a heuristic:
    any partner within `tol` lies in one of the 27 cells. Candidates are always
    verified by true distance, so a cell collision cannot pair the wrong two.

    Rejected alternative, measured 2026-08-14: mathutils.kdtree takes 923 ms
    where this takes 71 ms at 100k points, because both its build and its query
    are Python-level loops over n.
    """
    n = len(base_obj.data.vertices)
    stats = {"matched": 0, "total": n, "rate": 0.0, "rate_first_pass": 0.0,
             "tol": 0.0, "cells": 0, "bailed": False, "off_plane": 0}
    if n == 0:
        return np.full(0, -1, dtype=np.int64), stats

    co = np.empty(n * 3, dtype=np.float32)
    base_obj.data.vertices.foreach_get('co', co)
    co = co.reshape(-1, 3).astype(np.float64)

    if tol is None:
        tol = MIRROR_TOL_FACTOR * float(np.linalg.norm(co.max(0) - co.min(0)))
    stats["tol"] = tol
    if tol <= 0.0:
        # A degenerate mesh (every vertex in one spot) has no symmetry to find.
        return np.full(n, -1, dtype=np.int64), stats

    cell = np.floor(co / tol).astype(np.int64)
    lo = cell.min(0) - 1
    if int((cell.max(0) - lo + 2).max()) >= (1 << 20):
        # Three cell indices no longer fit in one int64. Refuse rather than
        # silently mis-pack and pair the wrong vertices.
        return np.full(n, -1, dtype=np.int64), stats

    def pack(idx):
        idx = idx - lo
        return (idx[:, 0] << 42) | (idx[:, 1] << 21) | idx[:, 2]

    key = pack(cell)
    order = np.argsort(key, kind='stable')
    sorted_key = key[order]

    mirrored = co.copy()
    mirrored[:, axis] *= -1.0
    mcell = np.floor(mirrored / tol).astype(np.int64)

    # A vertex ON the mirror plane is its own partner, so it pairs up whatever
    # the rest of the mesh looks like. Only the off-plane vertices can testify
    # that the mesh really is symmetric, and the refusal below reads their rate
    # alone: measured 2026-08-14, a half cube scores 0.28 counting the seam and
    # 0.00 without it, for a mesh whose symmetry is nil either way.
    off_plane = np.abs(co[:, axis]) > tol * 0.5
    n_off = int(off_plane.sum())
    stats["off_plane"] = n_off

    partner = np.full(n, -1, dtype=np.int64)
    best = np.full(n, np.inf)
    todo = np.arange(n)
    for step, off in enumerate(_mirror_cell_offsets()):
        stats["cells"] = step + 1
        probe = pack(mcell[todo] + off)
        pos = np.clip(np.searchsorted(sorted_key, probe), 0, n - 1)
        hit = sorted_key[pos] == probe
        if hit.any():
            src = todo[hit]
            cand = order[pos[hit]]
            dist = np.linalg.norm(co[cand] - mirrored[src], axis=1)
            near = dist <= tol
            src, cand, dist = src[near], cand[near], dist[near]
            closer = dist < best[src]
            partner[src[closer]] = cand[closer]
            best[src[closer]] = dist[closer]
        if step == 0:
            # Off-plane vertices only (see the comment above). A mesh that is
            # entirely IN the plane has nothing to fail at, hence the 1.0.
            stats["rate_first_pass"] = (
                float((partner[off_plane] >= 0).mean()) if n_off else 1.0)
            if stats["rate_first_pass"] < MIRROR_MIN_MATCH_RATE:
                # Wrong axis, or a mesh that is not symmetric at all. Stop here
                # instead of scanning 26 more cells to reach the same verdict.
                stats["bailed"] = True
                break
        todo = np.flatnonzero(partner < 0)
        if not len(todo):
            break

    stats["matched"] = int((partner >= 0).sum())
    stats["rate"] = stats["matched"] / n
    return partner, stats


def mirror_layer_delta(delta, coords, partner, axis, source_sign, mode, tol):
    """Pure. Return (new_delta, changed_count) for one layer's stored delta.

    delta, coords  (n, 3) arrays: the layer's per-vertex delta and the NEUTRAL
                   coordinates the pairing was computed from.
    partner        from mirror_vertex_map(); -1 rows keep the delta they had.
    source_sign    +1.0 when the source side is the positive one.
    mode           'SYMMETRIZE' (destination side only, source untouched) or
                   'FLIP' (every vertex; the two sides swap).

    The delta is REFLECTED, not merely relocated: the component along the axis
    is negated. Skipping that produces an ear that sticks out the wrong way,
    and every "the shape changed" assertion still passes while it happens.

    Seam vertices (their own partner): SYMMETRIZE zeroes the axis component --
    the only value that makes the result genuinely symmetric -- while FLIP
    negates it, which is what flipping means.
    """
    n = len(delta)
    out = np.array(delta, dtype=delta.dtype, copy=True)
    has = partner >= 0
    # -1 would index the last row; send those to themselves and mask them out.
    src = np.where(has, partner, np.arange(n))
    reflected = delta[src].copy()
    reflected[:, axis] *= -1.0

    seam = np.abs(coords[:, axis]) <= tol * 0.5
    if mode == 'FLIP':
        target = has
    else:
        # Destination = the side OPPOSITE the one the direction names.
        target = has & (coords[:, axis] * source_sign < -tol * 0.5)
    out[target] = reflected[target]
    if mode == 'SYMMETRIZE':
        out[seam, axis] = 0.0

    changed = int((out != delta).any(axis=1).sum())
    return out, changed


# ----------------------------------------------------------------------
# Poll helpers
# ----------------------------------------------------------------------
# These helpers are used by operator poll() classmethods to determine
# whether an operator should be enabled in the UI. They are READ-ONLY:
# they must never mutate scene state (poll is called on every redraw).

def poll_active_object(context):
    """True if there's an active object."""
    return context.active_object is not None

def object_hidden(obj):
    """READ-ONLY. True when *obj* is not visible in the view layer (the
    Outliner eye, the monitor, a hidden or excluded collection).

    Measured 2026-10-05 in the GUI, 4.5 and 5.2: hiding the sculpted object
    from the Outliner keeps it in Sculpt Mode with every layer control live, and
    the next click raised a traceback at the user -- `mode_set(SCULPT)` back
    fails on a hidden object ("Context missing active object") and, on
    Multires, GetMultires' `depsgraph.objects[name]` raises KeyError (a hidden
    object is not evaluated). Safe from poll()/draw()."""
    return obj is not None and not obj.visible_get()


def poll_has_layers(context):
    """True if the active object has a valid sculpt-layer context
    with at least one layer.

    Validates the full chain: active_obj -> parent_coll -> base_obj -> sculptLayers.
    Returns False on any missing link (no KeyError, no AttributeError)."""
    active_obj = context.active_object
    if not active_obj:
        return False
    parent_col = parent_collection(active_obj)
    if parent_col is None:
        return False
    base_obj = getBaseObject(parent_col)
    return bool(base_obj and base_obj.sculptLayers)

def find_multires_readonly(obj, require_show_viewport=False, min_total_levels=0):
    """Find the first MULTIRES modifier on the object. READ-ONLY: no side effects.

    Unlike GetMultires(), this function does NOT mutate any modifier state
    (no toggling of other modifiers, no level changes). Safe to call from
    poll() and draw() contexts where any side effect would be catastrophic.

    Args:
        obj: The object to search (can be None).
        require_show_viewport: If True, skip modifiers with show_viewport=False.
        min_total_levels: Minimum required total_levels (0 = no requirement).

    Returns:
        The Multires modifier if found and matching the constraints, else None.
    """
    if obj is None:
        return None
    for mod in obj.modifiers:
        if mod.type != 'MULTIRES':
            continue
        if require_show_viewport and not mod.show_viewport:
            continue
        if mod.total_levels < min_total_levels:
            continue
        return mod
    return None


def stamp_multires_levels(active_obj, base_obj):
    """Record the current Multires total_levels on the base object so desync
    detection can compare integers cheaply. Clears any stale desync flag.
    Stores 0 when no Multires is present."""
    if base_obj is None:
        return
    mr = find_multires_readonly(active_obj)
    base_obj["sculptLayers_mr_levels"] = mr.total_levels if mr is not None else 0
    if "sculptLayers_mr_desync" in base_obj:
        del base_obj["sculptLayers_mr_desync"]


def multires_out_of_sync(active_obj, base_obj):
    """READ-ONLY. True when the layer data no longer matches the object it
    belongs to. Safe to call from draw()/poll().

    Two independent conditions:

    1. No Multires is being *evaluated* while the base holds a different vertex
       count than the mesh. GetMultires only picks up a Multires with
       show_viewport on, so switching that toggle off -- or removing the
       modifier -- silently sends every layer write down the plain-mesh path,
       where foreach_set no-ops on the size mismatch and nothing is saved.
       Applying the modifier is deliberately NOT flagged: that bakes the grid
       into the mesh and leaves the two counts equal.
    2. A native Multires whose total_levels no longer matches the stamped one
       (native Subdivide / Unsubdivide), or a desync flagged by a failed Morph
       Brush arm.
    """
    if base_obj is None or not base_obj.sculptLayers:
        return False

    # (1) first: it also covers a removed Multires, where there is no modifier
    # left to compare levels against.
    if (find_multires_readonly(active_obj, require_show_viewport=True) is None
            and len(base_obj.data.vertices) != len(active_obj.data.vertices)):
        return True

    # (2)
    mr = find_multires_readonly(active_obj)
    if mr is None:
        return False
    if base_obj.get("sculptLayers_mr_desync"):
        return True
    stored = base_obj.get("sculptLayers_mr_levels")
    return stored is not None and stored != mr.total_levels


# ----------------------------------------------------------------------
# Dyntopo / changed-topology guard
# ----------------------------------------------------------------------
# Layers are per-vertex deltas against a base object of FIXED vertex count, so
# anything that replaces the object's cage -- Dynamic Topology, a remesh, an
# applied Decimate, editing in Edit Mode -- makes every stored delta meaningless.
#
# TWO independent signals, because neither can do the other's job (both measured
# on 4.5.11 and 5.2.0, 2026-08-13):
#
#   * WHILE Dyntopo is on, `obj.data.vertices` is STALE -- it still read 8 after a
#     full remesh, because the sculpt geometry lives in a BMesh until the session
#     ends. Only the flag can see that state.
#   * ONCE Dyntopo is off, the flag reads False again -- it survives neither
#     leaving Sculpt Mode nor being switched off inside it. Only a stamped count
#     can see that state -- and switching Dyntopo off flushes the BMesh back into
#     `obj.data` immediately (cage 8 -> 3074 without leaving Sculpt Mode), so the
#     stamp comparison goes live at the same instant the flag goes dark. No gap.
#
# `multires_out_of_sync()` cannot be extended to cover this: it skips its
# vertex-count comparison as soon as it finds a Multires with show_viewport on,
# and Dyntopo leaves `total_levels` untouched -- it reported False while the cage
# had gone 8 -> 3074 against a 98-point base.
CAGE_VERTS_PROP = "sculptLayers_cage_verts"


def dyntopo_active(obj):
    """READ-ONLY. True while Blender is sculpting *obj* with Dynamic Topology.

    `Object.use_dynamic_topology_sculpting` is a read-only BOOLEAN (measured), so
    this is free to call from draw()/poll(). The mode test is redundant -- the
    flag is only ever True in Sculpt Mode -- and kept as a statement of intent."""
    if obj is None:
        return False
    return bool(obj.mode == 'SCULPT'
                and getattr(obj, "use_dynamic_topology_sculpting", False))


def topology_changed(active_obj, base_obj):
    """READ-ONLY. True when the object's cage no longer has the vertex count its
    layer data was built against.

    Returns False when the stamp is absent: a .blend written by an older build
    has no stamp and must keep behaving exactly as it does today, until one of
    `stamp_cage_verts`' call sites writes one. The fallback IS the migration."""
    if active_obj is None or base_obj is None or not base_obj.sculptLayers:
        return False
    stamped = base_obj.get(CAGE_VERTS_PROP)
    if stamped is None:
        return False
    return stamped != len(active_obj.data.vertices)


def stamp_cage_verts(active_obj, base_obj):
    """EXECUTE-ONLY. Record the cage vertex count the layer data now matches.

    Called by the operators that legitimately establish or change that pairing,
    after their last mutation -- never from poll()/draw(), and never from a
    handler."""
    if active_obj is not None and base_obj is not None:
        base_obj[CAGE_VERTS_PROP] = len(active_obj.data.vertices)


# `sl.sync_multires` is the REPAIR for the out-of-sync state, so it necessarily
# runs while layers_blocked_reason() would otherwise refuse -- and at step 3 it
# calls `bpy.ops.sl.subdivide_layers()` in a loop, whose poll() is
# `_poll_editable`. `bpy.ops` CHECKS poll and raises RuntimeError when it fails,
# so without this marker the repair would grey itself out from the inside.
#
# Measured 2026-08-13 (dev_probe_multires_removed.py): with the refusal added and
# NO marker, the sync still happened to finish -- but only because forcing
# `show_viewport` back on makes multires_out_of_sync() False again by the time
# the loop runs. That is an accident of ordering, not a guarantee: it does not
# hold when the base carries a `sculptLayers_mr_desync` flag, which condition (2)
# honours regardless of show_viewport. Do not delete the marker on the strength
# of the probe finishing green.
_in_multires_sync = False


@contextlib.contextmanager
def multires_sync_active():
    """Suspend the OUT-OF-SYNC refusal for the length of sl.sync_multires.

    Deliberately narrow: Dyntopo and a changed cage are NOT suspended. Those
    describe data that cannot be repaired at all -- a delta is one vector per
    vertex, and 98 of them cannot describe 73730 -- so sync_multires has no
    business running there either, and its poll() keys off a predicate blind to
    them anyway."""
    global _in_multires_sync
    prev = _in_multires_sync
    _in_multires_sync = True
    try:
        yield
    finally:
        _in_multires_sync = prev


def layers_blocked_reason(context):
    """READ-ONLY. Why layer edits cannot be applied right now, or None.

    Single source of truth for both halves of the refusal: `poll_layers_editable`
    turns it into a bool, and the operators hand the string to
    `poll_message_set()` so a greyed-out button can say why. A failed poll()
    produces no report at all, so without that string the refusal would be as
    silent as the bug it replaces."""
    active_obj = context.active_object
    # First: every layer edit leaves and re-enters Sculpt Mode or reads the
    # evaluated mesh, and neither works on a hidden object (object_hidden()).
    if object_hidden(active_obj):
        return "The object is hidden - show it to edit layers"
    if dyntopo_active(active_obj):
        return "Turn Dyntopo off - layers need stable topology"
    # getBaseObject() is NOT None-safe, and this runs from SL_AddLayer.poll on
    # objects that have no layer data yet -- the normal case for Add Layer.
    parent_col = parent_collection(active_obj)
    base_obj = getBaseObject(parent_col) if parent_col is not None else None
    if topology_changed(active_obj, base_obj):
        return "The mesh topology changed - layer data unusable"
    # The Multires the layer data was built against is gone, switched off, or at
    # a different level. Every layer write then goes down the plain-mesh path,
    # where foreach_set only *reports* a size mismatch and stores nothing.
    # Reported by the user 2026-08-13 for a DELETED modifier and measured the
    # same day: the out-of-sync box was already drawn, and 17 of the 20 sl.*
    # operators stayed clickable behind it -- `sl.add_layer` returned FINISHED
    # and appended a third layer to data it could never apply, exactly the
    # Dyntopo defect in a second disguise. `topology_changed()` cannot cover
    # this: it compares the CAGE, and removing a Multires leaves the cage at 8.
    if not _in_multires_sync and multires_out_of_sync(active_obj, base_obj):
        return "Layer data is out of sync with the Multires"
    return None


def poll_layers_editable(context):
    """READ-ONLY. poll_has_layers(), and the layers can actually be applied.

    The panel deliberately keeps using poll_has_layers(): it must still draw the
    layer list and the alert, not fall back to its empty state."""
    return poll_has_layers(context) and layers_blocked_reason(context) is None


def data_collection_compatible(active_obj, coll):
    """READ-ONLY. True when *coll*'s layer data can actually drive *active_obj*.
    Safe to call from draw()/poll().

    Deliberately delegates to multires_out_of_sync(), the add-on's own
    definition of "this data no longer matches this object": that way the
    `sl.link_data` picker can never accept data the panel would immediately flag
    as "Layer data out of sync". Without a Multires that is an exact vertex-count
    comparison -- the case the old dropdown accepted silently. With a Multires it
    falls back to the stamped-level check, which is a proxy (two objects can
    share a level count and still differ in topology); anything slipping through
    is still caught by the panel alert, as before.

    An empty data collection is rejected too: linking one would rebuild the mesh
    onto the bare base shape with no layers to put back."""
    if active_obj is None or coll is None:
        return False
    base_obj = getBaseObject(coll)
    if base_obj is None or not base_obj.sculptLayers:
        return False
    return not multires_out_of_sync(active_obj, base_obj)


def list_data_collections(context):
    """READ-ONLY. Every layer-data collection under the root that holds a base
    object, in outliner order. Safe to call from draw()/poll()."""
    sc_coll = GetSculptCollection(context, False)  # read-only lookup, never creates
    if sc_coll is None:
        return []
    return [coll for coll in sc_coll.children if coll.objects]


# ---------------------------------------------------------------------------
# Bake to Shape Keys
# ---------------------------------------------------------------------------
MORPH_COLL_NAME = "LAYERS_SHAPE_KEYS"           # dedicated deliverable collection
# Pre-1.0.0 name, changed 2026-08-12: "blendshape" is Maya/Unreal terminology and
# this collection is visible in the Outliner, where a Blender user expects
# Blender's own term. Adopted and renamed in place on the first bake, exactly like
# SCULPT_COLL_NAME_LEGACY -- otherwise an existing file would grow a second
# collection while the old one kept its deliverables.
MORPH_COLL_NAME_LEGACY = "LAYERS_BLENDSHAPES"
MORPH_TARGET_PROP = 'sculptLayers_morph_target'  # on the source (layered) object
MORPH_SOURCE_PROP = 'sculptLayers_morph_source'  # back-link on the morph object

# The deliverable is a plain dense mesh, so a high Multires level makes it big.
# Above this many vertices the bake asks for confirmation first. One constant --
# retune freely, nothing else depends on the value.
BAKE_CONFIRM_VERTS = 250000


def bake_topology_source(context, active_obj):
    """Build the mesh whose TOPOLOGY the bake deliverable must have.

    Returns (mesh, multires_mod):
      * mesh -- ALWAYS a new datablock the caller owns and must either adopt
        into an object or remove. Without a Multires it is a copy of
        active_obj.data; with one it is the object evaluated at total_levels,
        which is the resolution the layer deltas are stored at.
      * multires_mod -- the GetMultires() tuple, or None. The CALLER restores
        `multires_mod[0].levels = multires_mod[1]` and OtherModsOn(): this
        function cannot, the mesh must stay readable until the bake is done.

    EXECUTE-ONLY -- it goes through GetMultires(), which mutates modifier state.
    Never call it from poll() or draw(); use bake_preview_counts() there.

    Must run in OBJECT mode: a Multires object's evaluated mesh is unreliable in
    Sculpt Mode (it comes back as the base cage).
    """
    multires_mod = GetMultires(context, active_obj)
    if multires_mod is None:
        return active_obj.data.copy(), None
    depsgraph = multires_mod[3]
    eval_obj = depsgraph.objects[active_obj.name]
    return (bpy.data.meshes.new_from_object(eval_obj, depsgraph=depsgraph),
            multires_mod)


def bake_preview_counts(active_obj, mode):
    """(vertex count, shape-key count) the bake would produce, for the panel.

    READ-ONLY, and free: the base object already holds exactly the vertex count
    the deliverable will have -- that is where the deltas live, at the Multires
    resolution -- and the keys are simply the visible layers. Returns (0, 0)
    when the object has no layer data.
    """
    coll = parent_collection(active_obj)
    base_obj = getBaseObject(coll) if coll is not None else None
    if base_obj is None:
        return 0, 0
    verts = len(base_obj.data.vertices)
    visible = sum(1 for sl_i in base_obj.sculptLayers if sl_i.visible)
    return verts, (visible if mode == 'PER_LAYER' else 1)


def get_or_create_morph_collection(context):
    """Return the dedicated LAYERS_SHAPE_KEYS collection, creating it and
    linking it to the current scene root if needed.

    EXECUTE-ONLY (it creates and renames datablocks). A file baked before
    2026-08-12 has the collection under MORPH_COLL_NAME_LEGACY; adopt it by
    renaming in place, so its existing deliverables come along instead of being
    orphaned next to a fresh empty collection."""
    coll = bpy.data.collections.get(MORPH_COLL_NAME)
    if coll is None:
        legacy = bpy.data.collections.get(MORPH_COLL_NAME_LEGACY)
        if legacy is not None:
            legacy.name = MORPH_COLL_NAME
            coll = legacy
    if coll is None:
        coll = bpy.data.collections.new(MORPH_COLL_NAME)
    if coll.name not in context.scene.collection.children:
        try:
            context.scene.collection.children.link(coll)
        except RuntimeError:
            # Already linked somewhere in this scene's tree; leave it.
            pass
    return coll


def _delete_morph_object(morph_obj):
    """Remove a morph object and its orphaned mesh."""
    mesh = morph_obj.data
    bpy.data.objects.remove(morph_obj, do_unlink=True)
    if mesh.users == 0:
        bpy.data.meshes.remove(mesh)


def _ensure_basis(morph_obj):
    """Guarantee the morph object has at least a Basis shape key."""
    if morph_obj.data.shape_keys is None:
        morph_obj.shape_key_add(name="Basis", from_mix=False)


def _build_morph_object(context, active_obj, base_obj, ref_mesh):
    """Create a fresh <obj>_MORPH from *ref_mesh*: the reference topology posed
    on the un-layered base shape, with a Basis shape key, linked into
    LAYERS_SHAPE_KEYS. Wires the source<->morph links. ADOPTS ref_mesh.

    The topology never comes from base_obj: the base object is a vertex-only
    point cloud (NewBaseObject stores coordinates and nothing else), so copying
    it yielded a deliverable with no edges and no faces. bake_topology_source()
    supplies topology, UVs and materials -- from the cage on a plain object, and
    from the evaluated subdivided mesh on a Multires one, where the cage would
    have the wrong vertex count entirely. Only the coordinates come from the
    base."""
    mesh = ref_mesh
    mesh.name = active_obj.name + "_MORPH"
    morph_obj = bpy.data.objects.new(active_obj.name + "_MORPH", mesh)

    # Drop any shape keys inherited from the source before touching coordinates:
    # with keys present the mesh coords are shadowed by the Basis key, so the
    # rewind below would be overridden.
    if mesh.shape_keys:
        morph_obj.shape_key_clear()

    # Rewind to the un-layered base shape -- the source mesh carries the whole
    # layer stack baked into its coordinates.
    np_base = np.zeros(len(base_obj.data.vertices) * 3, dtype=np.float32)
    base_obj.data.vertices.foreach_get('co', np_base)
    mesh.vertices.foreach_set('co', np_base)

    # Defensive: keep the deliverable free of any per-layer delta attribute.
    for sl_i in base_obj.sculptLayers:
        attr = mesh.attributes.get(sl_i.layer_attrib_name)
        if attr is not None:
            mesh.attributes.remove(attr)

    # Match the source object's world transform so the deliverable overlaps
    # the sculpted object instead of sitting at the world origin. Snapshot only
    # (no parenting) and only on build -- re-bakes reuse the object and must not
    # clobber a placement the user set manually.
    morph_obj.matrix_world = active_obj.matrix_world.copy()
    get_or_create_morph_collection(context).objects.link(morph_obj)
    morph_obj.shape_key_add(name="Basis", from_mix=False)
    active_obj[MORPH_TARGET_PROP] = morph_obj.name
    morph_obj[MORPH_SOURCE_PROP] = active_obj.name
    return morph_obj


def _resolve_morph_object(context, active_obj, base_obj, ref_mesh):
    """Reuse the linked <obj>_MORPH if it exists and matches the reference
    topology, else (re)build it. A vertex- or face-count mismatch means topology
    changed -> rebuild.

    CONSUMES ref_mesh: it is either adopted into a newly built morph object, or
    removed here. Callers must not touch it afterwards.

    Counts are compared against *ref_mesh*, not against active_obj: on a Multires
    object the source's own mesh is the base cage (6 faces where the deliverable
    has hundreds), so comparing with it would rebuild the deliverable on every
    single bake. It also rebuilds the face-less morphs left behind by versions
    that copied their mesh from the base object, which would otherwise be reused
    forever (their vertex count matches)."""
    verts_count = len(ref_mesh.vertices)
    polys_count = len(ref_mesh.polygons)
    name = active_obj.get(MORPH_TARGET_PROP)
    morph_obj = bpy.data.objects.get(name) if name else None
    if morph_obj is not None and (
            len(morph_obj.data.vertices) != verts_count
            or len(morph_obj.data.polygons) != polys_count):
        _delete_morph_object(morph_obj)
        morph_obj = None
    if morph_obj is None:
        return _build_morph_object(context, active_obj, base_obj, ref_mesh)
    bpy.data.meshes.remove(ref_mesh)
    coll = get_or_create_morph_collection(context)
    if morph_obj.name not in coll.objects:
        coll.objects.link(morph_obj)
    _ensure_basis(morph_obj)
    return morph_obj


def hide_baked_source(context, active_obj, morph_obj):
    """After a successful bake, hide the source object and hand the viewport over
    to the deliverable, which sits at the same world transform and would
    otherwise be buried inside it.

    Returns True when the source ended up hidden.

    Two details are MEASURED, not chosen (dev_probe_hide_after_bake.py, run on
    both 4.5.11 and 5.2.0):

    * It hides with `hide_set` (the Outliner eye), never `hide_viewport` (the
      monitor). Setting `hide_viewport` while the object is in Sculpt Mode traps
      it there for good, identically on both versions: `object.mode_set`'s poll
      then refuses with "Cannot edit hidden object", so the user cannot even
      leave the mode. `hide_set` is also the flag Alt+H restores, which is how
      the user gets the object back.

    * The caller must NOT return the source to Sculpt Mode, and this is where the
      two versions disagree -- which is itself the reason not to depend on
      either. `hide_set(True)` raises nothing in Sculpt Mode but leaves the
      object sculpting something invisible; entering Sculpt Mode while already
      hidden then RAISES on 4.5 ("Unable to execute 'Sculpt Mode'") and SUCCEEDS
      on 5.2. Object Mode + the eye is the one end state that behaves the same
      everywhere and stays recoverable. This function switches to Object Mode
      itself if needed.
    """
    if active_obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

    try:
        active_obj.select_set(False)
        active_obj.hide_set(True)
    except RuntimeError:
        # Not in the current view layer (multi-scene, or linked elsewhere).
        # Nothing to hide here, and no reason to fail a completed bake.
        return False

    if morph_obj is not None:
        # The deliverable becomes the selection: it is the result the user asked
        # for, and the source they were working on is now invisible, so leaving
        # an invisible object active would be a dead end.
        try:
            morph_obj.hide_set(False)
            morph_obj.select_set(True)
            context.view_layer.objects.active = morph_obj
        except RuntimeError:
            pass
    return True


def _write_shapekey(morph_obj, name, np_coords):
    """Create or overwrite a shape key by name from a flat (N*3,) coord array."""
    keys = morph_obj.data.shape_keys
    kb = keys.key_blocks.get(name) if keys else None
    if kb is None:
        kb = morph_obj.shape_key_add(name=name, from_mix=False)
        # New morphs rest at 0 so the deliverable looks neutral until animated.
        # On re-bake we keep the existing key's (possibly animated) value.
        kb.value = 0.0
    kb.data.foreach_set('co', np_coords)
    kb.slider_min = 0.0
    kb.slider_max = 1.0


def bake_shapekeys(context, active_obj, mode, pose_name, ref_mesh):
    """Project the sculpt-layer stack into shape keys on a separate <obj>_MORPH.

    PER_LAYER: one shape key per *visible* layer, each = base + full delta
    (opacity ignored; the shape-key value replaces it for animation).
    COMBINED_POSE: one shape key = base + sum(visible delta * opacity), named
    `pose_name`.

    Returns (morph_obj, baked_names). PER_LAYER with no visible layers returns
    (None, []).

    *ref_mesh* comes from bake_topology_source() and is CONSUMED on every path,
    including the empty-result early return -- otherwise a bake with no visible
    layers leaks a mesh datablock (a dense one, on Multires)."""
    parent_col = parent_collection(active_obj)
    base_obj = getBaseObject(parent_col)
    verts_count = len(base_obj.data.vertices)

    np_base = np.zeros(verts_count * 3, dtype=np.float32)
    base_obj.data.vertices.foreach_get('co', np_base)

    def _layer_delta(sl_i):
        np_delta = np.zeros(verts_count * 3, dtype=np.float32)
        base_obj.data.attributes[sl_i.layer_attrib_name].data.foreach_get(
            'vector', np_delta)
        return np_delta

    baked = []
    if mode == 'PER_LAYER':
        # Shape keys are addressed by name and layer names are freely editable,
        # so two layers called the same would collapse into one key (the second
        # silently overwriting the first). Suffix the repeats, Blender-style.
        used = set()

        def _unique(name):
            if name not in used:
                used.add(name)
                return name
            i = 1
            while f"{name}.{i:03d}" in used:
                i += 1
            candidate = f"{name}.{i:03d}"
            used.add(candidate)
            return candidate

        targets = [(_unique(sl_i.name), np_base + _layer_delta(sl_i))
                   for sl_i in base_obj.sculptLayers if sl_i.visible]
        if not targets:
            bpy.data.meshes.remove(ref_mesh)
            return None, []
        morph_obj = _resolve_morph_object(context, active_obj, base_obj, ref_mesh)
        for name, coords in targets:
            _write_shapekey(morph_obj, name, coords)
            baked.append(name)
    else:  # COMBINED_POSE
        # This branch reproduces the VISIBLE pose, so it honours layer_opacity.
        # The PER_LAYER branch above deliberately does not -- there the shape
        # key's own value is the strength, and baking a pre-scaled shape would
        # double up.
        coords = np_base.copy()
        for sl_i in base_obj.sculptLayers:
            if not sl_i.visible:
                continue
            np_delta = _layer_delta(sl_i)
            if sl_i.layer_opacity != 1.0:
                np_delta *= sl_i.layer_opacity
            coords += np_delta
        morph_obj = _resolve_morph_object(context, active_obj, base_obj, ref_mesh)
        _write_shapekey(morph_obj, pose_name, coords)
        baked.append(pose_name)

    return morph_obj, baked

