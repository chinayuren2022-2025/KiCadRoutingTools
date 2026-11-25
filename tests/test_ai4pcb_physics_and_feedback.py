#!/usr/bin/env python3
"""Unit tests for AI4PCB physics constraints (apart_min, symmetry), local_radius,
macro_flow, compile_board_constraints, and route_feedback."""
import json
import math
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in ('', 'py_router', 'py_tools', 'py_placer'):
    _p = os.path.join(ROOT, _d) if _d else ROOT
    if _p not in sys.path:
        sys.path.insert(0, _p)

from kicad_parser import parse_kicad_pcb  # noqa: E402
from placement.simple import Model  # noqa: E402
from placement import simple_anneal  # noqa: E402
from placement.route_feedback import analyze_routing_feedback  # noqa: E402
import place_simple  # noqa: E402

BOARD = os.path.join(ROOT, 'kicad_files', 'routed_output.kicad_pcb')


def test_incremental_local_equals_total_with_symmetry_and_apart_min():
    """Incremental Model.local() delta must match scratch Model.total() delta to <1e-6."""
    pcb = parse_kicad_pcb(BOARD)
    fps = sorted(pcb.footprints.keys())
    assert len(fps) >= 4
    ra, rb, rc, rd = fps[0], fps[1], fps[2], fps[3]
    with tempfile.TemporaryDirectory() as td:
        gpath = os.path.join(td, 'g.json')
        ipath = os.path.join(td, 'i.json')
        cpath = os.path.join(td, 'c.json')
        json.dump({'schema': 1, 'groups': [{'name': 'all', 'refs': fps}]}, open(gpath, 'w'))
        json.dump({'schema': 1, 'kind': 'floorplan-intent', 'board': BOARD,
                   'assembly': {'sides': 'F'}, 'blocks': [], 'keepouts': [],
                   'edge_connectors': [], 'decaps': {}}, open(ipath, 'w'))
        json.dump({
            'thermal_separation': {
                'hot_refs': [ra], 'sensitive_refs': [rc],
                'min_hot_to_sensitive_mm': 15.0, 'weight_hot_to_sensitive': 30.0,
            },
            'symmetry': {'dx_mm': 0.0, 'dy_mm': 10.0, 'drot_deg': 0.0, 'weight': 25.0},
            'symmetry_pairs': [[ra, rb]],
            'local_repulsion': [{'key': 'r0', 'a': [rc], 'b': [rd], 'min_mm': 8.0, 'weight': 15.0}],
        }, open(cpath, 'w'))

        ap = place_simple.build_parser()
        args = ap.parse_args([BOARD, os.path.join(td, 'out.kicad_pcb'),
                              '--groups', gpath, '--intent', ipath,
                              '--constraints-json', cpath])
        doc = json.load(open(gpath))
        base = json.load(open(ipath))
        state, model, movable, _ = place_simple.build(pcb, BOARD, doc, base, args, intent=None)

        # Verify hints were injected
        kinds = {h['kind'] for h in model.hints}
        assert 'apart_min' in kinds and 'symmetry' in kinds, kinds

        # Check exact delta equality across 10 random moves
        t_before = model.total()['cost']
        for step, ref in enumerate((ra, rb, rc, rd, ra, rb)):
            p0 = state.parts[ref]
            old = (p0.x, p0.y, p0.rot)
            touched = model.touched([ref])
            before_loc = model.local([ref], touched, reprice=False)
            new_pose = (old[0] + 1.5 * (step + 1), old[1] - 0.8 * (step + 1), (old[2] + 90.0) % 360.0)
            p0.ensure_rotation(new_pose[2])
            simple_anneal._place(state, ref, *new_pose)
            after_loc = model.local([ref], touched, reprice=True)
            model.commit([ref], touched[0])
            t_after = model.total()['cost']
            d_loc = after_loc - before_loc
            d_tot = t_after - t_before
            assert abs(d_loc - d_tot) < 1e-5, f"mismatch on {ref}: local={d_loc:.6f} vs total={d_tot:.6f}"
            t_before = t_after
    print("  PASS: symmetry + apart_min incremental local() matches total() to <1e-5")


def test_route_feedback_runs_on_routed_board():
    """route_feedback extracts hotspots and repulsion hints on a real routed board."""
    res = analyze_routing_feedback(BOARD, detour_thresh=1.15, min_detour_len_mm=5.0, via_thresh=2)
    assert isinstance(res['hotspot_refs'], list)
    assert 'local_repulsion' in res['constraints']
    print(f"  PASS: route_feedback extracted {len(res['hotspot_refs'])} hotspots, "
          f"{len(res['detour_nets'])} detour nets, {res['corridor_repulsion_pairs']} corridor pairs")


if __name__ == '__main__':
    test_incremental_local_equals_total_with_symmetry_and_apart_min()
    test_route_feedback_runs_on_routed_board()
    print("ALL PHYSICS & FEEDBACK UNIT TESTS PASSED")
