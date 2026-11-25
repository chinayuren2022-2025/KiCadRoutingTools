#!/usr/bin/env python3
"""Unit tests for AI4PCB Route-B constraint compiler and harness helpers."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _sub in ('py_router', 'py_placer', 'py_tools'):
    _p = str(ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

from compile_board_constraints import compile_constraints, render_judge_chain  # noqa: E402


def _make_minimal_pcb(path: Path) -> None:
    """Minimal KiCad PCB with X/Y paired connectors, power rails, /CHASSIS, and 2512 resistors."""
    path.write_text("""(kicad_pcb (version 20240108) (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" power)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
  (net 1 "/AGND")
  (net 2 "/+15V")
  (net 3 "/-15V")
  (net 4 "/VCC_DUT")
  (net 5 "/VEE_DUT")
  (net 6 "/CHASSIS")
  (net 7 "/V_GP")
  (net 8 "/V_GN")
  (net 9 "/X_IN")
  (net 10 "/Y_IN")
  (gr_rect (start 0 0) (end 100 80) (layer "Edge.Cuts"))
  (footprint "Connector:SMA" (layer "F.Cu") (at 10 20) (locked yes)
    (property "Reference" "J1") (property "Value" "SMA")
    (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8) (layers *.Cu *.Mask) (net 9 "/X_IN"))
    (pad "2" thru_hole circle (at 2 0) (size 1.5 1.5) (drill 0.8) (layers *.Cu *.Mask) (net 6 "/CHASSIS"))
  )
  (footprint "Connector:SMA" (layer "F.Cu") (at 10 60) (locked yes)
    (property "Reference" "J3") (property "Value" "SMA")
    (pad "1" thru_hole circle (at 0 0) (size 1.5 1.5) (drill 0.8) (layers *.Cu *.Mask) (net 10 "/Y_IN"))
    (pad "2" thru_hole circle (at 2 0) (size 1.5 1.5) (drill 0.8) (layers *.Cu *.Mask) (net 6 "/CHASSIS"))
  )
  (footprint "Package_SO:MSOP-8-1EP" (layer "F.Cu") (at 40 20)
    (property "Reference" "U1") (property "Value" "AMP")
    (pad "1" smd rect (at -1 0) (size 0.5 0.5) (layers F.Cu) (net 2 "/+15V"))
    (pad "2" smd rect (at 1 0) (size 0.5 0.5) (layers F.Cu) (net 1 "/AGND"))
  )
  (footprint "Resistor_SMD:R_2512_6332Metric" (layer "F.Cu") (at 70 20)
    (property "Reference" "R12") (property "Value" "100R")
    (pad "1" smd rect (at -2 0) (size 1 2) (layers F.Cu) (net 4 "/VCC_DUT"))
    (pad "2" smd rect (at 2 0) (size 1 2) (layers F.Cu) (net 7 "/V_GP"))
  )
  (footprint "Resistor_SMD:R_2512_6332Metric" (layer "F.Cu") (at 70 60)
    (property "Reference" "R16") (property "Value" "100R")
    (pad "1" smd rect (at -2 0) (size 1 2) (layers F.Cu) (net 5 "/VEE_DUT"))
    (pad "2" smd rect (at 2 0) (size 1 2) (layers F.Cu) (net 8 "/V_GN"))
  )
)
""", encoding='utf-8')


def _make_minimal_yaml(path: Path) -> None:
    path.write_text("""board: TEST_ANALOG
revision: r1
profile: analog_precision
fab:
  layers: 4
specs:
  reference_plane_nets:
    - /AGND
  power_rails:
    /+15V:
      min_width_mm: 0.8
    /-15V:
      min_width_mm: 0.8
  exposed_pad_refs:
    - U1
  thermal_budgets:
    - ref: R12
      max_pd_w: 0.75
    - ref: U1
      max_pd_w: 0.45
  interface_contract:
    boards:
      TEST_ANALOG:
        connectors:
          J1:
            signal_net: X_IN
            pin_map:
              "2": CHASSIS
          J3:
            signal_net: Y_IN
            pin_map:
              "2": CHASSIS
  global_constraints:
    safety_and_dfm:
      min_track_width_mm: 0.15
      min_clearance_mm: 0.15
      max_decap_loop_mm: 2.0
      max_return_via_dist_mm: 1.5
      min_ep_thermal_vias: 4
    board_specific_analog_constraints:
      xy_channel_geometric_symmetry_required: true
""", encoding='utf-8')


def test_compile_board_constraints():
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        pcb_p = td_p / 'test.kicad_pcb'
        yaml_p = td_p / 'board.yaml'
        _make_minimal_pcb(pcb_p)
        _make_minimal_yaml(yaml_p)

        res = compile_constraints(yaml_p, pcb_p, default_same_net_pad_clearance_mm=0.30)
        rp = res['routing_policy']
        cc = res['compiled_constraints']

        assert rp['layer_count'] == 4
        assert rp['route_layers'] == ['F.Cu', 'In2.Cu', 'B.Cu']
        assert rp['same_net_pad_clearance_mm'] == 0.30
        assert rp['forbid_regex_diff_pairs'] is True
        assert '/AGND' not in rp['routable_nets']
        p_map = dict(zip(rp['power_nets'], rp['power_nets_widths_mm']))
        assert p_map['/+15V'] == 0.8
        assert p_map['/-15V'] == 0.8
        assert p_map['/CHASSIS'] == 0.8
        assert p_map['/VCC_DUT'] == 0.65
        assert p_map['/VEE_DUT'] == 0.65

        sym = cc['symmetry']
        assert sym is not None
        assert sym['dx_mm'] == 0.0 and sym['dy_mm'] == 40.0
        assert sym['anchor_connector_pairs'] == [['J1', 'J3']]

        therm = cc['thermal_separation']
        assert 'R12' in therm['hot_refs'] and 'R16' in therm['hot_refs']
        assert 'U1' in therm['sensitive_refs']

        chain = render_judge_chain(rp)
        assert '--same-net-pad-clearance 0.3' in chain
        assert '"/CHASSIS"' in chain
        assert '"/AGND"' not in chain
        print('PASS: test_compile_board_constraints')


if __name__ == '__main__':
    test_compile_board_constraints()
