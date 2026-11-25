"""Geometric-diversity seed selection for multi-seed placement judging.

Why this exists (AI4PCB fork of KiCadRoutingTools):
    `place_simple.py` generates N placements (scattered / clustered / anchored)
    and originally passes the K lowest `final.cost` legal seeds (`legal[:k]`)
    to `judge_seeds.py`. Because simulated annealing frequently collapses
    multiple random seeds into the same macro basin, pure cost truncation
    spends parallel routing budget on near-duplicate layouts while discarding
    slightly higher-proxy-cost seeds that occupy distinct macro topologies and
    frequently route cleaner (fewer open nets, fewer vias, or shorter copper).

This module selects K seeds from the legal candidate pool while:
    1. Strictly respecting placement defect tiers (clean > fewest defects >
       ungraded) so a broken placement never displaces a clean one.
    2. Bounding proxy-cost slack within a tier via `cost_cap_ratio` (default
       1.20x of the tier's best cost, relaxing smoothly if fewer than K seeds
       fall within the cap).
    3. Always keeping the tier's #1 lowest-cost seed in slot 0 so the baseline
       proxy optimum is never lost.
    4. Spreading the remaining K-1 slots across distinct macro geometric basins
       using macro-weighted IC/component poses (`farthest` MaxMin sampling or
       `medoid` average-linkage clustering with best-cost medoids).
"""
from __future__ import annotations

import json
import math
import os
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


PoseEntry = Tuple[float, float, float, str, float, int]
# (x_mm, y_mm, rot_deg, side, weight, pin_count)


def _footprint_area_mm2(fp) -> float:
    """Rough pad-span area (mm^2) used to up-weight large 2-pin power parts."""
    pads = getattr(fp, 'pads', None) or ()
    if not pads:
        return 0.0
    xs = [float(p.local_x) for p in pads]
    ys = [float(p.local_y) for p in pads]
    w = (max(xs) - min(xs)) + max(float(p.size_x) for p in pads)
    h = (max(ys) - min(ys)) + max(float(p.size_y) for p in pads)
    return max(0.0, w * h)


def _part_weight(pin_count: int, area_mm2: float, is_anchor: bool = False) -> float:
    """Macro-aware geometric importance weight for a movable footprint."""
    if pin_count >= 4:
        w = 4.0 * math.sqrt(pin_count / 4.0)
    elif pin_count == 3:
        w = 1.5
    else:
        w = 1.0 if area_mm2 >= 10.0 else 0.25
    if is_anchor:
        w = max(w, 4.0) * 1.25
    return w


def extract_pose_signature(
    pcb_path: str,
    *,
    anchors: Optional[Iterable[str]] = None,
    exclude_refs: Optional[Iterable[str]] = None,
) -> Tuple[Dict[str, PoseEntry], float]:
    """Parse `pcb_path` and return `(poses_by_ref, board_diag_mm)`.

    Each pose entry is `(x_mm, y_mm, rot_deg, side, weight, pin_count)`.
    """
    from kicad_parser import parse_kicad_pcb

    pcb = parse_kicad_pcb(pcb_path)
    anchor_set = set(anchors or ())
    excl = set(exclude_refs or ())

    # Board diagonal from outline if present, else footprint bbox
    xs, ys = [], []
    outline = getattr(pcb, 'board_outline', None) or ()
    for seg in outline:
        if hasattr(seg, 'start_x'):
            xs.extend((float(seg.start_x), float(seg.end_x)))
            ys.extend((float(seg.start_y), float(seg.end_y)))
        elif isinstance(seg, (tuple, list)) and len(seg) >= 2:
            xs.append(float(seg[0]))
            ys.append(float(seg[1]))
    if not xs and pcb.footprints:
        xs = [float(fp.x) for fp in pcb.footprints.values()]
        ys = [float(fp.y) for fp in pcb.footprints.values()]
    if xs and ys:
        diag = math.hypot(max(xs) - min(xs), max(ys) - min(ys))
    else:
        diag = 100.0
    diag = max(diag, 10.0)

    poses: Dict[str, PoseEntry] = {}
    for ref, fp in pcb.footprints.items():
        if ref in excl:
            continue
        if getattr(fp, 'locked', False):
            continue
        pin_count = len(getattr(fp, 'pads', None) or ())
        if pin_count == 0:
            # Mechanical fiducials/logos without electrical pads do not define topology
            continue
        area = _footprint_area_mm2(fp)
        w = _part_weight(pin_count, area, is_anchor=(ref in anchor_set))
        layer = str(getattr(fp, 'layer', 'F.Cu') or 'F.Cu')
        side = 'B' if layer.startswith('B') else 'F'
        rot = float(getattr(fp, 'rotation', 0.0) or 0.0) % 360.0
        poses[ref] = (float(fp.x), float(fp.y), rot, side, w, pin_count)
    return poses, diag


def _movable_refs(sigs: Dict[int, Dict[str, PoseEntry]]) -> List[str]:
    """Return refs that actually change pose across at least two seeds.

    Automatically filters out locked/fixed connectors even when the board did
    not carry `(locked yes)` flags, so fixed parts never dilute distance.
    """
    seeds = sorted(sigs)
    if len(seeds) < 2:
        return sorted(sigs[seeds[0]]) if seeds else []
    common = set(sigs[seeds[0]])
    for s in seeds[1:]:
        common &= set(sigs[s])
    moved = []
    for r in sorted(common):
        x0, y0, rot0, side0, _, _ = sigs[seeds[0]][r]
        for s in seeds[1:]:
            x, y, rot, side, _, _ = sigs[s][r]
            if (abs(x - x0) > 1e-3 or abs(y - y0) > 1e-3
                    or abs((rot - rot0) % 360.0) > 1e-2 or side != side0):
                moved.append(r)
                break
    return moved if moved else sorted(common)


def pose_distance(
    sig_a: Dict[str, PoseEntry],
    sig_b: Dict[str, PoseEntry],
    refs: Sequence[str],
    diag_mm: float,
    *,
    rot_weight: float = 0.08,
    side_weight: float = 0.15,
) -> Tuple[float, float]:
    """Compute `(weighted_rms_norm, macro_mean_disp_mm)` between two seed poses.

    `weighted_rms_norm` combines board-diagonal-normalized Euclidean shift,
    quarter-turn orientation delta, and side-flip penalty using macro weights.
    `macro_mean_disp_mm` is the interpretable mean Euclidean displacement (mm)
    of multi-pin movable parts (`pin_count >= 3`, falling back to all moved
    parts if none have >= 3 pins).
    """
    w_sum = 0.0
    sq_sum = 0.0
    ic_sum = 0.0
    ic_cnt = 0
    all_sum = 0.0
    all_cnt = 0
    inv_diag = 1.0 / max(diag_mm, 1.0)

    for r in refs:
        xa, ya, ra, sa, wa, pa = sig_a[r]
        xb, yb, rb, sb, wb, pb = sig_b[r]
        disp_mm = math.hypot(xa - xb, ya - yb)
        drot = abs((ra - rb) % 360.0)
        drot = min(drot, 360.0 - drot) / 180.0
        dside = 1.0 if sa != sb else 0.0
        d_norm = disp_mm * inv_diag + rot_weight * drot + side_weight * dside
        w = 0.5 * (wa + wb)
        sq_sum += w * d_norm * d_norm
        w_sum += w
        all_sum += disp_mm
        all_cnt += 1
        if max(pa, pb) >= 3:
            ic_sum += disp_mm
            ic_cnt += 1

    rms = math.sqrt(sq_sum / w_sum) if w_sum > 0.0 else 0.0
    macro_mm = (ic_sum / ic_cnt) if ic_cnt > 0 else (all_sum / all_cnt if all_cnt else 0.0)
    return rms, macro_mm


def pairwise_distances(
    sigs: Dict[int, Dict[str, PoseEntry]],
    diag_mm: float,
) -> Tuple[Dict[Tuple[int, int], float], Dict[Tuple[int, int], float], List[str]]:
    """Return `(norm_dist, ic_mm_dist, moved_refs)` for all seed pairs."""
    refs = _movable_refs(sigs)
    seeds = sorted(sigs)
    d_norm: Dict[Tuple[int, int], float] = {}
    d_mm: Dict[Tuple[int, int], float] = {}
    for i, si in enumerate(seeds):
        d_norm[(si, si)] = 0.0
        d_mm[(si, si)] = 0.0
        for sj in seeds[i + 1:]:
            dn, dm = pose_distance(sigs[si], sigs[sj], refs, diag_mm)
            d_norm[(si, sj)] = d_norm[(sj, si)] = dn
            d_mm[(si, sj)] = d_mm[(sj, si)] = dm
    return d_norm, d_mm, refs


def _subset_spread(
    seeds: Sequence[int],
    d_norm: Dict[Tuple[int, int], float],
    d_mm: Dict[Tuple[int, int], float],
) -> Tuple[float, float, float, float]:
    """`(min_norm, mean_norm, min_ic_mm, mean_ic_mm)` over distinct pairs."""
    if len(seeds) < 2:
        return 0.0, 0.0, 0.0, 0.0
    norms, mms = [], []
    for i, a in enumerate(seeds):
        for b in seeds[i + 1:]:
            norms.append(d_norm.get((a, b), 0.0))
            mms.append(d_mm.get((a, b), 0.0))
    return (
        min(norms),
        sum(norms) / len(norms),
        min(mms),
        sum(mms) / len(mms),
    )


def _select_farthest(
    eligible: Sequence[int],
    k: int,
    d_norm: Dict[Tuple[int, int], float],
    costs: Dict[int, float],
    quality_weight: float,
) -> List[int]:
    """Quality-guarded MaxMin farthest-point selection; slot 0 is always `eligible[0]`."""
    if len(eligible) <= k:
        return list(eligible)
    chosen = [eligible[0]]
    remaining = list(eligible[1:])
    c_vals = [costs.get(s, float(i)) for i, s in enumerate(eligible)]
    c_min, c_max = min(c_vals), max(c_vals)
    c_span = max(c_max - c_min, 1e-9)

    while len(chosen) < k and remaining:
        min_dists = {
            c: min(d_norm.get((c, s), 0.0) for s in chosen)
            for c in remaining
        }
        d_max = max(min_dists.values())
        d_scale = d_max if d_max > 1e-12 else 1.0

        def score(c: int) -> Tuple[float, float, int]:
            norm_d = min_dists[c] / d_scale
            norm_c = (costs.get(c, c_max) - c_min) / c_span
            return (norm_d - quality_weight * norm_c, -costs.get(c, c_max), -c)

        best = max(remaining, key=score)
        chosen.append(best)
        remaining.remove(best)
    return chosen


def _select_medoid(
    eligible: Sequence[int],
    k: int,
    d_norm: Dict[Tuple[int, int], float],
    costs: Dict[int, float],
) -> Tuple[List[int], List[List[int]]]:
    """Average-linkage geometric clustering into K clusters -> best-cost medoid each."""
    if len(eligible) <= k:
        return list(eligible), [[s] for s in eligible]
    order_idx = {s: i for i, s in enumerate(eligible)}
    clusters: List[List[int]] = [[s] for s in eligible]

    def avg_dist(ca: Sequence[int], cb: Sequence[int]) -> float:
        tot = 0.0
        for a in ca:
            for b in cb:
                tot += d_norm.get((a, b), 0.0)
        return tot / (len(ca) * len(cb))

    while len(clusters) > k:
        best_pair = (0, 1)
        best_d = float('inf')
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                d = avg_dist(clusters[i], clusters[j])
                if d < best_d - 1e-12:
                    best_d = d
                    best_pair = (i, j)
        i, j = best_pair
        clusters[i] = sorted(clusters[i] + clusters[j], key=lambda s: order_idx[s])
        clusters.pop(j)

    # Best-cost (lowest rank index) representative in each cluster
    reps = [min(cl, key=lambda s: (costs.get(s, float(order_idx[s])), order_idx[s]))
            for cl in clusters]
    reps.sort(key=lambda s: order_idx[s])
    return reps, clusters


def select_diverse_seeds(
    ranked_seeds: Sequence[int],
    sigs: Dict[int, Dict[str, PoseEntry]],
    k: int,
    *,
    mode: str = 'farthest',
    costs: Optional[Dict[int, float]] = None,
    tiers: Optional[Dict[int, Tuple[bool, int]]] = None,
    cost_cap_ratio: float = 1.20,
    quality_weight: float = 0.15,
    diag_mm: float = 100.0,
    auto_spread_threshold_mm: float = 10.5,
) -> Tuple[List[int], Dict[str, object]]:
    """Select up to `k` geometrically diverse seeds from `ranked_seeds`.

    Args:
        ranked_seeds: Legal seeds already sorted in KRT's `judge_order`
            (best quality tier first, then lowest proxy `final.cost`).
        sigs: `{seed: pose_signature}` from `extract_pose_signature`.
        k: Target number of seeds to judge.
        mode: `'off' | 'auto' | 'farthest' | 'medoid'`.
            In `'auto'` mode, inspects the 90th-percentile pairwise macro IC
            displacement across the legal pool: if tight (`< auto_spread_threshold_mm`,
            e.g. heavily pre-zoned floorplans), uses `'medoid'` with tightened
            `min(cost_cap_ratio, 1.10)` to avoid picking corner outliers;
            otherwise uses `'farthest'` at `cost_cap_ratio`.
        costs: Optional `{seed: final_cost}` used for cost-cap filtering and
            tie-breaking.
        tiers: Optional `{seed: (ungraded_bool, defect_count)}` so clean seeds
            are always filled before any defected/ungraded seed.
        cost_cap_ratio: Maximum proxy cost ratio relative to a tier's lowest
            cost (e.g. 1.20 allows up to +20% proxy cost slack for diversity).
        quality_weight: Tie-break penalty weight on normalized cost in
            `farthest` mode (default 0.15).
        diag_mm: Board diagonal in mm for distance normalization.
        auto_spread_threshold_mm: Macro IC p90 spread cutoff for `'auto'` mode.

    Returns:
        `(selected_seeds, diagnostics_dict)`
    """
    ranked = [int(s) for s in ranked_seeds]
    baseline = ranked[:k]
    if mode == 'off' or k <= 0 or len(ranked) <= 1:
        return baseline, {'mode': mode, 'selected': baseline, 'baseline_top_k': baseline}

    avail_sigs = {s: sigs[s] for s in ranked if s in sigs}
    if len(avail_sigs) < 2:
        return baseline, {'mode': mode, 'selected': baseline, 'baseline_top_k': baseline,
                          'note': 'fewer than 2 pose signatures available'}

    d_norm, d_mm, moved_refs = pairwise_distances(avail_sigs, diag_mm)

    # Measure pool-wide macro IC spread for diagnostics and `'auto'` mode adaptation
    avail_list = sorted(avail_sigs)
    pair_mms = sorted(
        d_mm[(a, b)]
        for i, a in enumerate(avail_list)
        for b in avail_list[i + 1:]
    )
    pool_p90_ic_mm = pair_mms[min(len(pair_mms) - 1, int(0.90 * len(pair_mms)))] if pair_mms else 0.0
    effective_mode = mode
    effective_cap = float(cost_cap_ratio)
    if mode == 'auto':
        if pool_p90_ic_mm < float(auto_spread_threshold_mm):
            effective_mode = 'medoid'
            if effective_cap > 0:
                effective_cap = min(effective_cap, 1.10)
        else:
            effective_mode = 'farthest'
    cost_map: Dict[int, float] = {}
    for idx, s in enumerate(ranked):
        if costs and s in costs and costs[s] is not None:
            cost_map[s] = float(costs[s])
        else:
            cost_map[s] = float(idx + 1)

    # Group ranked seeds by defect tier while preserving tier order
    tier_groups: List[Tuple[Tuple[bool, int], List[int]]] = []
    seen_tiers: Dict[Tuple[bool, int], int] = {}
    for s in ranked:
        t = tiers[s] if (tiers and s in tiers) else (False, 0)
        if t not in seen_tiers:
            seen_tiers[t] = len(tier_groups)
            tier_groups.append((t, []))
        tier_groups[seen_tiers[t]][1].append(s)

    selected: List[int] = []
    clusters_out: List[List[int]] = []
    capped_pool_sizes: List[int] = []

    for _tier_key, group in tier_groups:
        rem = k - len(selected)
        if rem <= 0:
            break
        if len(group) <= rem:
            selected.extend(group)
            capped_pool_sizes.append(len(group))
            continue

        # Only consider seeds with pose signatures; keep any missing-sig seeds at end
        with_sig = [s for s in group if s in avail_sigs]
        best_c = cost_map[with_sig[0]] if with_sig else 0.0
        if effective_cap and effective_cap > 0 and best_c > 0:
            cap = best_c * effective_cap
            eligible = [s for s in with_sig if cost_map[s] <= cap + 1e-9]
            if len(eligible) < rem:
                # Relax cap gracefully in cost order until we have at least `rem`
                eligible = list(with_sig[:rem])
        else:
            eligible = list(with_sig)
        capped_pool_sizes.append(len(eligible))

        if effective_mode == 'medoid':
            picked, cl = _select_medoid(eligible, rem, d_norm, cost_map)
            clusters_out.extend(cl)
        elif effective_mode == 'farthest':
            picked = _select_farthest(eligible, rem, d_norm, cost_map, float(quality_weight))
        else:
            raise ValueError(f"unknown diversity mode {mode!r}; expected 'off', 'auto', 'farthest', or 'medoid'")
        selected.extend(picked)

    b_min_n, b_mean_n, b_min_mm, b_mean_mm = _subset_spread(baseline, d_norm, d_mm)
    s_min_n, s_mean_n, s_min_mm, s_mean_mm = _subset_spread(selected, d_norm, d_mm)
    gain_pct = ((s_mean_mm - b_mean_mm) / b_mean_mm * 100.0) if b_mean_mm > 1e-6 else 0.0
    min_gain_pct = ((s_min_mm - b_min_mm) / b_min_mm * 100.0) if b_min_mm > 1e-6 else 0.0

    diag: Dict[str, object] = {
        'mode': mode,
        'resolved_mode': effective_mode,
        'pool_p90_ic_mm': round(pool_p90_ic_mm, 2),
        'k': k,
        'pool_size': len(ranked),
        'capped_pool_sizes': capped_pool_sizes,
        'cost_cap_ratio': round(effective_cap, 3),
        'quality_weight': float(quality_weight),
        'moved_parts': len(moved_refs),
        'macro_parts': sum(1 for r in moved_refs if avail_sigs[ranked[0]][r][5] >= 3)
        if ranked and ranked[0] in avail_sigs else 0,
        'diag_mm': round(diag_mm, 2),
        'baseline_top_k': baseline,
        'selected': selected,
        'added_seeds': [s for s in selected if s not in baseline],
        'dropped_seeds': [s for s in baseline if s not in selected],
        'baseline_norm_min': round(b_min_n, 4),
        'baseline_norm_mean': round(b_mean_n, 4),
        'selected_norm_min': round(s_min_n, 4),
        'selected_norm_mean': round(s_mean_n, 4),
        'baseline_ic_min_mm': round(b_min_mm, 2),
        'baseline_ic_mean_mm': round(b_mean_mm, 2),
        'selected_ic_min_mm': round(s_min_mm, 2),
        'selected_ic_mean_mm': round(s_mean_mm, 2),
        'ic_mean_gain_pct': round(gain_pct, 1),
        'ic_min_gain_pct': round(min_gain_pct, 1),
    }
    if clusters_out:
        diag['clusters'] = clusters_out
    return selected, diag


def load_seed_costs_from_run(run_dir: str, seeds: Sequence[int]) -> Dict[int, float]:
    """Read `final.cost` per seed from `s<S>.log` (`JSON_SUMMARY:`) if present."""
    out: Dict[int, float] = {}
    for s in seeds:
        log_path = os.path.join(run_dir, f's{s}.log')
        if not os.path.isfile(log_path):
            continue
        try:
            with open(log_path, encoding='utf-8', errors='replace') as fh:
                for line in fh:
                    if line.startswith('JSON_SUMMARY: '):
                        doc = json.loads(line[len('JSON_SUMMARY: '):])
                        c = (doc.get('final') or {}).get('cost')
                        if c is not None:
                            out[int(s)] = float(c)
        except (OSError, ValueError, TypeError):
            continue
    return out


def select_diverse_from_run_dir(
    run_dir: str,
    ranked_seeds: Sequence[int],
    k: int,
    *,
    mode: str = 'farthest',
    costs: Optional[Dict[int, float]] = None,
    tiers: Optional[Dict[int, Tuple[bool, int]]] = None,
    anchors: Optional[Iterable[str]] = None,
    cost_cap_ratio: float = 1.20,
    quality_weight: float = 0.15,
    board_pattern: str = 's{seed}.kicad_pcb',
) -> Tuple[List[int], Dict[str, object]]:
    """Convenience entry point: load poses from `run_dir/s<S>.kicad_pcb` and select."""
    sigs: Dict[int, Dict[str, PoseEntry]] = {}
    diag_mm = 100.0
    for s in ranked_seeds:
        p = os.path.join(run_dir, board_pattern.format(seed=s))
        if not os.path.isfile(p):
            continue
        sig, d = extract_pose_signature(p, anchors=anchors)
        sigs[int(s)] = sig
        diag_mm = d
    if costs is None:
        costs = load_seed_costs_from_run(run_dir, ranked_seeds)
    return select_diverse_seeds(
        ranked_seeds,
        sigs,
        k,
        mode=mode,
        costs=costs,
        tiers=tiers,
        cost_cap_ratio=cost_cap_ratio,
        quality_weight=quality_weight,
        diag_mm=diag_mm,
    )
