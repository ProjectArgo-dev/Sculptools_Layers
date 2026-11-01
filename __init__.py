# SPDX-FileCopyrightText: 2024 mifth
# SPDX-FileCopyrightText: 2026 ProjectArgo
#
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Part of "Sculptools: Layers", a fork of the Sculpt Layers add-on by mifth,
# modified by ProjectArgo since 2026. See CREDITS.md.

# Blender 4.2+ extension: metadata lives in blender_manifest.toml, not bl_info.
# (Legacy add-on support via bl_info has been dropped.)

from . import layers_base
from . import ui


def register():
    layers_base.register()
    ui.register()


def unregister():
    layers_base.unregister()
    ui.unregister()


if __name__ == "__main__":
    register()