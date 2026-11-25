#!/usr/bin/env python3
"""Compile GongLab Route-B `board.yaml` + `.kicad_pcb` into KRT constraint artifacts.

Eliminates heuristic regex guessing (e.g. misclassifying `/V_GP` & `/V_GN` as
differential pairs or dropping `/CHASSIS` from widened nets) by compiling the
authoritative `board.yaml` specification directly into:

  1. `routing_policy.json`:
     - explicit `power_nets` and `power_nets_widths` (including input rails,
       downstream protected rails, and ESD/chassis bond nets like `/CHASSIS`)
     - explicit `reference_plane_nets` and `route_layers`
     - safe DFM defaults (`same_net_pad_clearance: 0.25`, never `-1.0`)
     - explicit `diff_pairs` (empty unless declared — stops regex false hits)
  2. `compiled_constraints.json`:
     - decap distance limits (`max_decap_loop_mm`, `max_return_via_dist_mm`)
     - EP thermal via targets (`exposed_pad_refs`, `min_ep_thermal_vias`)
     - thermal repulsion groups (`hot_refs` from `thermal_budgets`, `sensitive_refs`)
     - geometric translation symmetry spec inferred from locked paired connectors
       when `xy_channel_geometric_symmetry_required: true`
     - hard DFM signoff gates (`outer_zone_pad_conn: 1`, `min_perimeter_stitch_vias`)
  3. Optional `judge_chain.txt`: ready for `py_placer/judge_seeds.py`.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _d in ('py_router', 'py_tools', 'py_placer'):
    _p = os.path.join(ROOT, _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_yaml(path: Path) -> Dict:
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except ImportError:
        # Minimal fallback if PyYAML is absent: shell out to KiCad Python or error cleanly
        raise RuntimeError('PyYAML is required (`pip install pyyaml`) to compile board.yaml')


def _norm_net(name: str, board_nets: Sequence[str]) -> str:
    """Match a net name from board.yaml (`CHASSIS` or `/CHASSIS`) to board netlist."""
    if name in board_nets:
        return name
    if not name.startswith('/') and ('/' + name) in board_nets:
        return '/' + name
    if name.startswith('/') and name[1:] in board_nets:
        return name[1:]
    return name


def _infer_symmetry_from_connectors(pcb, board_yaml: Dict) -> Optional[Dict]:
    """Infer exact X/Y translation offset from locked paired connectors if declared."""
    specs = board_yaml.get('specs') or {}
    gc = specs.get('global_constraints') or {}
    analog = gc.get('board_specific_analog_constraints') or {}
    if not analog.get('xy_channel_geometric_symmetry_required'):
        return None

    # Look for X/Y paired connectors in interface_contract
    ic_root = (specs.get('interface_contract') or {}).get('boards') or {}
    b_ic = next(iter(ic_root.values()), {}) if isinstance(ic_root, dict) else {}
    conns = b_ic.get('connectors') or {}

    by_role: Dict[str, Dict[str, str]] = {}
    for ref, info in conns.items():
        if not isinstance(info, dict):
            continue
        sig = str(info.get('signal_net') or '')
        if sig.startswith('X_') or sig.startswith('Y_'):
            axis = sig[0]
            stem = sig[2:]
            by_role.setdefault(stem, {})[axis] = ref

    deltas = []
    paired_conns = []
    for stem, pair in sorted(by_role.items()):
        if 'X' in pair and 'Y' in pair:
            rx, ry = pair['X'], pair['Y']
            if rx in pcb.footprints and ry in pcb.footprints:
                fx, fy = pcb.footprints[rx], pcb.footprints[ry]
                deltas.append((round(fy.x - fx.x, 3), round(fy.y - fx.y, 3)))
                paired_conns.append([rx, ry])
    if not deltas:
        return None
    dx = round(sum(d[0] for d in deltas) / len(deltas), 3)
    dy = round(sum(d[1] for d in deltas) / len(deltas), 3)
    return {
        'kind': 'translation',
        'dx_mm': dx,
        'dy_mm': dy,
        'drot_deg': 0.0,
        'anchor_connector_pairs': paired_conns,
    }


def compile_constraints(
    board_yaml_path: Path,
    pcb_path: Path,
    *,
    default_power_width_mm: float = 0.75,
    default_dut_rail_width_mm: float = 0.65,
    default_chassis_width_mm: float = 0.80,
    default_signal_width_mm: float = 0.20,
    default_clearance_mm: float = 0.22,
    default_same_net_pad_clearance_mm: float = 0.25,
) -> Dict:
    """Compile `board.yaml` + `.kicad_pcb` into structured KRT policy & constraints."""
    from kicad_parser import parse_kicad_pcb

    by = _load_yaml(board_yaml_path)
    pcb = parse_kicad_pcb(str(pcb_path))
    board_nets = sorted({n.name for n in pcb.nets.values() if n.name})

    specs = by.get('specs') or {}
    gc = specs.get('global_constraints') or {}
    dfm = gc.get('safety_and_dfm') or {}
    fab = by.get('fab') or {}

    # 1. Reference plane nets
    ref_planes = [_norm_net(n, board_nets) for n in (specs.get('reference_plane_nets') or [])]
    if not ref_planes and '/AGND' in board_nets:
        ref_planes = ['/AGND']

    # 2. Power rails + downstream protected DUT rails + ESD/Chassis nets
    power_width_map: Dict[str, float] = {}
    for rail, rcfg in (specs.get('power_rails') or {}).items():
        n = _norm_net(rail, board_nets)
        if n in board_nets:
            w = float((rcfg or {}).get('min_width_mm') or default_power_width_mm)
            power_width_map[n] = max(power_width_map.get(n, 0.0), w)

    # Detect downstream protected rails (e.g. /VCC_DUT, /VEE_DUT) & chassis nets from pin maps
    ic_root = (specs.get('interface_contract') or {}).get('boards') or {}
    b_ic = next(iter(ic_root.values()), {}) if isinstance(ic_root, dict) else {}
    chassis_nets: List[str] = []
    for _cref, cinfo in (b_ic.get('connectors') or {}).items():
        if not isinstance(cinfo, dict):
            continue
        for _pin, pname in (cinfo.get('pin_map') or {}).items():
            pn = _norm_net(str(pname), board_nets)
            if 'CHASSIS' in pn.upper() and pn in board_nets:
                if pn not in chassis_nets:
                    chassis_nets.append(pn)
                power_width_map[pn] = max(power_width_map.get(pn, 0.0), default_chassis_width_mm)

    # Downstream DUT/protected supply rails present on board
    for n in board_nets:
        up = n.upper()
        if n in ref_planes or n in power_width_map:
            continue
        if any(tok in up for tok in ('VCC_DUT', 'VEE_DUT', 'VDD_DUT', 'VSS_DUT', 'AVCC', 'AVEE')):
            power_width_map[n] = default_dut_rail_width_mm
        elif 'CHASSIS' in up:
            if n not in chassis_nets:
                chassis_nets.append(n)
            power_width_map[n] = default_chassis_width_mm

    # 3. Copper layers & routing layers (keep In1.Cu clean for primary reference plane on 4L)
    layer_count = int(fab.get('layers') or 4)
    if layer_count >= 4:
        route_layers = ['F.Cu', 'In2.Cu', 'B.Cu']
        dedicated_plane_layers = ['In1.Cu']
    else:
        route_layers = ['F.Cu', 'B.Cu']
        dedicated_plane_layers = []

    # 4. Thermal budgets -> hot power parts vs precision active ICs
    hot_refs: List[str] = []
    ic_refs: List[str] = []
    for tb in specs.get('thermal_budgets') or ():
        ref = str(tb.get('ref') or '')
        pd = float(tb.get('max_pd_w') or 0.0)
        if not ref or ref not in pcb.footprints:
            continue
        if ref.startswith(('R', 'Q', 'D')) and pd >= 0.35:
            hot_refs.append(ref)
        elif ref.startswith(('U', 'RN')):
            ic_refs.append(ref)
    # Include matched sibling 2512 power resistors if present (e.g. R16-R19 next to R12-R15)
    for ref, fp in sorted(pcb.footprints.items()):
        if ref in hot_refs or not ref.startswith('R'):
            continue
        fp_name = str(getattr(fp, 'footprint_name', '') or '')
        if '2512' in fp_name:
            hot_refs.append(ref)
    for ref in (specs.get('exposed_pad_refs') or ()):
        if ref in pcb.footprints and ref not in ic_refs:
            ic_refs.append(ref)

    # 5. Symmetry inference
    symmetry = _infer_symmetry_from_connectors(pcb, by)

    # 6. Explicit differential pairs (only from board.yaml; empty prevents regex false positives)
    explicit_diff_pairs = list(specs.get('differential_pairs') or [])

    # 7. Routable nets (all multi-pad non-reference-plane nets; skip KiCad single-pad unconnected-*)
    routable_nets = [
        n for n in board_nets
        if n not in ref_planes and not n.startswith('unconnected-')
    ]

    # 8. Build routing policy
    p_nets = sorted(power_width_map.keys())
    p_widths = [power_width_map[n] for n in p_nets]
    routing_policy = {
        'board': by.get('board') or pcb_path.stem,
        'revision': by.get('revision'),
        'profile': by.get('profile'),
        'layer_count': layer_count,
        'reference_plane_nets': ref_planes,
        'dedicated_plane_layers': dedicated_plane_layers,
        'route_layers': route_layers,
        'routable_nets': routable_nets,
        'track_width_mm': max(default_signal_width_mm, float(dfm.get('min_track_width_mm') or 0.15)),
        'clearance_mm': max(default_clearance_mm, float(dfm.get('min_clearance_mm') or 0.15)),
        'via_size_mm': 0.60,
        'via_drill_mm': 0.30,
        'same_net_pad_clearance_mm': default_same_net_pad_clearance_mm,
        'power_nets': p_nets,
        'power_nets_widths_mm': p_widths,
        'chassis_nets': chassis_nets,
        'explicit_diff_pairs': explicit_diff_pairs,
        'forbid_regex_diff_pairs': len(explicit_diff_pairs) == 0,
        'max_ripup': 8,
    }

    compiled_constraints = {
        'source_board_yaml': str(board_yaml_path),
        'decaps': {
            'max_pin_distance_mm': float(dfm.get('max_decap_loop_mm') or 2.0),
            'max_return_via_dist_mm': float(dfm.get('max_return_via_dist_mm') or 1.5),
        },
        'exposed_pads': {
            'refs': list(specs.get('exposed_pad_refs') or []),
            'min_thermal_vias': int(dfm.get('min_ep_thermal_vias') or 4),
        },
        'thermal_separation': {
            'hot_refs': sorted(hot_refs),
            'sensitive_refs': sorted(ic_refs),
            'min_hot_to_sensitive_mm': 15.0,
            'min_hot_to_hot_mm': 6.0,
        },
        'symmetry': symmetry,
        'dfm_gates': {
            'outer_zone_pad_conn': 1,  # ZONE_CONNECTION_THERMAL
            'max_non_ep_via_in_smd': 0,
            'min_perimeter_stitch_vias': 12,
            'max_drc_errors': 0,
            'max_unconnected': 0,
        },
    }

    return {
        'routing_policy': routing_policy,
        'compiled_constraints': compiled_constraints,
    }


def render_judge_chain(policy: Dict) -> str:
    """Emit a KRT `judge_seeds.py` routing chain directly from `routing_policy`."""
    net_args = ' '.join(f'"{n}"' for n in policy['routable_nets'])
    layer_args = ' '.join(policy['route_layers'])
    p_args = ''
    if policy['power_nets']:
        pn = ' '.join(f'"{n}"' for n in policy['power_nets'])
        pw = ' '.join(str(w) for w in policy['power_nets_widths_mm'])
        p_args = f'--power-nets {pn} --power-nets-widths {pw} '
    return (
        "# Auto-compiled from board.yaml via py_tools/compile_board_constraints.py\n"
        f"python3 -X utf8 py_router/route.py {{in}} {{out}} "
        f"--nets {net_args} "
        f"--layers {layer_args} "
        f"--track-width {policy['track_width_mm']} "
        f"--clearance {policy['clearance_mm']} "
        f"--via-size {policy['via_size_mm']} --via-drill {policy['via_drill_mm']} "
        f"--same-net-pad-clearance {policy['same_net_pad_clearance_mm']} "
        f"{p_args}"
        f"--max-ripup {policy['max_ripup']} --json-out {{dir}}/route.json\n"
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Compile board.yaml + .kicad_pcb into KRT constraints')
    ap.add_argument('board_yaml', help='path to GongLab board.yaml')
    ap.add_argument('pcb', help='path to input .kicad_pcb')
    ap.add_argument('--out-dir', required=True, help='output folder for compiled artifacts')
    ap.add_argument('--power-width', type=float, default=0.75)
    ap.add_argument('--dut-rail-width', type=float, default=0.65)
    ap.add_argument('--chassis-width', type=float, default=0.80)
    ap.add_argument('--same-net-pad-clearance', type=float, default=0.25)
    a = ap.parse_args(argv)

    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    res = compile_constraints(
        Path(a.board_yaml),
        Path(a.pcb),
        default_power_width_mm=a.power_width,
        default_dut_rail_width_mm=a.dut_rail_width,
        default_chassis_width_mm=a.chassis_width,
        default_same_net_pad_clearance_mm=a.same_net_pad_clearance,
    )
    (out_dir / 'routing_policy.json').write_text(
        json.dumps(res['routing_policy'], indent=2), encoding='utf-8')
    (out_dir / 'compiled_constraints.json').write_text(
        json.dumps(res['compiled_constraints'], indent=2), encoding='utf-8')
    (out_dir / 'judge_chain.txt').write_text(
        render_judge_chain(res['routing_policy']), encoding='utf-8')
    print(f"compile_board_constraints: wrote routing_policy.json, compiled_constraints.json, "
          f"judge_chain.txt -> {out_dir}")
    print(f"  power_nets: {list(zip(res['routing_policy']['power_nets'], res['routing_policy']['power_nets_widths_mm']))}")
    print(f"  same_net_pad_clearance: {res['routing_policy']['same_net_pad_clearance_mm']} mm, "
          f"hot_refs: {res['compiled_constraints']['thermal_separation']['hot_refs']}, "
          f"symmetry: {res['compiled_constraints']['symmetry']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
