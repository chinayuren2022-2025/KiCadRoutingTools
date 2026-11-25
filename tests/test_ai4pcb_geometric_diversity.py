#!/usr/bin/env python3
"""Unit tests for AI4PCB geometric-diversity seed selection (`placement.diversity`)."""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in ('', 'py_router', 'py_tools', 'py_placer'):
    _p = os.path.join(ROOT, _d) if _d else ROOT
    if _p not in sys.path:
        sys.path.insert(0, _p)

from placement.diversity import (  # noqa: E402
    pairwise_distances,
    select_diverse_seeds,
)


def _make_sig(ic_pos, passive_pos=(50.0, 50.0), rot=0.0, side='F'):
    """Synthetic board with 2 locked connectors, 2 macro ICs (8 pins), 2 passives."""
    return {
        'J1': (0.0, 20.0, 0.0, 'F', 4.0, 4),      # constant across all seeds (locked)
        'J2': (100.0, 20.0, 0.0, 'F', 4.0, 4),    # constant across all seeds (locked)
        'U1': (ic_pos[0], ic_pos[1], rot, side, 5.656, 8),
        'U2': (ic_pos[2], ic_pos[3], rot, side, 5.656, 8),
        'R1': (passive_pos[0], passive_pos[1], 0.0, 'F', 0.25, 2),
        'R2': (passive_pos[0] + 2.0, passive_pos[1], 0.0, 'F', 0.25, 2),
    }


def test_cluster_collapse_avoided_and_slot0_preserved():
    """When top-cost seeds collapse into basin A, diversity picks basins B & C too."""
    sigs = {
        # Basin A (4 near-duplicate lowest-cost seeds)
        0: _make_sig((30.0, 20.0, 70.0, 20.0)),
        1: _make_sig((30.5, 20.2, 69.8, 20.1)),
        2: _make_sig((29.7, 19.9, 70.3, 20.4)),
        3: _make_sig((30.2, 20.5, 69.5, 19.8)),
        # Basin B (horizontally swapped ICs, +6% cost)
        4: _make_sig((70.0, 20.0, 30.0, 20.0)),
        5: _make_sig((69.5, 20.3, 30.2, 19.7)),
        # Basin C (vertically stacked ICs, +10% cost)
        6: _make_sig((50.0, 10.0, 50.0, 70.0), rot=90.0),
        # Outlier basin D (+40% cost, exceeds 1.20 cap)
        7: _make_sig((10.0, 80.0, 90.0, 80.0), rot=180.0),
    }
    costs = {0: 100.0, 1: 101.0, 2: 102.0, 3: 103.0, 4: 106.0, 5: 107.0, 6: 110.0, 7: 140.0}
    ranked = [0, 1, 2, 3, 4, 5, 6, 7]
    tiers = {s: (False, 0) for s in ranked}

    sel_f, diag_f = select_diverse_seeds(
        ranked, sigs, k=3, mode='farthest', costs=costs, tiers=tiers,
        cost_cap_ratio=1.20, quality_weight=0.15, diag_mm=128.0,
    )
    assert sel_f[0] == 0, f"slot 0 must stay best-cost seed 0, got {sel_f}"
    assert set(sel_f) == {0, 4, 6}, f"expected one from each basin within cap, got {sel_f}"
    assert 7 not in sel_f, "cost-capped outlier seed 7 must be excluded"
    assert diag_f['selected_ic_mean_mm'] > diag_f['baseline_ic_mean_mm'] * 10.0, diag_f

    sel_m, diag_m = select_diverse_seeds(
        ranked, sigs, k=3, mode='medoid', costs=costs, tiers=tiers,
        cost_cap_ratio=1.20, diag_mm=128.0,
    )
    assert sel_m[0] == 0, sel_m
    assert set(sel_m) == {0, 4, 6}, f"medoid expected {{0, 4, 6}}, got {sel_m}"
    assert 'clusters' in diag_m and len(diag_m['clusters']) == 3, diag_m
    print(f"  PASS: cluster collapse avoided -> farthest={sel_f}, medoid={sel_m}, "
          f"IC spread {diag_f['baseline_ic_mean_mm']} -> {diag_f['selected_ic_mean_mm']} mm")


def test_defect_tier_never_violated():
    """A defected seed far away must NEVER displace a clean seed."""
    sigs = {
        0: _make_sig((30.0, 20.0, 70.0, 20.0)),
        1: _make_sig((30.2, 20.1, 69.9, 20.0)),
        2: _make_sig((90.0, 80.0, 10.0, 80.0), rot=180.0),  # huge distance, but 1 defect!
    }
    costs = {0: 100.0, 1: 101.0, 2: 95.0}
    tiers = {0: (False, 0), 1: (False, 0), 2: (False, 1)}
    ranked = [0, 1, 2]
    sel, _ = select_diverse_seeds(ranked, sigs, k=2, mode='farthest',
                                  costs=costs, tiers=tiers, diag_mm=128.0)
    assert sel == [0, 1], f"defected seed 2 must never displace clean seed 1: {sel}"
    print("  PASS: clean tier strictly prioritized over defected outlier")


def test_locked_parts_auto_excluded():
    """Constant locked connectors must not appear in moved_refs."""
    sigs = {
        0: _make_sig((30.0, 20.0, 70.0, 20.0)),
        1: _make_sig((60.0, 20.0, 40.0, 20.0)),
    }
    _, d_mm, moved = pairwise_distances(sigs, 128.0)
    assert 'J1' not in moved and 'J2' not in moved, moved
    assert 'U1' in moved and 'U2' in moved, moved
    assert abs(d_mm[(0, 1)] - 30.0) < 1e-6, d_mm[(0, 1)]
    print("  PASS: constant locked parts auto-excluded; IC displacement exact (30.0 mm)")


def test_auto_mode_switches_by_macro_spread():
    """Auto mode picks farthest on wide macro spread and medoid@1.10 on tight pre-zoned spread."""
    wide_sigs = {
        0: _make_sig((20.0, 20.0, 80.0, 20.0)),
        1: _make_sig((21.0, 20.0, 79.0, 20.0)),
        2: _make_sig((80.0, 20.0, 20.0, 20.0)),
        3: _make_sig((50.0, 70.0, 50.0, 10.0)),
    }
    tight_sigs = {
        0: _make_sig((30.0, 20.0, 70.0, 20.0)),
        1: _make_sig((32.0, 21.0, 68.0, 21.0)),
        2: _make_sig((34.0, 22.0, 66.0, 22.0)),
        3: _make_sig((36.0, 23.0, 64.0, 23.0)),
    }
    costs = {0: 100.0, 1: 102.0, 2: 108.0, 3: 118.0}
    _, dw = select_diverse_seeds([0, 1, 2, 3], wide_sigs, k=2, mode='auto', costs=costs, diag_mm=128.0)
    assert dw['resolved_mode'] == 'farthest' and dw['cost_cap_ratio'] == 1.2, dw
    _, dt = select_diverse_seeds([0, 1, 2, 3], tight_sigs, k=2, mode='auto', costs=costs, diag_mm=128.0)
    assert dt['resolved_mode'] == 'medoid' and dt['cost_cap_ratio'] == 1.1, dt
    print(f"  PASS: auto mode resolved wide->farthest@{dw['cost_cap_ratio']} (p90={dw['pool_p90_ic_mm']}mm), "
          f"tight->medoid@{dt['cost_cap_ratio']} (p90={dt['pool_p90_ic_mm']}mm)")


if __name__ == '__main__':
    test_cluster_collapse_avoided_and_slot0_preserved()
    test_defect_tier_never_violated()
    test_locked_parts_auto_excluded()
    test_auto_mode_switches_by_macro_spread()
    print("ALL GEOMETRIC DIVERSITY UNIT TESTS PASSED")
