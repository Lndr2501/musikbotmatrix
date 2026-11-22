"""Qwen-Baer in Blender: per Skript gebaut, mit Gelenken, Tanz-Loops gerendert.

  blender -b -P baer3d.py -- --out DIR [--preview] [--clips a,b] [--variant qwen|generic]
  blender -b -P baer3d.py -- --out DIR --desk      # nur das DJ-Pult (desk/base.png + desk/glow.png)
  blender -b -P baer3d.py -- --out DIR --stage     # Buehne dahinter (stage/bg.png)

Das Pult steht in jedem Clip als Holdout in der Szene: es schneidet den Baeren aus, wo es vor
ihm steht. Der Bot legt erst das Pult-Bild, dann den Baeren darueber -> Pfoten liegen auf dem
Controller, der Bauch verschwindet dahinter.

Jeder Clip ist genau 4 Beats lang, 16 Bilder pro Beat (64 Bilder), nahtlos loopbar.
Der Bot spielt die Bilder im Songtempo ab -> Bewegung sitzt exakt auf dem Takt.
"""
import json
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = argv[argv.index("--out") + 1] if "--out" in argv else "/tmp/baer3d"
PREVIEW = "--preview" in argv
VARIANT = argv[argv.index("--variant") + 1] if "--variant" in argv else "generic"
ONLY = argv[argv.index("--clips") + 1].split(",") if "--clips" in argv else None
DESK_ONLY = "--desk" in argv
STAGE_ONLY = "--stage" in argv
TEX = os.path.dirname(os.path.abspath(__file__))
FPB = 16                 # Bilder pro Beat
BEATS = 4
RES = (448, 560)
TAU = 2 * math.pi
CAM_Z, CAM_DIST, LENS, SENSOR = 2.15, 6.6, 50, 24
CAM_TILT = 6.0               # Grad nach unten: man sieht auf den Controller
PLATE = (1024, 560)          # Pult-Bild: gleiche Kamera, nur breiter (gleiche Pixel pro Einheit)
STAGE = (1120, 640)          # Buehne: deckt das ganze Ausgabebild (oben/unten je 40 px mehr)
DESK_Z, HEAD_Z = 0.85, 2.45      # Pultkante und Kopfmitte (Blender-Einheiten) -> Bot skaliert danach


# ------------------------------------------------------------------ Szene
def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    for eng in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            sc.render.engine = eng
            break
        except TypeError:
            continue
    sc.render.resolution_x, sc.render.resolution_y = RES
    sc.render.resolution_percentage = 100
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    try:
        sc.eevee.taa_render_samples = 32
    except AttributeError:
        pass
    try:
        sc.view_settings.view_transform = "Standard"
        sc.view_settings.look = "None"
    except TypeError:
        pass
    world = bpy.data.worlds.new("w")
    sc.world = world
    world.use_nodes = True
    bg = world.node_tree.nodes.get("Background")
    bg.inputs[0].default_value = (0.05, 0.045, 0.07, 1)
    bg.inputs[1].default_value = 1.0
    return sc


def srgb(h):
    h = h.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(((x + 0.055) / 1.055) ** 2.4 if x > 0.04045 else x / 12.92 for x in c) + (1,)


def material(name, color, rough=0.4, coat=0.0, metal=0.0, sss=0.0, emit=None):
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    p = m.node_tree.nodes.get("Principled BSDF")
    p.inputs["Base Color"].default_value = srgb(color) if isinstance(color, str) else color
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    for key, val in (("Coat Weight", coat), ("Coat Roughness", 0.15), ("Subsurface Weight", sss)):
        if key in p.inputs:
            p.inputs[key].default_value = val
    if "Subsurface Radius" in p.inputs:
        p.inputs["Subsurface Radius"].default_value = (0.3, 0.15, 0.08)
    if emit and "Emission Color" in p.inputs:
        p.inputs["Emission Color"].default_value = srgb(emit)
        p.inputs["Emission Strength"].default_value = 1.0
    return m


def image_material(name, path):
    m = bpy.data.materials.new(name)
    try:
        m.use_nodes = True
    except Exception:
        pass
    nt = m.node_tree
    p = nt.nodes.get("Principled BSDF")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = bpy.data.images.load(path)
    nt.links.new(tex.outputs["Color"], p.inputs["Base Color"])
    nt.links.new(tex.outputs["Alpha"], p.inputs["Alpha"])
    p.inputs["Roughness"].default_value = 0.35
    if "Emission Color" in p.inputs:
        nt.links.new(tex.outputs["Color"], p.inputs["Emission Color"])
        p.inputs["Emission Strength"].default_value = 0.35
    for attr, val in (("surface_render_method", "DITHERED"), ("blend_method", "HASHED")):
        try:
            setattr(m, attr, val)
        except (AttributeError, TypeError):
            pass
    return m


def smooth(obj, sub=0):
    for poly in obj.data.polygons:
        poly.use_smooth = True
    if sub:
        mod = obj.modifiers.new("sub", "SUBSURF")
        mod.levels = mod.render_levels = sub
    return obj


def sphere(name, loc, radii, mat, seg=48):
    bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=seg // 2, radius=1, location=loc)
    o = bpy.context.active_object
    o.name = name
    o.scale = radii
    o.data.materials.append(mat)
    return smooth(o)


def capsule(name, loc, radii, length, mat, seg=48):
    """Kapsel: Halbkugeln (radii) mit geradem Mittelstueck der Laenge `length` (entlang Z)."""
    bpy.ops.mesh.primitive_uv_sphere_add(segments=seg, ring_count=seg // 2, radius=1, location=loc)
    o = bpy.context.active_object
    o.name = name
    for v in o.data.vertices:
        x, y, z = v.co
        off = length / 2 if z > 1e-6 else -length / 2 if z < -1e-6 else 0.0
        v.co = (x * radii[0], y * radii[1], z * radii[2] + off)
    o.data.materials.append(mat)
    return smooth(o)


def cylinder(name, loc, r, depth, mat, rot=(0, 0, 0), bevel=0.03):
    bpy.ops.mesh.primitive_cylinder_add(vertices=48, radius=r, depth=depth, location=loc, rotation=rot)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    if bevel:
        b = o.modifiers.new("bev", "BEVEL")
        b.width, b.segments = bevel, 4
    return smooth(o)


def torus(name, loc, major, minor, mat, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_torus_add(major_radius=major, minor_radius=minor, location=loc, rotation=rot,
                                     major_segments=64, minor_segments=16)
    o = bpy.context.active_object
    o.name = name
    o.data.materials.append(mat)
    return smooth(o)


def empty(name, loc, parent=None):
    bpy.ops.object.empty_add(type="PLAIN_AXES", location=loc, radius=0.05)
    e = bpy.context.active_object
    e.name = name
    e.rotation_mode = "XYZ"
    if parent is not None:
        attach(e, parent)
    return e


def attach(child, parent):
    bpy.context.view_layer.update()
    child.parent = parent
    child.matrix_parent_inverse = parent.matrix_world.inverted()


def curve(name, pts, mat, depth=0.012):
    cu = bpy.data.curves.new(name, "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = depth
    cu.bevel_resolution = 4
    sp = cu.splines.new("POLY")
    sp.points.add(len(pts) - 1)
    for p, c in zip(sp.points, pts):
        p.co = (*c, 1)
    o = bpy.data.objects.new(name, cu)
    bpy.context.collection.objects.link(o)
    o.data.materials.append(mat)
    return o


def on_ellipsoid(c, r, x, z, inset=0.0):
    """Punkt auf der Vorderseite (-Y) eines Ellipsoids bei (x, z)."""
    dx, dz = (x - c[0]) / r[0], (z - c[2]) / r[2]
    k = max(0.0, 1 - dx * dx - dz * dz)
    return (x, c[1] - r[1] * math.sqrt(k) + inset, z)


# ------------------------------------------------------------------ Baer
def build():
    fur = material("fur", "#D98A3D", rough=0.34, coat=0.3, sss=0.05)
    fur_dark = material("fur_dark", "#B86B2C", rough=0.35, coat=0.3, sss=0.05)
    muzzle_m = material("muzzle", "#B8702F", rough=0.35, coat=0.3)
    black = material("black", "#0C0C0D", rough=0.2, coat=0.5)
    eye_m = material("eye", "#1A1210", rough=0.3, coat=0.4)
    shirt_col = "#18181B" if VARIANT == "qwen" else "#3B2A8C"
    shirt = material("shirt", shirt_col, rough=0.75)
    shorts = material("shorts", "#7A5230", rough=0.7)
    phone = material("phone", "#1C1C1F", rough=0.3, metal=0.2, coat=0.4)
    phone_ring = material("phone_ring", "#5A5A62", rough=0.35, metal=0.6)
    mouth_m = material("mouth_in", "#6B1E1E", rough=0.5)
    tongue = material("tongue", "#E07A7A", rough=0.5)
    cup_w = material("cup_white", "#F4F2EE", rough=0.4)
    cup_band = material("cup_band", "#6248E6", rough=0.45)

    J = {}
    J["root"] = empty("root", (0, 0, 0))
    J["hips"] = empty("hips", (0, 0, 0.62), J["root"])
    J["chest"] = empty("chest", (0, 0, 0.95), J["hips"])
    J["head"] = empty("head", (0, 0, 1.45), J["chest"])
    for s, x in (("L", 1), ("R", -1)):
        J["shoulder_" + s] = empty("shoulder_" + s, (0.44 * x, 0, 1.40), J["chest"])
        J["elbow_" + s] = empty("elbow_" + s, (0.52 * x, 0, 1.02), J["shoulder_" + s])
        J["hand_" + s] = empty("hand_" + s, (0.55 * x, 0, 0.72), J["elbow_" + s])
        J["leg_" + s] = empty("leg_" + s, (0.2 * x, 0, 0.55), J["hips"])

    # Kopf
    hc, hr = (0, 0, 1.93), (0.56, 0.50, 0.52)
    head = sphere("head_m", hc, hr, fur, seg=64)
    parts = [head]
    for s, x in (("L", 1), ("R", -1)):
        ear = sphere("ear_" + s, (0.40 * x, 0.02, 2.33), (0.15, 0.09, 0.15), fur)
        inner = sphere("ear_in_" + s, (0.40 * x, -0.045, 2.32), (0.09, 0.04, 0.09), fur_dark)
        parts += [ear, inner]
        if s == "L":
            for i, (bx, bz) in enumerate(((0.555, 2.0), (0.535, 2.1))):
                parts.append(sphere(f"bump_{s}{i}", (bx * x, -0.02, bz), (0.045, 0.045, 0.045), fur))
        ex, ez = 0.23 * x, 1.99
        eye = sphere("eye_" + s, on_ellipsoid(hc, hr, ex, ez, 0.014), (0.085, 0.026, 0.03), eye_m)
        eye.rotation_euler = (0, math.radians(-7 * x), math.radians(-24 * x))
        parts.append(eye)
    parts.append(sphere("bump_top", (-0.27, 0.0, 2.40), (0.05, 0.05, 0.05), fur))
    mc, mr = (0, -0.405, 1.79), (0.165, 0.085, 0.19)
    parts.append(sphere("muzzle", mc, mr, muzzle_m))
    parts.append(sphere("nose", on_ellipsoid(mc, mr, 0, 1.885, 0.02), (0.075, 0.05, 0.05), black))
    line = [on_ellipsoid(mc, mr, 0, z, -0.004) for z in (1.86, 1.83, 1.80)]
    smile = [on_ellipsoid(mc, mr, x, 1.775 + 0.045 * (x / 0.075) ** 2, -0.004)
             for x in [(-0.075 + i * 0.015) for i in range(11)]]
    mouth_line = curve("mouth_line", line, eye_m, 0.009)
    mouth_smile = curve("mouth_smile", smile, eye_m, 0.009)
    mouth_open = sphere("mouth_open", on_ellipsoid(mc, mr, 0, 1.755, 0.022), (0.075, 0.03, 0.06), mouth_m)
    mo_tongue = sphere("mouth_tongue", on_ellipsoid(mc, mr, 0, 1.725, 0.026), (0.05, 0.026, 0.028), tongue)
    parts += [mouth_line, mouth_smile, mouth_open, mo_tongue]
    for p in parts:
        attach(p, J["head"])

    # Koerper
    torso = capsule("shirt", (0, 0, 1.1), (0.46, 0.36, 0.28), 0.42, shirt)
    attach(torso, J["chest"])
    neck = sphere("neck", (0, 0, 1.52), (0.3, 0.26, 0.12), fur)
    attach(neck, J["chest"])
    # Logo auf der Brust (auf das Shirt gewickelt)
    logo_path = os.path.join(TEX, "logo_qwen.png" if VARIANT == "qwen" else "logo_note.png")
    if os.path.exists(logo_path):
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=24, y_subdivisions=24, size=1,
                                        location=(0.0, -0.42, 1.17), rotation=(math.pi / 2, 0, 0))
        logo = bpy.context.active_object
        logo.name = "logo"
        logo.scale = (0.34, 0.38, 1)
        logo.data.materials.append(image_material("logo", logo_path))
        sw = logo.modifiers.new("wrap", "SHRINKWRAP")
        sw.target, sw.offset = torso, 0.006
        attach(logo, J["chest"])
    # Kopfhoerer um den Hals
    band = torus("phone_band", (0, 0.06, 1.50), 0.33, 0.04, phone, rot=(math.radians(-12), 0, 0))
    attach(band, J["chest"])
    for s, x in (("L", 1), ("R", -1)):
        rot = (0, math.radians(90), math.radians(-28 * x))
        cup = cylinder("phone_cup_" + s, (0.31 * x, -0.17, 1.45), 0.13, 0.09, phone, rot=rot, bevel=0.02)
        ring = torus("phone_ring_" + s, (0.355 * x, -0.19, 1.45), 0.1, 0.018, phone_ring, rot=rot)
        attach(cup, J["chest"])
        attach(ring, J["chest"])
    # Arme
    for s, x in (("L", 1), ("R", -1)):
        sleeve = sphere("sleeve_" + s, (0.47 * x, 0, 1.31), (0.2, 0.19, 0.18), shirt)
        upper = capsule("upper_" + s, (0.485 * x, 0, 1.21), (0.15, 0.15, 0.15), 0.3, fur)
        attach(sleeve, J["shoulder_" + s])
        attach(upper, J["shoulder_" + s])
        elbow = sphere("elbow_" + s, (0.52 * x, 0, 1.02), (0.152, 0.152, 0.152), fur)
        attach(elbow, J["shoulder_" + s])
        fore = capsule("fore_" + s, (0.535 * x, 0, 0.86), (0.148, 0.148, 0.148), 0.22, fur)
        attach(fore, J["elbow_" + s])
        paw = sphere("paw_" + s, (0.555 * x, -0.01, 0.68), (0.175, 0.17, 0.17), fur)
        attach(paw, J["hand_" + s])
    finger = sphere("finger", (-0.56, -0.14, 0.67), (0.045, 0.11, 0.045), fur)
    attach(finger, J["hand_R"])
    # Hose + Beine
    pelvis = capsule("shorts", (0, 0, 0.6), (0.42, 0.33, 0.15), 0.1, shorts)
    attach(pelvis, J["hips"])
    for s, x in (("L", 1), ("R", -1)):
        leg = capsule("leg_" + s, (0.2 * x, 0, 0.36), (0.18, 0.18, 0.12), 0.26, shorts)
        foot = sphere("foot_" + s, (0.21 * x, -0.06, 0.09), (0.19, 0.26, 0.11), fur)
        attach(leg, J["leg_" + s])
        attach(foot, J["leg_" + s])
    # Kaffeebecher (nur im Kaffee-Clip sichtbar)
    cup = []
    cup.append(cylinder("cup", (-0.75, -0.1, 0.74), 0.1, 0.3, cup_w, bevel=0.01))
    cup.append(cylinder("cup_band", (-0.75, -0.1, 0.72), 0.104, 0.15, cup_band, bevel=0.005))
    cup.append(cylinder("cup_lid", (-0.75, -0.1, 0.90), 0.108, 0.05, black, bevel=0.015))
    for c in cup:
        attach(c, J["hand_R"])
    return J, {"line": mouth_line, "smile": mouth_smile, "open": mouth_open, "tongue": mo_tongue,
               "finger": finger, "cup": cup}


# ------------------------------------------------------------------ DJ-Pult
GLOW = []          # (Material, Grundstaerke) -> set_glow skaliert alle Leuchtflaechen


def glow_material(name, color, strength):
    m = material(name, "#050507", rough=0.4)
    p = m.node_tree.nodes.get("Principled BSDF")
    p.inputs["Emission Color"].default_value = srgb(color) if isinstance(color, str) else color
    p.inputs["Emission Strength"].default_value = strength
    GLOW.append((m, strength))
    return m


def set_glow(k):
    for m, base_strength in GLOW:
        m.node_tree.nodes.get("Principled BSDF").inputs["Emission Strength"].default_value = base_strength * k


def box(name, loc, size, mat, bevel=0.0, rot=(0, 0, 0)):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc, rotation=rot)
    o = bpy.context.active_object
    o.name = name
    o.scale = size
    o.data.materials.append(mat)
    if bevel:
        b = o.modifiers.new("bev", "BEVEL")
        b.width, b.segments = bevel, 3
        b.use_clamp_overlap = True
    return o


def dot_texture(w=480, h=120, step=6, r=1.9):
    """LED-Gitter: Punkte, in der Mitte blau, aussen lila (wie eine LED-Front)."""
    img = bpy.data.images.new("leds", w, h, alpha=False)
    px = [0.0] * (w * h * 4)
    for y in range(h):
        for x in range(w):
            i = (y * w + x) * 4
            dx, dy = (x % step) - step / 2 + 0.5, (y % step) - step / 2 + 0.5
            t = min(1.0, abs(x / w - 0.5) * 2.2)
            if dx * dx + dy * dy <= r * r:
                c = (0.12 + 0.5 * t, 0.35 - 0.2 * t, 1.0)
            else:
                c = (0.01, 0.01, 0.015)
            px[i:i + 4] = (*c, 1.0)
    img.pixels = px
    return img


def led_panel_material(strength):
    m = bpy.data.materials.new("led_panel")
    m.use_nodes = True
    nt = m.node_tree
    p = nt.nodes.get("Principled BSDF")
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = dot_texture()
    tex.interpolation = "Closest"
    p.inputs["Base Color"].default_value = (0.01, 0.01, 0.015, 1)
    p.inputs["Roughness"].default_value = 0.5
    nt.links.new(tex.outputs["Color"], p.inputs["Emission Color"])
    p.inputs["Emission Strength"].default_value = strength
    GLOW.append((m, strength))
    return m


def build_desk():
    """Booth mit LED-Front + DJ-Controller. Liefert (Objekte, Regionen) fuer Holdout/Manifest."""
    body = material("booth", "#16141F", rough=0.32, metal=0.65)
    lip = material("booth_top", "#211E2B", rough=0.42, metal=0.5)
    ctl = material("controller", "#121216", rough=0.38, metal=0.3)
    deck = material("deck", "#1C1C23", rough=0.3, metal=0.5)
    platter = material("platter", "#0B0B0E", rough=0.25, coat=0.5)
    silver = material("silver", "#9A9AA6", rough=0.18, metal=0.95)
    knob = material("knob", "#2A2A31", rough=0.35, metal=0.3)
    cap = material("fader_cap", "#C9C9D2", rough=0.3, metal=0.6)
    slot = material("slot", "#050506", rough=0.6)
    edge = glow_material("edge", "#B44CFF", 7.0)
    edge_b = glow_material("edge_blue", "#4C7BFF", 6.0)
    panel = led_panel_material(2.6)
    objs, regions = [], {}

    def add(o, group=None):
        objs.append(o)
        if group:
            regions.setdefault(group, []).append(o)
        return o

    # Booth: Mittelteil mit LED-Front, schraege Seitenfluegel, Deckplatte auf Brusthoehe
    TOP = 0.91
    FY = -1.0                                            # Vorderkante Mittelteil
    add(box("booth_body", (0, -0.66, (TOP - 0.1) / 2), (2.0, 0.68, TOP - 0.1), body, bevel=0.015))
    add(box("booth_back", (0, -0.46, (TOP - 0.1) / 2), (3.2, 0.3, TOP - 0.1), body))
    add(box("booth_top", (0, -0.72, TOP - 0.05), (3.75, 0.82, 0.1), lip, bevel=0.03))
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, FY - 0.006, (TOP - 0.1) / 2 + 0.01),
                                     rotation=(math.pi / 2, 0, 0))
    led = bpy.context.active_object
    led.name = "led_front"
    led.scale = (1.94, TOP - 0.24, 1)
    led.data.materials.append(panel)
    add(led, "front")
    wing_dx, wing_dy = 0.66, 0.4
    a = math.atan2(wing_dy, wing_dx)
    nx, ny = math.sin(a), -math.cos(a)                    # Frontnormale rechter Fluegel
    H = TOP - 0.1
    for sx in (1, -1):
        cx, cy = sx * (1.0 + wing_dx / 2), FY + wing_dy / 2
        add(box(f"wing_{sx}", (cx - sx * nx * 0.06, cy - ny * 0.06, H / 2),
                (math.hypot(wing_dx, wing_dy) + 0.02, 0.12, H), body, bevel=0.012, rot=(0, 0, a * sx)))
        def on_wing(f, z, off=0.012):
            return (sx * (1.0 + wing_dx * f + nx * off), FY + wing_dy * f + ny * off, z)
        add(curve(f"wing_diag_{sx}", [on_wing(0.12, H - 0.08), on_wing(0.88, 0.1)], edge, 0.009), "edges")
        add(curve(f"wing_diag2_{sx}", [on_wing(0.42, H - 0.08), on_wing(0.95, 0.38)], edge, 0.007), "edges")
        add(curve(f"joint_{sx}", [(sx * 0.995, FY - 0.016, 0.04), (sx * 0.995, FY - 0.016, H - 0.02)],
                  edge_b, 0.01), "edges")
    add(curve("lip_glow", [(-1.86, -1.135, TOP - 0.1), (1.86, -1.135, TOP - 0.1)], edge, 0.01), "edges")
    add(curve("floor_glow", [(-1.0 - wing_dx, FY + wing_dy - 0.03, 0.02), (-1.0, FY - 0.03, 0.02),
                             (1.0, FY - 0.03, 0.02), (1.0 + wing_dx, FY + wing_dy - 0.03, 0.02)],
              edge, 0.012), "edges")

    # Controller: Gelenk-Empty, Teile ungekippt bauen, dann kippen
    c = empty("controller", (0, -0.74, TOP))
    parts = []

    def part(o, group=None):
        parts.append(o)
        return add(o, group)

    Z = TOP
    part(box("ctl_base", (0, -0.74, Z + 0.03), (2.75, 0.6, 0.06), ctl, bevel=0.02))
    for sx, led_col in ((-1, "#35D6FF"), (1, "#B44CFF")):
        x = 0.88 * sx
        part(box(f"deck_{sx}", (x, -0.74, Z + 0.065), (0.96, 0.54, 0.012), deck, bevel=0.006))
        part(cylinder(f"jog_{sx}", (x, -0.70, Z + 0.09), 0.23, 0.04, platter, bevel=0.01))
        part(torus(f"jog_ring_{sx}", (x, -0.70, Z + 0.11), 0.23, 0.016, silver))
        ring = part(torus(f"jog_led_{sx}", (x, -0.70, Z + 0.08), 0.258, 0.008,
                          glow_material(f"jog_led_m{sx}", led_col, 5.0)), f"jog_{'L' if sx < 0 else 'R'}")
        part(cylinder(f"jog_disp_{sx}", (x, -0.70, Z + 0.113), 0.07, 0.006,
                      glow_material(f"jog_disp_m{sx}", "#35D6FF", 2.0), bevel=0.0), "pads")
        part(cylinder(f"jog_dot_{sx}", (x, -0.70, Z + 0.118), 0.018, 0.006, platter, bevel=0.0))
        for i, col in enumerate(("#FF9A1F", "#FF9A1F", "#35D6FF", "#B44CFF")):
            part(box(f"pad_{sx}{i}", (x - 0.27 + i * 0.18, -0.955, Z + 0.075), (0.13, 0.065, 0.014),
                     glow_material(f"pad_m{sx}{i}", col, 3.0), bevel=0.004), "pads")
        for i in range(3):
            part(box(f"btn_{sx}{i}", (x + sx * 0.36, -0.86 + i * 0.09, Z + 0.075), (0.06, 0.05, 0.012),
                     knob, bevel=0.003))
        part(box(f"pitch_{sx}", (x - sx * 0.35, -0.68, Z + 0.073), (0.02, 0.3, 0.006), slot))
        part(box(f"pitch_cap_{sx}", (x - sx * 0.35, -0.70, Z + 0.083), (0.06, 0.03, 0.018), cap, bevel=0.004))
    part(box("mixer", (0, -0.74, Z + 0.065), (0.66, 0.54, 0.012), ctl, bevel=0.006))
    meters = []
    for ch, x in enumerate((-0.225, -0.075, 0.075, 0.225)):
        for row in range(3):
            part(cylinder(f"knob_{ch}{row}", (x, -0.56 + row * 0.07, Z + 0.085), 0.022, 0.03, knob, bevel=0.005))
        part(box(f"fslot_{ch}", (x, -0.85, Z + 0.073), (0.012, 0.16, 0.006), slot))
        part(box(f"fcap_{ch}", (x, -0.84 - 0.03 * (ch % 2), Z + 0.085), (0.05, 0.026, 0.022), cap, bevel=0.004))
        segs = []
        for k in range(9):
            col = "#2EE6A8" if k < 5 else "#FFD23F" if k < 7 else "#FF3B6B"
            segs.append(part(box(f"meter_{ch}_{k}", (x + 0.045, -0.93 + k * 0.032, Z + 0.073),
                                 (0.018, 0.022, 0.006), glow_material(f"meter_m{ch}{k}", col, 4.0))))
        meters.append(segs)
    part(box("xfade_slot", (0, -0.965, Z + 0.073), (0.2, 0.012, 0.006), slot))
    part(box("xfade_cap", (0.02, -0.965, Z + 0.085), (0.03, 0.05, 0.022), cap, bevel=0.004))
    for o in parts:
        attach(o, c)
    c.rotation_euler = (math.radians(10), 0, 0)
    objs.append(c)
    for ch, segs in enumerate(meters):
        regions[f"meter_{ch}"] = segs
    return objs, regions


def build_stage():
    """Buehne hinter dem Baeren: Boden, LED-Wand (dunkel, EQ malt der Bot), Truss mit Scheinwerfern,
    Publikum als Silhouetten. Liefert (Objekte, Regionen, Scheinwerfer-Linsen)."""
    import random
    rnd = random.Random(7)
    floor_m = material("floor", "#030305", rough=0.5, metal=0.1)
    wall_m = material("wall", "#08070D", rough=0.7)
    truss_m = material("truss", "#3A3A46", rough=0.35, metal=0.8)
    housing = material("housing", "#0A0A0D", rough=0.4, metal=0.4)
    crowd_m = material("crowd", "#000000", rough=1.0)
    crowd_m.node_tree.nodes.get("Principled BSDF").inputs["Specular IOR Level"].default_value = 0.0
    objs, regions, lenses = [], {}, []
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 1.5, 0))
    fl = bpy.context.active_object
    fl.scale = (16, 12, 1)
    fl.data.materials.append(floor_m)
    objs.append(fl)
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 5.2, 3), rotation=(math.pi / 2, 0, 0))
    wl = bpy.context.active_object
    wl.scale = (16, 8, 1)
    wl.data.materials.append(wall_m)
    objs.append(wl)
    # LED-Wand: gedimmtes Punktraster, der Bot laesst darauf den EQ laufen
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 4.9, 1.85), rotation=(math.pi / 2, 0, 0))
    led = bpy.context.active_object
    led.name = "eq_wall"
    led.scale = (6.8, 2.3, 1)
    led.data.materials.append(led_panel_material(0.35))
    objs.append(led)
    regions["eq"] = [led]
    # Truss: zwei Tuerme + Traeger oben, vorne ein Traeger fuer die Scheinwerfer
    def truss(a, b, w=0.32, name="truss"):
        a, b = Vector(a), Vector(b)
        d = b - a
        L = d.length
        n = max(2, int(L / 0.45))
        up = Vector((0, 0, 1)) if abs(d.normalized().z) < 0.9 else Vector((1, 0, 0))
        u = d.normalized().cross(up).normalized() * (w / 2)
        v = d.normalized().cross(u).normalized() * (w / 2)
        corners = [u + v, u - v, -u - v, -u + v]
        out = []
        for i, c in enumerate(corners):
            out.append(curve(f"{name}_c{i}", [tuple(a + c), tuple(b + c)], truss_m, 0.022))
        for i, (c1, c2) in enumerate(zip(corners, corners[1:] + corners[:1])):
            pts = [tuple(a + d * (k / n) + (c1 if k % 2 == 0 else c2)) for k in range(n + 1)]
            out.append(curve(f"{name}_z{i}", pts, truss_m, 0.012))
        return out
    for sx in (-1, 1):
        objs += truss((4.3 * sx, 4.4, 0), (4.3 * sx, 4.4, 3.7), name=f"tower{sx}")
        objs += truss((4.3 * sx, 2.4, 0), (4.3 * sx, 2.4, 3.2), name=f"tower_f{sx}")
    objs += truss((-4.3, 4.4, 3.7), (4.3, 4.4, 3.7), name="top")
    objs += truss((-4.3, 2.4, 3.2), (4.3, 2.4, 3.2), name="front")
    lens_cols = ("#C45CFF", "#4C8BFF", "#FFFFFF", "#FFFFFF", "#4C8BFF", "#C45CFF")
    for i, x in enumerate((-3.4, -2.1, -0.75, 0.75, 2.1, 3.4)):
        h = cylinder(f"spot_{i}", (x, 2.4, 2.92), 0.16, 0.34, housing, bevel=0.02)
        h.rotation_euler = (math.radians(25), 0, 0)
        objs.append(h)
        lens = cylinder(f"lens_{i}", (x, 2.33, 2.76), 0.12, 0.02, glow_material(f"lens_m{i}", lens_cols[i], 9.0),
                        bevel=0.0)
        lens.rotation_euler = (math.radians(25), 0, 0)
        objs.append(lens)
        lenses.append((x, 2.33, 2.76))
    for sx in (-1, 1):                                    # Par-Lichter am Boden
        for i in range(2):
            x = sx * (3.2 + 0.55 * i)
            objs.append(box(f"par_{sx}{i}", (x, 1.2, 0.12), (0.42, 0.2, 0.22), housing, bevel=0.02))
            for k in range(4):
                objs.append(cylinder(f"par_l{sx}{i}{k}", (x - 0.15 + k * 0.1, 1.09, 0.13), 0.035, 0.01,
                                     glow_material(f"par_m{sx}{i}{k}", "#FFB066", 6.0), rot=(math.pi / 2, 0, 0),
                                     bevel=0.0))
    # Publikum: Silhouetten zwischen Pult und LED-Wand, manche mit Armen oben
    for row, (y, z0) in enumerate(((2.2, -0.3), (3.1, -0.3))):
        n = 13 - row * 2
        for k in range(n):
            x = -3.9 + 7.8 * (k + 0.5 + rnd.uniform(-0.3, 0.3)) / n
            hgt = rnd.uniform(1.45, 1.75) + z0
            sc = rnd.uniform(0.9, 1.1)
            objs.append(sphere(f"head_{row}_{k}", (x, y, hgt), (0.13 * sc, 0.13 * sc, 0.15 * sc), crowd_m, seg=24))
            objs.append(capsule(f"body_{row}_{k}", (x, y, hgt - 0.95), (0.27 * sc, 0.17, 0.2), 1.3, crowd_m,
                                seg=24))
            if rnd.random() < 0.4:
                sx = rnd.choice((-1, 1))
                arm = capsule(f"arm_{row}_{k}", (x + sx * 0.25, y, hgt + 0.2), (0.06, 0.06, 0.06), 0.5, crowd_m,
                              seg=16)
                arm.rotation_euler = (0, math.radians(-18 * sx), 0)
                objs.append(arm)
    # Licht von der LED-Wand nach vorn (Gegenlicht fuers Publikum), etwas Dunst
    for name, x, col in (("wall_l", -2.2, (0.65, 0.3, 1.0)), ("wall_c", 0, (0.3, 0.5, 1.0)),
                         ("wall_r", 2.2, (0.65, 0.3, 1.0))):
        d = bpy.data.lights.new(name, "AREA")
        d.energy, d.color, d.size = 500, col, 2.5
        o = bpy.data.objects.new(name, d)
        bpy.context.collection.objects.link(o)
        o.location = (x, 4.6, 2.0)
        o.rotation_euler = (math.radians(-90), 0, 0)
        objs.append(o)
    world = bpy.context.scene.world
    nt = world.node_tree
    vol = nt.nodes.new("ShaderNodeVolumeScatter")
    vol.inputs["Density"].default_value = 0.0025
    out = next(n for n in nt.nodes if n.type == "OUTPUT_WORLD")
    nt.links.new(vol.outputs[0], out.inputs["Volume"])
    return objs, regions, lenses


def project_points(points, res):
    from bpy_extras.object_utils import world_to_camera_view
    sc = bpy.context.scene
    out = []
    for pt in points:
        co = world_to_camera_view(sc, sc.camera, Vector(pt))
        out.append([round(co.x * res[0], 1), round((1 - co.y) * res[1], 1)])
    return out


def project_regions(regions, res):
    """Objektgruppen -> Pixel-Boxen im Pult-Bild (x0, y0, x1, y1)."""
    from bpy_extras.object_utils import world_to_camera_view
    sc = bpy.context.scene
    bpy.context.view_layer.update()
    out = {}
    for name, objs in regions.items():
        xs, ys = [], []
        for o in objs:
            for corner in o.bound_box:
                co = world_to_camera_view(sc, sc.camera, o.matrix_world @ Vector(corner))
                xs.append(co.x * res[0])
                ys.append((1 - co.y) * res[1])
        out[name] = [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)]
    return out


def anchor_rows(res):
    """Bildzeile von Boden und Kopfoberkante (Mitte x=0, y=0) -> Bot skaliert danach."""
    from bpy_extras.object_utils import world_to_camera_view
    sc = bpy.context.scene
    bpy.context.view_layer.update()              # sonst steht die Kamera noch im Ursprung
    row = lambda z: round((1 - world_to_camera_view(sc, sc.camera, Vector((0, 0, z))).y) * res[1], 2)  # noqa: E731
    return {"floor": row(0.0), "head": row(HEAD_Z)}


def lights_and_camera():
    def area(name, loc, energy, color, size, target=(0, 0, 1.4)):
        d = bpy.data.lights.new(name, "AREA")
        d.energy, d.color, d.size = energy, color, size
        o = bpy.data.objects.new(name, d)
        bpy.context.collection.objects.link(o)
        o.location = loc
        direction = Vector(target) - Vector(loc)
        o.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    area("key", (-2.6, -4.0, 4.0), 420, (1.0, 0.95, 0.88), 3.0)
    area("fill", (3.2, -3.5, 1.8), 130, (0.9, 0.93, 1.0), 4.0)
    area("rim_pink", (-2.8, 2.6, 3.2), 380, (1.0, 0.35, 0.85), 2.0)
    area("rim_cyan", (2.8, 2.6, 2.8), 380, (0.3, 0.85, 1.0), 2.0)
    area("desk_top", (0, -2.2, 3.2), 160, (0.85, 0.8, 1.0), 3.0, target=(0, -0.8, 0.75))
    cam_d = bpy.data.cameras.new("cam")
    cam_d.lens = LENS
    cam_d.sensor_fit = "VERTICAL"
    cam_d.sensor_height = SENSOR
    cam = bpy.data.objects.new("cam", cam_d)
    bpy.context.collection.objects.link(cam)
    cam.location = (0, -CAM_DIST, CAM_Z)
    cam.rotation_euler = (math.radians(90 - CAM_TILT), 0, 0)
    bpy.context.scene.camera = cam


# ------------------------------------------------------------------ Bewegung
def wave(t, per=1.0, ph=0.0):
    return math.sin(TAU * (t / per + ph))


def bob(t, per=1.0):
    """0 genau auf dem Beat, 1 dazwischen; weich (Cosinus)."""
    return 0.5 - 0.5 * math.cos(TAU * t / per)


def onbeat(t, per=1.0):
    """1 auf dem Beat, 0 dazwischen; weich."""
    return 0.5 + 0.5 * math.cos(TAU * t / per)


def arms_down(p, swing=0.0):
    p["shoulder_L"] = (0.05 + swing, -0.12, 0)
    p["shoulder_R"] = (0.05 - swing, 0.12, 0)
    p["elbow_L"] = (-0.15, 0, 0)
    p["elbow_R"] = (-0.15, 0, 0)


def base(t, nod=0.1, bounce=0.03, sway=0.03):
    p = {}
    p["root_loc"] = (0, 0, -bounce * onbeat(t))
    p["hips"] = (0, sway * wave(t, 2), 0)
    p["chest"] = (0.04 * onbeat(t), -sway * 0.8 * wave(t, 2), 0.04 * wave(t, 4))
    p["head"] = (nod * onbeat(t, 1) - 0.02, 0.05 * wave(t, 2, 0.25), 0.05 * wave(t, 4, 0.1))
    arms_down(p, 0.06 * wave(t, 2))
    return p


def clip_mix(t):
    p = base(t, nod=0.13, bounce=0.035)
    p["shoulder_L"] = (-0.85 + 0.05 * onbeat(t), -0.18 + 0.12 * wave(t, 2), 0)
    p["elbow_L"] = (-0.75, 0, 0)
    p["shoulder_R"] = (-0.85 + 0.08 * wave(t, 0.5), 0.15, 0)
    p["elbow_R"] = (-0.8 - 0.15 * wave(t, 1, 0.25), 0, 0)
    return p


def clip_groove(t):
    p = base(t, nod=0.16, bounce=0.05, sway=0.07)
    p["root_loc"] = (0.05 * wave(t, 2), 0, -0.05 * onbeat(t))
    p["shoulder_L"] = (-0.3 + 0.25 * wave(t, 2), -0.35 - 0.1 * onbeat(t), 0)
    p["shoulder_R"] = (-0.3 - 0.25 * wave(t, 2), 0.35 + 0.1 * onbeat(t), 0)
    p["elbow_L"] = (-1.3, 0, 0)
    p["elbow_R"] = (-1.3, 0, 0)
    return p


def clip_pump(t):
    p = base(t, nod=0.18, bounce=0.05)
    p["shoulder_R"] = (-0.4, 1.95 + 0.25 * onbeat(t), 0)
    p["elbow_R"] = (0, 0.45 + 0.7 * bob(t), 0)
    p["shoulder_L"] = (-0.85, -0.18, 0)
    p["elbow_L"] = (-0.75, 0, 0)
    return p


def clip_cheer(t):
    p = base(t, nod=0.06, bounce=0.06)
    s = 0.18 * wave(t, 2)
    p["shoulder_L"] = (-0.35, -2.0 + s - 0.12 * onbeat(t), 0)
    p["shoulder_R"] = (-0.35, 2.0 + s + 0.12 * onbeat(t), 0)
    p["elbow_L"] = (0, -0.45 + 0.15 * onbeat(t), 0)
    p["elbow_R"] = (0, 0.45 - 0.15 * onbeat(t), 0)
    p["head"] = (-0.12 + 0.06 * onbeat(t), 0.08 * wave(t, 2), 0)
    return p


def clip_jump(t):
    p = clip_cheer(t)
    ph = (t % 2) / 2
    air = math.sin(math.pi * ph) ** 1.5
    p["root_loc"] = (0, 0, 0.45 * air - 0.06 * (1 - air))
    p["leg_L"] = (-0.35 * air, 0, 0)
    p["leg_R"] = (0.25 * air, 0, 0)
    return p


def clip_clap(t):
    p = base(t, nod=0.1, bounce=0.04)
    open_ = bob(t)
    p["shoulder_L"] = (-1.25 - 0.1 * onbeat(t), 0.25 - 0.55 * open_, -0.2)
    p["shoulder_R"] = (-1.25 - 0.1 * onbeat(t), -0.25 + 0.55 * open_, 0.2)
    p["elbow_L"] = (-0.35, 0, 0.35)
    p["elbow_R"] = (-0.35, 0, -0.35)
    p["head"] = (-0.1 + 0.06 * onbeat(t), 0, 0)
    return p


def clip_wave(t):
    p = base(t, nod=0.06, bounce=0.02)
    p["shoulder_R"] = (-0.3, 1.75, 0)
    p["elbow_R"] = (0, 1.0 + 0.45 * wave(t, 1), 0)
    p["head"] = (0.02, 0.1, 0.06)
    return p


def clip_point(t):
    p = base(t, nod=0.05, bounce=0.02, sway=0.02)
    p["shoulder_R"] = (-1.45 + 0.08 * onbeat(t), 0.05, -0.1)
    p["elbow_R"] = (-0.1, 0, 0)
    p["shoulder_L"] = (0.1, -0.45, 0)
    p["elbow_L"] = (-1.4, 0.4, 0)
    p["head"] = (0.04 + 0.03 * onbeat(t), -0.05, 0.04 * wave(t, 4))
    return p


def clip_talk(t):
    p = base(t, nod=0.04, bounce=0.015, sway=0.02)
    p["shoulder_L"] = (-0.7 + 0.15 * wave(t, 2), -0.25, 0)
    p["elbow_L"] = (-0.9 + 0.25 * wave(t, 1.3), 0.2, 0)
    p["shoulder_R"] = (-0.6 + 0.15 * wave(t, 2, 0.5), 0.25, 0)
    p["elbow_R"] = (-1.0 + 0.25 * wave(t, 1.7, 0.3), -0.2, 0)
    p["head"] = (0.03 * wave(t, 1.5), 0.06 * wave(t, 4), 0.08 * wave(t, 4, 0.3))
    return p


def clip_coffee(t):
    p = base(t, nod=0.06, bounce=0.02, sway=0.03)
    sip = max(0.0, math.sin(math.pi * (t - 2) / 2)) if 2 <= t <= 4 else 0.0
    sx, ex = -0.55 - 0.35 * sip, -1.1 - 0.35 * sip
    p["shoulder_R"] = (sx, 0.15 + 0.1 * sip, 0.0)
    p["elbow_R"] = (ex, 0, 0)
    p["hand_R"] = (-(sx + ex) - 0.5 * sip, 0, 0)      # Becher bleibt aufrecht, kippt nur beim Trinken
    p["head"] = (0.02 - 0.12 * sip, -0.04 * sip, -0.1 * sip)
    p["shoulder_L"] = (0.05, -0.12, 0)
    return p


def clip_spin(t):
    p = clip_cheer(t)
    e = (1 - math.cos(math.pi * t / 4)) / 2
    p["root_rot"] = (0, 0, TAU * e)
    return p


def clip_headbang(t):
    p = base(t, nod=0.0, bounce=0.06)
    p["head"] = (0.38 * onbeat(t) - 0.1, 0, 0)
    p["chest"] = (0.18 * onbeat(t), 0, 0)
    p["shoulder_L"] = (-0.35, -0.3, 0)
    p["shoulder_R"] = (-0.4, 2.05 + 0.2 * onbeat(t), 0)
    p["elbow_L"] = (-1.2, 0, 0)
    p["elbow_R"] = (0, 0.6, 0)
    return p


CLIPS = {
    # name: (funktion, art, mund, extra)
    "mix": (clip_mix, "groove", 0, None),
    "groove": (clip_groove, "groove", 0, None),
    "pump": (clip_pump, "groove", 0, None),
    "headbang": (clip_headbang, "hype", 1, None),
    "cheer": (clip_cheer, "hype", 1, None),
    "jump": (clip_jump, "hype", 1, None),
    "clap": (clip_clap, "hype", 1, None),
    "spin": (clip_spin, "hype", 1, None),
    "wave": (clip_wave, "special", 0, None),
    "coffee": (clip_coffee, "special", 0, "cup"),
    "talk": (clip_talk, "talk", "both", None),
    "point": (clip_point, "talk", "both", "finger"),
}


def apply_pose(J, p):
    for name, e in J.items():
        e.rotation_euler = (0, 0, 0)
    rest = getattr(apply_pose, "rest", None)
    if rest is None:
        apply_pose.rest = rest = {n: e.location.copy() for n, e in J.items()}
    for n, e in J.items():
        e.location = rest[n]
    for key, val in p.items():
        if key == "root_loc":
            J["root"].location = rest["root"] + Vector(val)
        elif key == "root_rot":
            J["root"].rotation_euler = val
        elif key in J:
            J[key].rotation_euler = val


def set_mouth(M, amount):
    show_open = amount > 0
    M["open"].hide_render = not show_open
    M["tongue"].hide_render = not show_open
    M["smile"].hide_render = show_open
    M["line"].hide_render = False


def set_extra(M, extra):
    M["finger"].hide_render = extra != "finger"
    for c in M["cup"]:
        c.hide_render = extra != "cup"


def render_to(path):
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)


def main():
    reset()
    J, M = build()
    D, regions = build_desk()
    lights_and_camera()
    os.makedirs(OUT, exist_ok=True)
    sc = bpy.context.scene
    if PREVIEW or DESK_ONLY:
        sc.render.resolution_x, sc.render.resolution_y = PLATE
    if STAGE_ONLY:
        st_objs, st_regions, lenses = build_stage()
        for o in list(D):
            o.hide_render = True
        keep = set(st_objs) | {o for o in bpy.data.objects if o.type in ("LIGHT", "CAMERA")}
        for o in bpy.data.objects:
            if o not in keep:
                o.hide_render = True
        sc.render.resolution_x, sc.render.resolution_y = STAGE
        sc.camera.data.sensor_height = SENSOR * STAGE[1] / RES[1]     # gleiche Pixel pro Einheit
        sc.render.film_transparent = False
        try:
            sc.eevee.taa_render_samples = 128
        except AttributeError:
            pass
        try:
            sc.eevee.use_volumetric_lights = True
            sc.eevee.volumetric_end = 40
        except AttributeError:
            pass
        os.makedirs(os.path.join(OUT, "stage"), exist_ok=True)
        set_glow(1.0)
        render_to(os.path.join(OUT, "stage", "bg.png"))
        info = {"size": list(STAGE), "oy": (STAGE[1] - RES[1]) / 2, "anchors": anchor_rows(STAGE),
                "regions": project_regions(st_regions, STAGE), "spots": project_points(lenses, STAGE)}
        with open(os.path.join(OUT, "stage", "stage.json"), "w") as fh:
            json.dump(info, fh, indent=1)
        print("STAGE", json.dumps(info), flush=True)
        return
    if DESK_ONLY:
        keep = set(D) | {o for o in bpy.data.objects if o.type in ("LIGHT", "CAMERA")}
        for o in bpy.data.objects:
            if o not in keep:
                o.hide_render = True
        os.makedirs(os.path.join(OUT, "desk"), exist_ok=True)
        set_glow(0.22)
        render_to(os.path.join(OUT, "desk", "base.png"))
        set_glow(1.0)
        render_to(os.path.join(OUT, "desk", "glow.png"))
        info = {"size": list(PLATE), "anchors": anchor_rows(PLATE), "regions": project_regions(regions, PLATE)}
        with open(os.path.join(OUT, "desk", "desk.json"), "w") as fh:
            json.dump(info, fh, indent=1)
        print("DESK", json.dumps(info), flush=True)
        return
    if PREVIEW:
        set_extra(M, None)
        set_mouth(M, 0)
        apply_pose(J, base(0))
        render_to(os.path.join(OUT, "preview_rest.png"))
        for name, t in (("cheer", 0.5), ("mix", 0.25), ("point", 0.0), ("coffee", 3.0), ("jump", 1.0),
                        ("clap", 0.5)):
            fn, kind, mouth, extra = CLIPS[name]
            set_extra(M, extra)
            set_mouth(M, 1 if mouth else 0)
            apply_pose(J, fn(t))
            render_to(os.path.join(OUT, f"preview_{name}.png"))
        return
    for o in D:                                   # Pult schneidet den Baeren aus, rendert selbst nicht
        try:
            o.is_holdout = True
        except AttributeError:
            o.hide_render = True
    anchors = anchor_rows(RES)
    manifest = {"fpb": FPB, "beats": BEATS, "size": list(RES), "anchors": anchors, "clips": {}}
    mpath = os.path.join(OUT, "manifest.json")
    if ONLY and os.path.exists(mpath):
        with open(mpath) as fh:
            manifest = json.load(fh)
        manifest["anchors"] = anchors
        manifest.pop("units", None)
    for name, (fn, kind, mouth, extra) in CLIPS.items():
        if ONLY and name not in ONLY:
            continue
        set_extra(M, extra)
        variants = [("", 0), ("_open", 1)] if mouth == "both" else [("", mouth)]
        for suffix, m in variants:
            set_mouth(M, m)
            d = os.path.join(OUT, name + suffix)
            os.makedirs(d, exist_ok=True)
            for f in range(FPB * BEATS):
                apply_pose(J, fn(f / FPB))
                render_to(os.path.join(d, f"{f:03d}.png"))
            print("CLIP", name + suffix, flush=True)
        manifest["clips"][name] = {"kind": kind, "talk": mouth == "both"}
    with open(os.path.join(OUT, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)


main()
