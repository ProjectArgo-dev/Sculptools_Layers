# SPDX-FileCopyrightText: 2024 mifth
# SPDX-FileCopyrightText: 2026 ProjectArgo
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Part of "Sculptools: Layers", a fork of the Sculpt Layers add-on by mifth,
# modified by ProjectArgo since 2026. See CREDITS.md.

import bpy
from bpy.types import Menu, Panel
from . import layers_utils

# LABEL BUDGET -- how wide a label may be before Blender clips it. Measured
# 2026-10-01 in a 220 px sidebar with region overlap on (the default), on 4.5.13
# and 5.2.2, by dev_probe_label_budget.py; widths are blf.dimensions at the UI
# font size, ui_scale 1:
#
#     in a box (alerts, sub-panel content)    155 px    with an icon 131 px
#     unboxed (panel root, the footer note)   166 px    with an icon 139 px
#
# A clipped label gets an ellipsis, at the end or in the middle, and nothing
# else tells the user that words are missing. Count PIXELS, not characters:
# "Native Multires Subdivide /" (27 chars, 141 px) fits where "Result: 31746
# verts x 3 key(s)" (29 chars, 159 px) did not. A label sharing a row with
# another widget gets less than any of these -- keep labels on their own line.
# dev_test_label_budget.py draws every panel and checks every label.

def _subpanel_column(layout):
    """A column for a sub-panel's content, boxed like the parent panel's sections.

    A sub-panel's layout is NOT inset the way `box()` is, so drawing straight
    onto it makes the widgets start further left *and* end further right than
    every section above them -- reported 2026-08-14 with the right edge marked
    on a screenshot, and true of `SL_PT_ShapeKeys` / `SL_PT_Data` since they
    were split out.

    This went back and forth once in a single day, so both answers are on the
    record and the reason the loop closed is the important half:

    * **`box()` -- what ships.** Widgets at x=21..178: the same span, frame and
      shade as the parent panel's sections.
    * Two separators around a plain column (`factor=0.85`; measured 0.35 ->
      18..181, 0.70 -> 20..179, 1.00 -> 22..177) give the same width with
      nothing drawn. Built when the box's frame read as a heavy "dark grey
      halo" -- but that was the user's CUSTOM theme, where the sub-panel
      background is far darker than the panel's, so the box sat inside a thick
      dark ring. Looked at again under Blender's DEFAULT theme the box is
      simply consistent, and the unframed version is the odd one out: its
      widgets sit on a lighter background running edge to edge while everything
      above them is framed.

    **Judge this under the default theme.** A custom theme can make either
    version look wrong, and that is what sent it round the loop.
    """
    return layout.box().column(align=True)


class SL_PT_Ii(Panel):
    bl_label = 'Layers'
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Layers"

    @classmethod
    def poll(cls, context):
        active_obj = context.active_object
        if active_obj is None:
            return False
        # A Morph session must stay reachable in ANY mode. During it the
        # original object is hidden and the active object is the temporary
        # copy, so the Apply / Cancel pair drawn by draw() is the only way back.
        # Gating on Sculpt Mode alone made the whole panel vanish the moment the
        # user pressed Tab, leaving a hidden object and no way to recover except
        # running sl.cancel_morph from F3-search.
        if layers_utils.get_morph_mask_source(active_obj) is not None:
            return True
        return active_obj.mode == 'SCULPT'

    # ------------------------------------------------------------------
    # Main draw
    # ------------------------------------------------------------------
    def draw(self, context):
        layout = self.layout
        active_obj = context.active_object

        if not active_obj:
            return

        # Morph session: the active object is the temporary dense copy the
        # Layer brush erases on. Show only Apply / Cancel and stop -- the normal
        # layer controls would operate on the wrong object.
        if layers_utils.get_morph_mask_source(active_obj) is not None:
            box = layout.box()
            col = box.column(align=True)
            col.label(text="Paint to erase", icon='BRUSH_DATA')
            col.label(text="Ctrl restores")
            col.operator("sl.apply_morph", text="Apply Morph", icon='CHECKMARK')
            col.operator("sl.cancel_morph", text="Cancel", icon='X')
            return

        # Resolve sculpt-layer data context (parent_col is used by the
        # SL_PT_Data sub-panel, which resolves it independently)
        _parent_col, base_obj = self._get_layer_context(active_obj)
        has_layers = bool(base_obj and base_obj.sculptLayers)

        # Find a Multires modifier (lightweight check, no side effects).
        # NOTE: do NOT use layers_utils.GetMultires() here — it has side effects
        # (toggles other modifiers) which must never happen during draw().
        multires_mod = layers_utils.find_multires_readonly(active_obj)

        # 0. Hidden object / Dyntopo / changed-topology alerts. Drawn FIRST:
        # they explain why everything below is greyed out. (The Multires
        # out-of-sync alert keeps its own place at 3b, beside the Sync Multires
        # button that repairs it.)
        if has_layers:
            self._draw_hidden_object_section(layout, active_obj)
        self._draw_dyntopo_section(layout, active_obj, base_obj)

        # 0b. Layer data written by a NEWER add-on. Advisory only -- it greys
        # nothing -- but RED like the boxes around it: in this panel red means
        # "your work is at risk", not "the controls below are dead" (see the
        # method's docstring for why it stopped being a grey note).
        self._draw_data_version_section(layout, base_obj)

        # ONE predicate for the whole panel, and it is the same one every
        # operator's poll() consults -- so a greyed button and a greyed slider
        # can never disagree. It is a superset of the section above: Dyntopo, a
        # changed cage, AND a Multires that was removed, switched off or moved to
        # another level. The operators grey themselves through poll(); `blocked`
        # exists for the widgets that are NOT operators (the layer-name field,
        # the SubD type enum, the sculpt-levels slider, the bake settings), which
        # would otherwise stay live in front of unusable data.
        blocked = layers_utils.layers_blocked_reason(context) is not None

        # 1. Active-layer rename field + layer list
        if has_layers:
            col = layout.column()
            col.enabled = not blocked
            idx = base_obj.sculptLayers_index
            if 0 <= idx < len(base_obj.sculptLayers):
                col.prop(base_obj.sculptLayers[idx], "name", text="Name")
            self._draw_layer_list(col, base_obj)

        # 2. Action buttons row (Add / Remove / Move / Merge / Duplicate / Save / Collapse)
        self._draw_action_buttons(layout, has_layers, base_obj)

        # 3. Morph Brush (erase)
        self._draw_morph_brush_section(layout, active_obj, has_layers)

        # 3a. Mirror lives in the SL_PT_Mirror sub-panel (collapsed by default),
        # above Shape Keys. It was an always-open box here until 2026-08-14:
        # four rows of permanent panel space for an occasional operation, in the
        # middle of the controls used on every stroke.

        # 3b. Multires out-of-sync warning + repair
        self._draw_multires_sync_section(layout, active_obj, base_obj)

        # 3c. Wrong shape key active
        self._draw_shape_key_section(layout, active_obj, has_layers)

        # 3d. Active layer hidden or at opacity 0
        self._draw_hidden_layer_section(layout, base_obj, has_layers)

        # 4. Subdivision section (Data lives in the SL_PT_Data sub-panel)
        self._draw_subdivision_section(layout, base_obj, has_layers, multires_mod,
                                       blocked)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _get_layer_context(active_obj):
        """Return (parent_col, base_obj) for the active object, or (None, None)."""
        parent_col = layers_utils.parent_collection(active_obj)
        base_obj = (layers_utils.getBaseObject(parent_col)
                    if parent_col is not None else None)
        return parent_col, base_obj


    # ------------------------------------------------------------------
    # Sections
    # ------------------------------------------------------------------
    def _draw_layer_list(self, layout, base_obj):
        """Custom layer list (replaces template_list to allow auto-save on layer change)."""
        box = layout.box()
        list_col = box.column(align=True)

        # Iterate in reverse: the topmost layer in the UI is the last one in the stack
        for i in range(len(base_obj.sculptLayers) - 1, -1, -1):
            sl_item = base_obj.sculptLayers[i]
            is_active = (i == base_obj.sculptLayers_index)
            row = list_col.row(align=True)

            # --- Layer name button ---
            # Grayed-out on non-active layers (visual cue for the active layer).
            name_sub = row.row(align=True)
            name_sub.active = is_active
            op = name_sub.operator("sl.set_active_layer", text=sl_item.name,
                                   depress=is_active, emboss=is_active)
            op.index = i

            # --- Opacity ---
            # An operator button, NOT layout.prop. A property edit is written by
            # RNA before any of our code can run and Blender snapshots it there,
            # before the mesh is rebuilt -- which is what made one settled drag
            # take two Ctrl+Z presses with an inconsistent state in between.
            # The click opens sl.set_opacity's popup: the slider in there edits
            # the OPERATOR's own value, which has no owner ID and so pushes no
            # undo step, and the single step is pushed by apply_layer_opacity on
            # confirm. Dragging this button directly is not an option -- an
            # operator button fires on LMB RELEASE (measured 2026-08-03), so a
            # modal started from invoke() never sees the press at all.
            opacity_sub = row.row(align=True)
            opacity_sub.scale_x = 0.5
            opacity_sub.operator(
                "sl.set_opacity", text=f"{sl_item.layer_opacity:.2f}").index = i

            # --- Visibility toggle ---
            # Always interactive (same rationale as opacity).
            vis_sub = row.row(align=True)
            vis_icon = 'HIDE_OFF' if sl_item.visible else 'HIDE_ON'
            vis_sub.operator("sl.change_visibility", text="",
                             emboss=False, icon=vis_icon).index = i

    def _draw_action_buttons(self, layout, has_layers, base_obj=None):
        """Row of small action buttons + the big Collapse row."""
        # Empty state: no layers yet -> a single, prominent labeled Add button.
        if not has_layers:
            layout.separator()
            layout.operator("sl.add_layer", text="Add Layer", icon='ADD')
            return

        # Disable the two buttons that create a layer -- "Add" and "Duplicate" --
        # at the hard layer cap (MAX_SCULPT_LAYERS), so the user is prevented
        # from exceeding it rather than hitting a silent no-op. Both operators
        # also refuse the call themselves; this only keeps the UI honest.
        at_limit = (base_obj is not None
                    and len(base_obj.sculptLayers) >= layers_utils.MAX_SCULPT_LAYERS)

        # Small icon buttons row, right-aligned to the panel's content edge.
        # It does NOT line up with the eye column of the list above, whatever
        # this comment used to claim: the list sits in a box(), and a box insets
        # its contents. Measured 2026-08-14 from the sidebar's pixels at default
        # width -- this row ends at x=210, every box's contents at x=192. The
        # 18 px are structural, not a consequence of how many icons are here.
        row = layout.row(align=True)
        row.alignment = 'RIGHT'
        add_sub = row.row(align=True)
        add_sub.enabled = not at_limit
        add_sub.operator("sl.add_layer", text="", icon='ADD')
        row.operator("sl.remove_layer", text="", icon='REMOVE')
        row.separator()
        row.operator("sl.move_layer", text="", icon='TRIA_UP').action = 'UP'
        row.operator("sl.move_layer", text="", icon='TRIA_DOWN').action = 'DOWN'
        row.separator()
        # Invert takes no options and creates no layer, so it belongs in the
        # icon row rather than in a box of its own -- flush against Merge, with
        # no separator between them.
        row.operator("sl.invert_layer", text="", icon='UV_SYNC_SELECT')
        row.operator("sl.merge_layer", text="", icon='SORT_ASC')
        dup_sub = row.row(align=True)
        dup_sub.enabled = not at_limit
        dup_sub.operator("sl.duplicate_layer", text="", icon='DUPLICATE')
        # Nudge the whole group left so the DUPLICATE icon sits centred under
        # the eye column of the list above. Right-aligning alone does not do it:
        # the eye is drawn emboss=False (glyph only) while these are embossed
        # buttons, so their padding pushes the last glyph ~5 px further right.
        # Measured from the sidebar's pixels at 8x zoom, 2026-08-14: without
        # this, eye centre 169.0 against duplicate centre ~174; with 0.7 the two
        # land within half a pixel. A trailing separator is the only lever that
        # works here -- a trailing label(icon='BLANK1') overshoots by a whole
        # icon width.
        row.separator(factor=0.7)

        if at_limit:
            layout.label(
                text=f"Max {layers_utils.MAX_SCULPT_LAYERS} layers reached",
                icon='INFO')

        layout.separator(factor=0.25)

        # Big Collapse row. (There is no manual "Save Layer" button: the active
        # layer is auto-committed on every path that could otherwise lose work --
        # mode change, opacity/visibility, active-layer/topology changes, and
        # .blend save. sl.save_layer still exists and is used internally.)
        # Boxed like the Morph Brush / Subdivision sections below, so all three
        # share the same inset and line up.
        box = layout.box()
        big_row = box.row()
        big_row.scale_y = 2
        big_row.operator("sl.collapse_layers", text="Collapse All",
                         icon='IMPORT')

    def _draw_morph_brush_section(self, layout, active_obj, has_layers):
        """Morph Brush (paint to erase the active layer, gradually).
        One workflow with or without Multires: starts a session on a temporary
        dense copy (see draw() above) where the Layer brush erases the active
        layer as you paint; Apply Morph keeps the result."""
        if not has_layers:
            return
        box = layout.box()
        col = box.column(align=True)
        col.operator("sl.morph_brush", text="Morph Brush", icon='BRUSH_DATA')

    def _draw_multires_sync_section(self, layout, active_obj, base_obj):
        """Alert shown when the layer data is desynced from the object: a
        natively-added Multires, or one switched off / removed.

        Sync Multires is offered only while a Multires is still there to rebuild
        against -- it force-enables show_viewport and re-reads the grid, which
        repairs the switched-off case. With no Multires left there is nothing to
        rebuild from, so the alert states the consequence instead of showing a
        button its own poll() would grey out."""
        if base_obj is None:
            return
        if not layers_utils.multires_out_of_sync(active_obj, base_obj):
            return
        box = layout.box()
        box.alert = True
        col = box.column(align=True)
        col.label(text="Layer data out of sync", icon='ERROR')
        if layers_utils.find_multires_readonly(active_obj) is not None:
            col.label(text="Native Multires detected.")
            col.label(text="Use SubD next time.")
            col.operator("sl.sync_multires", text="Sync Multires",
                         icon='FILE_REFRESH')
        else:
            col.label(text="Vertex counts don't match.")
            col.label(text="Layer edits are not saved.")

    def _draw_shape_key_section(self, layout, active_obj, has_layers):
        """Alert shown when the object has shape keys but the active one is not
        the Basis. Layers describe the neutral shape and read/write the
        reference key, while sculpt strokes land in the ACTIVE key -- so
        nothing would be captured. Saving is refused in that state, and this
        says why rather than letting it fail quietly."""
        if not has_layers or layers_utils.basis_key_active(active_obj):
            return
        box = layout.box()
        box.alert = True
        col = box.column(align=True)
        col.label(text="Shape key is not Basis", icon='ERROR')
        col.label(text="Layers use the Basis key.")
        col.label(text="Sculpt edits aren't saved.")

    def _draw_hidden_layer_section(self, layout, base_obj, has_layers):
        """Alert shown while the ACTIVE layer is hidden or at opacity 0.

        Strokes made then are committed INTO that layer, additively, and the
        mesh is rebuilt -- so they leave the view at the next commit and come
        back with the layer (sl.save_layer; the user's decision, 2026-10-05).
        Correct, and still a surprise without a word: the user is sculpting
        something they will not see. Red like the shape-key alert, which also
        greys nothing: in this panel red means "watch what happens to your
        work", not "the controls are dead"."""
        if not has_layers:
            return
        idx = base_obj.sculptLayers_index
        if not 0 <= idx < len(base_obj.sculptLayers):
            return
        sl = base_obj.sculptLayers[idx]
        if sl.visible and sl.layer_opacity != 0.0:
            return
        box = layout.box()
        box.alert = True
        col = box.column(align=True)
        col.label(text=("Active layer is hidden" if not sl.visible
                        else "Active layer opacity 0"), icon='ERROR')
        col.label(text="Strokes are saved to it")
        col.label(text="but stay out of view.")

    def _draw_hidden_object_section(self, layout, active_obj):
        """Alert shown while the sculpted object itself is hidden (the Outliner
        eye). Blender keeps it in Sculpt Mode, but no layer edit can run on it --
        layers_blocked_reason() greys every control -- so this says why. Before
        it, the controls stayed live and the next click raised a traceback at
        the user (measured 2026-10-05, layers_utils.object_hidden())."""
        if not layers_utils.object_hidden(active_obj):
            return
        box = layout.box()
        box.alert = True
        col = box.column(align=True)
        col.label(text="Object is hidden", icon='ERROR')
        col.label(text="Show it to use layers.")

    def _draw_dyntopo_section(self, layout, active_obj, base_obj):
        """Alert shown when the layer system cannot work on this mesh.

        Two distinct states, and the user needs to know which one they are in:

        * Dyntopo is ON -- preventive. Nothing is broken yet if they stop here.
        * the cage topology already changed -- diagnostic. The deltas are still
          stored but no longer describe this mesh, and they cannot be remapped:
          a delta is one vector per vertex, and 98 of them cannot describe 73730.

        Returns True when either state holds. draw() no longer reads it for the
        greying -- it uses layers_utils.layers_blocked_reason(), a superset that
        also covers a removed / switched-off Multires -- so this is now purely
        "did I draw a box"; the return is kept because it is what makes the two
        states testable apart from the out-of-sync one.

        Deliberately NO repair button. Sync Multires was measured unreachable in
        this state -- its poll() keys off multires_out_of_sync(), the predicate
        that is blind to Dyntopo -- and could not work if it were. The two real
        routes are named in the text."""
        # Every line must fit the sidebar's PIXEL budget for its container: 155
        # px in a box, 131 with an icon (LABEL BUDGET, top of this file). "The
        # mesh topology changed." (152 px) renders whole, "Dyntopo and remeshing
        # do this." (166 px) is clipped, and nothing tells the user something
        # was cut. dev_test_label_budget.py checks every label.
        if layers_utils.dyntopo_active(active_obj):
            box = layout.box()
            box.alert = True
            col = box.column(align=True)
            col.label(text="Dyntopo is active", icon='ERROR')
            col.label(text="Turn it off to use layers.")
            return True
        if layers_utils.topology_changed(active_obj, base_obj):
            box = layout.box()
            box.alert = True
            col = box.column(align=True)
            col.label(text="Layer data unusable", icon='ERROR')
            col.label(text="The mesh topology changed.")
            # There used to be a "Dyntopo or a remesh." line here, naming the
            # causes. Removed 2026-08-14: the user met this alert after applying
            # a Mirror modifier, a third cause it did not name, so the line read
            # as a wrong diagnosis rather than a hint. The alert's job is to say
            # WHAT happened and how to get out, not to enumerate every way in.
            col.label(text="Undo to recover layers,")
            col.label(text="or unlink it in Data.")
            return True
        return False

    def _draw_data_version_section(self, layout, base_obj):
        """Warn when the layer data came from a newer add-on version.

        NOT a guard: layers_blocked_reason() is untouched and every control
        stays live (the user's decision, 2026-08-17). What stops US from doing
        damage is layers_utils.migrate_layer_data() leaving newer data strictly
        alone; what the user can still do is sculpt and save, producing a file
        where fields this build does not know sit beside edits it made.

        RED (`alert` + 'ERROR'), like every other risk alert in this panel. It
        was drawn as a grey 'INFO' note until 2026-08-17, on the theory that red
        here means "the controls below are dead" -- which is NOT the panel's
        convention: `_draw_shape_key_section` is red and greys nothing
        (`basis_key_active` is absent from layers_blocked_reason). The rule is
        "your work is at risk", and it applies here.

        Every line is within the sidebar's pixel budget (LABEL BUDGET, top of
        this file): a longer label is clipped and nothing tells the user."""
        if not layers_utils.data_format_newer(base_obj):
            return
        box = layout.box()
        box.alert = True
        col = box.column(align=True)
        col.label(text="Newer layer format", icon='ERROR')
        col.label(text="Made by a newer version.")
        col.label(text="Update to be safe.")

    def _draw_subdivision_section(self, layout, base_obj, has_layers, multires_mod,
                                  blocked=False):
        """Subdivide / Unsubdivide controls + Multires sculpt levels slider.

        The whole box is disabled while the layers are blocked. SubD / UnSubD
        would grey themselves through poll() anyway, but the two widgets beside
        them are plain properties -- the SubD type enum and the Multires
        sculpt-levels slider -- and a live slider in front of layer data that
        cannot be applied is the silent failure this guard exists to stop."""
        if not has_layers:
            return
        box = layout.box()
        col = box.column(align=True)
        col.enabled = not blocked
        row = col.row(align=False)
        row.operator("sl.subdivide_layers", text="SubD", icon='MOD_SUBSURF')
        row.operator("sl.unsubdivide_layers", text="UnSubD", icon='MOD_SUBSURF')
        col.separator()
        col.prop(base_obj, "sculptLayers_subd_type", text='')
        if multires_mod:
            col.separator()
            col.prop(multires_mod, "sculpt_levels", text="Sculpt Levels")


class SL_PT_Mirror(Panel):
    """Dedicated sub-panel: mirror the active layer across a local axis plane.

    The axis and the source side are drawn as TWO rows, not as Blender's single
    six-value symmetrize_direction, because they are not equally meaningful:
    FLIP has no source side. Measured 2026-08-14 on 4.5 -- from a sculpt living
    only on +X, SYMMETRIZE gives 0 non-zero points with "-X to +X" (the empty
    side is the source, so it wipes the work) against 50 with "+X to -X", while
    FLIP gives bit-identical results either way. The single widget therefore
    offered six choices for three behaviours, and the source row is now simply
    absent in Flip rather than inviting a choice that does nothing. The labels
    still read in Blender's own wording, and they follow the chosen axis.

    It was an always-open box in the main panel until 2026-08-14. Mirroring is
    an occasional operation and it was taking four permanent rows in the middle
    of the controls used on every stroke, so it moved here at the user's
    request -- collapsed by default, above Shape Keys."""
    bl_label = 'Mirror Layer'
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Layers"
    bl_parent_id = "SL_PT_Ii"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 0  # above Shape Keys

    @classmethod
    def poll(cls, context):
        # Parent panel already requires SCULPT mode; only add the layer check.
        return layers_utils.poll_has_layers(context)

    def draw(self, context):
        scene = context.scene
        # Boxed like the parent panel's sections -- see _subpanel_column().
        col = _subpanel_column(self.layout)
        # Same predicate the main panel and every poll() use. The operator greys
        # itself through poll(), but these are plain properties and a live enum
        # in front of unusable layer data is the silent failure that guard
        # exists to stop.
        col.enabled = layers_utils.layers_blocked_reason(context) is None

        axis_row = col.row(align=True)
        axis_row.prop(scene, "sculptLayers_mirror_axis", expand=True)

        # expand=True on a COLUMN stacks the items full-width, one per row.
        # They belong side by side, as a pair of toggles -- hence the rows.
        mode_row = col.row(align=True)
        mode_row.prop(scene, "sculptLayers_mirror_mode", expand=True)

        axis = scene.sculptLayers_mirror_axis
        if scene.sculptLayers_mirror_mode == 'SYMMETRIZE':
            src_row = col.row(align=True)
            # prop_enum draws ONE item of an enum, with a label of our choosing:
            # that is what lets these follow the axis without a second property
            # and without draw() writing anything.
            src_row.prop_enum(scene, "sculptLayers_mirror_source", 'POSITIVE',
                              text="+%s to -%s" % (axis, axis))
            src_row.prop_enum(scene, "sculptLayers_mirror_source", 'NEGATIVE',
                              text="-%s to +%s" % (axis, axis))

        op = col.operator("sl.mirror_layer", text="Mirror Layer",
                          icon='MOD_MIRROR')
        op.direction = layers_utils.mirror_direction_id(
            axis, scene.sculptLayers_mirror_source)
        op.mode = scene.sculptLayers_mirror_mode


class SL_MT_DataLink(Menu):
    """Layer-data picker. Rebuilt on every open, so unlike the Scene enum it
    replaced it always reflects the ACTIVE object's link, and re-picking the
    entry that is already current still runs the operator."""
    bl_idname = "SL_MT_DataLink"
    bl_label = "Layer Data"

    def draw(self, context):
        layout = self.layout
        active_obj = context.active_object
        # Compared by identity, not by name: the link is resolved through the
        # collection pointer, so a renamed collection still shows its checkmark.
        current = layers_utils.parent_collection(active_obj)

        # 'None' first, as an explicit unlink (the old enum reserved its first
        # item for the same purpose).
        layout.operator("sl.link_data", text="None",
                        icon='CHECKMARK' if current is None else 'BLANK1'
                        ).coll_name = ""

        colls = layers_utils.list_data_collections(context)
        layout.separator()
        if not colls:
            layout.label(text="No layer data in this file", icon='INFO')
            return

        for coll in colls:
            if coll == current:
                icon = 'CHECKMARK'
            elif layers_utils.data_collection_compatible(active_obj, coll):
                icon = 'BLANK1'
            else:
                # Topology doesn't match -- sl.link_data refuses it and says why.
                icon = 'ERROR'
            layout.operator("sl.link_data", text=coll.name,
                            icon=icon).coll_name = coll.name


class SL_PT_Data(Panel):
    """Dedicated sub-panel: layer-data collection picker + Duplicate Data."""
    bl_label = 'Data'
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Layers"
    bl_parent_id = "SL_PT_Ii"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 2  # below Shape Keys

    @classmethod
    def poll(cls, context):
        # Carried over from the old inline section, this used to also test
        # `'sculptLayers_version' not in active_obj` to hide the panel for data made
        # by a different add-on version. That gate was doubly dead -- the property is
        # a registered RNA one (never in the object's custom-prop keys) and it is
        # written on the BASE object, never on the active one -- so it was always
        # True. The legacy migration it guarded was removed in 1.3.5.
        #
        # Hidden during a Morph session: the parent draws ONLY Apply /
        # Cancel there, but a sub-panel is drawn on its own poll, so this one
        # kept offering Link Data for the temporary Morph copy (found
        # 2026-10-01, dev_probe_audit.py T3).
        active_obj = context.active_object
        return (active_obj is not None
                and layers_utils.get_morph_mask_source(active_obj) is None)

    def draw(self, context):
        layout = self.layout
        active_obj = context.active_object

        parent_col = layers_utils.parent_collection(active_obj)

        col = _subpanel_column(layout)
        # The menu label IS the current link -- single source of truth. There
        # used to be a read-only label here above a Scene-level enum: the enum
        # never followed the active object, so the two routinely disagreed on
        # screen (label showing the real collection, enum showing 'None').
        col.menu("SL_MT_DataLink",
                 text=parent_col.name if parent_col else "None",
                 icon='OUTLINER_COLLECTION')
        # Counts the MESH GROUP -- the Alt+D copies -- not every user of the
        # data: an object with a different mesh (a fresh Shift+D copy) gets its
        # own data on its first edit (layers_utils.ensure_own_data), so it is
        # not sharing anything an edit could reach. The group really is: every
        # layer edit lands in all of them, through the one mesh. Separating one
        # of them is Blender's Make Single User on the MESH; its next edit then
        # splits the layers too. Fits the boxed-with-icon budget (LABEL BUDGET,
        # top of this file) up to four-digit counts.
        users = (len(layers_utils.mesh_group(active_obj))
                 if parent_col is not None else 0)
        if users > 1:
            col.label(text="Shared by %d objects" % users, icon='LINKED')
        col.separator()
        col.row().operator("sl.duplicate_layers", text="Duplicate Data",
                           icon='COPYDOWN')

        # Clean up orphaned/unused layer-data collections. Shown only when a
        # LAYERS_DATA root exists; greyed out when there is nothing unused.
        if layers_utils.GetSculptCollection(context, False) is not None:
            unused = len(layers_utils.get_unused_data_collections(context))
            clean_row = col.row()
            clean_row.enabled = unused > 0
            clean_row.operator("sl.cleanup_data",
                               text="Clean Up Unused Data", icon='TRASH')


class SL_PT_ShapeKeys(Panel):
    """Dedicated sub-panel: bake sculpt layers into animatable shape keys.

    Named after Blender's own term for the feature. It was SL_PT_Blendshapes /
    "Blendshapes" until 2026-08-12 -- Maya/Unreal terminology, and it contradicted
    the button inside it, which always read "Bake to Shape Keys". Renaming the
    class changes the panel's identifier, so Blender forgets the remembered
    per-region order and open/closed state for it and falls back to bl_order /
    DEFAULT_CLOSED: harmless, and that is the intended placement anyway."""
    bl_label = 'Shape Keys'
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Layers"
    bl_parent_id = "SL_PT_Ii"
    bl_options = {'DEFAULT_CLOSED'}
    bl_order = 1  # between Mirror Layer and Data

    @classmethod
    def poll(cls, context):
        # Parent panel already requires SCULPT mode; only add the layer check.
        return layers_utils.poll_has_layers(context)

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        active_obj = context.active_object
        # Boxed like the parent panel's sections -- see _subpanel_column().
        col = _subpanel_column(layout)
        # Same predicate the main panel and every poll() use. sl.bake_shapekeys
        # greys itself, but the mode enum and the pose-name field are plain
        # properties: without this they stay live in front of layer data that
        # cannot produce a deliverable.
        col.enabled = layers_utils.layers_blocked_reason(context) is None

        col.prop(scene, "sculptLayers_bake_mode", text="")
        if scene.sculptLayers_bake_mode == 'COMBINED_POSE':
            col.prop(scene, "sculptLayers_pose_name", text="Name")

        # Free and read-only: the base object already holds the deliverable's
        # vertex count (the deltas are stored at the Multires resolution), and
        # the keys are the visible layers.
        verts, keys = layers_utils.bake_preview_counts(
            active_obj, scene.sculptLayers_bake_mode)
        # No "Result:" prefix: with it, no format fits a box past five-digit
        # counts ("Result: 31746 verts x 3 key(s)" is 159 px against 155), and a
        # Multires deliverable reaches seven. Sitting right above the Bake
        # button, the line reads as the bake's size without it. Thousands
        # separators keep a seven-digit count readable; 1,234,567 verts with a
        # key per layer at the cap is 141 px.
        col.label(text="{:,} verts, {} {}".format(
            verts, keys, "key" if keys == 1 else "keys"))

        # The Rebuild Subdivisions warning is NOT drawn here: it took two lines
        # of permanent panel space for a footgun that only exists after a bake,
        # and only on a Multires source. It lives in the operator's tooltip (see
        # SL_BakeShapeKeys' docstring) and in README.md instead. Do not re-add it
        # as a label -- removed on the user's request, 2026-08-12.
        op = col.operator("sl.bake_shapekeys", text="Bake to Shape Keys",
                          icon='SHAPEKEY_DATA')
        op.mode = scene.sculptLayers_bake_mode
        op.pose_name = scene.sculptLayers_pose_name


class SL_PT_MultiresHint(Panel):
    """Headerless footer note, drawn last (below the Data sub-panel). When the
    active object has sculpt layers AND a Multires, it reminds the user to
    subdivide through the add-on's SubD / UnSubD instead of the native Multires
    Subdivide / Unsubdivide buttons -- those desync the layer base or fail
    outright (Blender's "No valid subdivisions found to rebuild a lower level").

    This used to be an alert box appended to Properties > Modifiers; it was
    moved here as a plain info line because it was too intrusive there."""
    bl_label = ""  # unused: HIDE_HEADER
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Layers"
    bl_parent_id = "SL_PT_Ii"
    bl_options = {'HIDE_HEADER'}
    bl_order = 3  # below Data

    @classmethod
    def poll(cls, context):
        # Parent panel already requires SCULPT mode. Only relevant with a
        # Multires on the object (read-only lookup -- never mutate in poll).
        if layers_utils.find_multires_readonly(context.active_object) is None:
            return False
        return layers_utils.poll_has_layers(context)

    def draw(self, context):
        col = self.layout.column(align=True)
        # Unboxed, so 166 px of room, 139 on the line with the icon (LABEL
        # BUDGET, top of this file). The old wording ran to 202 px and three of
        # its four lines were clipped mid-word in the live sidebar (2026-10-01).
        col.label(text="To change subdivisions,", icon='INFO')
        col.label(text="use SubD / UnSubD above.")
        col.label(text="Native Multires buttons")
        col.label(text="may desync layers or fail.")


def register():
    bpy.utils.register_class(SL_MT_DataLink)
    bpy.utils.register_class(SL_PT_Ii)
    bpy.utils.register_class(SL_PT_Mirror)
    bpy.utils.register_class(SL_PT_ShapeKeys)
    bpy.utils.register_class(SL_PT_Data)
    bpy.utils.register_class(SL_PT_MultiresHint)


def unregister():
    bpy.utils.unregister_class(SL_PT_MultiresHint)
    bpy.utils.unregister_class(SL_PT_Data)
    bpy.utils.unregister_class(SL_PT_ShapeKeys)
    bpy.utils.unregister_class(SL_PT_Mirror)
    bpy.utils.unregister_class(SL_PT_Ii)
    bpy.utils.unregister_class(SL_MT_DataLink)