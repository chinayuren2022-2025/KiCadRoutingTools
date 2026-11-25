#!/usr/bin/env python3
"""Deterministic analog/mixed-signal PCB finishing & DFM hard-gate tool.

Replaces fragile ad-hoc LLM `pcbnew` post-processing scripts with a single,
repeatable, audited pipeline step for KiCad 10 boards:

  1. Enforces outer-layer thermal relief (`ZONE_CONNECTION_THERMAL`, pad_conn=1)
     on F.Cu / B.Cu copper pours so SMD passive pads never get solid-flooded
     (prevents tombstoning / cold joints), while keeping inner planes direct.
  2. Places deterministic exposed-pad (EP) thermal via arrays (e.g., 2x2 grid
     inside U1/U2/RN1/RN2 pad 9) if not already populated.
  3. Places low-inductance SMD ground escape vias with BOTH foreign-copper AND
     same-net SMD pad edge clearance (eliminating via-in-paste / pad-grazing),
     optionally adding a short explicit copper stub from SMD pad to via.
  4. Places perimeter ground-stitching vias along the board outline for EMI
     shielding and return-plane stitching.
  5. Snaps off-center track endpoints cleanly onto same-net via centers
     (eliminating KRT `track_not_centered_on_via` warnings) without violating
     foreign clearances.
  6. Runs native `kicad-cli pcb drc --refill-zones --save-board` and emits a
     machine-checkable DFM & analog audit JSON.

Run with KiCad 10 bundled Python (for `pcbnew`), e.g.:
    "C:/Program Files/KiCad/10.0/bin/python.exe" py_tools/analog_finish.py \\
        input.kicad_pcb output.kicad_pcb --ground-net /AGND \\
        --ep-refs U1,U2,RN1,RN2 --ep-vias 4 --perimeter-pitch 15.0 \\
        --drc-json out_drc.json --audit-json out_audit.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import pcbnew as p


def _mm(nm: float) -> float:
    return float(nm) / 1e6


def _pt(x_mm: float, y_mm: float) -> p.VECTOR2I:
    return p.VECTOR2I(p.FromMM(x_mm), p.FromMM(y_mm))


def _dist_mm(a: p.VECTOR2I, b: p.VECTOR2I) -> float:
    return math.hypot(_mm(a.x - b.x), _mm(a.y - b.y))


def _find_kicad_cli(explicit: Optional[str] = None) -> str:
    if explicit and os.path.isfile(explicit):
        return explicit
    for cand in (
        shutil.which('kicad-cli'),
        r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe',
        r'C:\Program Files\KiCad\9.0\bin\kicad-cli.exe',
        '/usr/bin/kicad-cli',
    ):
        if cand and os.path.isfile(cand):
            return cand
    raise FileNotFoundError('kicad-cli not found; pass --kicad-cli PATH')


def _pad_edge_dist_mm(pad: p.PAD, pos: p.VECTOR2I, via_radius_mm: float) -> float:
    """Conservative edge-to-edge distance (mm) from a via circle to a pad bbox."""
    bb = pad.GetBoundingBox()
    dx = max(0, bb.GetLeft() - pos.x, pos.x - bb.GetRight())
    dy = max(0, bb.GetTop() - pos.y, pos.y - bb.GetBottom())
    return _mm(math.hypot(dx, dy)) - via_radius_mm


def _is_ep_pad(pad: p.PAD, ep_refs: set) -> bool:
    fp = pad.GetParentFootprint()
    if fp is None or fp.GetReference() not in ep_refs:
        return False
    num = str(pad.GetNumber() or '')
    if num == '9':
        return True
    # KiCad footprints (e.g., MSOP-8-1EP) carry unnamed paste/copper sub-pads inside EP pad 9
    if num == '' and pad.GetAttribute() == p.PAD_ATTRIB_SMD:
        ep9 = fp.FindPadByNumber('9')
        if ep9 is not None and ep9.GetBoundingBox().Inflate(p.FromMM(0.1)).Contains(pad.GetPosition()):
            return True
        sz = pad.GetSize()
        return _mm(sz.x) >= 1.2 and _mm(sz.y) >= 1.2
    return False


def _point_in_rule_keepout(board: p.BOARD, pos: p.VECTOR2I, via_radius_mm: float) -> bool:
    """True if placing a via at `pos` violates any rule-area via keepout."""
    margin = p.FromMM(via_radius_mm + 0.15)
    for z in board.Zones():
        if not z.GetIsRuleArea():
            continue
        if not z.GetDoNotAllowVias():
            continue
        if z.GetBoundingBox().Inflate(margin).Contains(pos):
            if z.HitTest(pos, margin):
                return True
    return False


def _via_clear(
    board: p.BOARD,
    pos: p.VECTOR2I,
    net_code: int,
    via_size_mm: float,
    foreign_clearance_mm: float,
    same_net_smd_clearance_mm: float,
    min_via_c2c_mm: float,
    edge_margin_mm: float,
    board_bbox: p.BOX2I,
    ep_refs: set,
    *,
    allow_inside_ep: bool = False,
    foreign_items: Optional[List] = None,
    same_net_smd_pads: Optional[List[p.PAD]] = None,
    existing_vias: Optional[List[p.PCB_VIA]] = None,
) -> bool:
    """Check foreign copper, same-net non-EP SMD pads, via spacing, board edge & keepouts."""
    vr = via_size_mm / 2.0
    # 1. Board edge clearance
    if (pos.x < board_bbox.GetLeft() + p.FromMM(edge_margin_mm)
            or pos.x > board_bbox.GetRight() - p.FromMM(edge_margin_mm)
            or pos.y < board_bbox.GetTop() + p.FromMM(edge_margin_mm)
            or pos.y > board_bbox.GetBottom() - p.FromMM(edge_margin_mm)):
        return False

    # 2. Rule-area via keepouts
    if _point_in_rule_keepout(board, pos, vr):
        return False

    # 3. Existing via spacing
    vias = existing_vias if existing_vias is not None else [
        t for t in board.GetTracks() if isinstance(t, p.PCB_VIA)
    ]
    min_v_nm = p.FromMM(min_via_c2c_mm)
    for v in vias:
        if (v.GetPosition() - pos).EuclideanNorm() < min_v_nm:
            return False

    # 4. Foreign copper & all through-hole drills (all layers!)
    circle = p.SHAPE_CIRCLE(pos, p.FromMM(vr))
    clear_nm = p.FromMM(foreign_clearance_mm)
    inflate_nm = p.FromMM(vr + foreign_clearance_mm + 0.05)
    f_items = foreign_items if foreign_items is not None else (
        [t for t in board.GetTracks() if t.GetNetCode() != net_code]
        + [pad for pad in board.GetPads() if pad.GetNetCode() != net_code]
    )
    for item in f_items:
        if not item.GetBoundingBox().Inflate(inflate_nm).Contains(pos):
            continue
        if isinstance(item, p.PAD):
            # Check pad effective shape on F_Cu and B_Cu (THT pads exist on all copper layers)
            for lyr in (p.F_Cu, p.In1_Cu, p.In2_Cu, p.B_Cu):
                if item.IsOnLayer(lyr):
                    if item.GetEffectiveShape(lyr).Collide(circle, clear_nm):
                        return False
        elif isinstance(item, p.PCB_VIA):
            if item.GetEffectiveShape(p.F_Cu).Collide(circle, clear_nm):
                return False
        else:
            if item.GetEffectiveShape(item.GetLayer()).Collide(circle, clear_nm):
                return False

    # 5. Same-net non-EP SMD pads (prevent via-in-paste / solder wicking!)
    if not allow_inside_ep and same_net_smd_clearance_mm > 0:
        s_pads = same_net_smd_pads if same_net_smd_pads is not None else [
            pad for pad in board.GetPads()
            if pad.GetNetCode() == net_code
            and pad.GetAttribute() == p.PAD_ATTRIB_SMD
            and not _is_ep_pad(pad, ep_refs)
        ]
        for sp in s_pads:
            if _pad_edge_dist_mm(sp, pos, vr) < same_net_smd_clearance_mm:
                return False

    # 6. Same-net THT/NPTH holes (never drill a via into a mounting hole or LEMO pin!)
    hole_margin_mm = vr + 0.30
    for pad in board.GetPads():
        if pad.GetAttribute() in (p.PAD_ATTRIB_PTH, p.PAD_ATTRIB_NPTH):
            drill_r = _mm(max(pad.GetDrillSize().x, pad.GetDrillSize().y)) / 2.0
            pad_r = max(drill_r, _mm(max(pad.GetSize().x, pad.GetSize().y)) / 2.0)
            if _dist_mm(pad.GetPosition(), pos) < pad_r + hole_margin_mm:
                return False

    return True


def _add_via(board: p.BOARD, pos: p.VECTOR2I, net_code: int, via_size_mm: float,
             via_drill_mm: float) -> p.PCB_VIA:
    v = p.PCB_VIA(board)
    v.SetPosition(pos)
    v.SetWidth(p.F_Cu, p.FromMM(via_size_mm))
    v.SetDrill(p.FromMM(via_drill_mm))
    v.SetNetCode(net_code)
    board.Add(v)
    return v


def _track_clear_of_foreign(
    start: p.VECTOR2I,
    end: p.VECTOR2I,
    layer: int,
    width_mm: float,
    net_code: int,
    clearance_mm: float,
    foreign_items: List,
) -> bool:
    seg = p.SHAPE_SEGMENT(start, end, p.FromMM(width_mm))
    clear_nm = p.FromMM(clearance_mm)
    xmin = min(start.x, end.x) - p.FromMM(width_mm / 2.0 + clearance_mm + 0.05)
    xmax = max(start.x, end.x) + p.FromMM(width_mm / 2.0 + clearance_mm + 0.05)
    ymin = min(start.y, end.y) - p.FromMM(width_mm / 2.0 + clearance_mm + 0.05)
    ymax = max(start.y, end.y) + p.FromMM(width_mm / 2.0 + clearance_mm + 0.05)
    seg_bb = p.BOX2I(p.VECTOR2I(xmin, ymin), p.VECTOR2I(xmax - xmin, ymax - ymin))
    for item in foreign_items:
        if not item.GetBoundingBox().Intersects(seg_bb):
            continue
        if isinstance(item, p.PAD):
            if item.IsOnLayer(layer) and item.GetEffectiveShape(layer).Collide(seg, clear_nm):
                return False
        elif isinstance(item, p.PCB_VIA):
            if item.GetEffectiveShape(layer).Collide(seg, clear_nm):
                return False
        else:
            if item.GetLayer() == layer and item.GetEffectiveShape(layer).Collide(seg, clear_nm):
                return False
    return True


def enforce_zone_thermal_relief(board: p.BOARD, ground_net_code: int) -> List[Dict]:
    """Keep outer copper pours (`F.Cu`, `B.Cu`) on standard THERMAL (pad_conn=1)."""
    changes = []
    outer_layers = {p.F_Cu, p.B_Cu}
    for z in board.Zones():
        if z.GetIsRuleArea():
            continue
        if z.GetNetCode() != ground_net_code:
            continue
        lyr = z.GetLayer()
        old = int(z.GetPadConnection())
        if lyr in outer_layers:
            if old != int(p.ZONE_CONNECTION_THERMAL):
                z.SetPadConnection(p.ZONE_CONNECTION_THERMAL)
                changes.append({
                    'layer': board.GetLayerName(lyr),
                    'old_pad_conn': old,
                    'new_pad_conn': int(p.ZONE_CONNECTION_THERMAL),
                })
            # Remove floating unreferenced outer copper islands (prevents Zone<->Zone DRC opens & EMI stubs)
            if hasattr(p, 'ISLAND_REMOVAL_MODE_ALWAYS') and hasattr(z, 'SetIslandRemovalMode'):
                z.SetIslandRemovalMode(p.ISLAND_REMOVAL_MODE_ALWAYS)
    return changes


def place_ep_thermal_vias(
    board: p.BOARD,
    ground_net_code: int,
    ep_refs: Sequence[str],
    target_vias: int,
    via_size_mm: float,
    via_drill_mm: float,
    foreign_clearance_mm: float,
    board_bbox: p.BOX2I,
    foreign_items: List,
    existing_vias: List[p.PCB_VIA],
) -> Dict[str, int]:
    """Ensure each exposed-pad IC has at least `target_vias` /AGND vias inside its EP."""
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    ep_set = set(ep_refs)
    added: Dict[str, int] = {}
    for ref in ep_refs:
        fp = fps.get(ref)
        if fp is None:
            continue
        ep_pad = fp.FindPadByNumber('9')
        if ep_pad is None:
            # fallback to largest ground SMD pad
            g_smds = [pad for pad in fp.Pads()
                      if pad.GetNetCode() == ground_net_code
                      and pad.GetAttribute() == p.PAD_ATTRIB_SMD]
            ep_pad = max(g_smds, key=lambda q: q.GetSize().x * q.GetSize().y, default=None)
        if ep_pad is None:
            continue
        # Exposed thermal pads must connect solidly to ground pour (never starved 1-spoke thermal!)
        if hasattr(ep_pad, 'SetLocalZoneConnection'):
            ep_pad.SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
        bb = ep_pad.GetBoundingBox()
        inside = [v for v in existing_vias
                  if v.GetNetCode() == ground_net_code and bb.Contains(v.GetPosition())]
        need = target_vias - len(inside)
        cnt = 0
        if need > 0:
            sz_x = _mm(ep_pad.GetSize().x)
            sz_y = _mm(ep_pad.GetSize().y)
            # Symmetric 2x2 grid fitting safely inside pad copper
            dx = min(0.45, max(0.35, sz_x * 0.24))
            dy = min(0.45, max(0.35, sz_y * 0.24))
            offsets = [(-dx, -dy), (dx, -dy), (-dx, dy), (dx, dy)]
            center = ep_pad.GetPosition()
            for ox, oy in offsets:
                if len(inside) + cnt >= target_vias:
                    break
                loc = center + _pt(ox, oy)
                if _via_clear(
                    board, loc, ground_net_code, via_size_mm, foreign_clearance_mm,
                    0.0, via_size_mm + 0.15, 0.5, board_bbox, ep_set,
                    allow_inside_ep=True, foreign_items=foreign_items,
                    existing_vias=existing_vias,
                ):
                    v = _add_via(board, loc, ground_net_code, via_size_mm, via_drill_mm)
                    existing_vias.append(v)
                    cnt += 1
        added[ref] = cnt
    return added


def place_smd_ground_escape_vias(
    board: p.BOARD,
    ground_net_code: int,
    ep_refs: set,
    max_return_dist_mm: float,
    via_size_mm: float,
    via_drill_mm: float,
    foreign_clearance_mm: float,
    same_net_smd_clearance_mm: float,
    board_bbox: p.BOX2I,
    foreign_items: List,
    same_net_smd_pads: List[p.PAD],
    existing_vias: List[p.PCB_VIA],
    *,
    add_stubs: bool = True,
    stub_width_mm: float = 0.25,
) -> Dict[str, int]:
    """Place safe escape vias (plus optional short copper stubs) for SMD ground pads."""
    vias_added = 0
    stubs_added = 0
    skipped = 0
    vr = via_size_mm / 2.0
    min_v_c2c = max(via_size_mm + 0.25, 0.85)

    # Prioritize decap & IC-adjacent ground pads first so they get the cleanest spots
    def pad_priority(pad: p.PAD) -> Tuple[int, str, str]:
        ref = pad.GetParentFootprint().GetReference() if pad.GetParentFootprint() else ''
        prio = 0 if ref.startswith('C') else (1 if ref.startswith(('U', 'RN', 'Q')) else 2)
        return (prio, ref, str(pad.GetNumber()))

    targets = sorted(same_net_smd_pads, key=pad_priority)
    for pad in targets:
        if not pad.IsOnLayer(p.F_Cu) and not pad.IsOnLayer(p.B_Cu):
            continue
        pad_layer = p.F_Cu if pad.IsOnLayer(p.F_Cu) else p.B_Cu
        pos = pad.GetPosition()
        g_vias = [v for v in existing_vias if v.GetNetCode() == ground_net_code]
        if any(_dist_mm(v.GetPosition(), pos) <= max_return_dist_mm for v in g_vias):
            continue

        # Radial search starting outside pad edge + same_net_smd_clearance_mm
        half_span = max(_mm(pad.GetSize().x), _mm(pad.GetSize().y)) / 2.0
        min_r = half_span + vr + same_net_smd_clearance_mm + 0.05
        radii = [
            round(r, 3) for r in (
                min_r,
                min_r + 0.15,
                min_r + 0.30,
                max_return_dist_mm - 0.05,
            ) if r <= max_return_dist_mm + 0.02
        ]
        if not radii:
            radii = [max_return_dist_mm]
        radii = sorted(set(radii))

        placed = False
        for rad in radii:
            for deg in range(0, 360, 15):
                ang = math.radians(deg)
                loc = pos + _pt(rad * math.cos(ang), rad * math.sin(ang))
                if not _via_clear(
                    board, loc, ground_net_code, via_size_mm, foreign_clearance_mm,
                    same_net_smd_clearance_mm, min_v_c2c, 0.8, board_bbox, ep_refs,
                    allow_inside_ep=False, foreign_items=foreign_items,
                    same_net_smd_pads=same_net_smd_pads, existing_vias=existing_vias,
                ):
                    continue
                v = _add_via(board, loc, ground_net_code, via_size_mm, via_drill_mm)
                existing_vias.append(v)
                vias_added += 1
                if add_stubs and _track_clear_of_foreign(
                    pos, loc, pad_layer, stub_width_mm, ground_net_code,
                    foreign_clearance_mm, foreign_items,
                ):
                    tr = p.PCB_TRACK(board)
                    tr.SetLayer(pad_layer)
                    tr.SetNetCode(ground_net_code)
                    tr.SetStart(pos)
                    tr.SetEnd(loc)
                    tr.SetWidth(p.FromMM(stub_width_mm))
                    board.Add(tr)
                    stubs_added += 1
                placed = True
                break
            if placed:
                break
        if not placed:
            skipped += 1
    return {'vias_added': vias_added, 'stubs_added': stubs_added, 'skipped_pads': skipped}


def place_perimeter_stitching_vias(
    board: p.BOARD,
    ground_net_code: int,
    ep_refs: set,
    pitch_mm: float,
    inset_mm: float,
    via_size_mm: float,
    via_drill_mm: float,
    foreign_clearance_mm: float,
    same_net_smd_clearance_mm: float,
    board_bbox: p.BOX2I,
    foreign_items: List,
    same_net_smd_pads: List[p.PAD],
    existing_vias: List[p.PCB_VIA],
) -> int:
    """Place perimeter ground-stitching vias around all four board edges.

    Skips automatically when fewer than 2 distinct copper layers carry a ground
    pour (e.g. a 2-layer board with only a B.Cu ground pour), because through-
    hole stitching vias connecting only 1 ground layer trigger KiCad
    `via_dangling` warnings without stitching anything.
    """
    if pitch_mm <= 0:
        return 0
    g_zone_layers = set()
    for z in board.Zones():
        if z.GetIsRuleArea() or z.GetNetCode() != ground_net_code:
            continue
        ls = z.GetLayerSet()
        for lyr in (p.F_Cu, p.In1_Cu, p.In2_Cu, p.In3_Cu, p.In4_Cu, p.B_Cu):
            if ls.Contains(lyr):
                g_zone_layers.add(lyr)
    if len(g_zone_layers) < 2:
        return 0
    x0, y0 = _mm(board_bbox.GetLeft()) + inset_mm, _mm(board_bbox.GetTop()) + inset_mm
    x1, y1 = _mm(board_bbox.GetRight()) - inset_mm, _mm(board_bbox.GetBottom()) - inset_mm
    if x1 <= x0 or y1 <= y0:
        return 0

    candidates: List[Tuple[float, float]] = []
    # Top and bottom edges
    nx = max(2, int(round((x1 - x0) / pitch_mm)))
    for i in range(nx + 1):
        x = x0 + (x1 - x0) * i / nx
        candidates.append((x, y0))
        candidates.append((x, y1))
    # Left and right edges (excluding corners already added)
    ny = max(2, int(round((y1 - y0) / pitch_mm)))
    for j in range(1, ny):
        y = y0 + (y1 - y0) * j / ny
        candidates.append((x0, y))
        candidates.append((x1, y))

    added = 0
    min_v_c2c = max(via_size_mm + 1.0, 2.0)
    for cx, cy in candidates:
        # Try nominal point and small nudges inward/along edge if blocked by a connector
        nudges = [(0.0, 0.0), (0.0, 1.0), (0.0, -1.0), (1.0, 0.0), (-1.0, 0.0)]
        for dx, dy in nudges:
            loc = _pt(cx + dx, cy + dy)
            if _via_clear(
                board, loc, ground_net_code, via_size_mm, foreign_clearance_mm + 0.05,
                same_net_smd_clearance_mm, min_v_c2c, 1.2, board_bbox, ep_refs,
                allow_inside_ep=False, foreign_items=foreign_items,
                same_net_smd_pads=same_net_smd_pads, existing_vias=existing_vias,
            ):
                v = _add_via(board, loc, ground_net_code, via_size_mm, via_drill_mm)
                existing_vias.append(v)
                added += 1
                break
    return added


def center_tracks_on_vias(
    board: p.BOARD,
    snap_radius_mm: float = 0.25,
) -> int:
    """Snap track endpoints lying off-center on a same-net via onto its exact center."""
    tracks = list(board.GetTracks())
    vias = [t for t in tracks if isinstance(t, p.PCB_VIA)]
    segs = [t for t in tracks if not isinstance(t, p.PCB_VIA)]
    vias_by_net: Dict[int, List[p.PCB_VIA]] = defaultdict(list)
    for v in vias:
        vias_by_net[v.GetNetCode()].append(v)

    snapped = 0
    snap_nm = p.FromMM(snap_radius_mm)
    for seg in segs:
        net_vias = vias_by_net.get(seg.GetNetCode())
        if not net_vias:
            continue
        changed = False
        for getter, setter in ((seg.GetStart, seg.SetStart), (seg.GetEnd, seg.SetEnd)):
            pt = getter()
            best_v = None
            best_d = snap_nm
            for v in net_vias:
                vp = v.GetPosition()
                d = (vp - pt).EuclideanNorm()
                if 0 < d <= best_d:
                    # Only snap if endpoint actually lies inside via pad copper
                    vr_nm = v.GetWidth(p.F_Cu) / 2.0
                    if d <= vr_nm:
                        best_d = d
                        best_v = vp
            if best_v is not None:
                setter(best_v)
                changed = True
                snapped += 1
        if changed and seg.GetStart() == seg.GetEnd():
            board.Remove(seg)
    return snapped


def audit_board(
    board_path: str,
    ground_net: str,
    ep_refs: Sequence[str],
    power_nets: Sequence[str],
    drc_report: Optional[Dict] = None,
) -> Dict:
    """Produce a comprehensive DFM + analog metrics audit dictionary."""
    b = p.LoadBoard(board_path)
    fps = {f.GetReference(): f for f in b.GetFootprints()}
    tracks = list(b.GetTracks())
    segs = [t for t in tracks if not isinstance(t, p.PCB_VIA)]
    vias = [t for t in tracks if isinstance(t, p.PCB_VIA)]
    g_vias = [v for v in vias if v.GetNetname() == ground_net]
    ep_set = set(ep_refs)

    layer_len = defaultdict(float)
    net_widths = defaultdict(list)
    for s in segs:
        layer_len[b.GetLayerName(s.GetLayer())] += _mm(s.GetLength())
        net_widths[s.GetNetname()].append((round(_mm(s.GetWidth()), 3), _mm(s.GetLength())))

    # Zone pad connections & In1 continuity
    zones = []
    outer_bad_thermal = []
    for z in b.Zones():
        if z.GetIsRuleArea():
            continue
        ln = b.GetLayerName(z.GetLayer())
        pc = int(z.GetPadConnection())
        filled = z.GetFilledPolysList(z.GetLayer())
        n_polys = filled.OutlineCount() if filled else 0
        area = sum(filled.Outline(i).Area() for i in range(n_polys)) / 1e12 if n_polys else 0.0
        zones.append({
            'layer': ln, 'net': z.GetNetname(), 'pad_conn': pc,
            'filled_polys': n_polys, 'filled_area_mm2': round(area, 1),
        })
        if ln in ('F.Cu', 'B.Cu') and z.GetNetname() == ground_net and pc != int(p.ZONE_CONNECTION_THERMAL):
            outer_bad_thermal.append((ln, pc))

    # Non-EP SMD via-in-pad & grazing <= 0.30 mm
    non_ep_smd = [
        pad for pad in b.GetPads()
        if pad.GetAttribute() == p.PAD_ATTRIB_SMD and not _is_ep_pad(pad, ep_set)
    ]
    via_in_smd = 0
    via_grazing_0p30 = 0
    via_grazing_0p20 = 0
    grazing_samples = []
    for v in vias:
        vp = v.GetPosition()
        vr = _mm(v.GetWidth(p.F_Cu)) / 2.0
        best_ed = 999.0
        best_info = None
        for sp in non_ep_smd:
            ed = _pad_edge_dist_mm(sp, vp, vr)
            if ed < best_ed:
                best_ed = ed
                best_info = (round(ed, 3), sp.GetParentFootprint().GetReference(),
                             str(sp.GetNumber()), sp.GetNetname(), v.GetNetname())
        if best_ed <= 0.0:
            via_in_smd += 1
        if best_ed <= 0.20:
            via_grazing_0p20 += 1
        if best_ed <= 0.30:
            via_grazing_0p30 += 1
            if len(grazing_samples) < 12:
                grazing_samples.append(best_info)

    # EP vias
    ep_counts = {}
    for ref in ep_refs:
        if ref not in fps:
            continue
        ep_pad = fps[ref].FindPadByNumber('9')
        if ep_pad is None:
            continue
        bb = ep_pad.GetBoundingBox()
        ep_counts[ref] = sum(1 for v in g_vias if bb.Contains(v.GetPosition()))

    # Perimeter stitching vias (within 3.5 mm of board edge)
    bb = b.GetBoardEdgesBoundingBox()
    x0, y0, x1, y1 = _mm(bb.GetLeft()), _mm(bb.GetTop()), _mm(bb.GetRight()), _mm(bb.GetBottom())
    perim_vias = sum(
        1 for v in g_vias
        if min(_mm(v.GetPosition().x) - x0, x1 - _mm(v.GetPosition().x),
               _mm(v.GetPosition().y) - y0, y1 - _mm(v.GetPosition().y)) <= 3.5
    )

    # Power net width breakdown
    pwr_stats = {}
    for kn in power_nets:
        wl = net_widths.get(kn, [])
        if not wl:
            pwr_stats[kn] = {'len_mm': 0.0, 'min_w': None, 'max_w': None}
        else:
            by_w = defaultdict(float)
            for w, l in wl:
                by_w[w] += l
            pwr_stats[kn] = {
                'len_mm': round(sum(l for _, l in wl), 2),
                'min_w': min(w for w, _ in wl),
                'max_w': max(w for w, _ in wl),
                'by_width_mm': {str(k): round(v, 2) for k, v in sorted(by_w.items())},
            }

    # Decap ground return distance (all 100n caps with ground pad)
    decap_return_mm = {}
    for ref, fp in sorted(fps.items()):
        if not ref.startswith('C'):
            continue
        g_pads = [pad for pad in fp.Pads() if pad.GetNetname() == ground_net]
        if not g_pads or not g_vias:
            continue
        d_min = min(_dist_mm(g_pads[0].GetPosition(), v.GetPosition()) for v in g_vias)
        decap_return_mm[ref] = round(d_min, 3)

    drc_summary = {}
    if drc_report is not None:
        viols = drc_report.get('violations') or []
        drc_summary = {
            'errors': sum(1 for v in viols if v.get('severity') == 'error'),
            'warnings': sum(1 for v in viols if v.get('severity') == 'warning'),
            'unconnected': len(drc_report.get('unconnected_items') or []),
            'by_type': dict(Counter((v.get('severity'), v.get('type')) for v in viols)),
        }
        # JSON-safe keys
        drc_summary['by_type'] = {f"{k[0]}:{k[1]}": cnt for k, cnt in drc_summary['by_type'].items()}

    return {
        'board': board_path,
        'total_copper_mm': round(sum(layer_len.values()), 2),
        'layer_len_mm': {k: round(v, 2) for k, v in sorted(layer_len.items())},
        'segments': len(segs),
        'vias': len(vias),
        'ground_vias': len(g_vias),
        'perimeter_stitch_vias': perim_vias,
        'ep_via_counts': ep_counts,
        'zones': zones,
        'outer_bad_thermal_zones': outer_bad_thermal,
        'non_ep_via_in_smd': via_in_smd,
        'non_ep_via_grazing_0p20mm': via_grazing_0p20,
        'non_ep_via_grazing_0p30mm': via_grazing_0p30,
        'grazing_samples': grazing_samples,
        'power_net_stats': pwr_stats,
        'decap_return_via_mm': decap_return_mm,
        'max_decap_return_mm': max(decap_return_mm.values()) if decap_return_mm else None,
        'drc': drc_summary,
    }


def _add_track_stub(board: p.BOARD, start: p.VECTOR2I, end: p.VECTOR2I,
                    layer: int, net_code: int, width_mm: float) -> p.PCB_TRACK:
    tr = p.PCB_TRACK(board)
    tr.SetLayer(layer)
    tr.SetNetCode(net_code)
    tr.SetStart(start)
    tr.SetEnd(end)
    tr.SetWidth(p.FromMM(width_mm))
    board.Add(tr)
    return tr


def _connect_pad_with_safe_via_or_stub(
    b: p.BOARD,
    pad: p.PAD,
    g_code: int,
    ep_set: set,
    via_size_mm: float,
    via_drill_mm: float,
    foreign_clearance_mm: float,
    same_net_smd_clearance_mm: float,
    board_bbox: p.BOX2I,
    foreign_items: List,
    same_net_smd_pads: List[p.PAD],
    existing_vias: List[p.PCB_VIA],
    stub_width_mm: float = 0.25,
) -> bool:
    """Connect an isolated/starved ground SMD pad via direct or L-bend stub to
    an existing nearby ground via, or by placing a new safe escape via + stub."""
    pad_layer = p.F_Cu if pad.IsOnLayer(p.F_Cu) else p.B_Cu
    pos = pad.GetPosition()

    def _try_route_stub(target: p.VECTOR2I, w_mm: float) -> bool:
        if _track_clear_of_foreign(pos, target, pad_layer, w_mm, g_code,
                                   foreign_clearance_mm, foreign_items):
            _add_track_stub(b, pos, target, pad_layer, g_code, w_mm)
            return True
        # Try 2-segment Manhattan / 45-deg elbow stubs around adjacent obstacles
        dx_nm, dy_nm = target.x - pos.x, target.y - pos.y
        elbows = [
            p.VECTOR2I(target.x, pos.y),
            p.VECTOR2I(pos.x, target.y),
            p.VECTOR2I(pos.x + dx_nm // 2, pos.y),
            p.VECTOR2I(pos.x, pos.y + dy_nm // 2),
        ]
        for mid in elbows:
            if mid == pos or mid == target:
                continue
            if (_track_clear_of_foreign(pos, mid, pad_layer, w_mm, g_code,
                                        foreign_clearance_mm, foreign_items)
                    and _track_clear_of_foreign(mid, target, pad_layer, w_mm, g_code,
                                                foreign_clearance_mm, foreign_items)):
                _add_track_stub(b, pos, mid, pad_layer, g_code, w_mm)
                _add_track_stub(b, mid, target, pad_layer, g_code, w_mm)
                return True
        return False

    # 1. Try direct or L-bend stub to an existing nearby ground via first
    g_vias = sorted(
        (v for v in existing_vias if v.GetNetCode() == g_code),
        key=lambda v: _dist_mm(v.GetPosition(), pos),
    )
    for w_try in (stub_width_mm, 0.20):
        for v in g_vias[:14]:
            if _dist_mm(v.GetPosition(), pos) > 4.0:
                break
            if _try_route_stub(v.GetPosition(), w_try):
                return True

    # 2. Otherwise search radially for a new safe escape via + direct/L-bend stub
    vr = via_size_mm / 2.0
    min_v_c2c = max(via_size_mm + 0.25, 0.85)
    half_span = max(_mm(pad.GetSize().x), _mm(pad.GetSize().y)) / 2.0
    for smd_clr in (same_net_smd_clearance_mm, min(same_net_smd_clearance_mm, 0.15)):
        min_r = half_span + vr + smd_clr + 0.05
        radii = [round(min_r + 0.20 * k, 3) for k in range(16) if min_r + 0.20 * k <= 4.0]
        for rad in radii:
            for deg in range(0, 360, 15):
                ang = math.radians(deg)
                loc = pos + _pt(rad * math.cos(ang), rad * math.sin(ang))
                if not _via_clear(
                    b, loc, g_code, via_size_mm, foreign_clearance_mm,
                    smd_clr, min_v_c2c, 0.8, board_bbox, ep_set,
                    foreign_items=foreign_items,
                    same_net_smd_pads=same_net_smd_pads,
                    existing_vias=existing_vias,
                ):
                    continue
                for w_try in (stub_width_mm, 0.20):
                    if _try_route_stub(loc, w_try):
                        v = _add_via(b, loc, g_code, via_size_mm, via_drill_mm)
                        existing_vias.append(v)
                        return True
    return False


def _heal_drc_issues_locally(
    board_path: Path,
    drc_report: Dict,
    ground_net: str,
    ep_set: set,
    via_size_mm: float = 0.6,
    via_drill_mm: float = 0.3,
    foreign_clearance_mm: float = 0.21,
    same_net_smd_clearance_mm: float = 0.25,
) -> Dict[str, int]:
    """Surgical per-item fixes after pass-1 DRC without weakening board-level rules:
      1. Unconnected SMD pads on `ground_net` (isolated by traces inside an outer
         zone) get a direct safe copper stub to a nearby ground via or a new safe
         escape via + stub.
      2. Fine-pitch IC pads (>=6 pins) hitting `starved_thermal` get a per-pad
         `ZONE_CONNECTION_FULL` override; 2-pin passives hitting `starved_thermal`
         get a direct 0.25mm stub to a ground via + `ZONE_CONNECTION_NONE` so
         thermal relief / zero-tombstone safety is preserved!
      3. Remaining `track_not_centered_on_via` warnings are snapped by exact UUID.
    """
    b = p.LoadBoard(str(board_path))
    g_net = b.FindNet(ground_net)
    g_code = g_net.GetNetCode() if g_net is not None else -1
    tracks = list(b.GetTracks())
    by_uuid = {t.m_Uuid.AsString(): t for t in tracks}
    pads_by_uuid = {pad.m_Uuid.AsString(): pad for pad in b.GetPads()}

    board_bbox = b.GetBoardEdgesBoundingBox()
    existing_vias = [t for t in tracks if isinstance(t, p.PCB_VIA)]
    foreign_items = (
        [t for t in tracks if t.GetNetCode() != g_code]
        + [pad for pad in b.GetPads() if pad.GetNetCode() != g_code]
    )
    same_net_smd_pads = [
        pad for pad in b.GetPads()
        if pad.GetNetCode() == g_code
        and pad.GetAttribute() == p.PAD_ATTRIB_SMD
        and not _is_ep_pad(pad, ep_set)
    ]

    healed_unconn = 0
    healed_starved_ic = 0
    healed_starved_passive = 0
    healed_center = 0
    seen_pads = set()

    # 1. Heal unconnected ground SMD pads reported by DRC
    if g_code >= 0:
        for u in drc_report.get('unconnected_items') or ():
            for it in u.get('items') or ():
                uid = it.get('uuid')
                pad = pads_by_uuid.get(uid)
                if pad is None or uid in seen_pads:
                    continue
                if pad.GetNetCode() == g_code and pad.GetAttribute() == p.PAD_ATTRIB_SMD:
                    seen_pads.add(uid)
                    if _connect_pad_with_safe_via_or_stub(
                        b, pad, g_code, ep_set, via_size_mm, via_drill_mm,
                        foreign_clearance_mm, same_net_smd_clearance_mm,
                        board_bbox, foreign_items, same_net_smd_pads, existing_vias,
                    ):
                        healed_unconn += 1

    # 2. Heal starved_thermal & off-center track-via warnings
    for v in drc_report.get('violations') or ():
        vtype = v.get('type')
        items = v.get('items') or []
        if vtype == 'starved_thermal':
            for it in items:
                uid = it.get('uuid')
                pad = pads_by_uuid.get(uid)
                if pad is None:
                    continue
                fp = pad.GetParentFootprint()
                if fp is not None and len(list(fp.Pads())) >= 6:
                    pad.SetLocalZoneConnection(p.ZONE_CONNECTION_FULL)
                    healed_starved_ic += 1
                elif g_code >= 0 and pad.GetNetCode() == g_code and pad.GetAttribute() == p.PAD_ATTRIB_SMD:
                    # 2-pin passive: connect with a dedicated 0.25mm stub+via and detach solid zone
                    # so thermal relief is preserved without tombstoning AND DRC clears!
                    ok = _connect_pad_with_safe_via_or_stub(
                        b, pad, g_code, ep_set, via_size_mm, via_drill_mm,
                        foreign_clearance_mm, same_net_smd_clearance_mm,
                        board_bbox, foreign_items, same_net_smd_pads, existing_vias,
                    )
                    if ok:
                        pad.SetLocalZoneConnection(p.ZONE_CONNECTION_NONE)
                        healed_starved_passive += 1
        elif vtype == 'track_not_centered_on_via' and len(items) == 2:
            objs = [by_uuid.get(it.get('uuid')) for it in items]
            if all(objs):
                t, via = objs
                if isinstance(t, p.PCB_VIA):
                    t, via = via, t
                if not isinstance(t, p.PCB_VIA) and isinstance(via, p.PCB_VIA):
                    vp = via.GetPosition()
                    ds = (t.GetStart() - vp).EuclideanNorm()
                    de = (t.GetEnd() - vp).EuclideanNorm()
                    if ds <= de:
                        t.SetStart(vp)
                    else:
                        t.SetEnd(vp)
                    if t.GetStart() == t.GetEnd():
                        b.Remove(t)
                    healed_center += 1

    if healed_unconn or healed_starved_ic or healed_starved_passive or healed_center:
        p.SaveBoard(str(board_path), b)
    return {
        'unconnected_gnd_pads_healed': healed_unconn,
        'starved_thermal_ic_pads': healed_starved_ic,
        'starved_thermal_passive_stubs': healed_starved_passive,
        'via_centers_snapped_pass2': healed_center,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='Deterministic analog PCB finishing & DFM gate')
    ap.add_argument('input_pcb', help='routed input .kicad_pcb')
    ap.add_argument('output_pcb', help='finished output .kicad_pcb')
    ap.add_argument('--ground-net', default='/AGND', help='ground plane net name (default: /AGND)')
    ap.add_argument('--ep-refs', default='', help='comma-separated EP IC refs (e.g. U1,U2,RN1,RN2)')
    ap.add_argument('--ep-vias', type=int, default=4, help='min /AGND vias inside each EP pad')
    ap.add_argument('--power-nets', default='/+15V,/-15V,/VCC_DUT,/VEE_DUT,/CHASSIS',
                    help='comma-separated power/chassis nets to report in audit')
    ap.add_argument('--via-size', type=float, default=0.6)
    ap.add_argument('--via-drill', type=float, default=0.3)
    ap.add_argument('--clearance', type=float, default=0.21)
    ap.add_argument('--same-net-smd-clearance', type=float, default=0.25,
                    help='min edge-to-edge mm from new ground via to non-EP SMD pads')
    ap.add_argument('--max-return-via-dist', type=float, default=1.45,
                    help='place escape via if SMD ground pad has no ground via within this mm')
    ap.add_argument('--no-stubs', action='store_true', help='do not add explicit stub tracks')
    ap.add_argument('--perimeter-pitch', type=float, default=15.0,
                    help='perimeter stitching via pitch in mm (0 disables)')
    ap.add_argument('--perimeter-inset', type=float, default=2.2,
                    help='perimeter stitching via inset from board edge in mm')
    ap.add_argument('--no-center-vias', action='store_true',
                    help='skip snapping track endpoints to via centers')
    ap.add_argument('--kicad-cli', default=None)
    ap.add_argument('--drc-json', default=None, help='path to write KiCad DRC JSON')
    ap.add_argument('--audit-json', default=None, help='path to write DFM audit JSON')
    ap.add_argument('--skip-drc', action='store_true', help='skip kicad-cli DRC & zone refill')
    ap.add_argument('--audit-only', action='store_true',
                    help='do not modify the board; only run DRC (unless --skip-drc) and DFM audit on input_pcb')
    a = ap.parse_args(argv)

    src = Path(a.input_pcb).resolve()
    dst = Path(a.output_pcb).resolve()
    ep_refs = [r.strip() for r in a.ep_refs.split(',') if r.strip()]
    power_nets = [n.strip() for n in a.power_nets.split(',') if n.strip()]

    if a.audit_only:
        drc_report = None
        if not a.skip_drc:
            kcli = _find_kicad_cli(a.kicad_cli)
            drc_out = Path(a.drc_json) if a.drc_json else src.with_suffix('.drc.json')
            drc_out.parent.mkdir(parents=True, exist_ok=True)
            cmd = [kcli, 'pcb', 'drc', '--refill-zones', '--format', 'json', '-o', str(drc_out), str(src)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if not drc_out.is_file():
                print(f'analog_finish: kicad-cli DRC failed (rc={res.returncode}): {res.stderr}',
                      file=sys.stderr)
                return 2
            drc_report = json.loads(drc_out.read_text(encoding='utf-8'))
        audit = audit_board(str(src), a.ground_net, ep_refs, power_nets, drc_report)
        if a.audit_json:
            Path(a.audit_json).parent.mkdir(parents=True, exist_ok=True)
            Path(a.audit_json).write_text(json.dumps(audit, indent=2), encoding='utf-8')
        print('ANALOG_FINISH_SUMMARY: ' + json.dumps({
            'audit_only': True,
            'drc_errors': audit['drc'].get('errors') if audit['drc'] else None,
            'drc_warnings': audit['drc'].get('warnings') if audit['drc'] else None,
            'drc_unconnected': audit['drc'].get('unconnected') if audit['drc'] else None,
            'total_copper_mm': audit['total_copper_mm'],
            'vias': audit['vias'],
            'perimeter_stitch_vias': audit['perimeter_stitch_vias'],
            'ep_via_counts': audit['ep_via_counts'],
            'outer_bad_thermal_zones': audit['outer_bad_thermal_zones'],
            'non_ep_via_in_smd': audit['non_ep_via_in_smd'],
            'non_ep_via_grazing_0p30mm': audit['non_ep_via_grazing_0p30mm'],
            'max_decap_return_mm': audit['max_decap_return_mm'],
            'power_min_widths': {k: v.get('min_w') for k, v in (audit.get('power_net_stats') or {}).items()},
        }))
        return 0

    dst.parent.mkdir(parents=True, exist_ok=True)

    # Copy sibling project files if present
    for ext in ('.kicad_pro', '.kicad_prl'):
        sib = src.with_suffix(ext)
        if sib.is_file() and sib != dst.with_suffix(ext):
            shutil.copyfile(sib, dst.with_suffix(ext))

    board = p.LoadBoard(str(src))
    g_net = board.FindNet(a.ground_net)
    if g_net is None:
        print(f'analog_finish: ground net {a.ground_net!r} not found', file=sys.stderr)
        return 2
    g_code = g_net.GetNetCode()
    ep_set = set(ep_refs)
    board_bbox = board.GetBoardEdgesBoundingBox()

    # Pre-index items once for fast collision checking
    existing_vias = [t for t in board.GetTracks() if isinstance(t, p.PCB_VIA)]
    foreign_items = (
        [t for t in board.GetTracks() if t.GetNetCode() != g_code]
        + [pad for pad in board.GetPads() if pad.GetNetCode() != g_code]
    )
    same_net_smd_pads = [
        pad for pad in board.GetPads()
        if pad.GetNetCode() == g_code
        and pad.GetAttribute() == p.PAD_ATTRIB_SMD
        and not _is_ep_pad(pad, ep_set)
    ]

    # Step 1: Enforce outer thermal relief
    zone_changes = enforce_zone_thermal_relief(board, g_code)

    # Step 2: EP thermal via arrays
    ep_added = place_ep_thermal_vias(
        board, g_code, ep_refs, a.ep_vias, a.via_size, a.via_drill,
        a.clearance, board_bbox, foreign_items, existing_vias,
    )

    # Step 3: Safe SMD ground escape vias + explicit stubs
    esc_stats = place_smd_ground_escape_vias(
        board, g_code, ep_set, a.max_return_via_dist, a.via_size, a.via_drill,
        a.clearance, a.same_net_smd_clearance, board_bbox,
        foreign_items, same_net_smd_pads, existing_vias,
        add_stubs=not a.no_stubs,
    )

    # Step 4: Perimeter stitching vias
    perim_added = place_perimeter_stitching_vias(
        board, g_code, ep_set, a.perimeter_pitch, a.perimeter_inset,
        a.via_size, a.via_drill, a.clearance, a.same_net_smd_clearance,
        board_bbox, foreign_items, same_net_smd_pads, existing_vias,
    )

    # Step 5: Center off-center track endpoints on vias
    snapped = 0 if a.no_center_vias else center_tracks_on_vias(board)

    p.SaveBoard(str(dst), board)

    # Step 6: Native KiCad DRC + zone refill + save filled board (with surgical pass-2 heal)
    drc_report = None
    heal_stats = {'starved_thermal_ic_pads': 0, 'via_centers_snapped_pass2': 0}
    if not a.skip_drc:
        kcli = _find_kicad_cli(a.kicad_cli)
        drc_out = Path(a.drc_json) if a.drc_json else dst.with_suffix('.drc.json')
        cmd = [
            kcli, 'pcb', 'drc',
            '--refill-zones', '--save-board',
            '--format', 'json',
            '-o', str(drc_out),
            str(dst),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if not drc_out.is_file():
            print(f'analog_finish: kicad-cli DRC failed (rc={res.returncode}): {res.stderr}',
                  file=sys.stderr)
            return 2
        drc_report = json.loads(drc_out.read_text(encoding='utf-8'))

        # Surgical pass-2 heal (up to 2 iterations) for unconnected ground SMD pads,
        # starved_thermal & residual off-center vias
        for _heal_iter in range(2):
            need_heal = (
                bool(drc_report.get('unconnected_items'))
                or any(
                    v.get('type') in ('starved_thermal', 'track_not_centered_on_via')
                    for v in (drc_report.get('violations') or ())
                )
            )
            if not need_heal:
                break
            hs = _heal_drc_issues_locally(
                dst, drc_report, a.ground_net, ep_set,
                via_size_mm=a.via_size, via_drill_mm=a.via_drill,
                foreign_clearance_mm=a.clearance,
                same_net_smd_clearance_mm=a.same_net_smd_clearance,
            )
            for k_h, v_h in hs.items():
                heal_stats[k_h] = heal_stats.get(k_h, 0) + v_h
            if not any(hs.values()):
                break
            subprocess.run(cmd, capture_output=True, text=True)
            if drc_out.is_file():
                drc_report = json.loads(drc_out.read_text(encoding='utf-8'))

    audit = audit_board(str(dst), a.ground_net, ep_refs, power_nets, drc_report)
    audit['actions'] = {
        'zone_thermal_changes': zone_changes,
        'ep_vias_added': ep_added,
        'smd_escape': esc_stats,
        'perimeter_vias_added': perim_added,
        'via_endpoints_snapped': snapped,
        'pass2_heal': heal_stats,
    }
    if a.audit_json:
        Path(a.audit_json).write_text(json.dumps(audit, indent=2), encoding='utf-8')

    print('ANALOG_FINISH_SUMMARY: ' + json.dumps({
        'drc_errors': audit['drc'].get('errors') if audit['drc'] else None,
        'drc_warnings': audit['drc'].get('warnings') if audit['drc'] else None,
        'drc_unconnected': audit['drc'].get('unconnected') if audit['drc'] else None,
        'total_copper_mm': audit['total_copper_mm'],
        'vias': audit['vias'],
        'perimeter_stitch_vias': audit['perimeter_stitch_vias'],
        'ep_via_counts': audit['ep_via_counts'],
        'outer_bad_thermal_zones': audit['outer_bad_thermal_zones'],
        'non_ep_via_in_smd': audit['non_ep_via_in_smd'],
        'non_ep_via_grazing_0p30mm': audit['non_ep_via_grazing_0p30mm'],
        'max_decap_return_mm': audit['max_decap_return_mm'],
    }))
    return 0


if __name__ == '__main__':
    sys.exit(main())
