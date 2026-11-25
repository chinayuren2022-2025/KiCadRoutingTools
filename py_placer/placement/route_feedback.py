#!/usr/bin/env python3
"""Routing-failure & congestion-heatmap feedback loop for local placement refinement.

Closes the placement <-> routing loop without discarding a good macro layout:
  1. Parses a first-pass routed `.kicad_pcb` + its `.route.json` summary.
  2. Identifies real routing pain points:
       - Unrouted / failed / open nets from `route.json`
       - High-detour nets (`routed_len / mst_l1 >= detour_thresh` and `len >= 12 mm`)
       - Excessive-via nets (`vias >= via_thresh`)
       - 2D track+via congestion hotspot grid cells (default 4 mm cell pitch)
  3. Maps those pain points back to the movable footprints touching them
     (`hotspot_refs`), keeping clean/quiet regions completely frozen!
  4. Synthesizes local corridor-expansion repulsion hints (`local_repulsion`)
     between cramped non-wired footprint pairs inside the congested cells.
  5. Writes `feedback_constraints.json` ready for:
       `place_simple.py <placed.kicad_pcb> <refined.kicad_pcb> --keep-poses
        --movable-only <hotspot_refs> --local-radius <R> --constraints-json ...`
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLACER_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(PLACER_DIR)
for _d in ('py_router', 'py_tools', 'py_placer'):
    _p = os.path.join(ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _mst_l1(pts: Sequence[Tuple[float, float]]) -> float:
    n = len(pts)
    if n < 2:
        return 0.0
    used = [False] * n
    dist = [float('inf')] * n
    dist[0] = 0.0
    total = 0.0
    for _ in range(n):
        u = min((i for i in range(n) if not used[i]), key=lambda i: dist[i])
        used[u] = True
        total += dist[u]
        ux, uy = pts[u]
        for v in range(n):
            if not used[v]:
                d = abs(ux - pts[v][0]) + abs(uy - pts[v][1])
                if d < dist[v]:
                    dist[v] = d
    return total


def analyze_routing_feedback(
    routed_pcb_path: str,
    route_json_path: Optional[str] = None,
    *,
    ground_nets: Sequence[str] = ('/AGND', 'GND', '/GND', 'AGND'),
    cell_mm: float = 4.0,
    detour_thresh: float = 1.40,
    min_detour_len_mm: float = 12.0,
    via_thresh: int = 4,
    corridor_gap_mm: float = 2.6,
    max_hotspot_refs: int = 28,
    base_constraints: Optional[Dict] = None,
) -> Dict:
    """Extract routing hotspots and synthesize local refinement constraints."""
    from kicad_parser import parse_kicad_pcb

    pcb = parse_kicad_pcb(routed_pcb_path)
    gnd_set = set(ground_nets)
    net_names = {nid: n.name for nid, n in pcb.nets.items()}

    # Pad membership per net & locked refs
    net_pads: Dict[int, List[Tuple[str, float, float]]] = defaultdict(list)
    part_nets: Dict[str, Set[int]] = defaultdict(set)
    locked_refs: Set[str] = set()
    for ref, fp in pcb.footprints.items():
        if getattr(fp, 'locked', False):
            locked_refs.add(ref)
        for pad in fp.pads:
            if pad.net_id > 0:
                net_pads[pad.net_id].append((ref, float(pad.global_x), float(pad.global_y)))
                part_nets[ref].add(pad.net_id)

    # 1. Failed / open nets from route.json
    failed_net_names: Set[str] = set()
    if route_json_path and os.path.isfile(route_json_path):
        rj = json.loads(Path(route_json_path).read_text(encoding='utf-8'))
        for key in ('failed_single', 'open_single', 'failed_multipoint'):
            for item in rj.get(key) or ():
                if isinstance(item, str):
                    failed_net_names.add(item)
                elif isinstance(item, dict) and item.get('net'):
                    failed_net_names.add(str(item['net']))

    # 2. Per-net routed length, via count, and 2D congestion grid
    net_len: Dict[int, float] = defaultdict(float)
    net_vias: Counter = Counter()
    cell_score: Counter = Counter()
    cell_nets: Dict[Tuple[int, int], Set[int]] = defaultdict(set)

    for seg in pcb.segments:
        nname = net_names.get(seg.net_id, '')
        if nname in gnd_set:
            continue
        L = math.hypot(seg.end_x - seg.start_x, seg.end_y - seg.start_y)
        net_len[seg.net_id] += L
        steps = max(1, int(math.ceil(L / (cell_mm * 0.5))))
        for k in range(steps + 1):
            t = k / steps
            x = seg.start_x + (seg.end_x - seg.start_x) * t
            y = seg.start_y + (seg.end_y - seg.start_y) * t
            cell = (int(math.floor(x / cell_mm)), int(math.floor(y / cell_mm)))
            cell_score[cell] += (L / (steps + 1)) * (1.0 + max(0.0, seg.width - 0.2) * 2.0)
            cell_nets[cell].add(seg.net_id)

    for via in pcb.vias:
        nname = net_names.get(via.net_id, '')
        if nname in gnd_set:
            continue
        net_vias[via.net_id] += 1
        cell = (int(math.floor(via.x / cell_mm)), int(math.floor(via.y / cell_mm)))
        cell_score[cell] += 3.5
        cell_nets[cell].add(via.net_id)

    # 3. High-detour & high-via nets
    detour_nets = []
    high_via_nets = []
    for nid, rlen in net_len.items():
        nname = net_names.get(nid, '')
        if not nname or nname in gnd_set:
            continue
        pts = [(x, y) for _r, x, y in net_pads.get(nid, ())]
        if len(pts) < 2:
            continue
        # Skip huge global rails (>15 pads) from simple detour ratio
        if len(pts) > 15:
            continue
        mst = _mst_l1(pts)
        ratio = rlen / max(mst, 1.0)
        if rlen >= min_detour_len_mm and ratio >= detour_thresh:
            detour_nets.append({
                'net': nname, 'routed_mm': round(rlen, 2),
                'mst_mm': round(mst, 2), 'detour_ratio': round(ratio, 2),
                'vias': net_vias[nid],
            })
        if net_vias[nid] >= via_thresh:
            high_via_nets.append({'net': nname, 'vias': net_vias[nid], 'routed_mm': round(rlen, 2)})

    detour_nets.sort(key=lambda d: -d['detour_ratio'])
    high_via_nets.sort(key=lambda d: -d['vias'])

    # 4. Top congestion cells (top 15% of active cells, min 4, max 16)
    ranked_cells = cell_score.most_common()
    n_hot_cells = min(16, max(4, int(len(ranked_cells) * 0.15))) if ranked_cells else 0
    hot_cells = [c for c, _sc in ranked_cells[:n_hot_cells]]
    hot_cell_set = set(hot_cells)

    # 5. Score movable footprints by involvement in failed / detour / via / hot cells
    ref_pain: Counter = Counter()
    name_to_nid = {name: nid for nid, name in net_names.items()}
    for fn in failed_net_names:
        nid = name_to_nid.get(fn)
        if nid is not None:
            for r, _x, _y in net_pads.get(nid, ()):
                if r not in locked_refs:
                    ref_pain[r] += 50.0
    for d in detour_nets[:10]:
        nid = name_to_nid.get(d['net'])
        if nid is not None:
            for r, _x, _y in net_pads.get(nid, ()):
                if r not in locked_refs:
                    ref_pain[r] += 10.0 * (d['detour_ratio'] - 1.0)
    for hv in high_via_nets[:8]:
        nid = name_to_nid.get(hv['net'])
        if nid is not None:
            for r, _x, _y in net_pads.get(nid, ()):
                if r not in locked_refs:
                    ref_pain[r] += 3.0 * hv['vias']

    # Also score footprints sitting inside or adjacent to hot congestion cells
    hot_cell_refs: Set[str] = set()
    for ref, fp in pcb.footprints.items():
        if ref in locked_refs:
            continue
        cx, cy = int(math.floor(fp.x / cell_mm)), int(math.floor(fp.y / cell_mm))
        if (cx, cy) in hot_cell_set:
            ref_pain[ref] += cell_score[(cx, cy)] * 0.5
            hot_cell_refs.add(ref)

    hotspot_refs = [r for r, _sc in ref_pain.most_common(max_hotspot_refs)]

    # 6. Synthesize local corridor-expansion repulsion hints between cramped non-wired
    #    pairs inside hotspot regions (opens routing channels without exploding the board)
    local_repulsion = list((base_constraints or {}).get('local_repulsion') or ())
    seen_pairs = set()
    hot_fps = [(r, pcb.footprints[r]) for r in hotspot_refs if r in pcb.footprints]
    for i, (ra, fa) in enumerate(hot_fps):
        for rb, fb in hot_fps[i + 1:]:
            # Only push apart parts that do NOT share a direct low-fanout signal net
            shared = {
                n for n in (part_nets[ra] & part_nets[rb])
                if net_names.get(n, '') not in gnd_set and len(net_pads[n]) <= 8
            }
            if shared:
                continue
            dist = math.hypot(fa.x - fb.x, fa.y - fb.y)
            if dist <= corridor_gap_mm + 3.0:
                pk = tuple(sorted((ra, rb)))
                if pk not in seen_pairs:
                    seen_pairs.add(pk)
                    local_repulsion.append({
                        'key': f'cong_corridor:{pk[0]}:{pk[1]}',
                        'a': [pk[0]],
                        'b': [pk[1]],
                        'min_mm': corridor_gap_mm,
                        'weight': 18.0,
                    })
                    if len(seen_pairs) >= 24:
                        break
        if len(seen_pairs) >= 24:
            break

    out_constraints = dict(base_constraints or {})
    out_constraints['local_repulsion'] = local_repulsion

    return {
        'routed_pcb': routed_pcb_path,
        'failed_nets': sorted(failed_net_names),
        'detour_nets': detour_nets[:12],
        'high_via_nets': high_via_nets[:12],
        'hot_cells_mm': [
            {'x_mm': round(c[0] * cell_mm, 1), 'y_mm': round(c[1] * cell_mm, 1),
             'score': round(cell_score[c], 2), 'net_count': len(cell_nets[c])}
            for c in hot_cells[:10]
        ],
        'hotspot_refs': hotspot_refs,
        'corridor_repulsion_pairs': len(seen_pairs),
        'constraints': out_constraints,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Extract routing congestion feedback for local placement refinement')
    ap.add_argument('routed_pcb', help='first-pass routed .kicad_pcb')
    ap.add_argument('--route-json', default=None, help='optional .route.json from route.py')
    ap.add_argument('--base-constraints', default=None, help='optional existing constraints JSON to merge')
    ap.add_argument('--out-feedback', required=True, help='output JSON with hotspots & feedback constraints')
    ap.add_argument('--out-constraints', required=True, help='output constraints JSON for place_simple --constraints-json')
    ap.add_argument('--corridor-gap', type=float, default=2.6)
    ap.add_argument('--max-hotspots', type=int, default=28)
    a = ap.parse_args(argv)

    base = json.loads(Path(a.base_constraints).read_text(encoding='utf-8')) if a.base_constraints else None
    res = analyze_routing_feedback(
        a.routed_pcb,
        a.route_json,
        corridor_gap_mm=a.corridor_gap,
        max_hotspot_refs=a.max_hotspots,
        base_constraints=base,
    )
    Path(a.out_feedback).write_text(json.dumps(res, indent=2), encoding='utf-8')
    Path(a.out_constraints).write_text(json.dumps(res['constraints'], indent=2), encoding='utf-8')
    print(f"route_feedback: failed_nets={len(res['failed_nets'])}, "
          f"detour_nets={len(res['detour_nets'])}, high_via_nets={len(res['high_via_nets'])}, "
          f"hotspot_refs={len(res['hotspot_refs'])}, corridor_pairs={res['corridor_repulsion_pairs']}")
    print(f"  top_hotspots: {res['hotspot_refs'][:12]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
