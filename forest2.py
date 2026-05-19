from ursina import *
import math
import random
import csv
from collections import defaultdict

# ============================================================
# FIXES IN THIS VERSION
# ============================================================
# FIX A — Stems were white
#   Cause: Mesh entity color wasn't applying with default shader
#   Fix:   Use Ursina's built-in 'cylinder' primitive for trunk
#          — guaranteed to respect color= without shader issues.
#
# FIX B — Checkerboard (black background not transparent)
#   Cause: PNG has black backgrounds, not real alpha channel.
#   Fix:   Set render_queue='transparent' + color=color.white
#          on the billboard quads so Ursina blends them correctly.
#
# FIX C — Trees massively too large (crown up to 132m wide!)
#   Cause: c=6000 is physically correct but visually overwhelming.
#   Fix:   VISUAL_SCALE=0.18 shrinks only the displayed crown width.
#          Heights stay physically correct. Tune this number freely.
#
# FIX D — Dark square artifact in scene centre
#   Cause: Entity created without model in an edge case.
#   Fix:   Guard H>0 before any Entity creation.
# ============================================================


# ── CONFIGURATION ────────────────────────────────────────────

CSV_FILE     = 'cohort_props.csv'
LAND_AREA    = 100 * 100   # m²
MAX_TREES    = 100          # hard cap — lower if slow, raise if sparse
MIN_DENSITY  = 1e-5         # skip near-extinct cohorts

# Crown display scale — 0.18 = show at 18% of real crown width.
# Real crown widths go up to 132m which fills the whole 100m plot.
# Tune: raise toward 0.3 for bigger canopies, lower to 0.1 if crowded.
VISUAL_SCALE = 0.18

STEM_SLICES  = 8
STEM_STACKS  = 10

SPECIES_TEXTURES = {
    1: 'tree_texture1.png',
    2: 'tree_texture2.png',
    3: 'tree_texture3.png',
}

SPECIES_BARK = {
    1: color.rgb(80,  50, 25),
    2: color.rgb(55,  80, 35),
    3: color.rgb(110, 75, 35),
}


# ── PLANT-FATE PARAMETERS ────────────────────────────────────

class TreeParameters:
    def __init__(self, basal_diameter):
        self.H_m   = 35
        self.a     = 75
        self.c     = 6000
        self.m     = 1.5
        self.n     = 2
        self.rho_s = 0.6
        self.D     = basal_diameter

        try:
            self.zmratio = ((self.n - 1) / (self.m * self.n - 1)) ** (1 / self.n)
        except (ValueError, ZeroDivisionError):
            self.zmratio = 0.0

    def calculate_height(self):
        return self.H_m * (1 - math.exp(-self.a * self.D / self.H_m))

    def crown_radius_max(self):
        H   = self.calculate_height()
        A_c = (math.pi * self.c) / (4 * self.a) * H * self.D
        return math.sqrt(A_c / math.pi) if A_c > 0 else 0.0

    def stem_radius_at_height(self, z, H):
        base  = self.D / 2.0
        if H <= 0:
            return base
        taper = 1.0 - (z / H) ** 1.5
        return max(base * taper, 0.005)


# ── TREE ENTITY ──────────────────────────────────────────────

class Tree(Entity):
    def __init__(self, params, species_id=1, **kwargs):
        super().__init__(**kwargs)

        H = params.calculate_height()
        if H <= 0:
            return

        bark  = SPECIES_BARK.get(species_id, color.rgb(80, 50, 25))
        tex   = SPECIES_TEXTURES.get(species_id, 'tree_texture1.png')

        base_r    = params.stem_radius_at_height(0, H)
        crown_w   = max(
            params.crown_radius_max() * 2 * VISUAL_SCALE,
            base_r * 6            # at least 6× trunk radius
        )

        # ── FIX A: built-in cylinder = always correct bark colour ──
        Entity(
            parent   = self,
            model    = 'cylinder',
            color    = bark,
            scale    = (base_r * 2, H, base_r * 2),
            position = (0, H / 2, 0),
        )

        # ── FIX B + C: billboard cross, scaled down, transparent ───
        # Two quads at 90° = tree looks full from every angle.
        for rot in (0, 90):
            Entity(
                parent             = self,
                model              = 'quad',
                texture            = tex,
                color              = color.white,
                scale              = (crown_w, H * 1.15, 1),
                position           = (0, H / 2, 0),
                rotation_y         = rot,
                double_sided       = True,
                render_queue       = 'transparent',   # FIX B
            )


# ── DATA LOADER ──────────────────────────────────────────────

def load_all_years(file_name):
    year_data = defaultdict(list)
    try:
        with open(file_name, mode='r', encoding='utf-8-sig') as f:
            for row in csv.DictReader(f):
                try:
                    yr  = int(float(row['YEAR']))
                    den = float(row['density'])
                    if den < MIN_DENSITY:
                        continue
                    year_data[yr].append({
                        'density'        : den,
                        'basal_diameter' : float(row['basal_diameter']),
                        'species_id'     : int(row['speciesID']),
                        'cohort_num'     : int(row['cohortNum']),
                    })
                except (ValueError, KeyError):
                    pass
    except FileNotFoundError:
        print(f"ERROR: '{file_name}' not found.")
        return []

    keys = sorted(year_data)
    print(f"Loaded {len(keys)} years  ({keys[0]} → {keys[-1]})")
    return [(yr, year_data[yr]) for yr in keys]


# ── FOREST DRAWING ────────────────────────────────────────────

cohort_positions = defaultdict(list)
trees            = []


def draw_forest(year, cohorts):
    global trees
    for t in trees:
        destroy(t)
    trees.clear()

    counts  = [max(1, round(c['density'] * LAND_AREA)) for c in cohorts]
    scale_f = min(1.0, MAX_TREES / max(sum(counts), 1))

    active  = set()
    placed  = 0

    for cohort, raw_n in zip(cohorts, counts):
        n   = max(1, round(raw_n * scale_f))
        key = (cohort['species_id'], cohort['cohort_num'])
        active.add(key)

        pos = cohort_positions[key]
        while len(pos) < n:
            pos.append((random.uniform(-48, 48), random.uniform(-48, 48)))
        cohort_positions[key] = pos[:n]

        params = TreeParameters(basal_diameter=cohort['basal_diameter'])
        if params.calculate_height() <= 0:
            continue

        for (x, z) in cohort_positions[key]:
            t = Tree(params=params, species_id=cohort['species_id'],
                     position=(x, 0, z))
            trees.append(t)
            placed += 1

    for key in [k for k in cohort_positions if k not in active]:
        del cohort_positions[key]

    year_text.text = (f"Year: {year}   Trees: {placed}   "
                      f"N=next  P=prev  R=restart")
    print(f"Year {year}: {placed} trees")


# ── APP SETUP ─────────────────────────────────────────────────

app = Ursina(title='PlantFATE Visual Twin', vsync=True)

ground = Entity(
    model='plane', scale=100,
    texture='soil.png', texture_scale=(10, 10),
)

sun = DirectionalLight(y=20, z=10, shadows=False)
sun.look_at(Vec3(0, 0, 0))
AmbientLight(color=color.rgba(140, 140, 140, 180))
Sky(texture='sky_sunset')

year_text = Text(text='Loading...', origin=(0, 0),
                 position=(0, -0.45), scale=1.4, background=True)

Text(text="Right-Click+Drag: Orbit | Scroll: Zoom | N: Next | P: Prev | R: Restart",
     origin=(0, 0), position=(0, -0.49), scale=0.7, color=color.light_gray)

# ── LOAD + START ──────────────────────────────────────────────

all_years  = load_all_years(CSV_FILE)
year_index = 0


def show_year(i):
    if all_years:
        draw_forest(*all_years[i])


def input(key):
    global year_index, cohort_positions
    if   key == 'n' and year_index < len(all_years) - 1:
        year_index += 1;  show_year(year_index)
    elif key == 'p' and year_index > 0:
        year_index -= 1;  show_year(year_index)
    elif key == 'r':
        cohort_positions.clear(); year_index = 0; show_year(0)
    elif key in ('q', 'escape'):
        quit()


EditorCamera()

if all_years:
    show_year(0)
else:
    year_text.text = f"ERROR: '{CSV_FILE}' not found."

app.run()
