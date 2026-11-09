"""Generate the hero brain point cloud -> static/brain.bin.

Anatomy is built as a union of ellipsoids (frontal, parietal, occipital and
temporal lobes per hemisphere, cerebellum, brainstem). Cortical folds come from
the zero-crossings of 3D gradient noise: where the noise is near zero we cut a
narrow groove (sulcus), everywhere else the surface stays full (gyrus). That
gives the meandering, maze-like pattern of a real cortex.

Binary layout: uint32 N, then N x (int16 x, int16 y, int16 z, uint8 groove, uint8 part)
coords scaled by 1/10000.
"""
import numpy as np, struct, sys, os
rng = np.random.default_rng(7)

# ---------- 3D Perlin noise (vectorised)
P = rng.permutation(256); P = np.concatenate([P, P])
G = rng.normal(size=(256, 3)); G /= np.linalg.norm(G, axis=1, keepdims=True)
def fade(t): return t * t * t * (t * (t * 6 - 15) + 10)
def perlin(p):
    pi = np.floor(p).astype(int); pf = p - pi; pi &= 255
    u = fade(pf); out = 0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                h = P[P[P[pi[:, 0] + dx] + pi[:, 1] + dy] + pi[:, 2] + dz]
                g = G[h]; d = pf - np.array([dx, dy, dz])
                w = (u[:, 0] if dx else 1 - u[:, 0]) * (u[:, 1] if dy else 1 - u[:, 1]) * (u[:, 2] if dz else 1 - u[:, 2])
                out = out + w * (g * d).sum(1)
    return out

def ell_pts(c, r, n):
    d = rng.normal(size=(n, 3)); d /= np.linalg.norm(d, axis=1, keepdims=True)
    return c + d * r, d

def inside(p, c, r, s=0.985):
    return (((p - c) / (np.array(r) * s)) ** 2).sum(1) < 1

def hemisphere(side):
    lobes = [  # center, radii, share of points
        ((side * .44, .16, .05), (.56, .60, 1.00), .45),   # main cerebrum (parietal/frontal)
        ((side * .40, .02, .62), (.47, .50, .48), .18),    # frontal pole
        ((side * .38, .06, -.70), (.42, .46, .42), .14),   # occipital
        ((side * .60, -.30, .18), (.36, .29, .62), .23),   # temporal lobe
    ]
    pts, norms = [], []
    for i, (c, r, share) in enumerate(lobes):
        n = int(34000 * share)
        p, d = ell_pts(np.array(c), np.array(r), n)
        keep = np.ones(n, bool)
        for j, (c2, r2, _) in enumerate(lobes):
            if j != i: keep &= ~inside(p, np.array(c2), r2)
        keep &= (p[:, 0] * side > .045)                   # longitudinal fissure
        pts.append(p[keep]); norms.append(d[keep])
    p = np.concatenate(pts); nrm = np.concatenate(norms)
    # lateral (Sylvian) fissure: deep groove above the temporal lobe
    syl_y = -.12 - .22 * (p[:, 2] - .55) * (p[:, 2] < .55) * .6
    syl = (np.abs(p[:, 1] - syl_y) < .035) & (p[:, 0] * side > .62) & (p[:, 2] > -.45) & (p[:, 2] < .78)
    p, nrm = p[~syl], nrm[~syl]
    # cortical folds
    q = p * 5.2 + side * 11.3
    warp = np.stack([perlin(q * .45 + 40), perlin(q * .45 + 80), perlin(q * .45 + 120)], 1) * 1.1
    n = perlin(q + warp)
    a = np.abs(n)
    groove = np.clip(1 - a / .13, 0, 1)                   # 1 inside a sulcus, 0 on a gyrus
    disp = -.06 * groove ** 1.3 + .02 * np.clip(a - .15, 0, .4)
    # central sulcus: one long groove over the top, slanting forward
    cen = np.abs(p[:, 2] - (.08 + .30 * (p[:, 1] - .1))) < .03
    cen &= p[:, 1] > -.15
    groove = np.where(cen, 1, groove); disp = np.where(cen, -.07, disp)
    p = p + nrm * disp[:, None]
    return p, groove

def cerebellum():
    out, gr = [], []
    for side in (-1, 1):
        c = np.array([side * .27, -.50, -.72]); r = np.array([.33, .24, .30])
        p, d = ell_pts(c, r, 5200)
        keep = (p[:, 0] * side > .02) & ~((p[:, 2] > -.55) & (p[:, 1] > -.42))
        p, d = p[keep], d[keep]
        s = np.sin(p[:, 1] * 95 + np.sin(p[:, 2] * 9) * 2)
        groove = np.clip(1 - (s + 1) / .45, 0, 1)
        p = p + d * (-.02 * groove)[:, None]
        out.append(p); gr.append(groove)
    return np.concatenate(out), np.concatenate(gr)

def stem(n=1300):
    h = rng.random(n); a = rng.random(n) * 6.283; r = .16 - .05 * h
    p = np.stack([np.cos(a) * r, -.30 - h * .62, -.18 - h * .30 + np.sin(a) * r], 1)
    return p, np.full(n, .25)

parts = []
for i, (p, g) in enumerate([hemisphere(-1), hemisphere(1), cerebellum(), stem()]):
    parts.append((p, g, np.full(len(p), min(i, 3))))
P_ = np.concatenate([x[0] for x in parts]); Gr = np.concatenate([x[1] for x in parts]); Pt = np.concatenate([x[2] for x in parts])
P_ -= np.array([0, .02, 0])
print("points", len(P_))
out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "..", "static", "brain.bin")
with open(out, "wb") as f:
    f.write(struct.pack("<I", len(P_)))
    q = np.round(P_ * 10000).astype("<i2"); g8 = np.round(Gr * 255).astype(np.uint8); pt = Pt.astype(np.uint8)
    rec = np.zeros(len(P_), dtype=[("x", "<i2"), ("y", "<i2"), ("z", "<i2"), ("g", "u1"), ("p", "u1")])
    rec["x"], rec["y"], rec["z"], rec["g"], rec["p"] = q[:, 0], q[:, 1], q[:, 2], g8, pt
    f.write(rec.tobytes())
if "--preview" in sys.argv:
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    fig, ax = plt.subplots(1, 3, figsize=(18, 6), facecolor="black")
    for a_, (i, j, t) in zip(ax, [(2, 1, "side"), (0, 2, "top"), (0, 1, "front")]):
        vis = P_[:, 0] > -0.02 if t == "side" else (P_[:, 1] > .0 if t == "top" else P_[:, 2] > 0)
        a_.scatter(P_[vis, i], P_[vis, j], s=.4, c=np.clip(1 - Gr[vis], 0, 1)**2, cmap="magma", alpha=.9, linewidths=0)
        a_.set_facecolor("black"); a_.set_aspect("equal"); a_.set_title(t, color="w"); a_.axis("off")
    plt.savefig("/tmp/brain_preview.png", dpi=90, facecolor="black")
