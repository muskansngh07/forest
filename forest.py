from ursina import *
import math
import random
import csv
from collections import defaultdict

# ============================================================
# WHAT WAS FIXED vs Forest.py (original)
# ============================================================
# FIX 1 — BLOB TREES
#   Old: flat horizontal rings stacked up = blob from any angle
#   New: two crossed vertical quads (billboard cross) textured
#        with species PNG + transparency. Looks like real trees
#        from all camera angles.
#
# FIX 2 — LAGGING
#   Old: geometry rebuilt every year, no tree count limit,
#        25 stacks × 16 slices per tree = ~800 verts × 300 trees
#   New: max 120 trees total on screen at once (configurable),
#        stem simplified to 8 slices instead of 16,
#        all years pre-loaded at startup so N/P is instant.
#
# FIX 3 — ONLY MOVES FORWARD (no calibration)
#   Old: generator-based — like a tape, read-once, forward only
#   New: all years loaded into a list at startup.
#        N = next year, P = previous year, free movement.
#
# FIX 4 — ONLY ONE SPECIES TEXTURE
#   Old: texture='tree_texture1.png' hardcoded for all species
#   New: species 1 → tree_texture1.png
#        species 2 → tree_texture2.png
#        species 3 → tree_texture3.png
# ============================================================


# ── CONFIGURATION ───────────────────────────────────────────
# Tune these if still slow or too sparse

CSV_FILE      = 'cohort_props.csv'
LAND_AREA     = 100 * 100        # m² — the simulated plot size
MAX_TREES     = 120              # hard cap: max trees on screen at once
MIN_DENSITY   = 1e-6             # cohorts below this are skipped (effectively extinct)
STEM_SLICES   = 8                # sides on the trunk cylinder (was 16 — halved for speed)
STEM_STACKS   = 15               # vertical segments on trunk (was 25)

# Which texture file each species uses
SPECIES_TEXTURES = {
    1: 'tree_texture1.png',
    2: 'tree_texture2.png',
    3: 'tree_texture3.png',
}

# Stem colour per species (subtle difference so you can tell them apart)
SPECIES_STEM_COLORS = {
    1: color.hex('5C3A1E'),   # dark brown  — Species 1
    2: color.hex('3B5E2B'),   # dark green  — Species 2
    3: color.hex('7A5C2E'),   # warm brown  — Species 3
}


# ── PLANT-FATE PARAMETERS CLASS ─────────────────────────────
# Unchanged from original — all equations from the paper.
# Only basal_diameter comes from CSV, everything else is fixed.

class TreeParameters:
    def __init__(self, basal_diameter):
        self.H_m  = 35       # Maximum height — Eq 1
        self.a    = 75       # Stem slenderness ratio — Eq 1
        self.c    = 6000     # Crown-area to sapwood-area ratio — Eq 2
        self.m    = 1.5      # Crown shape parameter
        self.n    = 2        # Crown shape parameter
        self.rho_s = 0.6     # Wood density
        self.D    = basal_diameter   # FROM CSV

        try:
            # Height of maximum crown width — Eq 12
            self.zmratio = ((self.n - 1) / (self.m * self.n - 1)) ** (1 / self.n)
        except (ValueError, ZeroDivisionError):
            self.zmratio = 0.0

    def calculate_height(self):
        """Eq 1: H = H_m * (1 - exp(-a*D/H_m))"""
        return self.H_m * (1 - math.exp(-self.a * self.D / self.H_m))

    def calculate_crown_area(self):
        """Eq 2: A_c = (π*c / 4*a) * H * D"""
        H = self.calculate_height()
        return (math.pi * self.c) / (4 * self.a) * H * self.D

    def crown_radius_at_height(self, z, H):
        """Eq 10: Crown radius at height z"""
        if z <= 0 or z >= H or H <= 0:
            return 0.0
        z_ratio = z / H
        base    = max(0.0, 1 - z_ratio ** self.n)
        q_z     = self.m * self.n * (z_ratio ** (self.n - 1)) * (base ** (self.m - 1))
        A_c     = self.calculate_crown_area()
        z_m     = H * self.zmratio
        q_m     = self._q(z_m, H)
        if q_m <= 1e-6:
            return 0.0
        r0 = math.sqrt(A_c / math.pi) / q_m
        return r0 * q_z

    def _q(self, z, H):
        """Helper: q(z) shape function"""
        if z <= 0 or z >= H or H <= 0:
            return 0.0
        z_ratio = z / H
        base    = max(0.0, 1 - z_ratio ** self.n)
        return self.m * self.n * (z_ratio ** (self.n - 1)) * (base ** (self.m - 1))

    def stem_radius_at_height(self, z, H):
        """Tapered stem radius — narrower at top"""
        base_radius  = self.D / 2.0
        if H <= 0:
            return base_radius
        height_ratio = z / H
        taper_factor = 1.0 - height_ratio ** 1.5
        return base_radius * taper_factor


# ── TREE ENTITY ─────────────────────────────────────────────

class Tree(Entity):
    def __init__(self, params, species_id=1, **kwargs):
        super().__init__(**kwargs)

        self.params     = params
        self.species_id = species_id
        H = self.params.calculate_height()

        if H <= 0:
            return

        # ── STEM (tapered cylinder) ──────────────────────────
        # FIX 2: reduced to STEM_SLICES=8 and STEM_STACKS=15
        # Half the vertices of the original — still looks good.

        stem_verts   = []
        stem_tris    = []
        stem_normals = []

        for i in range(STEM_STACKS):
            y1 = (i / STEM_STACKS) * H
            y2 = ((i + 1) / STEM_STACKS) * H
            r1 = self.params.stem_radius_at_height(y1, H)
            r2 = self.params.stem_radius_at_height(y2, H)
            base_idx = len(stem_verts)

            for j in range(STEM_SLICES + 1):
                angle     = 2 * math.pi * j / STEM_SLICES
                cos_a     = math.cos(angle)
                sin_a     = math.sin(angle)
                stem_verts.append((r1 * cos_a, y1, r1 * sin_a))
                stem_verts.append((r2 * cos_a, y2, r2 * sin_a))
                stem_normals.append((cos_a, 0, sin_a))
                stem_normals.append((cos_a, 0, sin_a))

            for j in range(STEM_SLICES):
                idx = base_idx + j * 2
                stem_tris.append((idx,     idx + 1, idx + 2))
                stem_tris.append((idx + 1, idx + 3, idx + 2))

        stem_color = SPECIES_STEM_COLORS.get(species_id, color.hex('5C3A1E'))

        if stem_verts:
            self.stem       = Entity(parent=self, color=stem_color)
            self.stem.model = Mesh(vertices=stem_verts,
                                   triangles=stem_tris,
                                   normals=stem_normals)

        # ── FOLIAGE (billboard cross) ────────────────────────
        # FIX 1: replaced flat horizontal rings with two crossed
        # vertical quads. Each quad is a flat plane standing upright.
        # Together they form a cross (+) shape that looks like a
        # full tree from any camera angle.
        #
        # FIX 4: texture chosen by species_id.
        #
        # How it works:
        #   crown_width = diameter of the crown at its widest point
        #   The quad is scaled to (crown_width, H, 1)
        #   Positioned at (0, H/2, 0) — centred on the tree height
        #   Two quads rotated 90° from each other → cross shape
        #   double_sided=True so visible from all angles
        #   The PNG black areas become transparent via alpha

        A_c         = self.params.calculate_crown_area()
        crown_width = math.sqrt(A_c / math.pi) * 2   # diameter
        crown_width = max(crown_width, self.params.D * 2)  # at least as wide as trunk

        tex = SPECIES_TEXTURES.get(species_id, 'tree_texture1.png')

        # Quad 1 — faces forward (along Z axis)
        self.foliage_a = Entity(
            parent      = self,
            model       = 'quad',
            texture     = tex,
            scale       = (crown_width, H, 1),
            position    = (0, H / 2, 0),
            double_sided = True,
            color       = color.white,
        )

        # Quad 2 — rotated 90° to face sideways (along X axis)
        # Together with quad 1 this makes a cross shape
        self.foliage_b = Entity(
            parent      = self,
            model       = 'quad',
            texture     = tex,
            scale       = (crown_width, H, 1),
            position    = (0, H / 2, 0),
            rotation_y  = 90,           # rotated 90° around vertical axis
            double_sided = True,
            color       = color.white,
        )


# ── DATA LOADER ─────────────────────────────────────────────
# FIX 3: loads ALL years into memory at startup.
# Returns a list of (year, cohorts) tuples sorted by year.
# This makes N and P instant — no file reading during navigation.

def load_all_years(file_name):
    """
    Read the entire CSV once.
    Returns: sorted list of (year_int, [cohort_dicts])
    """
    year_data = defaultdict(list)

    try:
        with open(file_name, mode='r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    year_int       = int(float(row['YEAR']))
                    density        = float(row['density'])
                    basal_diameter = float(row['basal_diameter'])
                    species_id     = int(row['speciesID'])
                    cohort_num     = int(row['cohortNum'])

                    if density < MIN_DENSITY:
                        continue   # skip near-extinct cohorts

                    year_data[year_int].append({
                        'density'       : density,
                        'basal_diameter': basal_diameter,
                        'species_id'    : species_id,
                        'cohort_num'    : cohort_num,
                    })

                except (ValueError, KeyError):
                    pass   # skip malformed rows silently

    except FileNotFoundError:
        print(f"ERROR: '{file_name}' not found. "
              f"Put it in the same folder as this script.")
        return []

    # Sort by year and return as list of tuples
    sorted_years = sorted(year_data.keys())
    print(f"Loaded {len(sorted_years)} years "
          f"({sorted_years[0]} → {sorted_years[-1]})")
    return [(yr, year_data[yr]) for yr in sorted_years]


# ── FOREST DRAWING ───────────────────────────────────────────

# Stores (x, z) positions per cohort key so trees don't
# jump around between years — same cohort = same location.
cohort_positions = defaultdict(list)

def draw_forest(year, cohorts):
    """
    Destroy old trees, place new ones for this year.
    Tree positions are persistent per cohort across years.
    """
    global trees

    # Destroy all current tree entities
    for t in trees:
        destroy(t)
    trees.clear()

    active_keys    = set()
    total_placed   = 0

    # FIX 2: calculate a per-cohort tree budget so total ≤ MAX_TREES
    # We scale each cohort's count proportionally if needed.
    raw_counts = []
    for c in cohorts:
        n = max(1, round(c['density'] * LAND_AREA))
        raw_counts.append(n)

    raw_total = sum(raw_counts)
    # Scale factor: if over budget, shrink all counts proportionally
    scale = min(1.0, MAX_TREES / max(raw_total, 1))

    for cohort, raw_n in zip(cohorts, raw_counts):
        n_target   = max(1, round(raw_n * scale))
        key        = (cohort['species_id'], cohort['cohort_num'])
        active_keys.add(key)

        # Grow or shrink the stored position list
        positions = cohort_positions[key]
        while len(positions) < n_target:
            positions.append((random.uniform(-50, 50),
                              random.uniform(-50, 50)))
        if len(positions) > n_target:
            positions = positions[:n_target]
        cohort_positions[key] = positions

        # Create one Tree entity per position
        params = TreeParameters(basal_diameter=cohort['basal_diameter'])
        for (x, z) in positions:
            t = Tree(params     = params,
                     species_id = cohort['species_id'],
                     position   = (x, 0, z))
            trees.append(t)
            total_placed += 1

    # Remove positions for cohorts that no longer exist
    for key in list(cohort_positions.keys()):
        if key not in active_keys:
            del cohort_positions[key]

    year_text.text = f"Year: {year}    Trees: {total_placed}    (N = next  |  P = prev  |  R = restart)"
    print(f"Year {year}: {total_placed} trees ({len(cohorts)} cohorts)")


# ── URSINA APP SETUP ─────────────────────────────────────────

app = Ursina()

# Ground
ground = Entity(
    model         = 'plane',
    scale         = 100,
    texture       = 'soil.png',
    texture_scale = (20, 20),
)

# Lighting
sun = DirectionalLight(y=10, z=5, shadows=True)
sun.look_at(Vec3(0, 0, 0))
AmbientLight(color=color.rgba(100, 100, 100, 128))

# Sky
Sky(texture='sky_sunset')

# UI
year_text = Text(
    text   = 'Loading...',
    origin = (0, -4),
    scale  = 1.2,
    background = True,
)

# ── LOAD ALL DATA UPFRONT ────────────────────────────────────
# FIX 3: everything in memory → N and P are instant

all_years      = load_all_years(CSV_FILE)   # list of (year, cohorts)
trees          = []
year_index     = 0                          # which year we're on


def show_year(index):
    """Draw the forest at all_years[index]."""
    if not all_years:
        return
    year, cohorts = all_years[index]
    draw_forest(year, cohorts)


# ── INPUT HANDLING ───────────────────────────────────────────
# FIX 3: P = previous year (was impossible before)

def input(key):
    global year_index

    if key == 'n':                          # Next year
        if year_index < len(all_years) - 1:
            year_index += 1
            show_year(year_index)
        else:
            print("Already at last year.")

    elif key == 'p':                        # Previous year — NEW
        if year_index > 0:
            year_index -= 1
            show_year(year_index)
        else:
            print("Already at first year.")

    elif key == 'r':                        # Restart
        global cohort_positions
        cohort_positions.clear()
        year_index = 0
        show_year(year_index)

    elif key == 'q' or key == 'escape':
        quit()


# ── CAMERA ───────────────────────────────────────────────────

EditorCamera()   # right-click drag = orbit, scroll = zoom, middle = pan

# ── START ────────────────────────────────────────────────────

if all_years:
    show_year(0)
else:
    year_text.text = f"ERROR: could not load '{CSV_FILE}'"

app.run()