"""VJ effects catalog for the VJ/DJ pipeline.

Three tiers of FFmpeg video filters, randomly selected and stacked onto clips.
Daypart profiles control the mood: energetic daytime, moody nighttime, dark overnight.
"""

import random

# Light effects — subtle, can stack freely
LIGHT_EFFECTS = [
    {"name": "warm_shift", "filter": "colorbalance=rs=0.15:gs=-0.05:bs=-0.1"},
    {"name": "cool_shift", "filter": "colorbalance=rs=-0.1:gs=0.05:bs=0.15"},
    {"name": "high_saturation", "filter": "eq=saturation=1.5"},
    {"name": "low_saturation", "filter": "eq=saturation=0.6"},
    {"name": "hue_drift", "filter": "hue=H=2*PI*t/10"},
    {"name": "vignette", "filter": "vignette=PI/4"},
    {"name": "soft_blur", "filter": "gblur=sigma=1.5"},
    {"name": "brightness_boost", "filter": "eq=brightness=0.08:contrast=1.1"},
    {"name": "dark_contrast", "filter": "eq=brightness=-0.05:contrast=1.3"},
    {"name": "slight_hue_rotate", "filter": "hue=h=30"},
    {"name": "sepia", "filter": "colorchannelmixer=.393:.769:.189:0:.349:.686:.168:0:.272:.534:.131"},
]

# Medium effects — more noticeable, max 2 per clip
MEDIUM_EFFECTS = [
    {"name": "frame_blend", "filter": "tblend=all_mode=average"},
    {"name": "frame_blend_screen", "filter": "tblend=all_mode=screen"},
    {"name": "rgba_shift", "filter": "rgbashift=rh=-3:bh=3"},
    {"name": "film_grain", "filter": "noise=alls=20:allf=t+u"},
    {"name": "cross_process", "filter": "curves=preset=cross_process"},
    {"name": "vintage", "filter": "curves=preset=vintage"},
    {"name": "negative", "filter": "curves=preset=negative"},
    {"name": "chromatic_aberration", "filter": "rgbashift=rh=5:rv=-2:bh=-5:bv=2"},
    {"name": "posterize", "filter": "lutyuv=y='bitand(val,240)':u='bitand(val,240)':v='bitand(val,240)'"},
    {"name": "scan_lines", "filter": "drawgrid=w=0:h=2:t=1:c=black@0.3"},
    {"name": "color_bleed", "filter": "gblur=sigma=3,rgbashift=rh=8:bh=-8"},
    {"name": "red_channel", "filter": "colorchannelmixer=rr=1:rg=0:rb=0:gg=0:bb=0"},
    {"name": "blue_channel", "filter": "colorchannelmixer=rr=0:gg=0:bb=1:bg=0:br=0"},
]

# Heavy effects — dramatic, max 1 per clip
HEAVY_EFFECTS = [
    {"name": "edge_glow", "filter": "edgedetect=low=0.1:high=0.3:mode=colormix"},
    {"name": "pixelate", "filter": "scale=iw/8:ih/8:flags=neighbor,scale=iw*8:ih*8:flags=neighbor"},
    {"name": "psychedelic_hue", "filter": "hue=H=2*PI*t/3:s=3"},
    {"name": "quad_mirror", "filter": "crop=iw/2:ih/2:0:0,split[a][b];[a]hflip[c];[b][c]hstack,split[d][e];[d]vflip[f];[e][f]vstack"},
    {"name": "heavy_trails", "filter": "tblend=all_mode=addition:all_opacity=0.7"},
    {"name": "solarize", "filter": "lutyuv=y='if(gt(val,128),256-val,val)*2'"},
    {"name": "glitch", "filter": "noise=alls=40:allf=t,rgbashift=rh=10:rv=5:bh=-10:bv=-3"},
    {"name": "deep_pixelate", "filter": "scale=iw/16:ih/16:flags=neighbor,scale=iw*16:ih*16:flags=neighbor"},
]


# ---------------------------------------------------------------------------
# Daypart profiles — mood control
# ---------------------------------------------------------------------------

# Overlay-only effects — reserved for future use
OVERLAY_EFFECTS = []

DAYPART_PROFILES = {
    "daytime": {
        "tier_weights": (0.60, 0.30, 0.10),   # mostly light, energetic
        "speed_range": (0.85, 1.0),            # PTS multiplier: 0.85-1.0 = plays 1.0-1.18x
        "overlay_chance": 0.40,
        "blend_modes": ["screen", "addition", "softlight"],
    },
    "nighttime": {
        "tier_weights": (0.25, 0.40, 0.35),   # heavier, moodier
        "speed_range": (1.5, 2.2),             # PTS multiplier: 1.5-2.2 = plays 0.45-0.67x
        "overlay_chance": 0.50,
        "blend_modes": ["multiply", "overlay", "softlight", "screen"],
    },
    "overnight": {
        "tier_weights": (0.15, 0.30, 0.55),   # mostly heavy, dark & weird
        "speed_range": (1.5, 2.5),             # PTS multiplier: 1.5-2.5 = plays 0.4-0.67x
        "overlay_chance": 0.55,
        "blend_modes": ["difference", "hardlight", "exclusion", "multiply"],
    },
    "default": {
        "tier_weights": (0.50, 0.35, 0.15),
        "speed_range": (0.9, 1.1),
        "overlay_chance": 0.40,
        "blend_modes": ["screen", "overlay", "softlight"],
    },
}


# Effects that clash — never pick these together
INCOMPATIBLE_PAIRS = [
    {"edge_glow", "high_saturation"},
]


def pick_effects(min_count=1, max_count=3, daypart=None):
    """Pick a random set of effects respecting tier limits.

    Daypart controls the probability of each tier.
    Returns a list of effect dicts.
    """
    profile = DAYPART_PROFILES.get(daypart, DAYPART_PROFILES["default"])
    light_w = profile["tier_weights"][0]
    medium_threshold = light_w + profile["tier_weights"][1]

    count = random.randint(min_count, max_count)
    chosen = []
    chosen_names = set()
    medium_count = 0
    heavy_count = 0

    for _ in range(count):
        roll = random.random()
        if roll < light_w:
            pool = LIGHT_EFFECTS
            tier = "light"
        elif roll < medium_threshold:
            pool = MEDIUM_EFFECTS
            tier = "medium"
        else:
            pool = HEAVY_EFFECTS
            tier = "heavy"

        # Enforce tier limits
        if tier == "medium" and medium_count >= 2:
            pool = LIGHT_EFFECTS
            tier = "light"
        elif tier == "heavy" and heavy_count >= 1:
            pool = MEDIUM_EFFECTS if medium_count < 2 else LIGHT_EFFECTS
            tier = "medium" if medium_count < 2 else "light"

        # Filter out incompatible effects
        blocked = set()
        for pair in INCOMPATIBLE_PAIRS:
            overlap = chosen_names & pair
            if overlap:
                blocked |= pair - overlap
        eligible = [e for e in pool if e["name"] not in blocked]
        if not eligible:
            eligible = pool

        effect = random.choice(eligible)
        chosen.append(effect)
        chosen_names.add(effect["name"])

        if tier == "medium":
            medium_count += 1
        elif tier == "heavy":
            heavy_count += 1

    return chosen


def pick_speed(daypart=None):
    """Pick a PTS multiplier for the current daypart.

    > 1.0 = slower playback (dreamy), < 1.0 = faster playback (energetic).
    """
    profile = DAYPART_PROFILES.get(daypart, DAYPART_PROFILES["default"])
    low, high = profile["speed_range"]
    return round(random.uniform(low, high), 2)


def should_overlay(daypart=None):
    """Return True if this clip should be a two-clip overlay composite."""
    profile = DAYPART_PROFILES.get(daypart, DAYPART_PROFILES["default"])
    return random.random() < profile["overlay_chance"]


def pick_blend_mode(daypart=None):
    """Pick a blend mode for overlay compositing."""
    profile = DAYPART_PROFILES.get(daypart, DAYPART_PROFILES["default"])
    return random.choice(profile["blend_modes"])


def pick_overlay_effects(min_count=1, max_count=3, daypart=None):
    """Pick effects for an overlay clip — includes overlay-only effects like zoompan."""
    effects = pick_effects(min_count, max_count, daypart=daypart)
    # 30% chance to add an overlay-only effect
    if OVERLAY_EFFECTS and random.random() < 0.30:
        effects.append(random.choice(OVERLAY_EFFECTS))
    return effects


def build_filter_string(effects):
    """Build a comma-separated FFmpeg filter string from a list of effects."""
    return ",".join(e["filter"] for e in effects)


def effect_names(effects):
    """Return a list of effect names for logging."""
    return [e["name"] for e in effects]
