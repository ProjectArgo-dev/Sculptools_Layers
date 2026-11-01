# SPDX-FileCopyrightText: 2024 mifth
# SPDX-FileCopyrightText: 2026 ProjectArgo
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Part of "Sculptools: Layers", a fork of the Sculpt Layers add-on by mifth,
# modified by ProjectArgo since 2026. See CREDITS.md.

import bpy
import numpy as np
from . import layers_utils

from bpy.props import (
    StringProperty,
    FloatProperty,
    BoolProperty,
    IntProperty,
    CollectionProperty,
    EnumProperty,
    PointerProperty,
)


LAYER_BRUSH_ID = "brushes/essentials_brushes-mesh_sculpt.blend/Brush/Layer"


def _activate_layer_brush(context):
    """Activate the Essentials Layer brush (the Morph session's brush) and
    return it, or None. Needs a VIEW_3D override -- asset_activate fails
    silently without one."""
    window, area = _find_view3d(context)
    if area is None:
        print("Sculptools: Layers could not activate the Layer brush: no 3D View")
        return None
    try:
        with context.temp_override(window=window, area=area):
            bpy.ops.brush.asset_activate(
                asset_library_type='ESSENTIALS',
                relative_asset_identifier=LAYER_BRUSH_ID,
            )
    except Exception as e:
        print(f"Sculptools: Layers could not activate the Layer brush: {e}")
        return None
    brush = context.tool_settings.sculpt.brush
    return brush if _sculpt_brush_type(brush) == 'LAYER' else None


def _find_view3d(context):
    """Return (window, area) of the first VIEW_3D area, or (None, None)."""
    for window in context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                return window, area
    return None, None


def _poll_editable(cls, context):
    """READ-ONLY. Shared poll() for every operator that edits layers.

    `layers_utils.poll_layers_editable()` plus the tooltip: a failed poll()
    produces NO report at all, so without `poll_message_set` the refusal would be
    exactly as silent as the bug it replaces -- a dead button and no explanation.
    The reason string comes from `layers_utils.layers_blocked_reason()`, the one
    source both halves of the refusal share.

    `poll_message_set` only sets a UI string, so this stays read-only. Verified
    present on 4.5.11 and 5.2.0."""
    if not layers_utils.poll_has_layers(context):
        return False
    reason = layers_utils.layers_blocked_reason(context)
    if reason is not None:
        cls.poll_message_set(reason)
        return False
    return True


def _report_topology_mismatch(op, verts_count, base_obj):
    """The vertex-count refusal, shared by all seven operators that have one.

    WARNING, not INFO: measured 2026-08-13, this is the branch a Dyntopo-remeshed
    object lands in, and at INFO it was a grey status line that vanished -- the
    user saw a dead button and nothing else. "Mesh should have 98 Points!" also
    told them nothing when the mesh had 3074, and did not name a cause.

    WARNING is safe even here: only an ERROR report makes bpy.ops raise a
    RuntimeError, and sl.save_layer -- which AutoSaveCurrentLayer calls from
    Python -- is one of these seven.

    With the poll() guard in place these branches are unreachable from the UI on a
    stamped object; they remain the only defence for a .blend written before the
    stamp existed, which is why the message still has to be good."""
    op.report({'WARNING'},
              "Sculptools: Layers cannot use this layer data: it has %d "
              "points, the mesh %d. Dyntopo or a remesh changes "
              "topology; undo to recover."
              % (verts_count, len(base_obj.data.vertices)))


def _others_keep(n):
    """"1 other object keeps" / "N other objects keep" -- the subject of every
    message about layer data that stays with the objects sharing it."""
    return ("1 other object keeps" if n == 1
            else "%d other objects keep" % n)


def _sculpt_brush_type(brush):
    """Sculpt brush type enum, across Blender versions.
    Blender 5.x exposes `sculpt_brush_type`; 4.x uses `sculpt_tool`."""
    if brush is None:
        return None
    return getattr(brush, 'sculpt_brush_type', None) or getattr(brush, 'sculpt_tool', None)


# The Layer brush the open Morph session armed, and its settings as found:
# (brush.name_full, {"use_persistent": .., "height": .., "direction": ..}), or
# None. Kept here AS WELL AS on the session copy: an undo past the session's
# start removes the copy, and brush edits are outside global undo (measured
# 2026-10-06, dev_gui_morph_facts.py F1) -- the user's Layer brush would stay
# armed for good.
_armed_layer_brush = None


def _brush_by_name_full(name_full):
    """READ-ONLY. The brush datablock called *name_full*, or None."""
    for b in bpy.data.brushes:
        if b.name_full == name_full:
            return b
    return None


def _brush_settings(brush):
    return {k: getattr(brush, k) for k in layers_utils.MORPH_BRUSH_SETTINGS}


def _set_brush_settings(brush, settings):
    """Set what can be set now; return {key: value} of what could not.

    `direction`'s items depend on the active object's mode: outside Sculpt
    Mode the enum offers only 'DEFAULT' and the assignment raises (measured
    2026-10-06, 4.5.13 + 5.2.2). Callers therefore arm and disarm in Sculpt
    Mode, and keep anything left over pending instead of raising."""
    failed = {}
    for k, default in layers_utils.MORPH_BRUSH_SETTINGS.items():
        if k in settings:
            try:
                setattr(brush, k, type(default)(settings[k]))
            except (TypeError, ValueError):
                failed[k] = settings[k]
    return failed


def _arm_layer_brush(brush, tmp_obj):
    """Save *brush*'s settings (once per session) and set the Morph ones. The
    saved values also go on the session copy, so a session reopened from a
    .blend still gives the brush back as it was found. Strength, size and
    falloff are never touched -- they are the user's. Call in Sculpt Mode."""
    global _armed_layer_brush
    saved = tmp_obj.get(layers_utils.MORPH_LAYER_BRUSH_PROP)
    if saved is not None and saved.get("brush") == brush.name_full:
        # Everything saved, the start strength included (see _start_strength).
        found = {k: saved[k] for k in saved.keys() if k != "brush"}
    else:
        found = _brush_settings(brush)
        if _armed_layer_brush is not None and _armed_layer_brush[0] == brush.name_full:
            # Still armed, or a restore left pending: those are the originals.
            found.update(_armed_layer_brush[1])
    tmp_obj[layers_utils.MORPH_LAYER_BRUSH_PROP] = dict(found, brush=brush.name_full)
    _armed_layer_brush = (brush.name_full, found)
    _set_brush_settings(brush, layers_utils.MORPH_BRUSH_SETTINGS)


def _disarm_layer_brush(saved=None):
    """Give the armed Layer brush its settings back -- the module copy first,
    else *saved* (the session copy's MORPH_LAYER_BRUSH_PROP, as a dict). Safe
    when nothing is armed. Call in Sculpt Mode: a setting that cannot be set
    now stays pending in the module copy for the next reconcile."""
    global _armed_layer_brush
    armed = _armed_layer_brush
    _armed_layer_brush = None
    if armed is None and saved and saved.get("brush"):
        armed = (saved["brush"], {k: v for k, v in saved.items() if k != "brush"})
    if armed is None:
        return
    brush = _brush_by_name_full(armed[0])
    if brush is None:
        return
    pending = _set_brush_settings(brush, armed[1])
    if "strength" in armed[1]:
        owner = (_unified_paint_settings(bpy.context)
                 if armed[1].get("strength_unified") else brush)
        if owner is not None:
            owner.strength = float(armed[1]["strength"])
    if pending:
        _armed_layer_brush = (armed[0], pending)


def _unified_paint_settings(context):
    """READ-ONLY. The sculpt unified paint settings (4.x keeps them on the tool
    settings, 5.x on the sculpt settings), or None."""
    ts = context.tool_settings
    return (getattr(ts, "unified_paint_settings", None)
            or getattr(ts.sculpt, "unified_paint_settings", None))


def _start_strength(context, brush, tmp_obj):
    """Start the session's brush at MORPH_START_STRENGTH -- the user's call,
    2026-10-06: at 1.0 the erase leaves hard edges. The strength the user sees
    lives in the unified paint settings when 'unified strength' is on, else on
    the brush; whichever it is is saved beside the other settings and given
    back at the end. Set ONCE: the watch and the reconcile never enforce it,
    so the user can change it mid-session."""
    global _armed_layer_brush
    ups = _unified_paint_settings(context)
    unified = bool(ups is not None and getattr(ups, "use_unified_strength", False))
    owner = ups if unified else brush
    found = dict(_armed_layer_brush[1]) if _armed_layer_brush is not None else {}
    found.update(strength=float(owner.strength), strength_unified=unified)
    _armed_layer_brush = (brush.name_full, found)
    tmp_obj[layers_utils.MORPH_LAYER_BRUSH_PROP] = dict(found, brush=brush.name_full)
    owner.strength = layers_utils.MORPH_START_STRENGTH


def _saved_layer_brush(tmp_obj):
    """READ-ONLY. The Layer brush settings a session copy saved, as a dict."""
    g = tmp_obj.get(layers_utils.MORPH_LAYER_BRUSH_PROP)
    return g.to_dict() if g is not None else None


def _capture_pre_morph_brush(context, tmp_obj):
    """Store the active sculpt brush's asset reference on the session copy, so
    Apply / Cancel give it back -- even after a save and reload mid-session,
    which the module global this replaced did not survive."""
    ref = getattr(context.tool_settings.sculpt, "brush_asset_reference", None)
    if ref is None:
        return
    tmp_obj[layers_utils.MORPH_PREV_BRUSH_PROP] = {
        "library_type": ref.asset_library_type,
        "library_identifier": ref.asset_library_identifier,
        "relative_identifier": ref.relative_asset_identifier,
    }


def _restore_pre_morph_brush(context, ref):
    """Re-activate the brush *ref* describes (a dict, or None)."""
    if not ref:
        return
    window, area = _find_view3d(context)
    if area is None:
        return
    try:
        with context.temp_override(window=window, area=area):
            bpy.ops.brush.asset_activate(
                asset_library_type=ref["library_type"],
                asset_library_identifier=ref["library_identifier"],
                relative_asset_identifier=ref["relative_identifier"],
            )
    except Exception as e:
        print(f"Sculptools: Layers could not restore the previous brush: {e}")


def _reshape_multires_to(context, obj, mr, coords):
    """Force obj's Multires so its evaluated mesh matches `coords`
    (flat float32 numpy array at grid resolution). Object Mode only."""
    tmp_mesh = bpy.data.meshes.new("_TMP_SYNC_RESHAPE")
    tmp_obj = None
    # try/finally: multires_reshape can raise, and the temp object is linked to
    # the scene collection -- leaking it leaves visible junk in the outliner.
    try:
        tmp_mesh.vertices.add(len(coords) // 3)
        tmp_mesh.vertices.foreach_set('co', coords)
        tmp_mesh.update()
        tmp_obj = bpy.data.objects.new(obj.name + "_TMP_SYNC", tmp_mesh)
        context.scene.collection.objects.link(tmp_obj)
        bpy.ops.object.select_all(action='DESELECT')
        tmp_obj.select_set(True)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        with context.temp_override(object=obj, active_object=obj,
                                   selected_objects=[tmp_obj, obj],
                                   selected_editable_objects=[tmp_obj, obj]):
            bpy.ops.object.multires_reshape(modifier=mr.name)
    finally:
        if tmp_obj is not None:
            bpy.data.objects.remove(tmp_obj, do_unlink=True)
        bpy.data.meshes.remove(tmp_mesh)


# Global state for tracking mode changes (used by msgbus subscriber)
_previous_mode = None
_msgbus_owner = object()  # Unique owner for msgbus subscription


def _on_mode_change():
    """Callback triggered when an object's mode changes.
    Performs auto-save when exiting Sculpt Mode."""
    global _previous_mode

    try:
        context = bpy.context
        active_obj = context.active_object

        if not active_obj:
            _previous_mode = None
            return

        current_mode = active_obj.mode

        # Detect transition from SCULPT to any other mode
        if _previous_mode == 'SCULPT' and current_mode != 'SCULPT':
            layers_utils.AutoSaveOnModeExit(context)

        # Update the stored mode
        _previous_mode = current_mode
    except Exception as e:
        print(f"Sculptools: Layers could not commit on a mode change: {e}")


def _seed_previous_mode():
    """Start the mode memory from the mode the active object is ALREADY in.

    _on_mode_change() only learns a mode from its own last notification. Reset
    to None, the first exit after a file saved in Sculpt Mode was reopened --
    or after the add-on was enabled with an object already sculpting -- read
    as None -> OBJECT and committed nothing (dev_test_mode_seed.py). Read-only:
    it sets a module global, never bpy.data, so it is safe in load_post."""
    global _previous_mode
    try:
        obj = bpy.context.active_object
    except AttributeError:      # a restricted context (add-on registration)
        obj = None
    _previous_mode = obj.mode if obj is not None else None


def _seed_previous_mode_timer():
    """One-shot: the seed for register(). Blender runs an add-on's register()
    with bpy.context RESTRICTED -- the active object cannot be read there
    (measured: the seed came back None with an object in Sculpt Mode) -- so it
    runs on the first event-loop tick instead. unregister() disarms it, since a
    registered timer outlives the add-on."""
    _seed_previous_mode()
    return None


@bpy.app.handlers.persistent
def _on_load_post(*args, **kwargs):
    """Called by Blender after a .blend file is loaded.
    Resets msgbus mode-tracking state and the arm cache so they don't leak
    across sessions, and RE-SUBSCRIBES the msgbus.

    Blender clears every msgbus subscriber on file load ("All subscribers will
    be cleared on file-load", bpy.msgbus.subscribe_rna docs), so without the
    re-subscription below the Sculpt-Mode-exit auto-save silently stops working
    for the rest of the session as soon as the user opens any .blend -- and the
    first rebuild from the stored deltas then discards the un-committed
    strokes."""
    global _armed_layer_brush
    _seed_previous_mode()
    # It named a brush of the file being replaced; a session saved in the new
    # one carries its own copy of the settings.
    _armed_layer_brush = None
    # A queued opacity rebuild refers to objects in the file being replaced.
    layers_utils.discard_pending_opacity_rebuild()
    layers_utils.discard_pending_live_opacity()
    # The undo steps it refers to belonged to the file being replaced.
    layers_utils.discard_step_current()
    _unregister_msgbus()  # defensive: never stack two subscriptions
    _register_msgbus()
    # A Morph session saved mid-way comes back with its copy; the brush and
    # the per-point factor are put in step on the first tick.
    if not bpy.app.timers.is_registered(_morph_reconcile_timer):
        bpy.app.timers.register(_morph_reconcile_timer, first_interval=0.0)


# NO undo_post handler that touches layer data or the mesh. One was added on
# 2026-07-31 to re-derive the mesh when an undo landed on a step pairing a new
# opacity value with the not-yet-rebuilt mesh, gated on a marker comparison so
# that undoing a sculpt stroke would be a no-op. It was REMOVED the same day: in
# the GUI, on a Multires object, the session ended with the active layer's
# stored delta at ALL ZEROS. The gate is not sufficient -- rebuilding from the
# stored deltas during an undo sequence races the undo system's own restoration
# of those very deltas, and a marker cannot distinguish "the value moved" from
# "the deltas moved underneath it". Do not reintroduce this without a way to
# observe the real undo stack.
# The one undo_post / redo_post handler there is, _on_morph_undo_redo, only arms
# a timer that touches the Morph copy's brush state; see _morph_reconcile.


@bpy.app.handlers.persistent
def _on_save_pre(filepath="", *args, **kwargs):
    """Called by Blender just before a .blend file is written.
    Commits the active layer so the on-disk deltas include the current sculpt
    work. Without this, saving mid-sculpt (same layer, still in Sculpt Mode)
    persists the last committed delta, not the un-saved strokes.

    AutoSaveCurrentLayer no-ops unless we're in Sculpt Mode on a layered object,
    and sl.save_layer saves/restores the mode itself, so this leaves the scene
    as it found it. Wrapped defensively: a save must never be blocked by us.

    Blender's periodic auto-save does NOT call save_pre (verified on 5.1: the
    auto-save file is written without this handler firing), so no undo step or
    Sculpt->Object flip happens on the auto-save timer. The '_autosave.blend'
    guard below is pure insurance in case another Blender version does fire it
    for auto-saves -- those files always carry that suffix, which a real
    user-chosen save target never does."""
    try:
        # str() around filepath, not filepath.replace() directly: the whole body
        # sits inside a broad `except` (a save must never be blocked by us), so a
        # signature change handing us a non-string first argument would raise
        # here and silently downgrade the COMMIT -- the point of this handler --
        # to a console line, losing the un-committed strokes. Measured on 4.5:
        # save_pre receives (str, None), so this is insurance, not a fix.
        if str(filepath or "").replace("\\", "/").lower().endswith("_autosave.blend"):
            return
        # A drag that ended less than _OPACITY_REBUILD_DELAY ago still has its
        # single rebuild queued on a timer. Saving now would write a mesh that
        # lags the opacity values it is stored with, so apply it first.
        layers_utils.flush_opacity_rebuild()
        layers_utils.flush_live_opacity()
        layers_utils.AutoSaveCurrentLayer(bpy.context)
    except Exception as e:
        print(f"Sculptools: Layers could not commit before saving: {e}")


def _register_msgbus():
    """Register the msgbus subscriber for mode changes."""
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Object, "mode"),
        owner=_msgbus_owner,
        args=(),
        notify=_on_mode_change,
    )


def _unregister_msgbus():
    """Unregister the msgbus subscriber."""
    bpy.msgbus.clear_by_owner(_msgbus_owner)


def _sl_name_update(self, context):
    """Guard the editable layer name against being emptied.
    Reverts to the previous non-empty value. Does not touch the underlying
    attribute (layer_attrib_name stays SLayer_N)."""
    if not self.name.strip():
        # Reassigning triggers this callback again, but with a non-empty
        # value, so it settles after one extra call (no infinite loop).
        self.name = self.get("_prev_name", layers_utils.SL_NAME)
        return
    self["_prev_name"] = self.name


class SL_ObjectLayers(bpy.types.PropertyGroup):
    name: StringProperty(default=layers_utils.SL_ATTRIB_NAME, update=_sl_name_update)
    layer_attrib_name: StringProperty()
    # Bounded to [0, 1]: it is an opacity, and the field had no range at all, so
    # it dragged into negatives (which mirrors the layer) and past 1 (which
    # exaggerates it) -- neither is a documented feature, and negatives in
    # particular corrupt what ApplyLayerOpacity(do_divide=True) stores back.
    layer_opacity: FloatProperty(default=1.0, min=0.0, max=1.0,
                                 update=layers_utils.UpdateLayerOpacity)
    visible: BoolProperty(default=True)


class SL_AddLayer(bpy.types.Operator):
    bl_idname = "sl.add_layer"
    bl_label = "Add Layer"
    bl_description = "Add Layer."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # A mesh is required: the body dereferences active_obj.mode and
        # ShapeKeysOff reads active_obj.data.shape_keys, both of which raise on
        # None / on a non-mesh object when the operator is run from F3-search.
        active_obj = context.active_object
        if active_obj is None or active_obj.type != 'MESH':
            return False
        # Deliberately NOT _poll_editable: an object with no layers yet must
        # still be able to get its first one, and poll_has_layers would refuse
        # that. Measured 2026-08-13: without this guard the operator returned
        # FINISHED on a Dyntopo-remeshed object, appended a third layer to data
        # that can never be applied, AND re-stamped the cage -- erasing the
        # evidence that anything was wrong.
        reason = layers_utils.layers_blocked_reason(context)
        if reason is not None:
            cls.poll_message_set(reason)
            return False
        return True

    def execute(self, context):
        active_obj = context.active_object

        # Hard cap: refuse with a clear message instead of silently doing
        # nothing once every SLayer_<n> attribute slot is taken.
        existing_coll = layers_utils.parent_collection(active_obj)
        if existing_coll is not None:
            base_check = layers_utils.getBaseObject(existing_coll)
            if (base_check is not None
                    and len(base_check.sculptLayers) >= layers_utils.MAX_SCULPT_LAYERS):
                self.report(
                    {'WARNING'},
                    "Sculptools: Layers reached the maximum of "
                    f"{layers_utils.MAX_SCULPT_LAYERS} layers.")
                return {'CANCELLED'}

        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Auto-save current layer before adding a new one (preserves unsaved changes)
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
        guard.shapekeys = shapekeys_mutes
        multires_mod = layers_utils.GetMultires(context, active_obj)
        guard.multires = multires_mod
        sc_coll = layers_utils.GetSculptCollection(context, True)

        parent_col = layers_utils.parent_collection(active_obj)
        if parent_col is None:
            parent_col = layers_utils.CreateParentCollection(context, active_obj, sc_coll)
            context.scene.sculptLayers_scene_id += 1

        base_obj = layers_utils.getBaseObject(parent_col)
        if base_obj is None:
            base_obj = layers_utils.NewBaseObject(context, active_obj, parent_col, multires_mod)

        # AddSculptLayer returns None once every SLayer_<n> attribute slot is
        # taken. The cap pre-check at the top of execute() only runs when the
        # object was ALREADY linked to a data collection, so this still covers a
        # base object whose attribute set and layer list have drifted apart.
        # Ignoring the None used to leave the object linked with the active
        # index pointing at some other layer.
        if layers_utils.AddSculptLayer(base_obj) is None:
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            self.report({'WARNING'},
                        "Sculptools: Layers reached the maximum of "
                        f"{layers_utils.MAX_SCULPT_LAYERS} layers.")
            return {'CANCELLED'}

        # The whole MESH GROUP: an Alt+D copy left unlinked would see this
        # object's layers in the mesh they share, and its own first layer would
        # then start a second dataset driving the same mesh.
        layers_utils.link_mesh_group(active_obj, parent_col)
        base_obj.sculptLayers_index = len(base_obj.sculptLayers) - 1
        base_obj.sculptLayers_version = layers_utils.ADDON_VERSION_STR

        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)

        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)

        bpy.ops.object.mode_set(mode=context_mode, toggle=False)

        # Record the cage this layer data is built against, AFTER the last
        # mutation: a cancelled run must never leave a stamp describing a state
        # that was rolled back.
        layers_utils.stamp_cage_verts(active_obj, base_obj)

        return {'FINISHED'}


class SL_DuplicateLayers(bpy.types.Operator):
    bl_idname = "sl.duplicate_layers"
    bl_label = "Duplicate Layers"
    bl_description = "Duplicate Layers."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    @classmethod
    def description(cls, context, properties):
        # READ-ONLY: a tooltip is computed like a draw(). On shared data this
        # button is Blender's "make single user" -- the way a Shift+D copy, or
        # an object linked through the Data menu, gets layers of its own -- so
        # it says so there, where the Data sub-panel shows "Shared by N".
        others = len(layers_utils.other_data_users(context.active_object))
        if others:
            return ("Give the active object its own copy of the layer data. "
                    "%s using the shared one" % _others_keep(others))
        return cls.bl_description

    def execute(self, context):
        active_obj = context.active_object
        # The object is about to move to a NEW base: a queued opacity would
        # otherwise land on the old one. See flush_pending_opacity().
        layers_utils.flush_pending_opacity()
        context_mode = active_obj.mode

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = (layers_utils.getBaseObject(parent_col)
                    if parent_col is not None else None)
        if base_obj is None or not base_obj.sculptLayers:
            return {'FINISHED'}

        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        # The copy, for the object's whole MESH GROUP: its Alt+D copies share
        # the mesh, so they must move to the new data with it. On data another
        # mesh uses, this is exactly the split a first edit makes.
        layers_utils.ensure_own_data(context, active_obj, force=True)
        bpy.ops.object.mode_set(mode=context_mode, toggle=False)

        # Commit the in-progress sculpt AFTER the copy, into the object's NEW
        # data. It used to run first, which on data shared with another mesh
        # committed the strokes into every other object's layer too (found
        # 2026-10-01). The data left behind keeps what was committed before.
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        return {'FINISHED'}


class SL_RemoveLayer(bpy.types.Operator):
    bl_idname = "sl.remove_layer"
    bl_label = "Remove Layer"
    bl_description = "Remove Layer."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    def execute(self, context):
        active_obj = context.active_object
        # Removing shifts every index above this one: a queued opacity would
        # land on the layer that slides into its slot. See
        # flush_pending_opacity().
        layers_utils.flush_pending_opacity()
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)
        base_verts_len = len(base_obj.data.vertices)

        if base_obj.sculptLayers and base_obj.sculptLayers_index != -1 and base_obj.sculptLayers_index < len(base_obj.sculptLayers):
            sl = base_obj.sculptLayers[base_obj.sculptLayers_index]
            if active_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

            shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
            guard.shapekeys = shapekeys_mutes
            multires_mod = layers_utils.GetMultires(context, active_obj)
            guard.multires = multires_mod

            if multires_mod:
                verts_count = len(multires_mod[2].vertices)
            else:
                verts_count = len(active_obj.data.vertices)

            if verts_count != len(base_obj.data.vertices):
                if multires_mod:
                    multires_mod[0].levels = multires_mod[1]
                    layers_utils.OtherModsOn(multires_mod)
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                _report_topology_mismatch(self, verts_count, base_obj)
                return {'CANCELLED'}

            if len(base_obj.sculptLayers) == 1:
                if multires_mod:
                    # Shares base_obj.data on purpose -- guard.track() removes
                    # only the object, and the mesh survives on its user count.
                    temp_obj = guard.track(bpy.data.objects.new(
                        active_obj.name + '_TMP_SCULPTLAYER', base_obj.data))
                    context.scene.collection.objects.link(temp_obj)
                    bpy.ops.object.select_all(action='DESELECT')
                    temp_obj.select_set(True)
                    active_obj.select_set(True)
                    context.view_layer.objects.active = active_obj
                    bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)
                    bpy.data.objects.remove(temp_obj, do_unlink=True)
                else:
                    base_mesh_vers = np.empty(base_verts_len * 3, dtype=np.float32)
                    base_obj.data.vertices.foreach_get('co', base_mesh_vers)
                    layers_utils.WriteLiveCoords(active_obj, base_mesh_vers)
                    base_mesh_vers = None

                # Shared data (a Shift+D copy, sl.link_data) is NOT deleted: this
                # object leaves it and the others keep their layer. The mesh
                # rewind above is this object's own, so it is right either way.
                others = layers_utils.RemoveSculptLayers(context, active_obj, base_obj)
                if others:
                    self.report({'INFO'},
                                "Sculptools: Layers removed the layer from this object only; "
                                "%s it."
                                % _others_keep(others))
                base_obj = None

            if base_obj:
                base_obj.data.attributes.remove(base_obj.data.attributes[sl.layer_attrib_name])
                base_obj.sculptLayers.remove(base_obj.sculptLayers_index)

            if base_obj and base_obj.sculptLayers_index >= len(base_obj.sculptLayers):
                base_obj.sculptLayers_index = len(base_obj.sculptLayers) - 1

            if base_obj and base_obj.sculptLayers:
                layers_utils.UpdateMesh(context, active_obj, multires_mod, False)

            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)

            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            active_obj.data.update()

        return {'FINISHED'}


class SL_SaveLayer(bpy.types.Operator):
    bl_idname = "sl.save_layer"
    bl_label = "Save Layer"
    bl_description = "Save current sculpt changes on the active layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    # True when the CALLER pushed its own boundary undo step just before this
    # commit, with nothing changed in between: the push below would hold the
    # same state, and each such duplicate is one more Ctrl+Z that does nothing.
    # Passed only by the operators that push first and commit second, through
    # AutoSaveCurrentLayer(boundary_pushed=True). HIDDEN: nothing for the F9
    # panel. SKIP_SAVE: a run never inherits it from the previous one.
    boundary_pushed: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False)

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        # LOAD-BEARING, and it survived an attempt to remove it on 2026-07-31.
        # The theory was that this push is what makes Ctrl+Z skip an intermediate
        # opacity value, because _run_opacity_rebuild calls this (via
        # AutoSaveCurrentLayer) with the opacities rolled back to their previous
        # values. Gating it on `_in_opacity_update` made undo strictly WORSE:
        # this push is the step boundary between the user's last sculpt stroke
        # and the opacity commit, so without it the two merge and a single
        # Ctrl+Z reverts the opacity AND deletes the stroke. Reported from the
        # GUI the same day and reverted immediately.
        # The one exception is explicit: a caller that has just pushed the same
        # boundary itself (boundary_pushed). Measured in the GUI on 2026-10-01:
        # stroke + Add Layer left Draw Brush, TWO "Add an undo step", Add Layer
        # -- two empty Ctrl+Z. With this push skipped there: one, and undo and
        # redo both landed on consistent states. With NO boundary at all the
        # first Ctrl+Z did not even remove the layer, so one must stay.
        if active_obj.mode != 'OBJECT' and not self.boundary_pushed:
            layers_utils.boundary_undo_push(context)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if (base_obj.sculptLayers and base_obj.sculptLayers_index != -1
            and base_obj.sculptLayers_index < len(base_obj.sculptLayers)):
            sl = base_obj.sculptLayers[base_obj.sculptLayers_index]

            # The opacities the VISIBLE MESH reflects, which is what the capture
            # has to divide back out. Normally these are simply the property
            # values; during the deferred opacity commit they are the previous
            # ones, handed over by _run_opacity_rebuild rather than written back
            # onto the properties (writing them back was visible to undo).
            layer_values = layers_utils.effective_opacities(base_obj)
            active_opacity = layer_values[base_obj.sculptLayers_index]

            # A hidden or zero-opacity active layer contributes nothing to the
            # mesh, so what the capture below finds is the new strokes ALONE:
            # they are ADDED to the layer instead of replacing it, and the mesh
            # is rebuilt so it shows the stack again (the strokes go out of view
            # with the layer they now belong to). The user's decision,
            # 2026-10-05. This used to refuse ("Cannot Save Hidden Layer!"),
            # and the auto-saves skipped such a layer in silence: the strokes
            # then stayed on the mesh until a later commit stored them in
            # whatever layer was active by then -- divided by ITS opacity -- or a
            # rebuild wiped them (measured in the GUI on 4.5 and 5.2).
            additive = (not sl.visible) or active_opacity == 0

            # Sculpt strokes go into the ACTIVE shape key; layers read the
            # reference (Basis) key. With a pose key active the capture would
            # read the neutral shape and store a wrong delta.
            # WARNING, not ERROR: an ERROR report raises RuntimeError when the
            # operator is called from Python (AutoSaveCurrentLayer does).
            if not layers_utils.basis_key_active(active_obj):
                self.report({'WARNING'},
                            "Sculptools: Layers works on the Basis shape key only: "
                            "make Basis the active shape key and try again.")
                return {'CANCELLED'}

            if active_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

            shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
            guard.shapekeys = shapekeys_mutes
            multires_mod = layers_utils.GetMultires(context, active_obj)
            guard.multires = multires_mod

            if multires_mod:
                verts_count = len(multires_mod[2].vertices)
            else:
                verts_count = len(active_obj.data.vertices)


            if verts_count != len(base_obj.data.vertices):
                if multires_mod:
                    multires_mod[0].levels = multires_mod[1]
                    layers_utils.OtherModsOn(multires_mod)
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                _report_topology_mismatch(self, verts_count, base_obj)
                return {'CANCELLED'}

            if multires_mod:
                np_active_verts = np.empty(verts_count * 3, dtype=np.float32)
                multires_mod[2].vertices.foreach_get('co', np_active_verts)
            else:
                np_active_verts = layers_utils.ReadLiveCoords(active_obj, verts_count)

            np_base_verts = np.empty(verts_count * 3, dtype=np.float32)
            base_obj.data.vertices.foreach_get('co', np_base_verts)

            np_applied_verts = np.subtract(np_active_verts, np_base_verts)

            # One scratch buffer reused for every other layer (foreach_get
            # overwrites it whole), as in UpdateMesh. Bit-identical to a fresh
            # array per layer -- dev_test_update_mesh_numpy.py section C.
            np_sl_i_verts = np.empty(verts_count * 3, dtype=np.float32)
            for i, sl_i in enumerate(base_obj.sculptLayers):
                if sl_i.visible and sl_i != sl:
                    base_obj.data.attributes[sl_i.layer_attrib_name].data.foreach_get('vector', np_sl_i_verts)

                    opacity_val = layer_values[i]
                    if opacity_val != 1.0:
                        layers_utils.ApplyLayerOpacity(np_sl_i_verts, opacity_val, verts_count, False)

                    np.subtract(np_applied_verts, np_sl_i_verts, out=np_applied_verts)
            np_sl_i_verts = None

            if additive:
                # Residue of the last rebuild is not a stroke, and an additive
                # capture would add it again on every commit -- see
                # layers_utils.HIDDEN_STROKE_TOL for the measurement.
                strokes = np_applied_verts.reshape(-1, 3)
                strokes[np.linalg.norm(strokes, axis=1)
                        < layers_utils.HIDDEN_STROKE_TOL] = 0.0
                if strokes.any():
                    # Divided like a visible capture, so the strokes look the
                    # same once the layer is shown at this opacity; at opacity
                    # 0 there is nothing to divide by, and they are kept as
                    # sculpted.
                    if active_opacity not in (0.0, 1.0):
                        layers_utils.ApplyLayerOpacity(
                            np_applied_verts, active_opacity, verts_count, True)
                    att = base_obj.data.attributes[sl.layer_attrib_name]
                    np_layer = np.empty(verts_count * 3, dtype=np.float32)
                    att.data.foreach_get('vector', np_layer)
                    np.add(np_layer, np_applied_verts, out=np_layer)
                    att.data.foreach_set('vector', np_layer)
                    # The strokes are still on the mesh while the layer that
                    # now holds them is out of view: rebuild, or the next
                    # commit on another layer would take them a second time.
                    layers_utils.UpdateMesh(context, active_obj, multires_mod, False)
            else:
                if active_opacity != 1.0:
                    layers_utils.ApplyLayerOpacity(np_applied_verts, active_opacity, verts_count, True)

                base_obj.data.attributes[sl.layer_attrib_name].data.foreach_set('vector', np_applied_verts)

            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)

            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)

        return {'FINISHED'}


class SL_SetActiveLayer(bpy.types.Operator):
    bl_idname = "sl.set_active_layer"
    bl_label = "Set Active Layer"
    bl_description = "Set the active layer (auto-saves the previous one if needed)"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    index: IntProperty(options={'HIDDEN'}, default=-1)

    def execute(self, context):
        active_obj = context.active_object

        if not active_obj:
            return {'CANCELLED'}

        # Split shared data BEFORE resolving base_obj: the auto-save below would
        # split it anyway, and the index would then land on the data the object
        # just left. See layers_utils.ensure_own_data().
        layers_utils.ensure_own_data(context, active_obj)
        parent_col = layers_utils.parent_collection(active_obj)
        if parent_col is None:
            return {'CANCELLED'}

        base_obj = layers_utils.getBaseObject(parent_col)

        if not base_obj or not base_obj.sculptLayers:
            return {'CANCELLED'}

        # Validate target index
        if self.index < 0 or self.index >= len(base_obj.sculptLayers):
            return {'CANCELLED'}

        current_index = base_obj.sculptLayers_index

        # If clicking the already active layer, do nothing
        if self.index == current_index:
            return {'FINISHED'}

        # Auto-save current layer before switching (only in Sculpt Mode)
        if active_obj.mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context)

        # Now switch to the new layer
        base_obj.sculptLayers_index = self.index

        return {'FINISHED'}


class SL_ChangeVisibility(bpy.types.Operator):
    bl_idname = "sl.change_visibility"
    bl_label = "Change Visibility"
    bl_description = ("Change layer visibility.\n"
                      "Shift+Click: isolate this layer (Shift+Click again to reveal all)")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    index: IntProperty(options={'HIDDEN'}, default=-1)
    # Set from event.shift in invoke(). SKIP_SAVE because bl_options {'UNDO'}
    # makes Blender remember the last used property values -- without it a plain
    # click after a Shift+Click would inherit isolate=True.
    isolate: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'}, default=False)

    def invoke(self, context, event):
        # Operator buttons deliver a real event, so this is the modifier state at
        # click time. Python and F3-search never reach invoke() and keep the
        # False default, i.e. the plain toggle this operator has always been.
        self.isolate = event.shift
        return self.execute(context)

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        # Validate BEFORE touching anything, so the early return has nothing to
        # undo. The UI always fills `index` in, but F3-search / Python leave it
        # at its default -1, which bpy_prop_collection resolves to the LAST
        # layer -- silently toggling the top row -- while an out-of-range value
        # raises. Mirrors the guard sl.set_active_layer already has.
        if self.index < 0 or self.index >= len(base_obj.sculptLayers):
            self.report({'INFO'}, "Sculptools: Layers got no layer index.")
            return {'CANCELLED'}
        sl = base_obj.sculptLayers[self.index]

        # The boundary step FIRST, then the commit, as in every operator that
        # commits: it used to come after the commit, so the commit's own push
        # and this one were two steps holding the same visible state.
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Auto-save current layer before changing visibility (preserves unsaved changes)
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
        guard.shapekeys = shapekeys_mutes
        multires_mod = layers_utils.GetMultires(context, active_obj)
        guard.multires = multires_mod

        if multires_mod:
            verts_count = len(multires_mod[2].vertices)
        else:
            verts_count = len(active_obj.data.vertices)

        if verts_count != len(base_obj.data.vertices):
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            _report_topology_mismatch(self, verts_count, base_obj)
            return {'CANCELLED'}

        # Isolate is modal: the stored flag is what separates "isolated, then a
        # couple of layers re-enabled by hand" from "nothing isolated, one layer
        # hidden by hand" -- the same visible flags, two different meanings. A
        # plain click deliberately neither reads nor writes it.
        # Every layer's flag is written here, BEFORE the single UpdateMesh below,
        # so an isolate costs one rebuild and one undo step, like a plain toggle.
        layers = base_obj.sculptLayers
        if self.isolate and len(layers) >= 2:
            if base_obj.get(layers_utils.ISOLATE_PROP, False) and sl.visible:
                # Leaving isolate: reveal everything, including layers the user
                # had hidden by hand before the isolate started.
                for sl_i in layers:
                    sl_i.visible = True
                base_obj[layers_utils.ISOLATE_PROP] = False
            else:
                # Entering (or re-targeting) isolate.
                for i, sl_i in enumerate(layers):
                    sl_i.visible = (i == self.index)
                base_obj[layers_utils.ISOLATE_PROP] = True
                # Never leave the active layer hidden: the next strokes would
                # go into a layer out of view (sl.save_layer captures a hidden
                # layer additively since 2026-10-05; it used to drop them).
                # AutoSaveCurrentLayer already ran at the top of this operator,
                # so the outgoing layer's strokes are safe. Reveal does not need
                # this -- it leaves nothing hidden.
                base_obj.sculptLayers_index = self.index
        else:
            sl.visible = not sl.visible

        layers_utils.UpdateMesh(context, active_obj, multires_mod, False)

        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)

        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
        bpy.ops.object.mode_set(mode=context_mode, toggle=False)
        active_obj.data.update()

        return {'FINISHED'}


# ----------------------------------------------------------------------
# Morph session (graded erase)
#
# One workflow for BOTH Multires and plain objects: a temporary dense copy of
# the visible mesh, the original hidden, and Blender's Layer brush on the copy
# with its persistent base armed so that painting erases the ACTIVE layer
# gradually (layers_utils.arm_morph_session). Apply Morph adds what changed on
# the copy to that layer; every other layer is untouched by construction.
#
# The copy is required: the Layer brush's persistent base does nothing on a
# Multires grid, and Erase Multires Displacement pulls toward the cage's limit
# surface, i.e. erases every layer (measured; CLAUDE.md, the Morph bullet).
# ----------------------------------------------------------------------

def _cleanup_morph_objects(context):
    """Remove any lingering Morph copies (an interrupted session), un-hide
    their source objects and give the Layer brush back."""
    for o in list(bpy.data.objects):
        if o.get(layers_utils.MORPH_SRC_PROP):
            _disarm_layer_brush(_saved_layer_brush(o))
            src = bpy.data.objects.get(o.get(layers_utils.MORPH_SRC_PROP))
            if src:
                src.hide_set(False)
            me = o.data
            bpy.data.objects.remove(o, do_unlink=True)
            if me and me.users == 0:
                bpy.data.meshes.remove(me)


def _start_morph_session(context, active_obj, base_obj, parent_col, idx):
    """Build the temporary copy of the visible mesh (grid resolution when
    Multires), hide the original, arm the copy and the Layer brush, and leave
    the user sculpting on the copy. Returns True on success."""
    _cleanup_morph_objects(context)
    context_mode = active_obj.mode

    # The evaluated mesh must be read in OBJECT mode (with Multires, Sculpt Mode
    # returns the base mesh).
    if active_obj.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

    shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
    # GetMultires (when Multires is present) sets it to its top level and turns
    # OFF other modifiers; without Multires we turn other modifiers off here so
    # the evaluated mesh is the plain layer mesh at base resolution.
    multires_mod = layers_utils.GetMultires(context, active_obj)
    other_mods = None
    if multires_mod is None:
        other_mods = layers_utils.OtherModsOff(active_obj)

    # try/finally: new_from_object can fail, and the restore below is the ONLY
    # thing that un-mutes the shape keys and switches the other modifiers back
    # on. Leaving those off would look like the object silently lost its setup.
    try:
        depsgraph = context.evaluated_depsgraph_get()
        eval_obj = active_obj.evaluated_get(depsgraph)
        tmp_mesh = bpy.data.meshes.new_from_object(eval_obj)
    finally:
        # Restore Multires level / other modifiers on the original.
        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)
        elif other_mods:
            for mod in other_mods.values():
                mod.show_viewport = True
        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)

    if len(tmp_mesh.vertices) != len(base_obj.data.vertices):
        bpy.data.meshes.remove(tmp_mesh)
        # A raw vertex mismatch on a Multires object means the layer base is
        # desynced from a natively-changed Multires; flag it so the panel shows
        # the Sync Multires repair (mirrors the old persistent-base arm).
        if multires_mod is not None:
            base_obj["sculptLayers_mr_desync"] = True
        bpy.ops.object.mode_set(mode=context_mode, toggle=False)
        print("Sculptools: Layers could not start the Morph Brush: "
              "vertex count mismatch")
        return False

    tmp_obj = bpy.data.objects.new(
        active_obj.name + layers_utils.MORPH_OBJ_SUFFIX, tmp_mesh)
    tmp_obj.matrix_world = active_obj.matrix_world.copy()
    context.scene.collection.objects.link(tmp_obj)

    # Swap to the copy.
    active_obj.hide_set(True)
    bpy.ops.object.select_all(action='DESELECT')
    tmp_obj.select_set(True)
    context.view_layer.objects.active = tmp_obj
    bpy.ops.object.mode_set(mode='SCULPT', toggle=False)

    # Let Blender create the persistent-base attributes the way the Layer brush
    # expects them, then overwrite them. Without a 3D View (--background)
    # arm_morph_session creates them itself.
    window, area = _find_view3d(context)
    if area is not None:
        try:
            with context.temp_override(window=window, area=area):
                bpy.ops.sculpt.set_persistent_base()
        except Exception as e:
            print(f"Sculptools: Layers could not set the persistent base: {e}")
    layers_utils.arm_morph_session(tmp_obj, active_obj, parent_col.name, base_obj, idx)

    _capture_pre_morph_brush(context, tmp_obj)
    brush = _activate_layer_brush(context)
    if brush is None:
        # Everything above already happened; tear it down, as Cancel would,
        # instead of leaving the user on a copy with "Could not start".
        bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        _finish_morph_session(context, tmp_obj, active_obj, rebuild=False)
        return False
    _arm_layer_brush(brush, tmp_obj)
    _start_strength(context, brush, tmp_obj)
    _start_morph_watch()
    return True


def _finish_morph_session(context, tmp_obj, src_obj, rebuild):
    """Remove the Morph copy, restore the original object (optionally
    rebuilding its visible mesh from the layer deltas), re-enter Sculpt Mode on
    it, then give the Layer brush and the previous brush back -- in Sculpt Mode,
    where the brush's `direction` can be set (see _set_brush_settings)."""
    prev = tmp_obj.get(layers_utils.MORPH_PREV_BRUSH_PROP)
    prev = prev.to_dict() if prev is not None else None
    saved = _saved_layer_brush(tmp_obj)
    me = tmp_obj.data
    bpy.data.objects.remove(tmp_obj, do_unlink=True)
    if me and me.users == 0:
        bpy.data.meshes.remove(me)

    src_obj.hide_set(False)
    bpy.ops.object.select_all(action='DESELECT')
    src_obj.select_set(True)
    context.view_layer.objects.active = src_obj

    if rebuild:
        # Runs in OBJECT mode (caller guarantees it): UpdateMesh writes vertices.
        shapekeys_mutes = layers_utils.ShapeKeysOff(src_obj)
        multires_mod = layers_utils.GetMultires(context, src_obj)
        layers_utils.UpdateMesh(context, src_obj, multires_mod, False)
        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)
        layers_utils.ShapeKeysOn(src_obj, shapekeys_mutes)
        src_obj.data.update()

    if src_obj.mode != 'SCULPT':
        bpy.ops.object.mode_set(mode='SCULPT', toggle=False)
    _disarm_layer_brush(saved)
    _restore_pre_morph_brush(context, prev)


def _morph_reconcile():
    """Bring an open Morph session back in step after undo / redo / file load,
    or give the Layer brush back once no session is left. Idempotent.

    Runs from a one-shot timer, never from the handlers (they only arm it).
    Writes ONLY the Morph copy's persistent-base attributes and the Layer
    brush's three settings: never layer data, never positions, no mode switch,
    no undo push. This is NOT the 2026-07-31 undo_post mistake -- that one
    rebuilt the mesh from the layers while the undo system was still walking
    the stack."""
    obj = bpy.context.active_object
    if obj is not None and layers_utils.get_morph_mask_source(obj) is not None:
        layers_utils.resync_morph_disp(obj)
        saved = obj.get(layers_utils.MORPH_LAYER_BRUSH_PROP)
        brush = _brush_by_name_full(saved.get("brush")) if saved is not None else None
        if brush is not None:
            _arm_layer_brush(brush, obj)
        _start_morph_watch()
        return
    if not any(o.get(layers_utils.MORPH_SRC_PROP) for o in bpy.data.objects):
        _disarm_layer_brush()


def _morph_reconcile_timer():
    """One-shot. unregister() disarms it: a registered timer outlives the
    add-on."""
    try:
        _morph_reconcile()
    except Exception as e:
        print(f"Sculptools: Layers could not reconcile the Morph session: {e}")
    return None


_MORPH_WATCH_INTERVAL = 0.5


def _layer_brush_armed(brush):
    """READ-ONLY. Does *brush* carry the Morph settings?"""
    for k, want in layers_utils.MORPH_BRUSH_SETTINGS.items():
        have = getattr(brush, k)
        if isinstance(want, float):
            if abs(float(have) - want) > 1e-6:
                return False
        elif have != want:
            return False
    return True


def _morph_watch_timer():
    """Keeps the Layer brush armed for as long as a Morph session is open;
    stops itself when none is left. unregister() disarms it.

    Measured 2026-10-06 on 4.5.13 with the user's startup file: the Essentials
    library finishes loading ~1 s after the first brush activation of a fresh
    instance and REPLACES the linked brush datablocks, edits lost (and the
    first activation there returns CANCELLED). A session started in that window
    went on painting with an unarmed Layer brush, which RAISES the layer
    instead of erasing it. It also undoes a direction flipped by hand (SUBTRACT
    overshoots and brings the layer back inverted). Cheap -- a name lookup and
    three reads -- and it never touches positions or layer data."""
    try:
        if not any(o.get(layers_utils.MORPH_SRC_PROP) for o in bpy.data.objects):
            return None
        obj = bpy.context.active_object
        if (obj is not None and obj.mode == 'SCULPT'
                and layers_utils.get_morph_mask_source(obj) is not None):
            saved = obj.get(layers_utils.MORPH_LAYER_BRUSH_PROP)
            brush = _brush_by_name_full(saved.get("brush")) if saved is not None else None
            if brush is not None and not _layer_brush_armed(brush):
                _arm_layer_brush(brush, obj)
    except Exception as e:
        print(f"Sculptools: Layers could not re-arm the Morph Brush: {e}")
    return _MORPH_WATCH_INTERVAL


def _start_morph_watch():
    if not bpy.app.timers.is_registered(_morph_watch_timer):
        bpy.app.timers.register(_morph_watch_timer, first_interval=_MORPH_WATCH_INTERVAL)


@bpy.app.handlers.persistent
def _on_morph_undo_redo(*args, **kwargs):
    """undo_post / redo_post: ONLY arms the reconcile timer. A sculpt undo
    restores positions but not the Layer brush's per-point factor
    (layers_utils.resync_morph_disp), and writing from inside the handler would
    run while the undo system is still restoring."""
    if not bpy.app.timers.is_registered(_morph_reconcile_timer):
        bpy.app.timers.register(_morph_reconcile_timer, first_interval=0.0)


class SL_MorphBrush(bpy.types.Operator):
    bl_idname = "sl.morph_brush"
    bl_label = "Morph Brush"
    bl_description = ("Erase part of the active layer by painting: a stroke "
                      "removes it gradually at the brush's strength, Ctrl+stroke "
                      "restores it. Apply Morph keeps the result")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    def execute(self, context):
        active_obj = context.active_object

        if active_obj.mode != 'SCULPT':
            self.report({'INFO'}, "Sculptools: Layers runs the Morph Brush in Sculpt Mode only.")
            return {'CANCELLED'}

        # Split shared data BEFORE resolving the collection the session records:
        # the commit below would split it anyway, after parent_col was read.
        layers_utils.ensure_own_data(context, active_obj)
        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)
        idx = base_obj.sculptLayers_index
        if idx < 0 or idx >= len(base_obj.sculptLayers):
            self.report({'INFO'}, "Sculptools: Layers found no active layer.")
            return {'CANCELLED'}
        if not base_obj.sculptLayers[idx].visible:
            self.report({'INFO'}, "Sculptools: Layers needs the active layer visible.")
            return {'CANCELLED'}
        # At opacity 0 none of the layer is on screen, so there is nothing to
        # erase -- and Apply divides the change by this opacity.
        if base_obj.sculptLayers[idx].layer_opacity <= 0.0:
            self.report({'INFO'}, "Sculptools: Layers has nothing to erase: the active "
                                  "layer's opacity is 0.")
            return {'CANCELLED'}

        # 1. Commit current sculpt into the active layer.
        layers_utils.AutoSaveCurrentLayer(context)

        # 2. Start the session (same workflow with or without Multires).
        if not _start_morph_session(context, active_obj, base_obj, parent_col, idx):
            if base_obj.get("sculptLayers_mr_desync"):
                self.report({'WARNING'},
                            "Sculptools: Layers found the Multires out of sync: "
                            "use 'Sync Multires' in the panel.")
            else:
                # The one other way to get here: the Layer brush did not
                # activate -- a fresh 4.5 instance answers CANCELLED to the
                # very first Essentials activation (measured 2026-10-06).
                self.report({'WARNING'},
                            "Sculptools: Layers could not activate the Layer "
                            "brush; try again.")
            return {'CANCELLED'}
        self.report({'INFO'}, "Sculptools: Layers started the Morph Brush: paint "
                    "to erase the active layer, Ctrl restores it.")
        return {'FINISHED'}


class SL_ApplyMorph(bpy.types.Operator):
    bl_idname = "sl.apply_morph"
    bl_label = "Apply Morph"
    bl_description = "Keep what the Morph Brush erased in the active layer"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Deliberately mode-agnostic (same as sl.cancel_morph): execute() reads
        # the copy in OBJECT mode anyway and switches there itself. Requiring
        # SCULPT greyed Apply out for a user who had pressed Tab mid-session.
        obj = context.active_object
        return (obj is not None
                and layers_utils.get_morph_mask_source(obj) is not None)

    def execute(self, context):
        tmp_obj = context.active_object
        # A session started by an older build has no P0 to compare with.
        # Refused BEFORE the boundary, and left open for Cancel.
        if tmp_obj.data.attributes.get(layers_utils.MORPH_P0_ATTR) is None:
            self.report({'WARNING'},
                        "Sculptools: Layers cannot apply a Morph session from an "
                        "older version: Cancel and start again.")
            return {'CANCELLED'}
        # The boundary step, as in every other operator: it records the session
        # as it stands, so one Ctrl+Z after Apply reopens it to refine. Without
        # it the first Ctrl+Z after Apply did nothing -- it landed on the last
        # sculpt step of the copy this operator deletes -- and the second
        # reopened the session with the work gone. Measured with real input
        # events on 5.2, 2026-10-05 (Undo History screenshots).
        if tmp_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)
        src_name = layers_utils.get_morph_mask_source(tmp_obj)
        idx = tmp_obj.get(layers_utils.MORPH_INDEX_PROP)
        src_obj = bpy.data.objects.get(src_name) if src_name else None

        # Write into the SOURCE's own data, resolved now -- never through the
        # collection name recorded when the session started. Data still shared
        # with another mesh is split first (ensure_own_data); the recorded name
        # would have changed the layer in every object sharing it.
        coll = None
        if src_obj is not None:
            layers_utils.ensure_own_data(context, src_obj)
            coll = layers_utils.parent_collection(src_obj)
        if coll is None:
            _cleanup_morph_objects(context)
            self.report({'WARNING'}, "Sculptools: Layers lost the Morph source "
                                      "object; nothing applied.")
            return {'CANCELLED'}

        base_obj = layers_utils.getBaseObject(coll)

        # P1 is read off the copy's mesh in OBJECT mode.
        if tmp_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        if (base_obj is None or idx is None
                or idx < 0 or idx >= len(base_obj.sculptLayers)):
            _finish_morph_session(context, tmp_obj, src_obj, rebuild=False)
            self.report({'WARNING'}, "Sculptools: Layers lost the active layer; "
                                      "nothing applied.")
            return {'CANCELLED'}

        changed = layers_utils.apply_morph_delta(tmp_obj, base_obj, idx)
        if changed is None:
            _finish_morph_session(context, tmp_obj, src_obj, rebuild=False)
            self.report({'WARNING'},
                        "Sculptools: Layers found layer data that does not match "
                        "the Morph copy; nothing applied.")
            return {'CANCELLED'}
        if changed == 0.0:
            # FINISHED, not CANCELLED: closing the session is a change of
            # state, and Blender records an end step only for FINISHED.
            _finish_morph_session(context, tmp_obj, src_obj, rebuild=False)
            self.report({'INFO'}, "Sculptools: Layers closed the Morph session: "
                                  "nothing was erased.")
            return {'FINISHED'}

        # Rebuild the visible mesh on the original object and return to it.
        _finish_morph_session(context, tmp_obj, src_obj, rebuild=True)
        return {'FINISHED'}


class SL_CancelMorph(bpy.types.Operator):
    bl_idname = "sl.cancel_morph"
    bl_label = "Cancel Morph"
    bl_description = "Discard the Morph Brush strokes and return to the object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (obj is not None
                and layers_utils.get_morph_mask_source(obj) is not None)

    def execute(self, context):
        tmp_obj = context.active_object
        # Same boundary as Apply Morph, same measured defect without it: the
        # first Ctrl+Z after Cancel did nothing and the second reopened the
        # session with the work gone. With it, one Ctrl+Z undoes a Cancel
        # clicked by mistake.
        if tmp_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)
        src_obj = bpy.data.objects.get(
            layers_utils.get_morph_mask_source(tmp_obj) or "")
        if tmp_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        if src_obj is not None:
            _finish_morph_session(context, tmp_obj, src_obj, rebuild=False)
        else:
            _cleanup_morph_objects(context)
        return {'FINISHED'}


class SL_SyncMultires(bpy.types.Operator):
    bl_idname = "sl.sync_multires"
    bl_label = "Sync Multires"
    bl_description = ("Rebuild layer data to match a natively-added Multires, "
                      "preserving the current sculpt")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not layers_utils.poll_has_layers(context):
            return False
        obj = context.active_object
        if layers_utils.find_multires_readonly(obj) is None:
            return False
        # It switches modes and reads the evaluated grid: neither works on a
        # hidden object (layers_utils.object_hidden).
        if layers_utils.object_hidden(obj):
            cls.poll_message_set("The object is hidden - show it to edit layers")
            return False
        parent_col = layers_utils.parent_collection(obj)
        base_obj = layers_utils.getBaseObject(parent_col) if parent_col else None
        return base_obj is not None and layers_utils.multires_out_of_sync(obj, base_obj)

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        # This operator forces the Multires' viewport settings, removes the
        # modifier or deletes its top levels, runs sl.subdivide_layers in a loop
        # and reshapes the grid. A raise anywhere in there used to leave the user
        # in Object Mode with the Multires forced to full display -- see
        # layers_utils.mutation_guard, and restore_state() below for the part the
        # guard cannot know about.
        #
        # multires_sync_active() suspends the out-of-sync refusal that
        # layers_blocked_reason() otherwise raises -- this operator is the repair
        # for that very state, and step 3 calls bpy.ops.sl.subdivide_layers(),
        # which would fail its own poll() without it. Outermost, so it is still
        # in force while mutation_guard runs its rollback.
        with layers_utils.multires_sync_active(), \
                layers_utils.mutation_guard(context, active_obj,
                                            context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        idx = base_obj.sculptLayers_index
        if idx < 0 or idx >= len(base_obj.sculptLayers):
            # Nothing has been touched yet, so there is nothing to put back.
            self.report({'INFO'}, "Sculptools: Layers found no active layer.")
            return {'CANCELLED'}

        # The boundary every mutating operator pushes first, while the state is
        # still the user's. Without it, a Sync clicked right after a stroke was
        # undone onto the stroke's Sculpt step, which reloads no Object data:
        # measured 2026-10-07 (dev_gui_sync_undo.py, 4.5 and 5.2), Ctrl+Z
        # never reached "before the Sync, with my stroke" -- two dead presses,
        # then the native Multires came back together with the stroke gone.
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        active_sl = base_obj.sculptLayers[idx]
        vis_before = active_sl.visible
        if not active_sl.visible:
            active_sl.visible = True

        # 1. Object Mode + snapshot the current sculpt at full grid resolution.
        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)
        native_mr = layers_utils.find_multires_readonly(active_obj)
        native_total = native_mr.total_levels
        mr_show_before = native_mr.show_viewport
        mr_levels_before = native_mr.levels

        # Everything this operator changes before it becomes destructive is
        # reversible, and every early exit -- including an exception, via
        # guard.on_failure -- has to reverse it. Without this the "Cannot read the
        # Multires grid" refusal left the user in Object Mode with their Multires
        # forced to full viewport display: a refusal that changed the scene.
        # Past the point marked `phase['destructive']` there is no way back from
        # Python (a removed Multires and deleted top levels are gone), which is
        # why bail() then says "undo to recover" and returns FINISHED -- there
        # the mode is all that can honestly be restored.
        phase = {"destructive": False}

        def restore_state():
            if phase["destructive"]:
                return
            native_mr.show_viewport = mr_show_before
            native_mr.levels = mr_levels_before
            active_sl.visible = vis_before

        guard.on_failure(restore_state)

        def bail(msg, level='ERROR'):
            """Report and stop, having put back everything still restorable.

            CANCELLED only while nothing irreversible has happened. Past
            phase['destructive'] the Multires is already changed, and CANCELLED
            would tell Blender that nothing happened: no end step, so Ctrl+Z
            could not reach the change. FINISHED records it, and the message
            says so -- "Undo to recover" only where there is something to undo,
            or Ctrl+Z would take back the user's previous action instead."""
            restore_state()
            if active_obj.mode != context_mode:
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            if phase["destructive"]:
                self.report({level}, msg + " The Multires was already changed; "
                                           "undo to recover.")
                return {'FINISHED'}
            self.report({level}, msg)
            return {'CANCELLED'}

        # Force full-resolution evaluation before reading the grid: the viewport
        # 'levels' may be below total_levels (or show_viewport off), which would
        # otherwise make evaluated_get() return the base mesh -> a degenerate
        # count. Both must be corrected for a faithful snapshot.
        native_mr.show_viewport = True
        native_mr.levels = native_total
        context.view_layer.update()
        depsgraph = context.evaluated_depsgraph_get()
        eval_mesh = active_obj.evaluated_get(depsgraph).data
        native_grid_count = len(eval_mesh.vertices)

        # Validate BEFORE any destructive change: the grid must be a real
        # subdivision of the base. If the read is still degenerate, abort with
        # NO data loss (the Multires is left untouched).
        if native_grid_count == len(base_obj.data.vertices):
            # Grid and base already agree, so there is nothing to rebuild: this
            # is the state left behind by a Multires whose show_viewport was
            # switched off (or whose viewport 'levels' were lowered). Forcing
            # both back on above IS the entire repair -- while the toggle was
            # off, GetMultires saw no Multires at all and every layer write
            # silently took the plain-mesh path. Re-stamp to clear the alert.
            layers_utils.stamp_multires_levels(active_obj, base_obj)
            layers_utils.stamp_cage_verts(active_obj, base_obj)
            if active_obj.mode != context_mode:
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            self.report({'INFO'},
                        "Sculptools: Layers restored the Multires viewport "
                        "display; the layer data already matches the grid.")
            return {'FINISHED'}
        if native_grid_count < len(base_obj.data.vertices):
            return bail("Sculptools: Layers cannot read the Multires grid. "
                        "Raise 'Levels Viewport' to its max and try again.")

        user_work = np.empty(native_grid_count * 3, dtype=np.float32)
        eval_mesh.vertices.foreach_get('co', user_work)

        window, area = _find_view3d(context)

        # 2. Bring the base into coherence with the Multires so the add-on SubD
        # can rebuild from a known state. Two desync shapes are handled:
        #   (a) base still at cage res with a Multires on top (Multires added
        #       natively, the add-on SubD never used) -> drop the Multires and
        #       rebuild the whole stack from the cage.
        #   (b) base already subdivided by the add-on with extra levels added
        #       natively on top (native Ctrl+3) -> trim the surplus top levels
        #       until the Multires grid matches the base, then rebuild.
        # The snapshot above already preserved the full-res sculpt, so trimming
        # top levels is lossless. Both shapes then share the rebuild / reshape /
        # capture steps below.
        if len(base_obj.data.vertices) == len(active_obj.data.vertices):
            phase["destructive"] = True
            active_obj.modifiers.remove(native_mr)
        else:
            trim_guard = 0
            while trim_guard <= native_total + 1:
                mr = layers_utils.find_multires_readonly(active_obj)
                if mr is None:
                    break
                mr.show_viewport = True
                mr.levels = mr.total_levels
                context.view_layer.update()
                depsgraph = context.evaluated_depsgraph_get()
                grid_now = len(active_obj.evaluated_get(depsgraph).data.vertices)
                if grid_now == len(base_obj.data.vertices):
                    break
                if grid_now < len(base_obj.data.vertices):
                    return bail("Sculptools: Layers could not sync: "
                                "unexpected topology.")
                phase["destructive"] = True
                with context.temp_override(window=window, area=area,
                                           active_object=active_obj,
                                           object=active_obj):
                    mr.levels = mr.total_levels - 1
                    bpy.ops.object.multires_higher_levels_delete(modifier=mr.name)
                trim_guard += 1
            else:
                return bail("Sculptools: Layers could not sync: the "
                            "Multires levels do not reconcile.")

        # 3. Rebuild coherently: add-on SubD until base matches the native grid.
        # SubD rewrites the base geometry and the stored deltas, so from here the
        # operator is past the point of no return either way.
        phase["destructive"] = True
        cap = native_total + 2
        it = 0
        while len(base_obj.data.vertices) < native_grid_count and it < cap:
            with context.temp_override(window=window, area=area,
                                       active_object=active_obj, object=active_obj):
                bpy.ops.sl.subdivide_layers()
            it += 1
        if len(base_obj.data.vertices) != native_grid_count:
            return bail("Sculptools: Layers could not sync: "
                        "unexpected topology.")

        # 4. Reshape the (now add-on-managed) Multires to the snapshot.
        mr = layers_utils.find_multires_readonly(active_obj)
        if mr is None:
            return bail("Sculptools: Layers could not sync: the "
                        "rebuild produced no Multires.")
        mr.levels = mr.total_levels
        _reshape_multires_to(context, active_obj, mr, user_work)

        # 5. Capture the snapshot into the active layer. boundary_pushed=True
        # whatever mode the Sync started in: sl.save_layer's own step would
        # record a half-synced state (base rebuilt, the sculpt on the mesh but
        # not yet in the layer), and the first Ctrl+Z after a Sync landed on it
        # -- measured 2026-10-07. The boundary above holds the state before.
        bpy.ops.object.mode_set(mode='SCULPT', toggle=False)
        layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        # 6. Stamp coherence, clear flag.
        layers_utils.stamp_multires_levels(active_obj, base_obj)
        layers_utils.stamp_cage_verts(active_obj, base_obj)

        if active_obj.mode != context_mode:
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)

        self.report({'INFO'},
                    "Sculptools: Layers synced the Multires and captured "
                    "the current sculpt into layer '%s'." % active_sl.name)
        return {'FINISHED'}


class SL_MoveLayer(bpy.types.Operator):
    bl_idname = "sl.move_layer"
    bl_label = "Move Layer"
    bl_description = "Move the active layer up or down"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    @classmethod
    def description(cls, context, properties):
        # Both arrow buttons share this operator; give each a distinct tooltip.
        if properties.action == 'UP':
            return "Move the active layer up"
        return "Move the active layer down"

    # HIDDEN so the F9 "Adjust Last Operation" panel has nothing to offer. F9
    # re-runs execute() against the OUTPUT of the previous run, not the state it
    # started from (shared rule §11.1), and this operator is relative: measured
    # on 4.5 and 5.2, UP followed by an F9 switch to DOWN lands back on the
    # ORIGINAL position instead of one below it -- F9 undoes rather than
    # corrects. Nothing is corrupted, but the panel is a lie. The two arrow
    # buttons set this from Python, which HIDDEN does not prevent.
    # See dev_probe_redo_panel.py for the measurement.
    action: EnumProperty(items=(('UP', "Up", ""), ('DOWN', "Down", "")),
                         options={'HIDDEN'})

    def execute(self, context):
        active_obj = context.active_object
        # Moving swaps two indices: a queued opacity would land on the other
        # layer. See flush_pending_opacity().
        layers_utils.flush_pending_opacity()
        # After the flush (whose record names the data as it stands), before
        # anything is written. See layers_utils.ensure_own_data().
        layers_utils.ensure_own_data(context, active_obj)

        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if base_obj.sculptLayers and base_obj.sculptLayers_index != -1 and base_obj.sculptLayers_index < len(base_obj.sculptLayers):
            index = base_obj.sculptLayers_index

            # The layer list is drawn in REVERSE index order (ui.py: the topmost
            # row is the highest index), so "up" in the panel means moving the
            # active layer to a HIGHER index and "down" to a lower one.
            if self.action == 'UP' and index < len(base_obj.sculptLayers) - 1 and len(base_obj.sculptLayers) > 1:
                base_obj.sculptLayers.move(index, index + 1)
                base_obj.sculptLayers_index += 1
            elif self.action == 'DOWN' and index >= 1 and len(base_obj.sculptLayers) > 1:
                base_obj.sculptLayers.move(index, index - 1)
                base_obj.sculptLayers_index -= 1

        return {'FINISHED'}


class SL_MergeLayer(bpy.types.Operator):
    bl_idname = "sl.merge_layer"
    bl_label = "Merge Down"
    bl_description = "Merge the current layer to the one below it"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        if not _poll_editable(cls, context):
            return False
        # READ-ONLY. The two refusals execute() makes, moved up here so the
        # button greys out and says why in its tooltip: a click on the bottom
        # layer, or with a hidden layer involved, used to be answered by an
        # INFO line in the status bar and nothing else (2026-10-05 usage pass).
        # A stale index is left to execute(), which reports it.
        base_obj = layers_utils.getBaseObject(
            layers_utils.parent_collection(context.active_object))
        index = base_obj.sculptLayers_index
        if index == 0:
            cls.poll_message_set("No layer below to merge with")
            return False
        if 0 < index < len(base_obj.sculptLayers) and not (
                base_obj.sculptLayers[index].visible
                and base_obj.sculptLayers[index - 1].visible):
            cls.poll_message_set("Both layers must be visible to merge")
            return False
        return True

    def execute(self, context):
        active_obj = context.active_object
        # Merging removes a layer and bakes the opacities in: a queued opacity
        # must land first, or it shifts onto another index AND misses the bake.
        # See flush_pending_opacity().
        layers_utils.flush_pending_opacity()
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Auto-save current layer before merging (preserves unsaved changes)
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if not base_obj.sculptLayers:
            self.report({'INFO'}, "Sculptools: Layers found no layers on this object.")
            return {'CANCELLED'}
        index = base_obj.sculptLayers_index
        # A stale index (a script, an older file) must be refused BEFORE it is
        # used: the range check used to sit after sculptLayers[index], where it
        # could never fail -- the indexing raised IndexError first. Same check
        # and wording as the sibling operators.
        if index < 0 or index >= len(base_obj.sculptLayers):
            self.report({'INFO'}, "Sculptools: Layers found no active layer.")
            return {'CANCELLED'}
        # Guard: cannot merge the bottom layer (no layer below it).
        # Without this check, index-1 would wrap to -1 and merge with the top layer.
        if index == 0:
            self.report({'INFO'}, "Sculptools: Layers has no layer below to merge with.")
            return {'CANCELLED'}
        sl = base_obj.sculptLayers[index]
        sl_prev = base_obj.sculptLayers[index - 1]

        if sl_prev.visible and sl.visible:
            if active_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

            shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
            guard.shapekeys = shapekeys_mutes
            multires_mod = layers_utils.GetMultires(context, active_obj)
            guard.multires = multires_mod

            if multires_mod:
                verts_count = len(multires_mod[2].vertices)
            else:
                verts_count = len(active_obj.data.vertices)


            if verts_count != len(base_obj.data.vertices):
                if multires_mod:
                    multires_mod[0].levels = multires_mod[1]
                    layers_utils.OtherModsOn(multires_mod)
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                _report_topology_mismatch(self, verts_count, base_obj)
                return {'CANCELLED'}

            if len(base_obj.sculptLayers) > 1:
                np_sl_verts = np.empty(verts_count * 3, dtype=np.float32)
                base_obj.data.attributes[sl.layer_attrib_name].data.foreach_get('vector', np_sl_verts)

                opacity_val = sl.layer_opacity
                if opacity_val != 1.0:
                    layers_utils.ApplyLayerOpacity(np_sl_verts, opacity_val, verts_count, False)

                np_sl_prev_verts = np.empty(verts_count * 3, dtype=np.float32)
                base_obj.data.attributes[sl_prev.layer_attrib_name].data.foreach_get('vector', np_sl_prev_verts)

                opacity_val = sl_prev.layer_opacity
                if opacity_val != 1.0:
                    layers_utils.ApplyLayerOpacity(np_sl_prev_verts, opacity_val, verts_count, False)

                np.add(np_sl_prev_verts, np_sl_verts, out=np_sl_prev_verts)
                base_obj.data.attributes[sl_prev.layer_attrib_name].data.foreach_set('vector', np_sl_prev_verts)
                # The opacity is already baked into the delta above, so this
                # is bookkeeping, not a user edit. Assigning an RNA property
                # from Python fires its update callback even for an unchanged
                # value, and UpdateLayerOpacity -> UpdateMesh would rebuild
                # the mesh mid-operator (with the fused delta counted twice,
                # plus a full multires_reshape on Multires). Suppress it: the
                # rebuild below produces the correct final state anyway.
                layers_utils._in_opacity_update = True
                try:
                    sl_prev.layer_opacity = 1.0
                finally:
                    layers_utils._in_opacity_update = False
                # The suppressed callback would have refreshed this; keep it
                # in sync so the next slider drag commits against 1.0.
                sl_prev["_prev_opacity"] = 1.0

            base_obj.data.attributes.remove(base_obj.data.attributes[sl.layer_attrib_name])
            base_obj.sculptLayers.remove(index)
            base_obj.sculptLayers_index -= 1

            if base_obj.sculptLayers:
                layers_utils.UpdateMesh(context, active_obj, multires_mod, False)
            else:
                layers_utils.RemoveSculptLayers(context, active_obj, base_obj)

            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)

            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            active_obj.data.update()
        else:
            self.report({'INFO'}, "Sculptools: Layers can merge only two visible layers.")
            return {'CANCELLED'}

        return {'FINISHED'}


class SL_DuplicateLayer(bpy.types.Operator):
    bl_idname = "sl.duplicate_layer"
    bl_label = "Duplicate Layer"
    bl_description = "Duplicate the active layer: the copy goes just above it and becomes active"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    def execute(self, context):
        active_obj = context.active_object
        # The copy is inserted above the active layer, which shifts every layer
        # above it by one index: a queued opacity must land first, on the layer
        # it was set on. See flush_pending_opacity().
        layers_utils.flush_pending_opacity()
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Auto-save current layer before duplicating (preserves unsaved changes)
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        # Check BEFORE indexing: sculptLayers[index] on an empty collection
        # raises. poll_has_layers covers the UI path, but the operator is also
        # reachable from F3-search / Python.
        if not base_obj.sculptLayers:
            self.report({'INFO'}, "Sculptools: Layers found no layers on this object.")
            return {'CANCELLED'}

        index = base_obj.sculptLayers_index
        if index < 0 or index >= len(base_obj.sculptLayers):
            self.report({'INFO'}, "Sculptools: Layers found no active layer.")
            return {'CANCELLED'}
        # Everything needed from the source layer is read BEFORE the add:
        # adding to a collection can reallocate its items (Blender's API
        # gotchas), which would leave a reference taken earlier pointing at
        # freed memory.
        sl = base_obj.sculptLayers[index]
        src_attrib = sl.layer_attrib_name
        src_opacity = sl.layer_opacity
        src_visible = sl.visible
        new_name = layers_utils.duplicate_layer_name(base_obj, sl.name)

        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
        guard.shapekeys = shapekeys_mutes
        multires_mod = layers_utils.GetMultires(context, active_obj)
        guard.multires = multires_mod

        if multires_mod:
            verts_count = len(multires_mod[2].vertices)
        else:
            verts_count = len(active_obj.data.vertices)

        if verts_count != len(base_obj.data.vertices):
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            _report_topology_mismatch(self, verts_count, base_obj)
            return {'CANCELLED'}

        new_sl = layers_utils.AddSculptLayer(base_obj)
        if new_sl is None:
            # Hard cap reached: no free SLayer_<n> attribute slot left.
            # AddSculptLayer returns None there, so bail out with a message
            # instead of an AttributeError on the next line -- and restore
            # everything this operator already touched.
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            self.report(
                {'WARNING'},
                "Sculptools: Layers reached the maximum of "
                f"{layers_utils.MAX_SCULPT_LAYERS} layers.")
            return {'CANCELLED'}
        new_sl.name = new_name
        # Copying the opacity would fire UpdateLayerOpacity -> UpdateMesh
        # mid-operator (RNA update callbacks run on assignment from Python even
        # for an unchanged value); the new layer's delta is still all-zero at
        # this point, so that rebuild is pure waste -- a full multires_reshape
        # per click on Multires. The explicit rebuild below is the real one.
        layers_utils._in_opacity_update = True
        try:
            new_sl.layer_opacity = src_opacity
        finally:
            layers_utils._in_opacity_update = False
        # The suppressed callback would have set this; keep it in sync so the
        # next slider drag commits against the right value.
        new_sl["_prev_opacity"] = src_opacity
        new_sl.visible = src_visible

        np_sl_prev_verts = np.empty(verts_count * 3, dtype=np.float32)
        base_obj.data.attributes[src_attrib].data.foreach_get('vector', np_sl_prev_verts)
        base_obj.data.attributes[new_sl.layer_attrib_name].data.foreach_set('vector', np_sl_prev_verts)

        # Right above the source, and active (the user's choice, 2026-10-05):
        # where every layer editor puts a duplicate, and where the next stroke
        # then goes. It used to land at the top of the stack with the source
        # still active. move() swaps items, so `new_sl` is not used after it.
        base_obj.sculptLayers.move(len(base_obj.sculptLayers) - 1, index + 1)
        base_obj.sculptLayers_index = index + 1

        if src_visible:
            layers_utils.UpdateMesh(context, active_obj, multires_mod, False)

        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)

        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
        bpy.ops.object.mode_set(mode=context_mode, toggle=False)
        active_obj.data.update()

        return {'FINISHED'}


class SL_InvertLayer(bpy.types.Operator):
    bl_idname = "sl.invert_layer"
    bl_label = "Invert Layer"
    bl_description = ("Invert the active layer: what was pushed out is pulled "
                      "in. Opacity and visibility are not changed")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    # Deliberately NO properties: the F9 "Adjust Last Operation" panel then has
    # nothing to re-execute from. (Inverting twice is the identity anyway, but
    # an empty panel is the guarantee, not the arithmetic.)

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if not base_obj.sculptLayers:
            self.report({'INFO'}, "Sculptools: Layers found no layers on this object.")
            return {'CANCELLED'}
        index = base_obj.sculptLayers_index
        if index < 0 or index >= len(base_obj.sculptLayers):
            self.report({'INFO'}, "Sculptools: Layers found no active layer.")
            return {'CANCELLED'}
        sl = base_obj.sculptLayers[index]
        # Refused BEFORE the auto-commit, as in sl.morph_brush: committing into
        # a layer we are about to refuse is work the user never asked for, and
        # a hidden layer gives no feedback for what the operator would do.
        if not sl.visible:
            self.report({'WARNING'}, "Sculptools: Layers needs the active layer visible.")
            return {'CANCELLED'}

        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
        guard.shapekeys = shapekeys_mutes
        multires_mod = layers_utils.GetMultires(context, active_obj)
        guard.multires = multires_mod

        if multires_mod:
            verts_count = len(multires_mod[2].vertices)
        else:
            verts_count = len(active_obj.data.vertices)

        if verts_count != len(base_obj.data.vertices):
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            _report_topology_mismatch(self, verts_count, base_obj)
            return {'CANCELLED'}

        att = base_obj.data.attributes[sl.layer_attrib_name]
        np_delta = np.empty(verts_count * 3, dtype=np.float32)
        att.data.foreach_get('vector', np_delta)
        np.negative(np_delta, out=np_delta)
        att.data.foreach_set('vector', np_delta)

        layers_utils.UpdateMesh(context, active_obj, multires_mod, False)

        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)

        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
        bpy.ops.object.mode_set(mode=context_mode, toggle=False)
        active_obj.data.update()

        return {'FINISHED'}


class SL_MirrorLayer(bpy.types.Operator):
    bl_idname = "sl.mirror_layer"
    bl_label = "Mirror Layer"
    bl_description = ("Mirror the active layer across the object's local axis "
                      "plane. Symmetrize copies the source side onto the "
                      "other; Flip swaps them")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    # Both HIDDEN, so the F9 "Adjust Last Operation" panel has nothing to
    # re-execute from. F9 re-runs execute() against the OUTPUT of the previous
    # run (measured on sl.move_layer and sl.bake_shapekeys, 2026-08-12), so an
    # axis switch there would mirror the ALREADY mirrored result -- stacking,
    # not correcting. The panel and the tests still set both from Python, which
    # HIDDEN does not prevent.
    direction: EnumProperty(
        name="Direction",
        # Deliberately the identifiers of Blender's own
        # tool_settings.sculpt.symmetrize_direction, which is the widget the
        # panel draws: the NAMED side is the SOURCE.
        items=(('NEGATIVE_X', "-X to +X", ""),
               ('POSITIVE_X', "+X to -X", ""),
               ('NEGATIVE_Y', "-Y to +Y", ""),
               ('POSITIVE_Y', "+Y to -Y", ""),
               ('NEGATIVE_Z', "-Z to +Z", ""),
               ('POSITIVE_Z', "+Z to -Z", "")),
        default='NEGATIVE_X',
        options={'HIDDEN'},
    )
    mode: EnumProperty(
        name="Mode",
        items=(('SYMMETRIZE', "Symmetrize",
                "Copy the source side onto the other one"),
               ('FLIP', "Flip", "Swap the two sides")),
        default='SYMMETRIZE',
        options={'HIDDEN'},
    )

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if not base_obj.sculptLayers:
            self.report({'INFO'}, "Sculptools: Layers found no layers on this object.")
            return {'CANCELLED'}
        index = base_obj.sculptLayers_index
        if index < 0 or index >= len(base_obj.sculptLayers):
            self.report({'INFO'}, "Sculptools: Layers found no active layer.")
            return {'CANCELLED'}
        sl = base_obj.sculptLayers[index]
        if not sl.visible:
            self.report({'WARNING'}, "Sculptools: Layers needs the active layer visible.")
            return {'CANCELLED'}

        # The pairing is read-only and needs nothing but the base mesh, so it
        # runs BEFORE the auto-commit and before ShapeKeysOff / GetMultires: a
        # refusal here leaves absolutely nothing to restore.
        axis, source_sign = layers_utils.mirror_direction_axis(self.direction)
        partner, stats = layers_utils.mirror_vertex_map(base_obj, axis)
        if stats["rate_first_pass"] < layers_utils.MIRROR_MIN_MATCH_RATE:
            self.report(
                {'WARNING'},
                "Sculptools: Layers found the mesh not symmetric on %s "
                "(a Mirror modifier does not count)."
                % self.direction[-1])
            return {'CANCELLED'}

        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
        guard.shapekeys = shapekeys_mutes
        multires_mod = layers_utils.GetMultires(context, active_obj)
        guard.multires = multires_mod

        if multires_mod:
            verts_count = len(multires_mod[2].vertices)
        else:
            verts_count = len(active_obj.data.vertices)

        if verts_count != len(base_obj.data.vertices):
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            _report_topology_mismatch(self, verts_count, base_obj)
            return {'CANCELLED'}

        n = len(base_obj.data.vertices)
        np_co = np.empty(n * 3, dtype=np.float32)
        base_obj.data.vertices.foreach_get('co', np_co)
        att = base_obj.data.attributes[sl.layer_attrib_name]
        np_delta = np.empty(n * 3, dtype=np.float32)
        att.data.foreach_get('vector', np_delta)

        new_delta, _changed = layers_utils.mirror_layer_delta(
            np_delta.reshape(-1, 3), np_co.reshape(-1, 3), partner,
            axis, source_sign, self.mode, stats["tol"])
        att.data.foreach_set('vector', new_delta.ravel())

        layers_utils.UpdateMesh(context, active_obj, multires_mod, False)

        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)

        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
        bpy.ops.object.mode_set(mode=context_mode, toggle=False)
        active_obj.data.update()

        # Silence on a full success, like the rest of Blender. A partial one is
        # reported, because a mirror that quietly skipped part of the mesh is
        # the silent failure this add-on has paid for three times.
        missed = stats["total"] - stats["matched"]
        if missed:
            self.report({'WARNING'},
                        "Sculptools: Layers mirrored %d points; %d had no match."
                        % (stats["matched"], missed))

        return {'FINISHED'}


class SL_CollapseLayers(bpy.types.Operator):
    bl_idname = "sl.collapse_layers"
    bl_label = "Collapse All"
    bl_description = "Collapse all layers."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    # NOTE: this used to carry an `action` EnumProperty with a 'CLEAN' item that
    # nothing implemented -- and since it was the FIRST item it was also the
    # default, so running the operator without setting it (F3-search, Python)
    # skipped the deletion below and still cleared the object's link: the layer
    # data survived as an unreferenced _DATA_<n> orphan. Removed: collapsing is
    # the operator's only behaviour, so there is nothing left to select.

    def invoke(self, context, event):
        # Collapsing discards the layer data for good and there is no rebuild:
        # whatever is ON the mesh right now is what the user keeps, so a layer
        # left at 0.3 is frozen at 0.3. That has always been the semantics, but
        # it should not be a surprise -- hence the opacity line below.
        #
        # invoke_props_dialog, copied from Sculptools: Palette's
        # SCULPTOOLS_OT_delete_palette. This is the one that opens AT THE MOUSE:
        # invoke_confirm is placed by Blender wherever it likes (measured -- it
        # landed across the viewport from the button), and invoke_popup opens at
        # the mouse but has no Cancel. Only this one gives both.
        #
        # NOT copied from Palette: its `confirmed` BoolProperty guard, which
        # makes execute() cancel unless invoke ran. Here that would rebuild the
        # exact footgun removed from this operator once already (see the note
        # above about the 'CLEAN' enum item): an operator that silently does
        # nothing when called from F3-search or a script. Repeat Last is not a
        # risk either -- after a collapse the object has no layers, so poll()
        # blocks it.
        #
        # invoke() only runs for UI invocation; bpy.ops.sl.collapse_layers() from
        # Python still goes straight to execute(), so scripts are unaffected.
        active_obj = context.active_object
        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = (layers_utils.getBaseObject(parent_col)
                    if parent_col is not None else None)

        # Shared data is not discarded: this object leaves it and the others
        # keep their layers (see layers_utils.RemoveSculptLayers). Saying
        # "discarded" there would be false, and frightening.
        others = len(layers_utils.other_data_users(active_obj))
        if others:
            self._confirm_lines = ["Only this object is collapsed.",
                                   "%s the layers." % _others_keep(others)]
        else:
            self._confirm_lines = ["Layer data will be discarded."]
        # Alt+D copies share the mesh, so they are collapsed with it -- they
        # cannot keep layers on a mesh this object bakes them into.
        linked = len(layers_utils.mesh_group(active_obj)) - 1
        if linked > 0:
            self._confirm_lines.append(
                "Also applies to %d linked duplicate%s."
                % (linked, "" if linked == 1 else "s"))
        # Collapse keeps exactly what is on the mesh: a hidden layer is not
        # there, so it is dropped for good. Said in so many words since
        # 2026-10-05 -- the dialog used to say only "discarded", and the
        # opacity line below fired for a HIDDEN layer's opacity, which read as
        # if that layer would be kept.
        # Not when other objects keep the data: the layer survives with them.
        layers = list(base_obj.sculptLayers) if base_obj is not None else []
        hidden = sum(1 for sl_i in layers if not sl_i.visible)
        if hidden and not others:
            self._confirm_lines.append(
                "%d hidden layer%s will be lost."
                % (hidden, "" if hidden == 1 else "s"))
        # Collapse freezes the mesh at its CURRENT intensity, so any VISIBLE
        # layer shown below full strength is baked in at that strength. Worth
        # spelling out -- it is the one part of the operation that is not
        # obvious from its name.
        if any(sl_i.visible and sl_i.layer_opacity != 1.0 for sl_i in layers):
            self._confirm_lines.append(
                "Layer opacities below 1.00 will be baked in.")

        return context.window_manager.invoke_props_dialog(
            self, width=260, title="Collapse All?", confirm_text="Collapse")

    def draw(self, context):
        col = self.layout.column(align=True)
        for line in getattr(self, "_confirm_lines", ()):
            col.label(text=line)

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode

        # A queued opacity change has not touched the mesh yet (trailing-edge
        # timer). Collapsing on top of it would freeze the shape from BEFORE the
        # change while the panel showed the new value. Same reason _on_save_pre
        # flushes before writing a .blend.
        layers_utils.flush_pending_opacity()

        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if base_obj.sculptLayers:
            if active_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

            # The visible mesh already carries the whole stack baked in, so
            # collapsing is just dropping the layer data -- or, while other
            # objects share it, just leaving it. This deleted the data inline
            # until 2026-10-01, which took a Shift+D original's layers with it
            # (dev_probe_audit.py T2).
            others = layers_utils.RemoveSculptLayers(context, active_obj, base_obj)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            if others:
                self.report({'INFO'}, "Sculptools: Layers collapsed this object "
                            "only; %s the layers."
                            % _others_keep(others))

        return {'FINISHED'}


class SL_LinkData(bpy.types.Operator):
    bl_idname = "sl.link_data"
    bl_label = "Link Layer Data"
    bl_description = "Use the selected layer data on the active object"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # A mesh is required: linking rebuilds the visible mesh from the deltas.
        # Never the temporary Morph-mask copy: it has the base's vertex count,
        # so it read as "compatible" and accepted any dataset (found 2026-10-01).
        active_obj = context.active_object
        if not (active_obj is not None and active_obj.type == 'MESH'
                and layers_utils.get_morph_mask_source(active_obj) is None):
            return False
        # Linking rebuilds the mesh, which cannot run on a hidden object (see
        # layers_utils.object_hidden). Not layers_blocked_reason() as a whole:
        # this is the way OUT of the other blocked states.
        if layers_utils.object_hidden(active_obj):
            cls.poll_message_set("The object is hidden - show it to edit layers")
            return False
        return True

    coll_name: StringProperty(
        name="Data Collection",
        description="Layer-data collection to link, or empty to unlink",
        default="")

    @classmethod
    def description(cls, context, properties):
        # One operator drives every menu entry; give each its own tooltip.
        if not properties.coll_name:
            return "Unlink the layer data from the active object"
        return ("Use '%s' as the layer data of the active object"
                % properties.coll_name)

    def execute(self, context):
        active_obj = context.active_object

        # Validate FIRST, so a refusal has nothing to undo.
        coll = None
        if self.coll_name:
            coll = bpy.data.collections.get(self.coll_name)
            if coll is None:
                self.report({'WARNING'},
                            "Sculptools: Layers cannot find layer data '%s'."
                            % self.coll_name)
                return {'CANCELLED'}

            base_obj = layers_utils.getBaseObject(coll)
            if base_obj is None or not base_obj.sculptLayers:
                self.report({'WARNING'}, "Sculptools: Layers found no layers in '%s'."
                            % coll.name)
                return {'CANCELLED'}

            # Refuse incompatible data instead of linking it. foreach_set only
            # REPORTS a size mismatch rather than raising, so the old dropdown
            # accepted e.g. a 266-point sphere's data on an 8-point cube in
            # complete silence and every later layer write became a no-op.
            if not layers_utils.data_collection_compatible(active_obj, coll):
                self.report({'WARNING'},
                            "Sculptools: Layers cannot link '%s': its layer data "
                            "has %d points, the mesh %d, so layer edits would "
                            "not be saved."
                            % (coll.name, len(base_obj.data.vertices),
                               len(active_obj.data.vertices)))
                return {'CANCELLED'}

        # The object is about to use OTHER data: a queued opacity must land on
        # the data it was set on, before the switch -- applied after it, its
        # commit divided the new data's capture by the old data's opacities
        # (measured: the new dataset's delta halved). After the validation, so
        # a refusal still changes nothing of its own. See
        # flush_pending_opacity().
        layers_utils.flush_pending_opacity()

        # ONE boundary undo step, before anything changes -- the shape every
        # operator that commits has. It used to be three: the commit's own push,
        # then UpdateLayersValue's two brackets, the second holding the final
        # state, so after a stroke the first Ctrl+Z did nothing and the second
        # landed on the new data linked to a mesh not yet rebuilt from it.
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Commit the in-progress sculpt before the link changes: relinking
        # rebuilds the visible mesh from the stored deltas, so un-saved strokes
        # would be wiped off the mesh AND left uncaptured in the layer being
        # abandoned. Measured on 2026-07-29 -- the old dropdown lost them both
        # ways. No-ops outside Sculpt Mode / on an unlinked object.
        layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        # The whole MESH GROUP moves: the object's Alt+D copies share its mesh,
        # and one dataset per mesh is what keeps a mesh from being driven by
        # two. See layers_utils.mesh_group().
        if coll is None:
            layers_utils.link_mesh_group(active_obj, None)
            self.report({'INFO'}, "Sculptools: Layers unlinked the layer data.")
            return {'FINISHED'}

        # Data another MESH uses is copied, not shared: two meshes on one
        # dataset diverge, and the next commit on the one left behind corrupts
        # the layers (dev_probe_shared_stale.py). Taking a copy keeps what this
        # menu is for -- putting compatible layers on another object -- without
        # tying the two together.
        source = coll
        if any(o.data != active_obj.data
               for o in layers_utils.data_users(source)):
            coll = layers_utils.copy_layer_data(
                context, active_obj, layers_utils.getBaseObject(source))

        layers_utils.link_mesh_group(active_obj, coll)
        # Link Data switches rather than writes, so it never passes through
        # ensure_own_data() -- but the rebuild below reads the data it lands on,
        # which may be in an older format. Migrate THAT data (the copy, when
        # one was made); the source stays as it is for its other users.
        layers_utils._migrate_on_entry(active_obj)
        # push_undo=False: the boundary was pushed above.
        layers_utils.UpdateLayersValue(context, push_undo=False)
        # Linking re-establishes which cage this data belongs to. The unlink path
        # above has nothing to stamp.
        layers_utils.stamp_cage_verts(active_obj,
                                      layers_utils.getBaseObject(coll))
        if coll != source:
            self.report({'INFO'}, "Sculptools: Layers linked a copy of layer data '%s'."
                        % source.name)
        else:
            self.report({'INFO'}, "Sculptools: Layers linked layer data '%s'." % coll.name)
        return {'FINISHED'}


class SL_CleanupData(bpy.types.Operator):
    bl_idname = "sl.cleanup_data"
    bl_label = "Clean Up Unused Data"
    bl_description = ("Delete layer-data collections in LAYERS_DATA that no "
                      "object uses anymore (orphans from deleted objects, "
                      "Duplicate Data, or reassignment)")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # Enabled whenever the layer-data root exists; the UI greys the button
        # out when there is nothing unused to remove.
        return layers_utils.GetSculptCollection(context, False) is not None

    # How many names the confirmation lists before summarising the rest.
    _LIST_MAX = 10

    def invoke(self, context, event):
        # This used to be a bare invoke_confirm, i.e. a generic "OK?" that named
        # nothing. Two reasons it changed:
        #   * invoke_props_dialog is the one that opens AT THE MOUSE (see the
        #     convention note on sl.collapse_layers); invoke_confirm lands
        #     wherever Blender likes;
        #   * naming the collections is the only clue that separates a real
        #     orphan from live data the user is about to lose. A collection the
        #     user RENAMED in the Outliner used to be reported as unused, because
        #     the link was resolved by name -- that is fixed at the root now
        #     (layers_utils.parent_collection), but a file linked by an older
        #     build still resolves by name, and there the list is the last guard.
        return context.window_manager.invoke_props_dialog(
            self, width=320, title="Clean Up Unused Data?",
            confirm_text="Delete")

    def draw(self, context):
        # Computed here, not cached from invoke(): get_unused_data_collections is
        # read-only (safe in draw) and this way the list can never be stale.
        doomed = layers_utils.get_unused_data_collections(context)
        col = self.layout.column(align=True)
        if not doomed:
            col.label(text="Nothing unused to remove.")
            return
        col.label(text="Deleted permanently, with their layers:", icon='TRASH')
        for coll in doomed[:self._LIST_MAX]:
            col.label(text="    " + coll.name)
        if len(doomed) > self._LIST_MAX:
            col.label(text="    ... and %d more"
                           % (len(doomed) - self._LIST_MAX))

    def execute(self, context):
        unused = layers_utils.get_unused_data_collections(context)
        removed = 0
        for coll in list(unused):
            for ob in list(coll.objects):
                me = ob.data
                bpy.data.objects.remove(ob, do_unlink=True)
                if me is not None and me.users == 0:
                    bpy.data.meshes.remove(me)
            bpy.data.collections.remove(coll)
            removed += 1

        # Drop the LAYERS_DATA root if it is now empty.
        layers_utils._remove_root_if_empty(context)

        self.report({'INFO'}, "Sculptools: Layers removed %d unused data %s."
                    % (removed, "collection" if removed == 1 else "collections"))
        return {'FINISHED'}


class SL_SubdivideLayers(bpy.types.Operator):
    bl_idname = "sl.subdivide_layers"
    bl_label = "Subdivide Layers"
    bl_description = "Subdivide Layers."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        # This operator mutes shape keys, switches other modifiers off, drives
        # the Multires level, may ADD a Multires, and builds a temp object --
        # then runs multires_subdivide / _reshape / _higher_levels_delete once
        # per layer. Any of those raising used to leave every one of those
        # changes behind. See layers_utils.mutation_guard.
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Auto-save current layer before subdividing (preserves unsaved changes)
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if base_obj.sculptLayers:
            if active_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

            shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
            guard.shapekeys = shapekeys_mutes
            multires_mod = layers_utils.GetMultires(context, active_obj, True)
            guard.multires = multires_mod

            subd_type = base_obj.sculptLayers_subd_type
            subd_mode = 'CATMULL_CLARK'
            if subd_type == 'Simple':
                subd_mode = 'SIMPLE'
            elif subd_type == 'Linear':
                subd_mode = 'LINEAR'

            if multires_mod:
                verts_count = len(multires_mod[2].vertices)
                depsgraph = multires_mod[3]
            else:
                verts_count = len(active_obj.data.vertices)

            if verts_count != len(base_obj.data.vertices) and verts_count >= len(active_obj.data.vertices):
                if multires_mod:
                    multires_mod[0].levels = multires_mod[1]
                    layers_utils.OtherModsOn(multires_mod)
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                _report_topology_mismatch(self, verts_count, base_obj)
                return {'CANCELLED'}

            tmp_mesh_2 = guard.track(bpy.data.meshes.new("_TMP_SUBDLAYER"))
            tmp_mesh_2.vertices.add(verts_count)
            tmp_obj_2 = guard.track(bpy.data.objects.new(
                active_obj.name + '_TMP_SUBDLAYERS2', tmp_mesh_2))
            context.scene.collection.objects.link(tmp_obj_2)

            bpy.ops.object.select_all(action='DESELECT')
            tmp_obj_2.select_set(True)
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj

            if not multires_mod:
                other_mods = layers_utils.OtherModsOff(active_obj)
                new_mult = active_obj.modifiers.new("Multires", type='MULTIRES')
                multires_mod = (new_mult, new_mult.levels, None, None, other_mods)
                guard.multires = multires_mod
                # A failure from here on must not leave behind a Multires the
                # object never had.
                guard.on_failure(
                    lambda: active_obj.modifiers.remove(
                        active_obj.modifiers[new_mult.name]))

            np_base_verts = np.empty(verts_count * 3, dtype=np.float32)
            base_obj.data.vertices.foreach_get("co", np_base_verts)

            if multires_mod[0].levels == 0:
                layers_utils.WriteLiveCoords(active_obj, np_base_verts)
                active_obj.data.update()
            else:
                tmp_obj_2.data.vertices.foreach_set("co", np_base_verts)
                tmp_obj_2.data.update()
                bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)

            depsgraph = context.evaluated_depsgraph_get()
            bpy.ops.object.multires_subdivide(modifier=multires_mod[0].name, mode=subd_mode)
            depsgraph.update()

            verts_count_sub = len(depsgraph.objects[active_obj.name].data.vertices)
            tmp_mesh_3 = depsgraph.objects[active_obj.name].data

            np_base_verts_sub = np.empty(verts_count_sub * 3, dtype=np.float32)
            tmp_mesh_3.vertices.foreach_get('co', np_base_verts_sub)

            np_sl_verts_list = []
            for sl in base_obj.sculptLayers:
                np_sl_verts = np.empty(verts_count * 3, dtype=np.float32)
                base_obj.data.attributes[sl.layer_attrib_name].data.foreach_get('vector', np_sl_verts)
                np.add(np_sl_verts, np_base_verts, out=np_sl_verts)

                multires_mod[0].levels -= 1
                bpy.ops.object.multires_higher_levels_delete(modifier=multires_mod[0].name)

                if multires_mod[0].levels == 0:
                    layers_utils.WriteLiveCoords(active_obj, np_sl_verts)
                    active_obj.data.update()
                else:
                    tmp_obj_2.data.vertices.foreach_set("co", np_sl_verts)
                    tmp_obj_2.data.update()
                    bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)

                bpy.ops.object.multires_subdivide(modifier=multires_mod[0].name, mode=subd_mode)
                depsgraph.update()
                tmp_mesh_3 = depsgraph.objects[active_obj.name].data

                np_sl_verts_sub = np.empty(verts_count_sub * 3, dtype=np.float32)
                tmp_mesh_3.vertices.foreach_get('co', np_sl_verts_sub)
                np.subtract(np_sl_verts_sub, np_base_verts_sub, out=np_sl_verts_sub)

                np_sl_verts_list.append((np_sl_verts_sub, sl.visible))

            base_obj.data.vertices.add(verts_count_sub - verts_count)
            base_obj.data.vertices.foreach_set('co', np_base_verts_sub)

            # The stored delta is written first, unscaled; only the local copy
            # used to rebuild the visible shape below gets scaled by the layer's
            # opacity, so SubD lands on the same shape the viewport was already
            # showing.
            for idx, np_sl_verts_2 in enumerate(np_sl_verts_list):
                sl_i = base_obj.sculptLayers[idx]
                base_obj.data.attributes[sl_i.layer_attrib_name].data.foreach_set('vector', np_sl_verts_2[0])

                if np_sl_verts_2[1]:
                    opacity_val = sl_i.layer_opacity
                    if opacity_val != 1.0:
                        layers_utils.ApplyLayerOpacity(np_sl_verts_2[0], opacity_val, verts_count_sub, False)
                    np.add(np_sl_verts_2[0], np_base_verts_sub, out=np_base_verts_sub)

            tmp_obj_2.data.vertices.add(verts_count_sub - verts_count)
            tmp_obj_2.data.vertices.foreach_set('co', np_base_verts_sub)
            tmp_obj_2.data.update()
            bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)

            bpy.data.objects.remove(tmp_obj_2, do_unlink=True)
            bpy.data.meshes.remove(tmp_mesh_2)
            np_sl_verts_list = None

            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)

            if multires_mod:
                layers_utils.OtherModsOn(multires_mod)

            layers_utils.stamp_multires_levels(active_obj, base_obj)
            layers_utils.stamp_cage_verts(active_obj, base_obj)

        return {'FINISHED'}


class SL_UnsubdivideLayers(bpy.types.Operator):
    bl_idname = "sl.unsubdivide_layers"
    bl_label = "Unsubdivide Layers"
    bl_description = "Unsubdivide Layers."
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # _poll_editable, not poll_has_layers: this one has its own extra
        # condition, and without the guard it was the ONE layer control left
        # clickable while Dyntopo was on (caught by looking at the panel, not by
        # the suite -- headless never renders a button).
        if not _poll_editable(cls, context):
            return False
        # Lightweight Multires check (no side effects).
        # Mirrors the requirement enforced inside execute().
        return layers_utils.find_multires_readonly(
            context.active_object,
            require_show_viewport=True,
            min_total_levels=1,
        ) is not None

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode
        # Same exposure as SubD: shape keys, other modifiers, Multires levels and
        # a temp object, plus multires ops in a per-layer loop.
        with layers_utils.mutation_guard(context, active_obj, context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        if active_obj.mode != 'OBJECT':
            layers_utils.boundary_undo_push(context)

        # Auto-save current layer before unsubdividing (preserves unsaved changes)
        if context_mode == 'SCULPT':
            layers_utils.AutoSaveCurrentLayer(context, boundary_pushed=True)

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        if base_obj.sculptLayers:
            if active_obj.mode != 'OBJECT':
                bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

            shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
            guard.shapekeys = shapekeys_mutes
            # is_return_zero_lvl=True: a Multires with 0 subdivisions must come
            # back as a tuple, or GetMultires() returns None and this operator
            # tells an object that HAS a Multires that it needs one -- while the
            # "at least 1 Subdivision" branch right below, which says the true
            # thing, stays unreachable.
            multires_mod = layers_utils.GetMultires(context, active_obj, True)
            guard.multires = multires_mod

            if multires_mod:
                verts_count = len(multires_mod[2].vertices)
            else:
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                self.report({'INFO'}, "Sculptools: Layers needs a Multires modifier on "
                                      "the object.")
                return {'CANCELLED'}

            if multires_mod[0].total_levels < 1:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                self.report({'INFO'}, "Sculptools: Layers needs at least 1 Multires "
                                      "subdivision.")
                return {'CANCELLED'}

            if verts_count != len(base_obj.data.vertices):
                if multires_mod:
                    multires_mod[0].levels = multires_mod[1]
                    layers_utils.OtherModsOn(multires_mod)
                layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
                bpy.ops.object.mode_set(mode=context_mode, toggle=False)
                _report_topology_mismatch(self, verts_count, base_obj)
                return {'CANCELLED'}

            depsgraph = multires_mod[3]

            tmp_mesh = guard.track(bpy.data.meshes.new("_TMP_SUBDLAYER"))
            tmp_mesh.vertices.add(verts_count)
            tmp_obj = guard.track(bpy.data.objects.new(
                active_obj.name + '_TMP_SUBDLAYERS', tmp_mesh))
            context.scene.collection.objects.link(tmp_obj)

            bpy.ops.object.select_all(action='DESELECT')
            tmp_obj.select_set(True)
            active_obj.select_set(True)
            context.view_layer.objects.active = active_obj

            np_base_verts = np.empty(verts_count * 3, dtype=np.float32)
            base_obj.data.vertices.foreach_get('co', np_base_verts)
            tmp_mesh.vertices.foreach_set('co', np_base_verts)
            tmp_mesh.update()
            bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)

            if multires_mod[0].levels == 1:
                bpy.ops.object.multires_base_apply(modifier=multires_mod[0].name)

            multires_mod[0].sculpt_levels = multires_mod[0].total_levels - 1
            multires_mod[0].levels = multires_mod[0].total_levels - 1
            depsgraph = context.evaluated_depsgraph_get()
            verts_count_new = len(depsgraph.objects[active_obj.name].data.vertices)

            np_base_verts_new = np.empty(verts_count_new * 3, dtype=np.float32)
            depsgraph.objects[active_obj.name].data.vertices.foreach_get('co', np_base_verts_new)

            np_sl_verts_list = []
            for sl in base_obj.sculptLayers:
                multires_mod[0].sculpt_levels = multires_mod[0].total_levels + 1
                multires_mod[0].levels = multires_mod[0].total_levels + 1
                depsgraph.update()

                np_sl_verts = np.empty(verts_count * 3, dtype=np.float32)
                base_obj.data.attributes[sl.layer_attrib_name].data.foreach_get('vector', np_sl_verts)
                np.add(np_sl_verts, np_base_verts, out=np_sl_verts)

                tmp_mesh.vertices.foreach_set('co', np_sl_verts)
                tmp_mesh.update()
                bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)

                if multires_mod[0].levels == 1:
                    bpy.ops.object.multires_base_apply(modifier=multires_mod[0].name)

                multires_mod[0].sculpt_levels = multires_mod[0].total_levels - 1
                multires_mod[0].levels = multires_mod[0].total_levels - 1
                depsgraph.update()

                np_sl_verts_new = np.empty(verts_count_new * 3, dtype=np.float32)
                depsgraph.objects[active_obj.name].data.vertices.foreach_get('co', np_sl_verts_new)
                np.subtract(np_sl_verts_new, np_base_verts_new, out=np_sl_verts_new)

                np_sl_verts_list.append((np_sl_verts_new, sl.visible))

            bpy.ops.object.multires_higher_levels_delete(modifier=multires_mod[0].name)

            base_obj.data.clear_geometry()
            base_obj.data.vertices.add(verts_count_new)
            base_obj.data.vertices.foreach_set('co', np_base_verts_new)

            # See the matching note in SL_SubdivideLayers: stored delta unscaled,
            # local copy scaled by the layer opacity so the rebuilt shape matches
            # what the viewport showed before UnSubD.
            for idx, np_sl_verts_2 in enumerate(np_sl_verts_list):
                sl_i = base_obj.sculptLayers[idx]
                base_obj.data.attributes.new(name=sl_i.layer_attrib_name, type='FLOAT_VECTOR', domain='POINT')
                base_obj.data.attributes[sl_i.layer_attrib_name].data.foreach_set('vector', np_sl_verts_2[0])

                if np_sl_verts_2[1]:
                    opacity_val = sl_i.layer_opacity
                    if opacity_val != 1.0:
                        layers_utils.ApplyLayerOpacity(np_sl_verts_2[0], opacity_val, verts_count_new, False)
                    np.add(np_sl_verts_2[0], np_base_verts_new, out=np_base_verts_new)

            if multires_mod[0].levels > 0:
                tmp_mesh.clear_geometry()
                tmp_mesh.vertices.add(verts_count_new)
                tmp_mesh.vertices.foreach_set('co', np_base_verts_new)
                tmp_mesh.update()
                bpy.ops.object.multires_reshape(modifier=multires_mod[0].name)
            else:
                layers_utils.WriteLiveCoords(active_obj, np_base_verts_new)
                active_obj.data.update()

            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)

            bpy.data.objects.remove(tmp_obj, do_unlink=True)
            bpy.data.meshes.remove(tmp_mesh)
            np_sl_verts_list = None

            if multires_mod:
                layers_utils.OtherModsOn(multires_mod)

            bpy.ops.object.mode_set(mode=context_mode, toggle=False)

            layers_utils.stamp_multires_levels(active_obj, base_obj)
            layers_utils.stamp_cage_verts(active_obj, base_obj)

        return {'FINISHED'}


class SL_BakeShapeKeys(bpy.types.Operator):
    """Bake sculpt layers into animatable shape keys on a separate object"""
    bl_idname = "sl.bake_shapekeys"
    bl_label = "Bake to Shape Keys"
    # The warning used to be two labels in the panel; it now rides on the button's
    # tooltip, which is where the user is when the decision is made. It cannot be
    # enforced in code: Blender's Rebuild Subdivisions deletes every shape key on
    # a dense mesh silently and still returns FINISHED (measured on 4.5 and 5.2,
    # 2026-08-12), so there is nothing to intercept. Keep this sentence.
    bl_description = ("Bake sculpt layers into animatable shape keys on a "
                      "separate object. The result is a plain dense mesh: do "
                      "NOT run the Multires modifier's Rebuild Subdivisions on "
                      "it, Blender deletes every shape key without warning")
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        # A bake reads the deltas at base resolution, so changed topology would
        # produce a garbage deliverable rather than a refusal.
        obj = context.active_object
        return (obj is not None and obj.mode == 'SCULPT'
                and _poll_editable(cls, context))

    # Both HIDDEN, so the F9 "Adjust Last Operation" panel has nothing to offer.
    # F9 re-runs execute() against the OUTPUT of the previous run (shared rule
    # §11.1): measured on 4.5 and 5.2, a PER_LAYER bake followed by an F9 switch
    # to COMBINED_POSE leaves the per-layer keys behind and appends the pose one,
    # so the deliverable ends up a hybrid -- [Basis, Layer_0, Layer_1, Pose]
    # where a clean COMBINED_POSE bake gives [Basis, Pose]. Making F9 work
    # properly would mean DELETING the keys the previous run wrote, which the
    # user may already have animated; that is the silent destruction this
    # add-on warns against elsewhere. The panel sets both from the Scene
    # properties in Python, which HIDDEN does not prevent.
    # See dev_probe_redo_panel.py for the measurement.
    mode: EnumProperty(
        name="Mode",
        items=(('PER_LAYER', "Per Layer",
                "One shape key per visible layer"),
               ('COMBINED_POSE', "Combined Pose",
                "One shape key capturing the current visible look")),
        default='PER_LAYER',
        options={'HIDDEN'},
    )
    pose_name: StringProperty(name="Pose Name", default="Pose",
                              options={'HIDDEN'})

    def invoke(self, context, event):
        # The deliverable is a plain dense mesh, so a high Multires level makes
        # it big. Ask first above the threshold; below it, behave exactly like a
        # plain button press. Deliberately no guard property gating execute() on
        # invoke() having run -- that is the footgun already removed from
        # sl.collapse_layers once: an operator that silently does nothing from
        # F3-search or a script.
        verts, _keys = layers_utils.bake_preview_counts(
            context.active_object, self.mode)
        if verts > layers_utils.BAKE_CONFIRM_VERTS:
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def draw(self, context):
        # Confirmation dialog only -- the operator's own properties are set by
        # the panel and must not be re-offered here.
        verts, keys = layers_utils.bake_preview_counts(
            context.active_object, self.mode)
        col = self.layout.column(align=True)
        # Same format as the Shape Keys panel's preview line, so the number the
        # user just read there is the one asked about here.
        col.label(text="This will build {:,} verts, {} {}.".format(
            verts, keys, "key" if keys == 1 else "keys"), icon='ERROR')
        col.label(text="A dense result costs time and memory.")

    def execute(self, context):
        active_obj = context.active_object
        context_mode = active_obj.mode

        # Commit any in-progress sculpt into the active layer FIRST: it only
        # works from Sculpt Mode, and the body below leaves for Object Mode.
        layers_utils.AutoSaveCurrentLayer(context)

        with layers_utils.mutation_guard(context, active_obj,
                                         context_mode) as guard:
            return self._execute(context, active_obj, context_mode, guard)

    def _execute(self, context, active_obj, context_mode, guard):
        # Object Mode is required: a Multires object's evaluated mesh is
        # unreliable in Sculpt Mode -- it comes back as the base cage.
        if active_obj.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=False)

        shapekeys_mutes = layers_utils.ShapeKeysOff(active_obj)
        guard.shapekeys = shapekeys_mutes

        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = layers_utils.getBaseObject(parent_col)

        # The deliverable's topology: the cage on a plain object, the evaluated
        # subdivided mesh on a Multires one -- where the cage has the wrong
        # vertex count entirely, which is what used to make this operator refuse
        # Multires outright. Owns a new mesh datablock from here on;
        # bake_shapekeys() consumes it on every path.
        ref_mesh, multires_mod = layers_utils.bake_topology_source(
            context, active_obj)
        guard.multires = multires_mod
        guard.track(ref_mesh)

        # The layer base and the reference topology must share a vertex count:
        # the shape keys are computed from base-sized arrays but written onto the
        # reference topology. They diverge when the base was built from a
        # Multires that has since been disabled or removed (GetMultires only
        # sees a Multires with show_viewport on, so the topology source falls
        # back to the cage), or when the mesh was edited outside the add-on.
        # Blender's foreach_set only *reports* a size mismatch instead of
        # raising, so without this check the bake claims success and hands over
        # shape keys identical to the Basis.
        if len(base_obj.data.vertices) != len(ref_mesh.vertices):
            ref_verts = len(ref_mesh.vertices)
            bpy.data.meshes.remove(ref_mesh)
            guard.temp_ids.clear()
            if multires_mod:
                multires_mod[0].levels = multires_mod[1]
                layers_utils.OtherModsOn(multires_mod)
            layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            self.report({'ERROR'},
                        "Sculptools: Layers cannot bake: the layer data has %d "
                        "points, the mesh %d. Re-enable the Multires modifier, or rebuild "
                        "the layers on the current topology."
                        % (len(base_obj.data.vertices), ref_verts))
            return {'CANCELLED'}

        morph_obj, baked = layers_utils.bake_shapekeys(
            context, active_obj, self.mode, self.pose_name, ref_mesh)
        # Consumed: either adopted into morph_obj or removed. Either way the
        # guard must stop treating it as disposable temp state.
        guard.temp_ids.clear()

        if multires_mod:
            multires_mod[0].levels = multires_mod[1]
            layers_utils.OtherModsOn(multires_mod)
        layers_utils.ShapeKeysOn(active_obj, shapekeys_mutes)

        if not baked:
            # Nothing was produced, so nothing changed for the user: put them
            # back exactly where they were, Sculpt Mode included.
            bpy.ops.object.mode_set(mode=context_mode, toggle=False)
            self.report({'WARNING'}, "Sculptools: Layers has no visible layers to bake.")
            return {'CANCELLED'}

        # Deliberate exception to the "restore context_mode" convention: the
        # source is about to be hidden, and a hidden object cannot be put back
        # into Sculpt Mode (measured -- see hide_baked_source). Object Mode is
        # therefore the end state on the success path.
        hidden = layers_utils.hide_baked_source(context, active_obj, morph_obj)

        self.report({'INFO'},
                    "Sculptools: Layers baked %d shape key(s) to '%s'.%s"
                    % (len(baked), morph_obj.name,
                       " Source hidden (Alt+H to bring it back)." if hidden
                       else ""))
        return {'FINISHED'}


# invoke() pre-fills `value`, and that assignment fires the update callback like
# any other. Without this the popup would apply the value it just read, pushing
# an undo step for a change nobody made.
_in_opacity_prefill = False


def _live_opacity_update(self, context):
    """The popup's slider moved: queue the trailing-edge apply.

    `self` here is the OperatorProperties struct, not the Operator -- which is
    also why dragging this slider pushes no undo step of its own: the property
    has no owner ID."""
    if _in_opacity_prefill:
        return
    base_obj = layers_utils.resolve_layer(context, self.index)
    if base_obj is None:
        return
    layers_utils.queue_live_opacity(base_obj, self.index, self.value)


# The popup's width at UI scale 1.0. invoke_popup scales it, and invoke() needs
# the same number to centre the slider on the mouse.
_OPACITY_POPUP_WIDTH = 200


class SL_SetOpacity(bpy.types.Operator):
    bl_idname = "sl.set_opacity"
    bl_label = "Set Layer Opacity"
    bl_description = "Set this layer's opacity: drag the slider or type a value"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _poll_editable(cls, context)

    index: IntProperty(options={'HIDDEN'}, default=-1)
    value: FloatProperty(name="Opacity", default=1.0, min=0.0, max=1.0,
                         update=_live_opacity_update)

    def invoke(self, context, event):
        global _in_opacity_prefill
        base_obj = layers_utils.resolve_layer(context, self.index)
        if base_obj is None:
            self.report({'INFO'}, "Sculptools: Layers got no layer index.")
            return {'CANCELLED'}
        # Pre-fill with what the layer actually holds, so opening the popup and
        # dismissing it without touching anything is a no-op.
        _in_opacity_prefill = True
        try:
            self.value = base_obj.sculptLayers[self.index].layer_opacity
        finally:
            _in_opacity_prefill = False
        # invoke_popup, not invoke_props_dialog: no OK button. The slider applies
        # itself when it settles, exactly like the old panel slider did, so a
        # confirm button would be a step the user does not need -- and a Cancel
        # button would be a lie, since the settled value is already applied.
        # Dismissing closes the popup; Ctrl+Z is what undoes.
        # One popup is one undo step, however many pauses the drag has.
        layers_utils.begin_live_opacity_session()
        # invoke_popup takes no position: Blender puts the popup's TOP-LEFT
        # corner on the window's last mouse position, so the slider used to
        # open to the right of the click. That position is read once, when
        # the popup is created, and every redraw of the popup reuses the copy
        # -- and Window.cursor_warp sets it at once. So: move the pointer half
        # a popup left and half a row up, open, move it straight back. The
        # slider ends up centred on the click and the pointer where it was.
        window = context.window
        if window is None:
            return context.window_manager.invoke_popup(
                self, width=_OPACITY_POPUP_WIDTH)
        scale = context.preferences.system.ui_scale or 1.0
        x, y = event.mouse_x, event.mouse_y
        window.cursor_warp(x - round(_OPACITY_POPUP_WIDTH * scale / 2),
                           y + round(10 * scale))
        try:
            return context.window_manager.invoke_popup(
                self, width=_OPACITY_POPUP_WIDTH)
        finally:
            window.cursor_warp(x, y)

    def cancel(self, context):
        # Blender calls this whenever the popup closes -- Esc or a click
        # outside, the only two ways it closes (measured 2026-10-01). The value
        # still waiting for the timer is applied here, at once: the closing
        # click may land on the mesh and start a stroke.
        layers_utils.end_live_opacity_session()

    def draw(self, context):
        # slider=True so it can be dragged, which is the whole point.
        self.layout.prop(self, "value", slider=True)

    def execute(self, context):
        # The script / F3-search path. Drop anything the popup queued first, or
        # the same change would land twice: once here, once when the timer fires.
        layers_utils.discard_pending_live_opacity()
        base_obj = layers_utils.resolve_layer(context, self.index)
        if base_obj is None:
            self.report({'INFO'}, "Sculptools: Layers got no layer index.")
            return {'CANCELLED'}
        layers_utils.apply_layer_opacity(context, base_obj, self.index, self.value)
        return {'FINISHED'}


classes = (
    SL_AddLayer,
    SL_SaveLayer,
    SL_RemoveLayer,
    SL_ChangeVisibility,
    SL_SetActiveLayer,
    SL_MorphBrush,
    SL_ApplyMorph,
    SL_CancelMorph,
    SL_BakeShapeKeys,
    SL_SyncMultires,
    SL_ObjectLayers,
    SL_MoveLayer,
    SL_MergeLayer,
    SL_CollapseLayers,
    SL_LinkData,
    SL_CleanupData,
    SL_DuplicateLayers,
    SL_DuplicateLayer,
    SL_InvertLayer,
    SL_MirrorLayer,
    SL_SubdivideLayers,
    SL_UnsubdivideLayers,
    SL_SetOpacity,
)


def register():
    from bpy.utils import register_class
    for cls in classes:
        register_class(cls)

    bpy.types.Object.sculptLayers = CollectionProperty(type=SL_ObjectLayers)
    bpy.types.Object.sculptLayers_index = IntProperty(default=-1)
    bpy.types.Object.sculptLayers_parent_coll = StringProperty(default="")
    # The rename-proof half of the same link; the name above is kept because it
    # is persisted in every .blend the add-on has written. Always write both
    # through layers_utils.set_parent_collection() and read through
    # parent_collection(). Blender remaps this pointer on rename and NULLS it when
    # the collection is removed, so it can never dangle (measured, 4.5 + 5.2).
    bpy.types.Object.sculptLayers_parent_coll_ptr = PointerProperty(
        type=bpy.types.Collection)
    bpy.types.Object.sculptLayers_subd_type = EnumProperty(
        items=(('Subdivide', "Subdivide", ""),
               ('Simple', "Simple", ""),
               ('Linear', "Linear", "")),
        default='Subdivide'
    )
    bpy.types.Object.sculptLayers_version = StringProperty()
    bpy.types.Scene.sculptLayers_scene_id = IntProperty(default=0)
    bpy.types.Scene.sculptLayers_bake_mode = EnumProperty(
        name="Bake Mode",
        items=(('PER_LAYER', "Per Layer",
                "One shape key per visible layer"),
               ('COMBINED_POSE', "Combined Pose",
                "One shape key capturing the current visible look")),
        default='PER_LAYER'
    )
    bpy.types.Scene.sculptLayers_pose_name = StringProperty(
        name="Pose Name", default="Pose")
    # Tool preferences, not layer data, so they live on the Scene beside the
    # bake settings. Three of them rather than Blender's single six-value
    # symmetrize_direction, because the axis and the source side are not
    # equally meaningful: FLIP has no source side (measured -- the two
    # directions of one axis give bit-identical results), so the panel drops
    # that row entirely in Flip instead of offering a choice that means
    # nothing. layers_utils.mirror_direction_id() composes the two halves back
    # into the operator's single identifier, which stays Blender's vocabulary.
    bpy.types.Scene.sculptLayers_mirror_axis = EnumProperty(
        name="Mirror Axis",
        items=(('X', "X", "Mirror across the local YZ plane"),
               ('Y', "Y", "Mirror across the local XZ plane"),
               ('Z', "Z", "Mirror across the local XY plane")),
        default='X'
    )
    bpy.types.Scene.sculptLayers_mirror_source = EnumProperty(
        name="Source Side",
        items=(('POSITIVE', "Positive", "The positive side is the source"),
               ('NEGATIVE', "Negative", "The negative side is the source")),
        default='POSITIVE'
    )
    bpy.types.Scene.sculptLayers_mirror_mode = EnumProperty(
        name="Mirror Mode",
        items=(('SYMMETRIZE', "Symmetrize",
                "Copy the source side onto the other one"),
               ('FLIP', "Flip", "Swap the two sides")),
        default='SYMMETRIZE'
    )

    # Register msgbus subscriber for mode change detection
    _register_msgbus()
    # Enabled mid-session -- an extension update, say -- with an object already
    # in Sculpt Mode, the first exit must still commit. See the timer's
    # docstring for why this cannot simply call _seed_previous_mode().
    bpy.app.timers.register(_seed_previous_mode_timer, first_interval=0.0)

    # Register load_post handler to reset msgbus mode-tracking state
    if _on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_on_load_post)

    # Register save_pre handler to auto-commit the active layer before saving
    if _on_save_pre not in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.append(_on_save_pre)

    # Disarms the skip of a redundant boundary undo step when the object
    # changes -- it only clears a flag (see layers_utils.boundary_undo_push).
    if layers_utils._on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(layers_utils._on_depsgraph_update)

    # The ONLY undo_post / redo_post handler: it arms the Morph reconcile timer
    # and nothing else (see _morph_reconcile, and the comment above
    # _on_save_pre for why nothing may rebuild from undo_post).
    for _hl in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_morph_undo_redo not in _hl:
            _hl.append(_on_morph_undo_redo)


def unregister():
    from bpy.utils import unregister_class

    # A queued opacity rebuild must not outlive the add-on. bpy.app.timers holds
    # the callback, so the registration survives addon_disable (measured) and the
    # timer would rebuild a mesh -- undo push and mode switches included -- for an
    # add-on that is no longer enabled.
    layers_utils.cancel_pending_timers()
    if bpy.app.timers.is_registered(_seed_previous_mode_timer):
        bpy.app.timers.unregister(_seed_previous_mode_timer)
    for _fn in (_morph_reconcile_timer, _morph_watch_timer):
        if bpy.app.timers.is_registered(_fn):
            bpy.app.timers.unregister(_fn)
    for _hl in (bpy.app.handlers.undo_post, bpy.app.handlers.redo_post):
        if _on_morph_undo_redo in _hl:
            _hl.remove(_on_morph_undo_redo)

    # Defensive: strip an undo_post handler left behind by a build that had one.
    for _h in list(bpy.app.handlers.undo_post):
        if getattr(_h, "__name__", "") == "_on_undo_post":
            bpy.app.handlers.undo_post.remove(_h)

    # Unregister save_pre handler
    if _on_save_pre in bpy.app.handlers.save_pre:
        bpy.app.handlers.save_pre.remove(_on_save_pre)

    if layers_utils._on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(layers_utils._on_depsgraph_update)

    # Unregister load_post handler
    if _on_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_on_load_post)

    # Unregister msgbus subscriber first
    _unregister_msgbus()

    for cls in reversed(classes):
        unregister_class(cls)

    del bpy.types.Object.sculptLayers
    del bpy.types.Object.sculptLayers_index
    del bpy.types.Object.sculptLayers_parent_coll
    del bpy.types.Object.sculptLayers_parent_coll_ptr
    del bpy.types.Object.sculptLayers_subd_type
    del bpy.types.Object.sculptLayers_version
    del bpy.types.Scene.sculptLayers_scene_id
    del bpy.types.Scene.sculptLayers_bake_mode
    del bpy.types.Scene.sculptLayers_mirror_mode
    del bpy.types.Scene.sculptLayers_mirror_axis
    del bpy.types.Scene.sculptLayers_mirror_source
    del bpy.types.Scene.sculptLayers_pose_name