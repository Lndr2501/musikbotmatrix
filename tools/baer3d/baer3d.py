"""Qwen-Baer in Blender: per Skript gebaut, mit Gelenken, Tanz-Loops gerendert.

  blender -b -P baer3d.py -- --out DIR [--preview] [--clips a,b] [--variant qwen|generic]
  blender -b -P baer3d.py -- --out DIR --scale 1.5   # fuer 720p (Plueschbaer, ~1,4 s pro Bild)
  blender -b -P baer3d.py -- --out DIR --clips talk,point --edges-only   # nur Ein-/Ausstiege
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
EDGES_ONLY = "--edges-only" in argv   # nur Ein-/Ausstiege neu rendern (mit --clips)
DESK_ONLY = "--desk" in argv
SHEET = "--sheet" in argv          # Vorschau: jeder Clip (aus --clips) zu 4 Zeitpunkten, mit Pult
FRAME = argv[argv.index("--frame") + 1] if "--frame" in argv else None   # clip:t -> ein Bild in voller Groesse
STAGE_ONLY = "--stage" in argv
MODEL = int(argv[argv.index("--model") + 1]) if "--model" in argv else 2   # 1 = alter Kugel-Baer
TEX = os.path.dirname(os.path.abspath(__file__))
FPB = 16                 # Bilder pro Beat
BEATS = 4
SCALE = float(argv[argv.index("--scale") + 1]) if "--scale" in argv else 1.0   # 1.5 -> fuer 720p
RES = (round(448 * SCALE), round(560 * SCALE))
TAU = 2 * math.pi
CAM_Z, CAM_DIST, LENS, SENSOR = 2.15, 6.6, 50, 24
CAM_TILT = 6.0               # Grad nach unten: man sieht auf den Controller
PLATE = (round(1024 * SCALE), round(560 * SCALE))          # Pult-Bild: gleiche Kamera, nur breiter (gleiche Pixel pro Einheit)
STAGE = (round(1120 * SCALE), round(640 * SCALE))          # Buehne: deckt das ganze Ausgabebild (oben/unten je 40 px mehr)
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
        sc.eevee.taa_render_samples = SAMPLES
    except AttributeError:
        pass
    _try(sc.eevee, "shadow_pool_size", "1024")
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


# ------------------------------------------------------------------ Baer v2 (Pluesch)
# Ein weicher Koerper (Metaballs -> Mesh) statt Einzelteilen, per Armature an die Gelenk-Empties
# gebunden (Copy Transforms) -> alle Clips bleiben gleich. Fell als Partikel-Haare, Gesicht mit
# Augen/Glanzpunkt/Brauen/Baeckchen, Kopfhoerer auf dem Kopf, Ohren wackeln nach.
from mathutils import Matrix, Quaternion  # noqa: E402

MB_K = 0.575                 # Metaball (Steifheit 2): Oberflaeche bei 0.575 * Radius
A_POSE = 1.0                 # Arme beim Modellieren seitlich abgespreizt (rad) -> saubere Gewichte
FUR_LEN = 0.034
SHIRT_OUT = 0.028           # Shirt liegt so weit ueber der Haut (Fell sticht nicht durch)
FUR_KIDS = int(argv[argv.index("--fur-kids") + 1]) if "--fur-kids" in argv else 14
SAMPLES = int(argv[argv.index("--samples") + 1]) if "--samples" in argv else 20


def _smooth01(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def _rot_y(angle):
    return Matrix.Rotation(angle, 4, "Y")


def _seg_dist(p, a, b):
    """(Abstand, Parameter 0..1) von Punkt p zur Strecke a-b."""
    ab = b - a
    t = max(0.0, min(1.0, (p - a).dot(ab) / max(1e-9, ab.length_squared)))
    return (p - (a + ab * t)).length, t


def _try(obj, attr, val):
    try:
        setattr(obj, attr, val)
    except (AttributeError, TypeError):
        print("skip", attr)


def fur_material(name, root, tip, rough=0.55):
    """Haar-Material: Wurzel dunkler, Spitzen heller, je Haar leicht anders."""
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    p = nt.nodes.get("Principled BSDF")
    info = nt.nodes.new("ShaderNodeHairInfo")
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = srgb(root)
    ramp.color_ramp.elements[1].color = srgb(tip)
    nt.links.new(info.outputs["Intercept"], ramp.inputs["Fac"])
    mix = nt.nodes.new("ShaderNodeMix")
    mix.data_type = "RGBA"
    mix.blend_type = "MULTIPLY"
    nt.links.new(ramp.outputs["Color"], mix.inputs[6])
    var = nt.nodes.new("ShaderNodeMapRange")
    nt.links.new(info.outputs["Random"], var.inputs["Value"])
    var.inputs["To Min"].default_value = 0.82
    var.inputs["To Max"].default_value = 1.08
    comb = nt.nodes.new("ShaderNodeCombineColor")
    for i in range(3):
        nt.links.new(var.outputs["Result"], comb.inputs[i])
    nt.links.new(comb.outputs["Color"], mix.inputs[7])
    mix.inputs["Factor"].default_value = 1.0
    nt.links.new(mix.outputs[2], p.inputs["Base Color"])
    p.inputs["Roughness"].default_value = rough
    if "Sheen Weight" in p.inputs:
        p.inputs["Sheen Weight"].default_value = 0.4
    return m


def skin_material(name, color, rough=0.6):
    """Untergrund unter dem Fell / Samt: matt mit etwas Sheen (wirkt flauschig)."""
    m = material(name, color, rough=rough, sss=0.08)
    p = m.node_tree.nodes.get("Principled BSDF")
    if "Sheen Weight" in p.inputs:
        p.inputs["Sheen Weight"].default_value = 0.8
        p.inputs["Sheen Roughness"].default_value = 0.4
    return m


def fabric_material(name, color):
    """Baumwoll-Shirt: matt, feine Strick-Struktur, wenig Glanz."""
    m = material(name, color, rough=0.92)
    nt = m.node_tree
    p = nt.nodes.get("Principled BSDF")
    if "Sheen Weight" in p.inputs:
        p.inputs["Sheen Weight"].default_value = 0.35
        p.inputs["Sheen Roughness"].default_value = 0.5
    if "Specular IOR Level" in p.inputs:
        p.inputs["Specular IOR Level"].default_value = 0.25
    tex = nt.nodes.new("ShaderNodeTexNoise")
    tex.inputs["Scale"].default_value = 260.0
    tex.inputs["Detail"].default_value = 2.0
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.25
    bump.inputs["Distance"].default_value = 0.002
    nt.links.new(tex.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], p.inputs["Normal"])
    return m


def add_fur(obj, mat, count, length, children=24, group=None, rough=0.012, clump=0.5, comb=0.45,
            curl=0.003):
    """Kurzes Pluesch-Fell als Partikel-Haar (folgt der Verformung). clump: Spitzen buescheln,
    comb: nach unten gekaemmt (Anteil der Haarlaenge), curl: leichte Locke (Blender-Einheiten)."""
    obj.data.materials.append(mat)
    slot = len(obj.data.materials)
    mod = obj.modifiers.new("fur", "PARTICLE_SYSTEM")
    ps = obj.particle_systems[mod.name] if mod.name in obj.particle_systems else obj.particle_systems[-1]
    st = ps.settings
    st.type = "HAIR"
    st.count = count
    st.emit_from = "FACE"
    _try(st, 'use_emit_random', True)
    _try(st, 'use_even_distribution', True)
    st.material = slot
    st.child_type = "INTERPOLATED"
    _try(st, 'child_nbr', 2)
    st.rendered_child_count = children
    st.child_length = 1.0
    _try(st, 'child_length_threshold', 0.25)
    st.clump_factor = clump
    _try(st, "clump_shape", -0.35)
    st.roughness_1 = rough
    st.roughness_1_size = 0.6
    st.roughness_endpoint = rough * 0.8
    _try(st, 'roughness_end_shape', 1.0)
    _try(st, "roughness_2", rough * 0.35)
    _try(st, "roughness_2_size", 1.0)
    if curl:
        _try(st, "kink", "CURL")
        _try(st, "kink_amplitude", curl)
        _try(st, "kink_frequency", 2.0)
        _try(st, "kink_shape", 0.0)
    st.root_radius = 1.0
    st.tip_radius = 0.28
    st.radius_scale = 0.0085
    st.display_step = 2
    st.render_step = 2
    _try(st, "length_random", 0.35)
    _try(st, 'use_close_tip', True)
    st.hair_length = length          # zuletzt: setzt intern die Normal-Geschwindigkeit
    if comb:                         # Strich nach unten wie bei einem gebuersteten Teddy
        _try(st, "object_align_factor", (0.0, 0.0, -comb * st.normal_factor))
    if group:
        ps.vertex_group_density = group
    ps.seed = 7
    return ps


def mb_body(name, elements, resolution=0.018):
    """Metaball-Elemente -> ein Mesh. elements: (typ, co, radien|(halbe_laenge, dicke), rot_quat)."""
    mb = bpy.data.metaballs.new(name)
    mb.resolution = mb.render_resolution = resolution
    mb.threshold = 0.6
    o = bpy.data.objects.new(name + "_mb", mb)
    bpy.context.collection.objects.link(o)
    for typ, co, dims, rot in elements:
        e = mb.elements.new()
        e.type = typ
        e.co = co
        e.stiffness = 2.0
        if typ == "BALL":
            e.radius = dims / MB_K
        elif typ == "ELLIPSOID":
            e.radius = 1.0 / MB_K
            e.size_x, e.size_y, e.size_z = dims
        elif typ == "CAPSULE":
            half, thick = dims
            e.radius = thick / MB_K
            e.size_x = half
        if rot is not None:
            e.rotation = rot
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(o.evaluated_get(dg))
    bpy.data.objects.remove(o)
    obj = bpy.data.objects.new(name, me)
    bpy.context.collection.objects.link(obj)
    for poly in me.polygons:
        poly.use_smooth = True
    return obj


def capsule_el(a, b, thick):
    """Metaball-Kapsel zwischen zwei Punkten (Achse lokal X)."""
    a, b = Vector(a), Vector(b)
    d = b - a
    rot = Vector((1, 0, 0)).rotation_difference(d.normalized())
    return ("CAPSULE", (a + b) / 2, (d.length / 2, thick), rot)


def surface_point(obj, x, z, inset=0.0, front=-1):
    """Punkt auf der Vorderseite eines Objekts bei (x, z) per Raycast (Ruhe-Pose)."""
    origin = Vector((x, front * 5.0, z))
    ok, loc, normal, _ = obj.ray_cast(obj.matrix_world.inverted() @ origin, Vector((0, -front, 0)))
    if not ok:
        return Vector((x, -0.45, z)), Vector((0, -1, 0))
    w = obj.matrix_world @ loc
    n = (obj.matrix_world.to_3x3() @ normal).normalized()
    return w + n * inset, n


def place(o, loc, normal, spin=0.0):
    """Objekt auf eine Flaeche setzen: lokales -Y zeigt entlang der Normalen."""
    q = Vector((0, -1, 0)).rotation_difference(normal)
    o.rotation_mode = "QUATERNION"
    o.rotation_quaternion = q @ Quaternion((0, 1, 0), spin)
    o.location = loc


def build_v2():
    shirt_col = "#18181B" if VARIANT == "qwen" else "#3B2A8C"
    fur_skin = skin_material("fur_skin", "#C9782F", rough=0.7)
    fur_hair = fur_material("fur_hair", "#8E4A18", "#E89A45")
    cream = skin_material("cream", "#F3D3A4", rough=0.65)
    cream_hair = fur_material("cream_hair", "#D9AE78", "#FFF0D8")
    ear_in = skin_material("ear_in", "#EBB98A", rough=0.7)
    black = material("black", "#0B0A0C", rough=0.12, coat=0.8)
    eye_m = material("eye", "#120C0A", rough=0.05, coat=1.0)
    shine = material("shine", "#FFFFFF", rough=0.2, emit="#FFFFFF")
    brow_m = skin_material("brow", "#6A3A16", rough=0.6)
    blush = material("blush", "#FF7F8F", rough=0.7)
    p_blush = blush.node_tree.nodes.get("Principled BSDF")
    bnt = blush.node_tree
    tco = bnt.nodes.new("ShaderNodeTexCoord")
    vlen = bnt.nodes.new("ShaderNodeVectorMath")
    vlen.operation = "LENGTH"
    bnt.links.new(tco.outputs["Object"], vlen.inputs[0])
    fall = bnt.nodes.new("ShaderNodeMapRange")
    fall.inputs["From Min"].default_value = 0.25
    fall.inputs["From Max"].default_value = 1.0
    fall.inputs["To Min"].default_value = 0.5
    fall.inputs["To Max"].default_value = 0.0
    bnt.links.new(vlen.outputs["Value"], fall.inputs["Value"])
    bnt.links.new(fall.outputs["Result"], p_blush.inputs["Alpha"])
    for attr, val in (("surface_render_method", "DITHERED"), ("blend_method", "HASHED")):
        try:
            setattr(blush, attr, val)
        except (AttributeError, TypeError):
            pass
    shirt = fabric_material("shirt", shirt_col)
    rib = fabric_material("shirt_rib", shirt_col)
    pad_m = skin_material("paw_pad", "#E6A98A", rough=0.5)
    phone = material("phone", "#16161A", rough=0.28, metal=0.3, coat=0.6)
    phone_pad = skin_material("phone_pad", "#2A2A30", rough=0.8)
    phone_metal = material("phone_metal", "#8D8D99", rough=0.2, metal=0.95)
    led = glow_material("phone_led", "#B44CFF", 9.0)
    mouth_m = material("mouth_in", "#4A1014", rough=0.5)
    tongue = material("tongue", "#E8747C", rough=0.45, sss=0.2)
    cup_w = material("cup_white", "#F4F2EE", rough=0.4)
    cup_band = material("cup_band", "#6248E6", rough=0.45)

    J = {}
    J["root"] = empty("root", (0, 0, 0))
    J["hips"] = empty("hips", (0, 0, 0.62), J["root"])
    J["chest"] = empty("chest", (0, 0, 0.95), J["hips"])
    J["head"] = empty("head", (0, 0, 1.45), J["chest"])
    joint_pos = {"root": Vector((0, 0, 0)), "hips": Vector((0, 0, 0.62)), "chest": Vector((0, 0, 0.95)),
                 "head": Vector((0, 0, 1.45))}
    for s, x in (("L", 1), ("R", -1)):
        J["shoulder_" + s] = empty("shoulder_" + s, (0.44 * x, 0, 1.40), J["chest"])
        J["elbow_" + s] = empty("elbow_" + s, (0.52 * x, 0, 1.02), J["shoulder_" + s])
        J["hand_" + s] = empty("hand_" + s, (0.55 * x, 0, 0.72), J["elbow_" + s])
        J["leg_" + s] = empty("leg_" + s, (0.2 * x, 0, 0.55), J["hips"])
        J["ear_" + s] = empty("ear_" + s, (0.36 * x, 0.04, 2.24), J["head"])
        for k in ("shoulder", "elbow", "hand", "leg", "ear"):
            joint_pos[f"{k}_{s}"] = J[f"{k}_{s}"].location.copy()

    # A-Pose fuer die Arme: um die Schulter seitlich hoch
    def apose(s):
        x = 1 if s == "L" else -1
        piv = joint_pos["shoulder_" + s]
        rot = _rot_y(-A_POSE * x)
        return lambda v: piv + (rot @ (Vector(v) - piv).to_4d()).to_3d(), rot

    # ---- Koerper (Metaballs, Arme in A-Pose)
    els = [
        ("ELLIPSOID", (0, 0.02, 1.20), (0.41, 0.33, 0.30), None),        # Brust
        ("ELLIPSOID", (0, -0.03, 0.88), (0.45, 0.38, 0.33), None),       # Bauch
        ("ELLIPSOID", (0, 0.02, 0.60), (0.40, 0.33, 0.22), None),        # Becken
        ("BALL", (0, 0.0, 1.48), 0.25, None),                            # Hals
        ("ELLIPSOID", (0, 0.03, 1.96), (0.53, 0.47, 0.47), None),        # Kopf
        ("ELLIPSOID", (0.25, -0.17, 1.80), (0.21, 0.17, 0.16), None),    # Backen
        ("ELLIPSOID", (-0.25, -0.17, 1.80), (0.21, 0.17, 0.16), None),
    ]
    chains = {}
    for s, x in (("L", 1), ("R", -1)):
        f, rot = apose(s)
        sh, el, ha = joint_pos["shoulder_" + s], joint_pos["elbow_" + s], joint_pos["hand_" + s]
        tip = ha + Vector((0.01 * x, -0.01, -0.17))
        els.append(("BALL", f((0.41 * x, 0.0, 1.35)), 0.17, None))     # Schulter
        els.append(capsule_el(f(sh + Vector((0.01 * x, 0, -0.06))), f(el), 0.15))
        els.append(capsule_el(f(el), f(ha + Vector((0, 0, 0.04))), 0.145))
        els.append(("ELLIPSOID", f(ha + Vector((0.005 * x, -0.015, -0.05))), (0.165, 0.16, 0.16),
                    rot.to_quaternion()))
        chains["arm_" + s] = [f(sh), f(el), f(ha), f(tip)]
        lg = joint_pos["leg_" + s]
        els.append(capsule_el(lg + Vector((0.01 * x, 0, -0.02)), lg + Vector((0.015 * x, 0, -0.38)), 0.175))
        els.append(("ELLIPSOID", (0.22 * x, -0.08, 0.10), (0.17, 0.25, 0.11), None))
        chains["leg_" + s] = [lg, lg + Vector((0.015 * x, 0, -0.42)), Vector((0.22 * x, -0.2, 0.08))]
    body = mb_body("body", els)

    # ---- Gewichte (in A-Pose eindeutig: Arme stehen ab)
    groups = {n: body.vertex_groups.new(name=n) for n in
              ("hips", "chest", "head") + tuple(f"{k}_{s}" for k in ("shoulder", "elbow", "hand", "leg")
                                                  for s in "LR")}
    P = 8.0
    for v in body.data.vertices:
        co = v.co
        rad = math.hypot(co.x, co.y * 1.2)
        s_t = max(0.05, rad / 0.45)
        best = None
        for cname, pts in chains.items():
            r_l = 0.16 if cname.startswith("arm") else 0.18
            dmin, seg, tpar = 1e9, 0, 0.0
            for i in range(len(pts) - 1):
                d, t = _seg_dist(co, pts[i], pts[i + 1])
                if d < dmin:
                    dmin, seg, tpar = d, i, t
            s_l = max(0.05, dmin / r_l)
            share = 1.0 / (1.0 + (s_l / s_t) ** P)
            if best is None or share > best[0]:
                best = (share, cname, seg, tpar, pts)
        share, cname, seg, tpar, pts = best
        if cname.startswith("leg") and co.z > 0.62:
            share *= 1 - _smooth01((co.z - 0.62) / 0.1)
        torso = 1.0 - share
        w = {}
        if share > 1e-3:
            side = cname[-1]
            if cname.startswith("arm"):
                # entlang der Kette: Schulter -> Ellbogen -> Pfote, weich am Gelenk
                lens = [(pts[i + 1] - pts[i]).length for i in range(3)]
                u = sum(lens[:seg]) + lens[seg] * tpar
                e1, e2 = lens[0], lens[0] + lens[1]
                el_w = _smooth01((u - (e1 - 0.07)) / 0.14)
                ha_w = _smooth01((u - (e2 - 0.04)) / 0.08)
                w["shoulder_" + side] = share * (1 - el_w)
                w["elbow_" + side] = share * (el_w - ha_w)
                w["hand_" + side] = share * ha_w
            else:
                w["leg_" + side] = share
        if torso > 1e-3:
            z = co.z
            head = 1.0 if z >= 1.62 else _smooth01((z - 1.40) / 0.2) * (1 - _smooth01((rad - 0.30) / 0.1))
            hips = 1 - _smooth01((z - 0.80) / 0.2)
            chest = max(0.0, 1 - head - hips)
            for n, val in (("head", head), ("hips", hips), ("chest", chest)):
                if val > 1e-3:
                    w[n] = w.get(n, 0.0) + torso * val
        tot = sum(w.values()) or 1.0
        for n, val in w.items():
            if val / tot > 1e-3:
                groups[n].add([v.index], val / tot, "REPLACE")

    # ---- Armature: Knochen in Bindepose (Arme in A-Pose), folgen den Empties (Copy Transforms)
    arm_d = bpy.data.armatures.new("rig")
    rig = bpy.data.objects.new("rig", arm_d)
    bpy.context.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    for name in groups:
        b = arm_d.edit_bones.new(name)
        b.head, b.tail = (0, 0, 0), (0, 0.1, 0)
        pos = joint_pos[name]
        rot = Matrix.Identity(4)
        side = name[-1]
        if name.split("_")[0] in ("shoulder", "elbow", "hand"):
            f, rot = apose(side)
            pos = f(pos)
        b.matrix = Matrix.Translation(pos) @ rot
    bpy.ops.object.mode_set(mode="OBJECT")
    for name in groups:
        c = rig.pose.bones[name].constraints.new("COPY_TRANSFORMS")
        c.target = J[name]
        c.owner_space = c.target_space = "WORLD"
    mod = body.modifiers.new("arm", "ARMATURE")
    mod.object = rig
    mod.use_deform_preserve_volume = True
    body.data.materials.append(fur_skin)

    # ---- Shirt: Kopie des Koerpers, sauber abgeschnitten (Saum, Kragen, kurze Aermel)
    import bmesh
    sh_me = body.data.copy()
    bm = bmesh.new()
    bm.from_mesh(sh_me)
    cuts = [((0, 0, 0.60), (0, 0, -1)), ((0, 0, 1.53), (0, 0, 1))]
    for s in "LR":
        pts = chains["arm_" + s]
        d = (pts[1] - pts[0]).normalized()
        cuts.append((pts[0] + d * 0.17, d))
    for co, no in cuts:
        geom = bm.verts[:] + bm.edges[:] + bm.faces[:]
        res = bmesh.ops.bisect_plane(bm, geom=geom, plane_co=co, plane_no=no, clear_outer=True)
        del res
    # nur der zusammenhaengende Rumpf-Teil (Beine/Pfoten-Reste weg)
    bm.verts.ensure_lookup_table()
    start = min(bm.verts, key=lambda v: (v.co - Vector((0, -0.4, 1.0))).length)
    keep, todo = {start}, [start]
    while todo:
        v = todo.pop()
        for e in v.link_edges:
            o = e.other_vert(v)
            if o not in keep:
                keep.add(o)
                todo.append(o)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if v not in keep], context="VERTS")
    bmesh.ops.remove_doubles(bm, verts=bm.verts[:], dist=0.004)
    bmesh.ops.dissolve_degenerate(bm, edges=bm.edges[:], dist=0.004)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    bm.to_mesh(sh_me)
    bm.free()
    sh_me.materials.clear()
    sh_me.materials.append(shirt)
    shirt_o = bpy.data.objects.new("shirt", sh_me)
    bpy.context.collection.objects.link(shirt_o)
    disp = shirt_o.modifiers.new("inflate", "DISPLACE")
    disp.strength = SHIRT_OUT
    disp.mid_level = 0.0
    sol = shirt_o.modifiers.new("thick", "SOLIDIFY")
    sol.thickness = 0.014
    sol.offset = -1
    sol.use_rim = True
    m2 = shirt_o.modifiers.new("arm", "ARMATURE")
    m2.object = rig
    m2.use_deform_preserve_volume = True
    smooth(shirt_o)

    # ---- Kragen, Aermelsaum, Pfotenballen: in A-Pose gebaut, Gewichte vom naechsten Koerperpunkt
    from mathutils import kdtree
    from mathutils.bvhtree import BVHTree
    kd = kdtree.KDTree(len(body.data.vertices))
    for v in body.data.vertices:
        kd.insert(v.co, v.index)
    kd.balance()
    vw = [{body.vertex_groups[g.group].name: g.weight for g in v.groups} for v in body.data.vertices]
    sh_bvh = BVHTree.FromPolygons([v.co.copy() for v in sh_me.vertices], [pl.vertices[:] for pl in sh_me.polygons])
    body_bvh = BVHTree.FromPolygons([v.co.copy() for v in body.data.vertices],
                                    [pl.vertices[:] for pl in body.data.polygons])

    def bind(o):
        bpy.context.view_layer.update()
        o.data.transform(o.matrix_world)
        o.matrix_world = Matrix.Identity(4)
        for v in o.data.vertices:
            _, idx, _ = kd.find(v.co)
            for gname, w in vw[idx].items():
                if w > 1e-3:
                    vg = o.vertex_groups.get(gname) or o.vertex_groups.new(name=gname)
                    vg.add([v.index], w, "REPLACE")
        m = o.modifiers.new("arm", "ARMATURE")
        m.object = rig
        m.use_deform_preserve_volume = True

    # Schnittkanten des Shirts als Schlaufen: Kragen (oben) und zwei Aermel
    sh_me.calc_loop_triangles()
    ecount = {}
    for pl in sh_me.polygons:
        for ek in pl.edge_keys:
            ecount[ek] = ecount.get(ek, 0) + 1
    nbr = {}
    for (i, j), c in ecount.items():
        if c == 1:
            nbr.setdefault(i, []).append(j)
            nbr.setdefault(j, []).append(i)
    loops, seen = [], set()
    for start in nbr:
        if start in seen:
            continue
        loop, prev, cur = [start], None, start
        seen.add(start)
        while True:
            nxt = [n for n in nbr[cur] if n != prev and n not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            seen.add(cur)
            loop.append(cur)
        if len(loop) > 12:
            loops.append(loop)

    def hem(name, loop, depth):
        pts = [sh_me.vertices[i].co + sh_me.vertices[i].normal * (SHIRT_OUT * 0.8) for i in loop]
        for _ in range(3):                                   # Treppen vom Schnitt glaetten
            pts = [(pts[k - 1] + pts[k] * 2 + pts[(k + 1) % len(pts)]) / 4 for k in range(len(pts))]
        cu = bpy.data.curves.new(name, "CURVE")
        cu.dimensions = "3D"
        cu.bevel_depth = depth
        cu.bevel_resolution = 3
        sp = cu.splines.new("POLY")
        sp.points.add(len(pts) - 1)
        for k, q in enumerate(pts):
            sp.points[k].co = (q.x, q.y, q.z, 1)
        sp.use_cyclic_u = True
        o = bpy.data.objects.new(name, cu)
        bpy.context.collection.objects.link(o)
        bpy.ops.object.select_all(action="DESELECT")
        bpy.context.view_layer.objects.active = o
        o.select_set(True)
        bpy.ops.object.convert(target="MESH")
        o = bpy.context.active_object
        o.data.materials.clear()
        o.data.materials.append(rib)
        smooth(o)
        bind(o)
        return o

    for loop in loops:
        zs = [sh_me.vertices[i].co.z for i in loop]
        xm = sum(sh_me.vertices[i].co.x for i in loop) / len(loop)
        if min(zs) > 1.45:
            hem("collar", loop, 0.03)
        elif abs(xm) > 0.3 and max(zs) > 1.0:
            hem("sleeve_" + ("L" if xm > 0 else "R"), loop, 0.024)
    print("HEMS", [(len(l_), round(min(sh_me.vertices[i].co.z for i in l_), 2)) for l_ in loops], flush=True)
    # Pfotenballen + drei Zehenballen vorne auf der Pfote (sieht man beim Winken/Jubeln)
    pad_spots = []
    for s, x in (("L", 1), ("R", -1)):
        f, rot = apose(s)
        ha = joint_pos["hand_" + s]
        hc = f(ha + Vector((0.005 * x, -0.015, -0.05)))
        hit, nrm, _, _ = body_bvh.ray_cast(hc + Vector((0, -1.0, 0)), Vector((0, 1, 0)))
        if hit is None:
            continue
        up = (chains["arm_" + s][3] - chains["arm_" + s][2]).normalized()
        side = up.cross(nrm).normalized()
        up = nrm.cross(side).normalized()
        pad_spots.append(hit)
        pad = sphere("paw_pad_" + s, (0, 0, 0), (0.07, 0.016, 0.058), pad_m, seg=32)
        place(pad, hit + nrm * 0.014 - up * 0.012, nrm)
        bind(pad)
        PAWS.setdefault(s, []).append(pad)
        for k, (ds, du) in enumerate(((-1, 0.0), (0, 0.012), (1, 0.0))):
            q = hit + up * (0.072 + du) + side * (ds * 0.046)
            loc, n2, _, _ = body_bvh.find_nearest(q)
            if loc is None:
                continue
            bean = sphere(f"paw_bean_{s}{k}", (0, 0, 0), (0.024, 0.012, 0.022), pad_m, seg=24)
            place(bean, loc + n2 * 0.013, n2)
            bind(bean)
            PAWS.setdefault(s, []).append(bean)

    # Fell nur wo kein Shirt ist (spart Haare)
    furg = body.vertex_groups.new(name="fur")
    furl = body.vertex_groups.new(name="furlen")
    for v in body.data.vertices:
        co = v.co
        inside = 0.63 < co.z < 1.50
        for s in "LR":
            pts = chains["arm_" + s]
            d = (pts[1] - pts[0]).normalized()
            if (co - pts[0]).dot(d) > 0.15:
                inside = False
        furg.add([v.index], 0.0 if inside else 1.0, "REPLACE")
        # Gesicht vorne: kuerzeres Fell, damit Augen/Brauen frei bleiben
        e = math.hypot(co.x / 0.36, (co.z - 2.02) / 0.2)
        front = _smooth01((-co.y - 0.12) / 0.15)
        paw = max((1 - _smooth01(((co - ps_).length - 0.12) / 0.1) for ps_ in pad_spots), default=0.0)
        furl.add([v.index], (1.0 - 0.45 * front * (1 - _smooth01((e - 0.6) / 0.6))) * (1 - 0.6 * paw),
                 "REPLACE")
    ps = add_fur(body, fur_hair, count=8000, length=FUR_LEN, children=FUR_KIDS, group="fur")
    ps.vertex_group_length = "furlen"

    # Logo auf dem Shirt
    logo_path = os.path.join(TEX, "logo_qwen.png" if VARIANT == "qwen" else "logo_note.png")
    if os.path.exists(logo_path):
        bpy.ops.mesh.primitive_grid_add(x_subdivisions=24, y_subdivisions=24, size=1,
                                        location=(0.0, -0.45, 1.13), rotation=(math.pi / 2, 0, 0))
        logo = bpy.context.active_object
        logo.name = "logo"
        logo.scale = (0.32, 0.36, 1)
        logo.data.materials.append(image_material("logo", logo_path))
        sw = logo.modifiers.new("wrap", "SHRINKWRAP")
        sw.target, sw.offset = shirt_o, 0.004
        attach(logo, J["chest"])

    # ---- Gesicht (alles starr am Kopf)
    face = []
    muzzle = sphere("muzzle", (0, -0.37, 1.76), (0.205, 0.14, 0.155), cream, seg=64)
    face.append(muzzle)
    bpy.context.view_layer.update()
    mlen = muzzle.vertex_groups.new(name="mfur_len")
    mw = muzzle.matrix_world
    for v in muzzle.data.vertices:
        w = mw @ v.co
        # Mund/Nase frei: kurzes Fell in einem weichen Feld um Mundlinie und Nase
        dx = max(0.0, abs(w.x) - 0.07)
        dz = max(0.0, 1.70 - w.z, w.z - 1.86)
        d = math.hypot(dx, dz)
        front = _smooth01((-w.y - 0.42) / 0.06)
        mlen.add([v.index], 1.0 - 0.8 * front * (1 - _smooth01(d / 0.035)), "REPLACE")
    mps = add_fur(muzzle, cream_hair, count=1500, length=0.018, children=16, rough=0.006, comb=0.15,
                  curl=0.0015)
    mps.vertex_group_length = "mfur_len"
    bpy.context.view_layer.update()
    nose_p, nose_n = surface_point(muzzle, 0, 1.86, 0.01)
    nose = sphere("nose", (0, 0, 0), (0.078, 0.05, 0.052), black)
    place(nose, nose_p, nose_n)
    face.append(nose)
    shine_n = sphere("nose_shine", (0, 0, 0), (0.018, 0.008, 0.01), shine)
    place(shine_n, nose_p + nose_n * 0.045 + Vector((-0.02, 0, 0.02)), nose_n)
    face.append(shine_n)
    # Mund: Strich + Laecheln (zu) / offener Mund mit Zunge
    mid = [surface_point(muzzle, 0, z, 0.003)[0] for z in (1.815, 1.79, 1.765)]
    mouth_line = curve("mouth_line", mid, eye_m, 0.008)
    smile = [surface_point(muzzle, x, 1.745 + 0.04 * (x / 0.08) ** 2, 0.003)[0]
             for x in [(-0.085 + i * 0.017) for i in range(11)]]
    mouth_smile = curve("mouth_smile", smile, eye_m, 0.008)
    mo_p, mo_n = surface_point(muzzle, 0, 1.735, 0.0)
    mouth_open = sphere("mouth_open", (0, 0, 0), (0.085, 0.035, 0.07), mouth_m)
    place(mouth_open, mo_p, mo_n)
    mo_tongue = sphere("mouth_tongue", (0, 0, 0), (0.055, 0.03, 0.03), tongue)
    place(mo_tongue, mo_p + mo_n * 0.012 + Vector((0, 0, -0.03)), mo_n)
    face += [mouth_line, mouth_smile, mouth_open, mo_tongue]

    eyes, happy, brows = {}, {}, {}
    for s, x in (("L", 1), ("R", -1)):
        ep, en = surface_point(body, 0.2 * x, 1.99, 0.0)
        eye = sphere("eye_" + s, (0, 0, 0), (0.072, 0.03, 0.085), eye_m, seg=48)
        place(eye, ep + en * 0.012, en)
        sh = sphere("eye_shine_" + s, (0, 0, 0), (0.022, 0.01, 0.024), shine)
        place(sh, ep + en * 0.04 + Vector((0.022 * -x, 0, 0.032)), en)
        attach(sh, eye)
        eye["base_scale"] = list(eye.scale)
        eye["shine"] = sh.name
        eyes[s] = eye
        arc = [surface_point(body, 0.2 * x + dx, 1.985 + 0.045 * (1 - (dx / 0.075) ** 2), 0.01)[0]
               for dx in [(-0.075 + i * 0.015) for i in range(11)]]
        happy[s] = curve("eye_happy_" + s, arc, eye_m, 0.014)
        bp, bn = surface_point(body, 0.2 * x, 2.13, 0.012)
        brow = capsule("brow_" + s, (0, 0, 0), (0.025, 0.016, 0.025), 0.07, brow_m)
        place(brow, bp, bn, spin=math.radians(90))
        brow.rotation_mode = "XYZ"
        brows[s] = (brow, brow.location.copy(), brow.rotation_euler.copy())
        cp, cn = surface_point(body, 0.33 * x, 1.85, 0.017)
        bl = sphere("blush_" + s, (0, 0, 0), (0.085, 0.006, 0.055), blush)
        place(bl, cp, cn)
        face += [eye, happy[s], brow, bl]
    for p in face:
        attach(p, J["head"])

    # ---- Ohren (eigene Empties: wackeln nach)
    for s, x in (("L", 1), ("R", -1)):
        ear = sphere("ear_" + s, (0.40 * x, 0.05, 2.33), (0.165, 0.085, 0.155), fur_skin)
        ear.rotation_euler = (0, math.radians(-18 * x), 0)
        add_fur(ear, fur_hair, count=700, length=FUR_LEN * 0.8, children=20, comb=0.2)
        inner = sphere("ear_in_" + s, (0.40 * x, -0.01, 2.32), (0.10, 0.04, 0.095), ear_in)
        inner.rotation_euler = (0, math.radians(-18 * x), 0)
        attach(ear, J["ear_" + s])
        attach(inner, J["ear_" + s])

    # ---- Kopfhoerer auf dem Kopf (Buegel hinter den Ohren, Muscheln seitlich, LED-Ring)
    bpy.ops.mesh.primitive_torus_add(major_radius=0.575, minor_radius=0.038, location=(0, 0.16, 1.93),
                                     rotation=(math.pi / 2, 0, 0), major_segments=96, minor_segments=16)
    band = bpy.context.active_object
    band.name = "phone_band"
    bm = bmesh.new()
    bm.from_mesh(band.data)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.y < -0.12], context="VERTS")  # untere Haelfte weg
    bm.to_mesh(band.data)
    bm.free()
    band.data.materials.append(phone)
    smooth(band)
    parts = [band]
    for s, x in (("L", 1), ("R", -1)):
        rot = (0, math.radians(90), 0)
        cup = cylinder("phone_cup_" + s, (0.58 * x, 0.08, 1.90), 0.165, 0.12, phone, rot=rot, bevel=0.035)
        pad = torus("phone_pad_" + s, (0.52 * x, 0.08, 1.90), 0.12, 0.04, phone_pad, rot=rot)
        ring = torus("phone_led_" + s, (0.645 * x, 0.08, 1.90), 0.11, 0.014, led, rot=rot)
        cap = cylinder("phone_cap_" + s, (0.648 * x, 0.08, 1.90), 0.085, 0.012, phone_metal, rot=rot,
                       bevel=0.004)
        yoke = cylinder("phone_yoke_" + s, (0.58 * x, 0.12, 2.10), 0.025, 0.16, phone_metal, bevel=0.005)
        parts += [cup, pad, ring, cap, yoke]
    for p in parts:
        attach(p, J["head"])

    # ---- Zeigefinger / Kaffeebecher (Extras)
    finger = capsule("finger", (-0.57, -0.15, 0.66), (0.045, 0.045, 0.045), 0.09, fur_skin)
    finger.rotation_euler = (math.radians(90), 0, 0)
    attach(finger, J["hand_R"])
    cup = []
    cup.append(cylinder("cup", (-0.69, -0.14, 0.74), 0.1, 0.3, cup_w, bevel=0.01))
    cup.append(cylinder("cup_band", (-0.69, -0.14, 0.72), 0.104, 0.15, cup_band, bevel=0.005))
    cup.append(cylinder("cup_lid", (-0.69, -0.14, 0.90), 0.108, 0.05, black, bevel=0.015))
    for c in cup:
        attach(c, J["hand_R"])
    return J, {"line": mouth_line, "smile": mouth_smile, "open": mouth_open, "tongue": mo_tongue,
               "finger": finger, "cup": cup, "eyes": eyes, "happy": happy, "brows": brows,
               "body": body}


# Gesicht je Clip: Augen offen/froh, Brauen hoch (0..1), Blinzeln (Beat-Zeitpunkte)
FACE_KIND = {"groove": ("open", 0.0, (2.55,)), "hype": ("happy", 0.4, ()), "special": ("open", 0.2, (1.3,)),
             "talk": ("open", 0.25, (2.2,)), "shout": ("open", 0.8, ())}
FACE_CLIP = {"heart": ("happy", 0.3, ()), "bow": ("happy", 0.0, ()), "stretch": ("happy", 0.5, ()),
             "lookout": ("wide", 0.7, ()), "coffee": ("open", 0.1, (0.7,)), "mix": ("open", 0.0, (1.55, 3.6)),
             "sway": ("happy", 0.1, ()), "thumbsup": ("wink", 0.5, ()), "robot": ("open", 0.0, ()),
             "disco": ("happy", 0.3, ()), "cue": ("focus", -0.25, (3.1,)), "knobs": ("focus", -0.35, (1.1,)),
             "scratch": ("focus", -0.3, (2.3,)), "point": ("wink", 0.3, ()), "dab": ("happy", 0.2, ()),
             "shimmy": ("happy", 0.2, ()), "airdrum": ("focus", 0.1, ()), "explain": ("open", 0.35, (1.7,))}


def set_face(M, name, kind, t):
    """Augen/Brauen fuer Clip `name` zum Zeitpunkt t (Beats)."""
    if "eyes" not in M:
        return
    eyes, brow, blinks = FACE_CLIP.get(name, FACE_KIND.get(kind, ("open", 0.0, ())))
    blink = 0.0
    for b in blinks:
        d = abs(((t - b + 2) % 4) - 2)
        if d < 0.18:
            blink = max(blink, 1 - d / 0.18)
    hop = {"hype": 0.35, "groove": 0.15}.get(kind, 0.0) * punch(t)
    for s in "LR":
        mode = ("happy" if s == "L" else "open") if eyes == "wink" else eyes
        is_open = mode in ("open", "focus", "wide")
        e = M["eyes"][s]
        e.hide_render = not is_open
        bpy.data.objects[e["shine"]].hide_render = not is_open or blink > 0.5
        M["happy"][s].hide_render = is_open
        bx, by, bz = e["base_scale"]
        sq = 0.78 if mode == "focus" else 1.1 if mode == "wide" else 1.0
        wd = 1.06 if mode == "wide" else 1.0
        e.scale = (bx * wd, by, bz * sq * max(0.08, 1 - blink))
        o, loc, rot = M["brows"][s]
        o.location = loc + Vector((0, 0, 0.035 * (brow + hop) - 0.02 * blink))
        x = 1 if s == "L" else -1
        o.rotation_euler = (rot.x, rot.y, rot.z)
        o.rotation_euler.rotate_axis("Y", math.radians(-12 * brow * x))


def secondary(fn, t, wrap=True):
    """Nachschwingen: Ohren folgen Kopf-Nicken und Huepfen leicht verzoegert."""
    def head_state(tt):
        p = fn(tt % 4.0 if wrap else tt)
        nod = p.get("head", (0, 0, 0))[0] + p.get("chest", (0, 0, 0))[0]
        z = p.get("root_loc", (0, 0, 0))[2]
        roll = p.get("head", (0, 0, 0))[1] + p.get("chest", (0, 0, 0))[1]
        return nod, z, roll
    lag, dt = 0.12, 0.06
    n1, z1, r1 = head_state(t - lag)
    n0, z0, r0 = head_state(t - lag - dt)
    vn, vz, vr = (n1 - n0) / dt, (z1 - z0) / dt, (r1 - r0) / dt
    out = {}
    for s, x in (("L", 1), ("R", -1)):
        out["ear_" + s] = (max(-0.5, min(0.5, -0.35 * vn + 0.9 * vz)),
                           max(-0.5, min(0.5, -1.2 * vz * x - 0.3 * vr)), 0.0)
    return out


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
    def area(name, loc, energy, color, size, target=(0, 0, 1.4), shadow=True):
        d = bpy.data.lights.new(name, "AREA")
        d.energy, d.color, d.size = energy, color, size
        d.use_shadow = shadow                # weiche Aufheller brauchen keinen Schatten (spart Speicher)
        o = bpy.data.objects.new(name, d)
        bpy.context.collection.objects.link(o)
        o.location = loc
        direction = Vector(target) - Vector(loc)
        o.rotation_euler = direction.to_track_quat("-Z", "Y").to_euler()
    area("key", (-2.6, -4.0, 4.0), 470, (1.0, 0.93, 0.84), 3.0)
    area("fill", (3.2, -3.5, 1.8), 85, (0.88, 0.92, 1.0), 4.0, shadow=False)
    area("rim_pink", (-2.6, 2.4, 3.4), 1100, (1.0, 0.35, 0.85), 1.6)
    area("rim_cyan", (2.6, 2.4, 3.0), 1100, (0.3, 0.85, 1.0), 1.6)
    area("top", (0, 0.8, 5.0), 60, (1.0, 0.96, 0.9), 2.0, target=(0, 0, 1.9), shadow=False)
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


def punch(t, attack=0.08):
    """Beat-Akzent: schiesst kurz vor dem Beat hoch, klingt danach weich ab (1-Beat-Loop).
    Wirkt knackiger als ein Sinus – der Koerper 'trifft' den Beat statt zu schweben."""
    u = t % 1.0
    if u > 1 - attack:
        return ss((u - (1 - attack)) / attack)
    return math.exp(-u / 0.3) * (1 - ss(u / (1 - attack)))


def base(t, nod=0.1, bounce=0.03, sway=0.03):
    p = {}
    k = punch(t)
    p["root_loc"] = (0, 0, -bounce * (0.3 * onbeat(t) + 0.7 * k))
    p["hips"] = (0, sway * wave(t, 2), 0.025 * wave(t, 4, 0.1))
    p["chest"] = (0.05 * k, -sway * 0.8 * wave(t, 2), 0.065 * wave(t, 4))
    p["head"] = (nod * (0.35 * onbeat(t) + 0.65 * k) - 0.02, 0.07 * wave(t, 2, 0.25), 0.075 * wave(t, 4, 0.1))
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


def ss(x):
    """weicher Uebergang 0..1"""
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def pulse_hold(t, on, off, ramp=0.25):
    """1 zwischen on und off (Beats im 4er-Loop), weich ein/aus."""
    return ss((t - on) / ramp) * (1 - ss((t - off) / ramp))


# --- Grooven am Pult
def clip_scratch(t):
    p = base(t, nod=0.12, bounce=0.03)
    p["shoulder_L"] = (-0.95 + 0.05 * wave(t, 0.5), -0.22, 0.3 * wave(t, 0.5))
    p["elbow_L"] = (-0.65, 0, 0)
    p["shoulder_R"] = (-0.9, 0.08, 0)
    p["elbow_R"] = (-0.7 + 0.12 * wave(t, 1), 0, 0)
    p["head"] = (0.14 + 0.08 * onbeat(t), 0, 0.18)
    return p


def clip_knobs(t):
    p = base(t, nod=0.14, bounce=0.03)
    p["shoulder_L"] = (-1.0, 0.2, 0)
    p["shoulder_R"] = (-1.0, -0.2, 0)
    p["elbow_L"] = (-0.55, 0, 0.3)
    p["elbow_R"] = (-0.55, 0, -0.3)
    p["hand_L"] = (0, 0, 0.6 * wave(t, 1))
    p["hand_R"] = (0, 0, -0.6 * wave(t, 1, 0.5))
    p["head"] = (0.16 + 0.1 * onbeat(t), 0.04 * wave(t, 2), 0)
    return p


def clip_cue(t):
    p = base(t, nod=0.1, bounce=0.03)
    p["shoulder_R"] = (-0.25, 2.3, 0)
    p["elbow_R"] = (0, -2.25, 0)
    p["shoulder_L"] = (-0.9, -0.2, 0.12 * wave(t, 2))
    p["elbow_L"] = (-0.7, 0, 0)
    p["head"] = (0.08 + 0.08 * onbeat(t), -0.18, 0.05 * wave(t, 4))
    return p


def clip_sway(t):
    p = base(t, nod=0.08, bounce=0.03, sway=0.0)
    w = wave(t, 2)
    p["root_loc"] = (0.09 * w, 0, -0.03 * onbeat(t))
    p["hips"] = (0, 0, 0.08 * w)
    p["chest"] = (0.03 * onbeat(t), -0.1 * w, 0.06 * w)
    p["shoulder_L"] = (-0.25, -0.5 - 0.2 * w, 0)
    p["shoulder_R"] = (-0.25, 0.5 - 0.2 * w, 0)
    p["elbow_L"] = (-1.0, 0, 0)
    p["elbow_R"] = (-1.0, 0, 0)
    p["head"] = (0.04 * onbeat(t), 0.14 * w, 0)
    return p


def clip_shimmy(t):
    p = base(t, nod=0.06, bounce=0.03)
    p["chest"] = (0.04 * onbeat(t), 0, 0.2 * wave(t, 0.5))
    p["shoulder_L"] = (-0.6, -0.3, 0)
    p["shoulder_R"] = (-0.6, 0.3, 0)
    p["elbow_L"] = (-1.6, 0, 0)
    p["elbow_R"] = (-1.6, 0, 0)
    p["head"] = (0.04, 0, -0.12 * wave(t, 0.5))
    return p


def clip_lowpump(t):
    p = base(t, nod=0.15, bounce=0.05)
    a, b = (wave(t, 2) + 1) / 2, (1 - wave(t, 2)) / 2
    p["shoulder_L"] = (-0.8 - 0.5 * a, -0.25, 0)
    p["shoulder_R"] = (-0.8 - 0.5 * b, 0.25, 0)
    p["elbow_L"] = (-1.1 + 0.4 * a, 0, 0)
    p["elbow_R"] = (-1.1 + 0.4 * b, 0, 0)
    return p


# --- Hype
def clip_airdrum(t):
    p = base(t, nod=0.0, bounce=0.05)
    p["shoulder_L"] = (-0.8, -0.25, 0)
    p["shoulder_R"] = (-0.8, 0.25, 0)
    p["elbow_L"] = (-1.7 + 0.8 * onbeat(t, 1), 0, 0)
    p["elbow_R"] = (-1.7 + 0.8 * onbeat(t + 0.5, 1), 0, 0)
    p["head"] = (0.22 * onbeat(t, 0.5) - 0.05, 0, 0)
    return p


def clip_fistpump(t):
    p = base(t, nod=0.1, bounce=0.06)
    a, b = onbeat(t, 2), onbeat(t + 1, 2)
    p["shoulder_L"] = (-0.25, -2.05 - 0.25 * a, 0)
    p["shoulder_R"] = (-0.25, 2.05 + 0.25 * b, 0)
    p["elbow_L"] = (0, 1.3 * (1 - a), 0)
    p["elbow_R"] = (0, -1.3 * (1 - b), 0)
    p["head"] = (-0.1 + 0.08 * onbeat(t), 0.08 * wave(t, 2), 0)
    return p


def clip_roof(t):
    p = base(t, nod=0.1, bounce=0.06)
    up = onbeat(t, 1)
    p["shoulder_L"] = (-0.2, -1.85 - 0.25 * up, 0)
    p["shoulder_R"] = (-0.2, 1.85 + 0.25 * up, 0)
    p["elbow_L"] = (0, 1.1 * (1 - up), 0)
    p["elbow_R"] = (0, -1.1 * (1 - up), 0)
    p["hand_L"] = (0, 0.8, 0)
    p["hand_R"] = (0, -0.8, 0)
    p["head"] = (-0.12 + 0.1 * up, 0, 0)
    return p


def clip_disco(t):
    p = base(t, nod=0.05, bounce=0.04, sway=0.06)
    up = (wave(t, 2, 0.25) + 1) / 2                   # Beat 0/2 oben, 1/3 unten
    p["shoulder_R"] = (-0.5 + 0.2 * up, 2.4 * up - 0.4 * (1 - up), 0)
    p["elbow_R"] = (-0.1, 0, 0)
    p["shoulder_L"] = (0.05, -0.45, 0)
    p["elbow_L"] = (-0.4, 1.4, 0)
    p["hips"] = (0, 0.08 * wave(t, 2, 0.25), 0)
    p["head"] = (0.05 - 0.15 * up, -0.1 * up, 0.15 * (up - 0.5))
    return p


def clip_robot(t):
    p = base(t, nod=0.0, bounce=0.0, sway=0.0)
    step = math.floor(t * 2) % 4                        # alle halben Beats ein Ruck
    frac = ss((t * 2 - math.floor(t * 2)) / 0.3)
    poses = [(-1.0, -2.1, 0.25), (-2.1, -1.0, -0.25), (-1.0, -1.0, 0.0), (-2.1, -2.1, 0.0)]
    a, b = poses[step - 1], poses[step]
    el, er, tw = (a[i] + (b[i] - a[i]) * frac for i in range(3))
    p["shoulder_L"] = (-0.45, -0.35, 0)
    p["shoulder_R"] = (-0.45, 0.35, 0)
    p["elbow_L"] = (el, 0, 0)
    p["elbow_R"] = (er, 0, 0)
    p["chest"] = (0, 0, tw)
    p["head"] = (0, 0, -tw * 1.2)
    return p


def clip_crowdwave(t):
    p = base(t, nod=0.05, bounce=0.04)
    w = wave(t, 2)
    p["root_loc"] = (0.06 * w, 0, -0.03 * onbeat(t))
    p["shoulder_L"] = (-0.2, -2.25 + 0.35 * w, 0)
    p["shoulder_R"] = (-0.2, 2.25 + 0.35 * w, 0)
    p["elbow_L"] = (0, -0.2, 0)
    p["elbow_R"] = (0, 0.2, 0)
    p["chest"] = (0, -0.1 * w, 0)
    p["head"] = (-0.08, 0.12 * w, 0)
    return p


def clip_dab(t):
    p = base(t, nod=0.05, bounce=0.03)
    d = max(pulse_hold(t, 0.5, 1.5), pulse_hold(t, 2.5, 3.5))
    p["shoulder_L"] = (-0.3 * d - 0.05 * (1 - d), -2.2 * d - 0.12 * (1 - d), 0)
    p["elbow_L"] = (-0.15 * (1 - d), 0, 0)
    p["shoulder_R"] = (-1.35 * d + 0.05 * (1 - d), 0.12 - 0.5 * d, -0.6 * d)
    p["elbow_R"] = (-0.6 * d - 0.15 * (1 - d), 0, -0.9 * d)
    p["head"] = (0.4 * d, 0.1 * d, 0.35 * d)
    return p


# --- Spezial
def clip_heart(t):
    p = base(t, nod=0.04, bounce=0.02, sway=0.05)
    p["shoulder_L"] = (-1.15, 0.35, 0)
    p["shoulder_R"] = (-1.15, -0.35, 0)
    p["elbow_L"] = (-0.9, 0, 0.6)
    p["elbow_R"] = (-0.9, 0, -0.6)
    p["head"] = (0.02, 0.15 * wave(t, 2), 0)
    return p


def clip_thumbsup(t):
    p = base(t, nod=0.06, bounce=0.03)
    k = onbeat(t, 1)
    p["shoulder_R"] = (-1.35 - 0.1 * k, 0.25, 0)
    p["elbow_R"] = (-0.35, 0, 0)
    p["hand_R"] = (0, 0, 0.3)
    p["shoulder_L"] = (0.05, -0.12, 0)
    p["head"] = (0.0, 0.12, -0.08)
    return p


def clip_stretch(t):
    p = base(t, nod=0.0, bounce=0.0, sway=0.0)
    up = ss(t / 1.5) * (1 - ss((t - 2.5) / 1.5))
    p["shoulder_L"] = (-0.15 * up, -0.12 - 2.45 * up, 0)
    p["shoulder_R"] = (-0.15 * up, 0.12 + 2.45 * up, 0)
    p["elbow_L"] = (-0.15 * (1 - up), 0.4 * up, 0)
    p["elbow_R"] = (-0.15 * (1 - up), -0.4 * up, 0)
    p["chest"] = (-0.12 * up, 0, 0)
    p["head"] = (-0.25 * up, 0, 0)
    return p


def clip_lookout(t):
    p = base(t, nod=0.0, bounce=0.02, sway=0.0)
    turn = 0.35 * math.sin(TAU * t / 4)
    p["shoulder_R"] = (-0.7, 2.0, 0)
    p["elbow_R"] = (0, -2.45, 0)
    p["shoulder_L"] = (0.05, -0.12, 0)
    p["chest"] = (0, 0, turn * 0.5)
    p["head"] = (-0.05, 0, turn)
    return p


def clip_bow(t):
    p = base(t, nod=0.0, bounce=0.0, sway=0.0)
    b = pulse_hold(t, 0.5, 2.5, 0.6)
    p["chest"] = (0.38 * b, 0, 0)
    p["head"] = (0.3 * b, 0, 0)
    p["shoulder_L"] = (-0.3 * b + 0.05, -0.12, 0)
    p["shoulder_R"] = (-0.3 * b + 0.05, 0.12, 0)
    p["elbow_L"] = (-0.4 * b, 0, 0)
    p["elbow_R"] = (-0.4 * b, 0, 0)
    return p


# --- Reden
def clip_shout(t):
    p = base(t, nod=0.05, bounce=0.03)
    p["shoulder_R"] = (-1.0, 1.05, 0)
    p["elbow_R"] = (0, -1.95, 0)
    p["shoulder_L"] = (-0.9, -1.2 + 0.15 * wave(t, 1), 0)
    p["elbow_L"] = (-0.4, 0, 0)
    p["head"] = (-0.05 + 0.06 * onbeat(t), 0.06 * wave(t, 2), 0)
    return p


def clip_explain(t):
    p = base(t, nod=0.04, bounce=0.015, sway=0.02)
    p["shoulder_L"] = (-1.1, -0.6, 0.4 * wave(t, 4))
    p["elbow_L"] = (-0.3, 0, 0)
    p["hand_L"] = (0, 0, 0.3)
    p["shoulder_R"] = (-0.85, 0.15, 0)
    p["elbow_R"] = (-0.75, 0, 0)
    p["head"] = (0.03 * wave(t, 1.5), 0.05 * wave(t, 4), -0.1 * wave(t, 4))
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
    "scratch": (clip_scratch, "groove", 0, None),
    "knobs": (clip_knobs, "groove", 0, None),
    "cue": (clip_cue, "groove", 0, None),
    "sway": (clip_sway, "groove", 0, None),
    "shimmy": (clip_shimmy, "groove", 0, None),
    "lowpump": (clip_lowpump, "groove", 0, None),
    "airdrum": (clip_airdrum, "hype", 1, None),
    "fistpump": (clip_fistpump, "hype", 1, None),
    "roof": (clip_roof, "hype", 1, None),
    "disco": (clip_disco, "hype", 0, "finger"),
    "robot": (clip_robot, "hype", 0, None),
    "crowdwave": (clip_crowdwave, "hype", 1, None),
    "dab": (clip_dab, "hype", 0, None),
    "heart": (clip_heart, "special", 0, None),
    "thumbsup": (clip_thumbsup, "special", 0, None),
    "stretch": (clip_stretch, "special", 1, None),
    "lookout": (clip_lookout, "special", 0, None),
    "bow": (clip_bow, "special", 0, None),
    "shout": (clip_shout, "shout", "both", None),
    "explain": (clip_explain, "talk", "both", None),
}


PAWS = {}          # Seite -> Ballen-Objekte (Pfotenballen + Zehen)


def update_paws():
    """Ballen nur zeigen, wenn die Handflaeche nicht nach oben zeigt (Pfoten auf dem Pult)."""
    if not PAWS:
        return
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    for s, objs in PAWS.items():
        ev = objs[0].evaluated_get(dg)
        me = ev.data
        n = Vector((0, 0, 0))
        for pl in me.polygons:
            n += pl.normal * pl.area
        n = (ev.matrix_world.to_3x3() @ n).normalized()
        hide = n.z > 0.12 or n.y > 0.35         # nach oben oder vom Betrachter weg
        for o in objs:
            o.hide_render = hide


def apply_pose(J, p):
    for name, e in J.items():
        e.rotation_euler = (0, 0, 0)
    J["root"].scale = p.get("root_scale", (1, 1, 1))
    rest = getattr(apply_pose, "rest", None)
    if rest is None:
        apply_pose.rest = rest = {n: e.location.copy() for n, e in J.items()}
    for n, e in J.items():
        e.location = rest[n]
    for key, val in p.items():
        if key == "root_scale":
            continue
        if key == "root_loc":
            J["root"].location = rest["root"] + Vector(val)
        elif key == "root_rot":
            J["root"].rotation_euler = val
        elif key in J:
            J[key].rotation_euler = val
    update_paws()


# Mund bei Clips ohne Reden: laecheln, mitsingen (offen auf 1 und 3) oder einmal je Takt
MOUTH_KIND = {"groove": "smile", "special": "smile", "hype": "sing"}
MOUTH_CLIP = {"wave": "sing", "shimmy": "sing", "lowpump": "once", "sway": "once", "heart": "smile",
              "thumbsup": "smile", "bow": "smile", "lookout": "smile", "coffee": "smile"}


def mouth_at(name, kind, t):
    mode = MOUTH_CLIP.get(name, MOUTH_KIND.get(kind, "smile"))
    u = t % 4.0
    if mode == "sing":
        return 1 if (u % 2.0) < 0.55 else 0
    if mode == "once":
        return 1 if 2.0 <= u < 2.6 else 0
    return 0


def follow(fn, t, wrap):
    """Overlap: der Kopf haengt der Rumpfbewegung kurz hinterher und holt dann auf."""
    def at(tt):
        return fn(tt % 4.0 if wrap else tt)
    a, b = at(t), at(t - 0.14)
    ca, cb = a.get("chest", (0, 0, 0)), b.get("chest", (0, 0, 0))
    za, zb = a.get("root_loc", (0, 0, 0))[2], b.get("root_loc", (0, 0, 0))[2]
    h = a.get("head", (0, 0, 0))
    return (h[0] + 0.45 * (cb[0] - ca[0]) + 1.1 * (zb - za), h[1] + 0.45 * (cb[1] - ca[1]),
            h[2] + 0.45 * (cb[2] - ca[2]))


def pose_full(M, name, kind, fn, t, wrap=True, face=None):
    """Pose eines Clips plus Overlap (Kopf), Nachschwingen (Ohren), Stauchen auf dem Beat, Gesicht."""
    p = fn(t)
    if MODEL == 2:
        p["head"] = follow(fn, t, wrap)
        p.update(secondary(fn, t, wrap))
        k = 0.022 * punch(t) if kind not in ("talk", "shout") else 0.0
        p["root_scale"] = (1 + k * 0.5, 1 + k * 0.5, 1 - k)
        set_face(M, *(face or (name, kind)), t)
        if face is None and name in CLIPS and CLIPS[name][2] == 0:
            set_mouth(M, mouth_at(name, kind, t))
    return p


# --- Ein-/Ausstieg: jeder Clip kann in 1 Beat aus der Grundpose kommen bzw. in sie zurueck.
# Der Bot plant Wechsel einen Beat vorher: alter Clip "@out", neuer "@in" -> kein Ueberblenden.
def hub(t):
    """Grundpose: Pfoten am Controller, leichtes Nicken."""
    p = base(t, nod=0.1, bounce=0.03)
    p["shoulder_L"] = (-0.85, -0.18, 0)
    p["elbow_L"] = (-0.75, 0, 0)
    p["shoulder_R"] = (-0.85, 0.15, 0)
    p["elbow_R"] = (-0.8, 0, 0)
    return p


def _wrap_angle(a):
    return (a + math.pi) % TAU - math.pi


def blend_pose(a, b, w):
    out = {}
    for k in set(a) | set(b):
        d = (1, 1, 1) if k == "root_scale" else (0, 0, 0)
        va, vb = a.get(k, d), b.get(k, d)
        if k == "root_rot":
            va, vb = tuple(_wrap_angle(x) for x in va), tuple(_wrap_angle(x) for x in vb)
        out[k] = tuple(x + (y - x) * w for x, y in zip(va, vb))
    return out


def edge_fn(fn, which):
    if which == "in":
        return lambda t: hub(t) if t < 0 else blend_pose(hub(t), fn(t), ss(t)) if t < 1 else fn(t)
    return lambda t: blend_pose(fn(t), hub(t), ss(t - 3)) if t >= 3 else fn(t)


EDGE_KINDS = ("groove", "hype", "special", "talk", "shout")   # Reden: sauber zurueck zum Tanzen


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
    import time
    t0 = time.time()
    bpy.context.scene.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print(f"RT {time.time() - t0:.2f} {os.path.basename(os.path.dirname(path))}", flush=True)


def main():
    reset()
    J, M = build_v2() if MODEL == 2 else build()
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
    if FRAME:
        for o in D:
            try:
                o.is_holdout = True
            except AttributeError:
                o.hide_render = True
        for item in FRAME.split(","):
            name, t = item.split(":")
            fn, kind, mouth, extra = CLIPS[name]
            set_extra(M, extra)
            set_mouth(M, 1 if mouth else 0)
            apply_pose(J, pose_full(M, name, kind, fn, float(t)))
            render_to(os.path.join(OUT, f"frame_{name}_{t}.png"))
        return
    if SHEET:
        sc.render.resolution_x, sc.render.resolution_y = PLATE
        sc.render.resolution_percentage = 50
        for name in (ONLY or list(CLIPS)):
            fn, kind, mouth, extra = CLIPS[name]
            set_extra(M, extra)
            set_mouth(M, 1 if mouth else 0)
            for t in (0.0, 1.0, 2.0, 3.0):
                apply_pose(J, pose_full(M, name, kind, fn, t))
                render_to(os.path.join(OUT, f"sheet_{name}_{t:.0f}.png"))
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
            apply_pose(J, pose_full(M, name, kind, fn, t))
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
        for suffix, m in ([] if EDGES_ONLY else variants):
            set_mouth(M, m)
            d = os.path.join(OUT, name + suffix)
            os.makedirs(d, exist_ok=True)
            for f in range(FPB * BEATS):
                apply_pose(J, pose_full(M, name, kind, fn, f / FPB))
                render_to(os.path.join(d, f"{f:03d}.png"))
            print("CLIP", name + suffix, flush=True)
        entry = {"kind": kind, "talk": mouth == "both"}
        if kind in EDGE_KINDS:
            for which in ("in", "out"):
                efn = edge_fn(fn, which)
                d = os.path.join(OUT, f"{name}@{which}")
                os.makedirs(d, exist_ok=True)
                for f in range(FPB):
                    t = f / FPB + (3 if which == "out" else 0)
                    hubby = t < 0.5 if which == "in" else t >= 3.5     # Haelfte bei der Grundpose
                    set_extra(M, None if hubby else extra)
                    set_mouth(M, 0 if hubby or mouth == "both" else (1 if mouth else 0))
                    face = ("_hub", "groove") if hubby else None
                    apply_pose(J, pose_full(M, name, kind, efn, t, wrap=which == "out", face=face))
                    render_to(os.path.join(d, f"{f:03d}.png"))
                print("CLIP", f"{name}@{which}", flush=True)
            entry["edges"] = True
        manifest["clips"][name] = entry
    with open(os.path.join(OUT, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=1)


main()
