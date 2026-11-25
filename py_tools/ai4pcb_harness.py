#!/usr/bin/env python3
"""AI4PCB Closed-Loop Agent Harness CLI over KiCadRoutingTools (Fork).

Fixes the structural bottlenecks identified in upstream `pcb_agent_harness.py`
and first-generation LLM PCB layout runs:
  1. Compiles Route-B `board.yaml` directly into KRT routing & physics constraints
     (no manual regex diff-pair mistakes or dropped `/CHASSIS` / rail widths).
  2. Restores true multi-seed parallel judging with geometric diversity selection
     (`--diverse auto|farthest|medoid`) and two-stage `--confirm close` full
     routing (never hardcoding `judged_list[0]`).
  3. Adds an early native KiCad courtyard-overlap DRC gate right after placement
     before committing routing compute.
  4. Provides deterministic congestion/failure-heatmap route-feedback local SA
     refinement (`feedback-refine`) with strict keep-only-if-better rollback.
  5. Replaces ad-hoc `pcbnew` finishing scripts with deterministic `analog_finish.py`
     (outer thermal relief preserved, surgical fine-pitch IC starved-thermal fix,
     EP thermal vias, safe SMD ground escape vias, perimeter stitching, DRC).
  6. Maintains reversible board snapshots (`undo`) and compact JSON outputs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
KRT_ROOT = HERE.parent
for _sub in ('py_router', 'py_placer', 'py_tools'):
    _p = str(KRT_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _find_kicad_cli(explicit: Optional[str] = None) -> str:
    for cand in (
        explicit,
        os.environ.get('KICAD_CLI'),
        shutil.which('kicad-cli'),
        r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe',
        r'C:\Program Files\KiCad\9.0\bin\kicad-cli.exe',
        '/usr/bin/kicad-cli',
    ):
        if cand and os.path.isfile(cand):
            return cand
    raise FileNotFoundError('kicad-cli not found; set KICAD_CLI or install KiCad')


def _find_kicad_python(explicit: Optional[str] = None) -> str:
    for cand in (
        explicit,
        os.environ.get('KICAD_PYTHON'),
        r'C:\Program Files\KiCad\10.0\bin\python.exe',
        r'C:\Program Files\KiCad\9.0\bin\python.exe',
        shutil.which('python3'),
    ):
        if cand and os.path.isfile(cand):
            return cand
    return sys.executable


def _copy_with_siblings(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    for ext in ('.kicad_pro', '.kicad_prl', '.kicad_dru'):
        sib = src.with_suffix(ext)
        if sib.is_file():
            shutil.copyfile(sib, dst.with_suffix(ext))


def _state_path(work_dir: Path) -> Path:
    return work_dir / 'ai4pcb_session.json'


def _load_state(work_dir: Path) -> Dict:
    p = _state_path(work_dir)
    if not p.is_file():
        raise FileNotFoundError(f'Session not initialized in {work_dir}; run `init` first.')
    return json.loads(p.read_text(encoding='utf-8'))


def _save_state(work_dir: Path, st: Dict) -> None:
    _state_path(work_dir).write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding='utf-8')


def _push_snapshot(work_dir: Path, st: Dict, label: str, pcb_path: Path) -> None:
    snaps_dir = work_dir / 'snapshots'
    snaps_dir.mkdir(parents=True, exist_ok=True)
    idx = len(st.get('snapshots') or ())
    snap_pcb = snaps_dir / f'{idx:03d}_{label}.kicad_pcb'
    _copy_with_siblings(pcb_path, snap_pcb)
    st.setdefault('snapshots', []).append({
        'index': idx,
        'label': label,
        'pcb': str(snap_pcb),
        'ts': round(time.time(), 2),
    })
    st['current_pcb'] = str(pcb_path)


def _run_cmd(cmd: Sequence[str], *, cwd: Path = KRT_ROOT, timeout_s: int = 900) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env['PYTHONUTF8'] = '1'
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    extra = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
    return subprocess.run(
        list(cmd),
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding='utf-8',
        errors='replace',
        timeout=timeout_s,
        **extra,
    )


def _parse_json_summary(stdout: str, prefix: str = 'JSON_SUMMARY: ') -> Optional[Dict]:
    got = None
    for line in stdout.splitlines():
        if line.startswith(prefix):
            try:
                got = json.loads(line[len(prefix):])
            except ValueError:
                pass
    return got


def _early_courtyard_check(pcb_path: Path, kcli: str) -> Dict:
    """Fast native KiCad courtyard-overlap check (no zone refill, ~1-2 s)."""
    tmp_drc = pcb_path.with_suffix('.courtyard_drc.json')
    res = _run_cmd([kcli, 'pcb', 'drc', '--format', 'json', '-o', str(tmp_drc), str(pcb_path)], timeout_s=60)
    if not tmp_drc.is_file():
        return {'ok': False, 'error': f'kicad-cli rc={res.returncode}: {res.stderr.strip()[:200]}'}
    doc = json.loads(tmp_drc.read_text(encoding='utf-8'))
    cy = [v for v in (doc.get('violations') or ()) if v.get('type') == 'courtyard_overlap']
    edge = [v for v in (doc.get('violations') or ()) if 'edge' in str(v.get('type', ''))]
    pairs = []
    for v in cy[:10]:
        descs = [it.get('description', '') for it in (v.get('items') or ())]
        pairs.append(' <-> '.join(descs))
    return {
        'ok': len(cy) == 0,
        'courtyard_overlaps': len(cy),
        'edge_violations': len(edge),
        'sample_pairs': pairs,
    }


def _geometry_audit(pcb_path: Path, constraints_path: Optional[Path]) -> Dict:
    """Evaluate exact X/Y translation symmetry & thermal spacing directly from placed PCB."""
    if not constraints_path or not constraints_path.is_file():
        return {}
    from kicad_parser import parse_kicad_pcb
    cc = json.loads(constraints_path.read_text(encoding='utf-8'))
    pcb = parse_kicad_pcb(str(pcb_path))
    out: Dict = {}

    sym = cc.get('symmetry') or {}
    sym_pairs = cc.get('symmetry_pairs') or []
    if sym and sym_pairs:
        dx = float(sym.get('dx_mm') or 0.0)
        dy = float(sym.get('dy_mm') or 0.0)
        drot = float(sym.get('drot_deg') or 0.0)
        pos_errs, rot_mismatches = [], 0
        worst = None
        for pair in sym_pairs:
            if len(pair) != 2:
                continue
            ra, rb = pair[0], pair[1]
            if ra not in pcb.footprints or rb not in pcb.footprints:
                continue
            fa, fb = pcb.footprints[ra], pcb.footprints[rb]
            err = math.hypot((fb.x - fa.x) - dx, (fb.y - fa.y) - dy)
            dr = abs(((fb.rotation - fa.rotation) - drot) % 360.0)
            dr = min(dr, 360.0 - dr)
            pos_errs.append(err)
            if dr > 1.0:
                rot_mismatches += 1
            if worst is None or err > worst[0]:
                worst = (err, ra, rb)
        if pos_errs:
            out['symmetry'] = {
                'pairs_checked': len(pos_errs),
                'mean_err_mm': round(sum(pos_errs) / len(pos_errs), 3),
                'max_err_mm': round(max(pos_errs), 3),
                'within_2mm_pct': round(100.0 * sum(1 for e in pos_errs if e <= 2.0) / len(pos_errs), 1),
                'rot_mismatches': rot_mismatches,
                'worst_pair': f'{worst[1]}/{worst[2]} ({worst[0]:.2f}mm)' if worst else None,
            }

    therm = cc.get('thermal_separation') or {}
    hot = [r for r in (therm.get('hot_refs') or ()) if r in pcb.footprints]
    sens = [r for r in (therm.get('sensitive_refs') or ()) if r in pcb.footprints]
    if hot and sens:
        hs = [
            (math.hypot(pcb.footprints[h].x - pcb.footprints[s].x,
                        pcb.footprints[h].y - pcb.footprints[s].y), h, s)
            for h in hot for s in sens
        ]
        hh = [
            (math.hypot(pcb.footprints[hot[i]].x - pcb.footprints[hot[j]].x,
                        pcb.footprints[hot[i]].y - pcb.footprints[hot[j]].y), hot[i], hot[j])
            for i in range(len(hot)) for j in range(i + 1, len(hot))
        ]
        min_hs = min(hs)
        min_hh = min(hh) if hh else (0.0, '', '')
        out['thermal'] = {
            'min_hot_to_sensitive_mm': round(min_hs[0], 2),
            'closest_hot_sensitive_pair': f'{min_hs[1]}-{min_hs[2]}',
            'min_hot_to_hot_mm': round(min_hh[0], 2),
            'closest_hot_hot_pair': f'{min_hh[1]}-{min_hh[2]}',
        }
    return out


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_init(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    src_in = Path(args.pcb).resolve()
    if not src_in.is_file():
        print(json.dumps({'ok': False, 'error': f'PCB not found: {src_in}'}))
        return 2

    source_pcb = work_dir / 'source.kicad_pcb'
    _copy_with_siblings(src_in, source_pcb)

    fixed_pcb = work_dir / 'fixed.kicad_pcb'
    locked_refs = [r.strip() for r in (args.lock or '').split(',') if r.strip()]

    work_pcb = work_dir / '_prep.kicad_pcb'
    _copy_with_siblings(source_pcb, work_pcb)

    if locked_refs:
        r = _run_cmd([
            sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'place_pose.py'),
            str(work_pcb), str(work_pcb), '--allow-routed', 'lock', *locked_refs,
        ])
        if r.returncode != 0:
            print(json.dumps({'ok': False, 'error': 'place_pose lock failed', 'stderr': r.stderr}))
            return 2

    if args.unplace:
        strip_script = KRT_ROOT / 'tests' / 'stress' / 'strip_copper_only.py'
        if strip_script.is_file():
            stripped_pcb = work_dir / '_stripped.kicad_pcb'
            rs = _run_cmd([
                sys.executable, '-X', 'utf8', str(strip_script),
                str(work_pcb), str(stripped_pcb),
            ])
            if rs.returncode == 0 and stripped_pcb.is_file():
                _copy_with_siblings(stripped_pcb, work_pcb)
                stripped_pcb.unlink(missing_ok=True)
        unplace_script = None
        for cand in (
            KRT_ROOT / '.claude' / 'skills' / 'plan-pcb-placement' / 'scripts' / 'make_unplaced.py',
            KRT_ROOT / 'py_tools' / 'make_unplaced.py',
            KRT_ROOT / 'py_placer' / 'make_unplaced.py',
        ):
            if cand.is_file():
                unplace_script = cand
                break
        if unplace_script is None:
            print(json.dumps({'ok': False, 'error': 'make_unplaced.py not found in KRT_ROOT'}))
            return 2
        r = _run_cmd([
            sys.executable, '-X', 'utf8', str(unplace_script),
            '--keep-locked', str(work_pcb), str(fixed_pcb),
        ])
        if r.returncode != 0 or not fixed_pcb.is_file():
            print(json.dumps({'ok': False, 'error': 'make_unplaced failed', 'stdout': r.stdout, 'stderr': r.stderr}))
            return 2
        _copy_with_siblings(source_pcb, work_dir / '_sib_tmp.kicad_pcb')
        for ext in ('.kicad_pro', '.kicad_prl', '.kicad_dru'):
            sib = source_pcb.with_suffix(ext)
            if sib.is_file():
                shutil.copyfile(sib, fixed_pcb.with_suffix(ext))
        (work_dir / '_sib_tmp.kicad_pcb').unlink(missing_ok=True)
    else:
        _copy_with_siblings(work_pcb, fixed_pcb)
    work_pcb.unlink(missing_ok=True)

    compiled_dir = work_dir / 'compiled'
    compiled_info = None
    if args.board_yaml:
        by = Path(args.board_yaml).resolve()
        if not by.is_file():
            print(json.dumps({'ok': False, 'error': f'board.yaml not found: {by}'}))
            return 2
        r = _run_cmd([
            sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_tools' / 'compile_board_constraints.py'),
            str(by), str(fixed_pcb), '--out-dir', str(compiled_dir),
            '--same-net-pad-clearance', str(args.same_net_pad_clearance),
        ])
        if r.returncode != 0:
            print(json.dumps({'ok': False, 'error': 'compile_board_constraints failed', 'stderr': r.stderr}))
            return 2
        compiled_info = {
            'routing_policy': str(compiled_dir / 'routing_policy.json'),
            'compiled_constraints': str(compiled_dir / 'compiled_constraints.json'),
            'judge_chain': str(compiled_dir / 'judge_chain.txt'),
        }

    st = {
        'work_dir': str(work_dir),
        'source_pcb': str(source_pcb),
        'fixed_pcb': str(fixed_pcb),
        'current_pcb': str(fixed_pcb),
        'best_placed_pcb': None,
        'ground_net': args.ground_net,
        'compiled': compiled_info,
        'place_runs': 0,
        'last_place_dir': None,
        'last_judge_json': None,
        'groups_path': None,
        'intent_path': None,
        'constraints_path': compiled_info['compiled_constraints'] if compiled_info else None,
        'snapshots': [],
    }
    _push_snapshot(work_dir, st, 'init_fixed', fixed_pcb)
    _save_state(work_dir, st)

    print(json.dumps({
        'ok': True,
        'work_dir': str(work_dir),
        'fixed_pcb': str(fixed_pcb),
        'compiled': compiled_info,
    }, indent=2))
    return 0


def cmd_overview(args) -> int:
    from kicad_parser import parse_kicad_pcb
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    pcb_path = Path(st['fixed_pcb'])
    pcb = parse_kicad_pcb(str(pcb_path))

    xs, ys = [], []
    for seg in getattr(pcb, 'board_outline', None) or ():
        if hasattr(seg, 'start_x'):
            xs.extend((float(seg.start_x), float(seg.end_x)))
            ys.extend((float(seg.start_y), float(seg.end_y)))
    bounds = {
        'min_x': round(min(xs), 2), 'max_x': round(max(xs), 2),
        'min_y': round(min(ys), 2), 'max_y': round(max(ys), 2),
        'width_mm': round(max(xs) - min(xs), 2),
        'height_mm': round(max(ys) - min(ys), 2),
    } if xs else {}

    locked = sorted([r for r, fp in pcb.footprints.items() if getattr(fp, 'locked', False)])
    movable = sorted([r for r, fp in pcb.footprints.items() if not getattr(fp, 'locked', False)])
    ics = []
    for r, fp in sorted(pcb.footprints.items()):
        pads = getattr(fp, 'pads', None) or ()
        if len(pads) >= 4 or r.startswith(('U', 'IC', 'RN', 'Q')):
            nets = sorted({pcb.nets[q.net_id].name for q in pads if q.net_id in pcb.nets and pcb.nets[q.net_id].name})
            ics.append({
                'ref': r,
                'value': getattr(fp, 'value', ''),
                'footprint': getattr(fp, 'footprint_name', ''),
                'pins': len(pads),
                'locked': bool(getattr(fp, 'locked', False)),
                'pos': [round(float(fp.x), 2), round(float(fp.y), 2)],
                'nets': nets[:12],
            })

    all_nets = sorted({n.name for n in pcb.nets.values() if n.name})
    txt = pcb_path.read_text(encoding='utf-8', errors='replace')
    cu_layers = re.findall(r'\(\d+\s+"([^"]+\.Cu)"', txt)
    # Also compute outline bounds from gr_line / gr_rect on Edge.Cuts if board_outline empty
    if not bounds:
        ex, ey = [], []
        for m in re.finditer(r'\((?:start|end|xy)\s+([-\d.]+)\s+([-\d.]+)\)', txt):
            pass
        xs_fp = [float(fp.x) for fp in pcb.footprints.values()]
        ys_fp = [float(fp.y) for fp in pcb.footprints.values()]
        if xs_fp:
            bounds = {
                'footprint_bbox_min_x': round(min(xs_fp), 2), 'footprint_bbox_max_x': round(max(xs_fp), 2),
                'footprint_bbox_min_y': round(min(ys_fp), 2), 'footprint_bbox_max_y': round(max(ys_fp), 2),
            }

    out = {
        'ok': True,
        'fixed_pcb': str(pcb_path),
        'current_pcb': st['current_pcb'],
        'copper_layers': cu_layers,
        'bounds_mm': bounds,
        'footprint_counts': {'total': len(pcb.footprints), 'locked': len(locked), 'movable': len(movable)},
        'locked_refs': locked,
        'movable_refs': movable,
        'key_ics_and_actives': ics,
        'net_count': len(all_nets),
        'nets': all_nets,
        'compiled': st.get('compiled'),
    }
    if st.get('compiled'):
        rp = Path(st['compiled']['routing_policy'])
        cc = Path(st['compiled']['compiled_constraints'])
        if rp.is_file():
            out['routing_policy'] = json.loads(rp.read_text(encoding='utf-8'))
        if cc.is_file():
            out['compiled_constraints'] = json.loads(cc.read_text(encoding='utf-8'))
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


def cmd_place(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    fixed_pcb = Path(st['fixed_pcb'])
    groups_path = Path(args.groups).resolve()
    intent_path = Path(args.intent).resolve()

    # 1. Pre-validate groups & floorplan intent cleanly
    if not args.skip_validate:
        r_g = _run_cmd([
            sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_tools' / 'check_groups.py'),
            str(fixed_pcb), str(groups_path),
        ])
        if r_g.returncode != 0:
            print(json.dumps({'ok': False, 'stage': 'check_groups', 'stdout': r_g.stdout, 'stderr': r_g.stderr}, indent=2))
            return 2
        r_i = _run_cmd([
            sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_tools' / 'check_floorplan.py'),
            str(fixed_pcb), '--intent', str(intent_path),
        ])
        # check_floorplan returns 0/1 depending on unplaced board checks; check JSON/syntax errors only
        if r_i.returncode == 2:
            print(json.dumps({'ok': False, 'stage': 'check_floorplan', 'stdout': r_i.stdout, 'stderr': r_i.stderr}, indent=2))
            return 2

    run_idx = int(st.get('place_runs') or 0) + 1
    run_dir = work_dir / 'runs' / f'place_{run_idx:02d}'
    run_dir.mkdir(parents=True, exist_ok=True)
    out_pat = run_dir / 's{seed}.kicad_pcb'

    constraints_path = Path(args.constraints_json).resolve() if args.constraints_json else (
        Path(st['constraints_path']) if st.get('constraints_path') and Path(st['constraints_path']).is_file() else None
    )

    cmd = [
        sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'place_simple.py'),
        str(fixed_pcb), str(out_pat),
        '--groups', str(groups_path),
        '--intent', str(intent_path),
        '--scattered', str(args.scattered),
        '--clustered', str(args.clustered),
        '--anchored', str(args.anchored),
        '--judge', str(args.judge),
        '--diverse', args.diverse,
        '--diverse-cost-cap', str(args.diverse_cost_cap),
        '--jobs', str(args.jobs),
    ]
    if constraints_path:
        cmd += ['--constraints-json', str(constraints_path)]
    if args.macro_flow:
        cmd.append('--macro-flow')
    if args.local_radius is not None:
        cmd += ['--local-radius', str(args.local_radius)]
    if args.moves is not None:
        cmd += ['--moves', str(args.moves)]

    t0 = time.time()
    r = _run_cmd(cmd, timeout_s=900)
    dt = round(time.time() - t0, 1)
    judge_json = run_dir / 'judge.json'
    if not judge_json.is_file():
        print(json.dumps({
            'ok': False, 'stage': 'place_simple', 'rc': r.returncode,
            'seconds': dt, 'stdout': r.stdout[-2500:], 'stderr': r.stderr[-1500:],
        }, indent=2))
        return 2

    jdoc = json.loads(judge_json.read_text(encoding='utf-8'))
    selected_seeds = [int(s) for s in (jdoc.get('judge') or ())]

    # 2. Early native KiCad courtyard DRC gate on selected diverse seeds!
    courtyard_checks = {}
    clean_cy_seeds = []
    if args.check_courtyard and selected_seeds:
        try:
            kcli = _find_kicad_cli(args.kicad_cli)
            for s in selected_seeds:
                sp = run_dir / f's{s}.kicad_pcb'
                if sp.is_file():
                    chk = _early_courtyard_check(sp, kcli)
                    courtyard_checks[str(s)] = chk
                    if chk.get('ok'):
                        clean_cy_seeds.append(s)
        except FileNotFoundError as ex:
            courtyard_checks['note'] = str(ex)

    # Geometry audit on first selected seed
    geom = {}
    if selected_seeds:
        p0 = run_dir / f's{selected_seeds[0]}.kicad_pcb'
        if p0.is_file():
            geom = _geometry_audit(p0, constraints_path)

    st['place_runs'] = run_idx
    st['last_place_dir'] = str(run_dir)
    st['last_judge_json'] = str(judge_json)
    st['groups_path'] = str(groups_path)
    st['intent_path'] = str(intent_path)
    if constraints_path:
        st['constraints_path'] = str(constraints_path)
    _save_state(work_dir, st)

    print(json.dumps({
        'ok': True,
        'seconds': dt,
        'run_dir': str(run_dir),
        'judge_json': str(judge_json),
        'selected_seeds': selected_seeds,
        'cost_judge_seeds': jdoc.get('cost_judge'),
        'illegal_seeds': jdoc.get('illegal'),
        'costs': jdoc.get('costs'),
        'diversity': jdoc.get('diversity'),
        'early_courtyard_drc': courtyard_checks,
        'courtyard_clean_seeds': clean_cy_seeds,
        'seed0_geometry_audit': geom,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_judge(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    judge_json = Path(args.judge_json).resolve() if args.judge_json else Path(st['last_judge_json'])
    run_dir = judge_json.parent
    intent_path = Path(args.intent).resolve() if args.intent else (
        Path(st['intent_path']) if st.get('intent_path') else None
    )

    # Resolve chain file
    if args.chain:
        chain_path = Path(args.chain).resolve()
    elif st.get('compiled') and Path(st['compiled']['judge_chain']).is_file():
        chain_path = Path(st['compiled']['judge_chain'])
    else:
        print(json.dumps({'ok': False, 'error': 'No --chain provided and no compiled judge_chain.txt present.'}))
        return 2

    # Check compiled routing policy for forbid_regex_diff_pairs
    no_dp_warn = bool(args.no_diff_pair_warn)
    if not no_dp_warn and st.get('compiled'):
        rp_path = Path(st['compiled']['routing_policy'])
        if rp_path.is_file():
            rp = json.loads(rp_path.read_text(encoding='utf-8'))
            if rp.get('forbid_regex_diff_pairs'):
                no_dp_warn = True

    cmd = [
        sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'judge_seeds.py'),
        str(judge_json),
        '--chain', str(chain_path),
        '--jobs', str(args.jobs),
    ]
    if intent_path and intent_path.is_file():
        cmd += ['--intent', str(intent_path)]
    if args.seeds:
        cmd += ['--seeds', args.seeds]
    elif args.diverse and args.diverse != 'off':
        cmd += ['--diverse', args.diverse]
        if args.diverse_count:
            cmd += ['--diverse-count', str(args.diverse_count)]
    if args.confirm and args.confirm.lower() != 'none':
        cmd += ['--confirm', args.confirm]
    if no_dp_warn:
        cmd.append('--no-diff-pair-warn')

    t0 = time.time()
    r = _run_cmd(cmd, timeout_s=1800)
    dt = round(time.time() - t0, 1)
    summary = _parse_json_summary(r.stdout)
    if not summary or summary.get('best') is None:
        print(json.dumps({
            'ok': False, 'stage': 'judge_seeds', 'rc': r.returncode,
            'seconds': dt, 'stdout': r.stdout[-3000:], 'stderr': r.stderr[-1500:],
        }, indent=2))
        return 2

    best = int(summary['best'])
    full_pcb = run_dir / f's{best}_full.kicad_pcb'
    full_route_ran = full_pcb.is_file()

    # NEVER deliver a --quick 2 board! If confirm did not full-route `best`, run `--seeds best --full` now!
    if not full_route_ran:
        cmd_full = [
            sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'judge_seeds.py'),
            str(judge_json), '--chain', str(chain_path),
            '--seeds', str(best), '--full', '--jobs', '1',
        ]
        if intent_path and intent_path.is_file():
            cmd_full += ['--intent', str(intent_path)]
        if no_dp_warn:
            cmd_full.append('--no-diff-pair-warn')
        r_full = _run_cmd(cmd_full, timeout_s=900)
        full_sum = _parse_json_summary(r_full.stdout)
        summary['promoted_full_route'] = full_sum

    placed_best = run_dir / f's{best}.kicad_pcb'
    routed_best = full_pcb if full_pcb.is_file() else (run_dir / f's{best}_fast.kicad_pcb')
    active_routed = work_dir / 'active_routed.kicad_pcb'
    _copy_with_siblings(routed_best, active_routed)

    st['best_seed'] = best
    st['best_placed_pcb'] = str(placed_best)
    st['last_chain_path'] = str(chain_path)
    _push_snapshot(work_dir, st, f'judge_best_s{best}', active_routed)
    _save_state(work_dir, st)

    # Load final full score JSON if present
    sc_path = run_dir / f's{best}_full.score.json'
    if not sc_path.is_file():
        sc_path = run_dir / f's{best}_fast.score.json'
    best_score = json.loads(sc_path.read_text(encoding='utf-8')) if sc_path.is_file() else {}

    print(json.dumps({
        'ok': True,
        'seconds': round(time.time() - t0, 1),
        'best_seed': best,
        'best_placed_pcb': str(placed_best),
        'current_pcb': str(active_routed),
        'ranking_l2': summary.get('ranking'),
        'close_seeds': summary.get('close'),
        'confirmed_ranking': summary.get('confirmed'),
        'best_full_score': {
            'blocking': best_score.get('blocking'),
            'joins_needed': best_score.get('joins_needed'),
            'quality': best_score.get('quality'),
        },
        'l2_seeds': summary.get('seeds'),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_feedback_refine(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    run_dir = Path(st['last_place_dir'])
    best = int(args.seed) if args.seed is not None else int(st['best_seed'])
    placed_in = run_dir / f's{best}.kicad_pcb'
    routed_in = run_dir / f's{best}_full.kicad_pcb'
    rjson_in = run_dir / f's{best}_full_route' / 'route.json'
    if not routed_in.is_file():
        routed_in = run_dir / f's{best}_fast.kicad_pcb'
        rjson_in = run_dir / f's{best}_route' / 'route.json'

    fb_dir = work_dir / 'runs' / f'feedback_s{best}'
    fb_dir.mkdir(parents=True, exist_ok=True)
    fb_json = fb_dir / 'feedback.json'
    fc_json = fb_dir / 'feedback_constraints.json'

    r_fb = _run_cmd([
        sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'placement' / 'route_feedback.py'),
        str(routed_in),
        *([] if not rjson_in.is_file() else ['--route-json', str(rjson_in)]),
        '--out-feedback', str(fb_json),
        '--out-constraints', str(fc_json),
        '--corridor-gap', str(args.corridor_gap),
        '--max-hotspots', str(args.max_hotspots),
    ])
    if r_fb.returncode != 0 or not fb_json.is_file():
        print(json.dumps({'ok': False, 'stage': 'route_feedback', 'stderr': r_fb.stderr}))
        return 2

    fb = json.loads(fb_json.read_text(encoding='utf-8'))
    hot_refs = fb.get('hotspot_refs') or []
    if not hot_refs:
        print(json.dumps({'ok': True, 'kept': False, 'note': 'no routing hotspots detected; refinement skipped'}))
        return 0

    # Merge existing physics constraints (e.g. thermal/symmetry) with feedback corridor hints if present
    merged_constraints = json.loads(fc_json.read_text(encoding='utf-8'))
    if st.get('constraints_path') and Path(st['constraints_path']).is_file():
        base_cc = json.loads(Path(st['constraints_path']).read_text(encoding='utf-8'))
        base_cc['local_repulsion'] = merged_constraints.get('local_repulsion', [])
        merged_path = fb_dir / 'merged_constraints.json'
        merged_path.write_text(json.dumps(base_cc, indent=2), encoding='utf-8')
    else:
        merged_path = fc_json

    refined_placed = fb_dir / 's0.kicad_pcb'
    r_pl = _run_cmd([
        sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'place_simple.py'),
        str(placed_in), str(refined_placed),
        '--groups', st['groups_path'],
        '--intent', st['intent_path'],
        '--keep-poses', '--local-radius', str(args.local_radius),
        '--movable-only', ','.join(hot_refs),
        '--constraints-json', str(merged_path),
        '--moves', str(args.moves), '--seed', str(best + 100),
    ])
    if not refined_placed.is_file():
        print(json.dumps({'ok': False, 'stage': 'local_sa', 'stderr': r_pl.stderr[-1500:]}))
        return 2

    # Write minimal judge.json & full-route via same chain
    j_fb = fb_dir / 'judge.json'
    j_fb.write_text(json.dumps({'judge': [0], 'baseline': st['fixed_pcb']}), encoding='utf-8')
    r_rt = _run_cmd([
        sys.executable, '-X', 'utf8', str(KRT_ROOT / 'py_placer' / 'judge_seeds.py'),
        str(j_fb), '--chain', st['last_chain_path'],
        '--intent', st['intent_path'], '--seeds', '0', '--full', '--no-diff-pair-warn',
    ], timeout_s=900)
    sc_after_p = fb_dir / 's0_full.score.json'
    sc_before_p = run_dir / f's{best}_full.score.json'
    sc_before = json.loads(sc_before_p.read_text(encoding='utf-8')) if sc_before_p.is_file() else {}
    sc_after = json.loads(sc_after_p.read_text(encoding='utf-8')) if sc_after_p.is_file() else {}

    from quick_score import judge_key
    kept = False
    if sc_before and sc_after and judge_key(sc_after) < judge_key(sc_before):
        kept = True
        active_routed = work_dir / 'active_routed.kicad_pcb'
        _copy_with_siblings(fb_dir / 's0_full.kicad_pcb', active_routed)
        st['best_placed_pcb'] = str(refined_placed)
        _push_snapshot(work_dir, st, f'feedback_s{best}', active_routed)
        _save_state(work_dir, st)

    print(json.dumps({
        'ok': True,
        'kept': kept,
        'hotspots': len(hot_refs),
        'corridor_pairs': fb.get('corridor_repulsion_pairs'),
        'before': {'blocking': sc_before.get('blocking'), 'joins_needed': sc_before.get('joins_needed'), 'quality': sc_before.get('quality')},
        'after': {'blocking': sc_after.get('blocking'), 'joins_needed': sc_after.get('joins_needed'), 'quality': sc_after.get('quality')},
    }, indent=2))
    return 0


def cmd_finish(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    in_pcb = Path(args.input_pcb).resolve() if args.input_pcb else Path(st['current_pcb'])
    out_pcb = Path(args.output_pcb).resolve() if args.output_pcb else (work_dir / 'delivered.kicad_pcb')
    drc_json = out_pcb.with_suffix('.drc.json')
    audit_json = out_pcb.with_suffix('.audit.json')

    # Auto-populate defaults from compiled constraints if present
    ground_net = args.ground_net or st.get('ground_net') or '/AGND'
    ep_refs = args.ep_refs or ''
    ep_vias = args.ep_vias
    power_nets = args.power_nets or ''
    if st.get('compiled'):
        cc_p = Path(st['compiled']['compiled_constraints'])
        rp_p = Path(st['compiled']['routing_policy'])
        if cc_p.is_file():
            cc = json.loads(cc_p.read_text(encoding='utf-8'))
            if not ep_refs:
                ep_refs = ','.join((cc.get('exposed_pads') or {}).get('refs') or ())
            if ep_vias is None:
                ep_vias = int((cc.get('exposed_pads') or {}).get('min_thermal_vias') or 4)
        if rp_p.is_file():
            rp = json.loads(rp_p.read_text(encoding='utf-8'))
            if not args.ground_net and rp.get('reference_plane_nets'):
                ground_net = rp['reference_plane_nets'][0]
            if not power_nets and rp.get('power_nets'):
                power_nets = ','.join(rp['power_nets'])
    if ep_vias is None:
        ep_vias = 4

    kpy = _find_kicad_python(args.kicad_python)
    kcli = _find_kicad_cli(args.kicad_cli)
    cmd = [
        kpy, str(KRT_ROOT / 'py_tools' / 'analog_finish.py'),
        str(in_pcb), str(out_pcb),
        '--ground-net', ground_net,
        '--ep-refs', ep_refs,
        '--ep-vias', str(ep_vias),
        '--same-net-smd-clearance', str(args.same_net_smd_clearance),
        '--perimeter-pitch', str(args.perimeter_pitch),
        '--kicad-cli', kcli,
        '--drc-json', str(drc_json),
        '--audit-json', str(audit_json),
    ]
    if power_nets:
        cmd += ['--power-nets', power_nets]
    if args.no_stubs:
        cmd.append('--no-stubs')

    t0 = time.time()
    r = _run_cmd(cmd, timeout_s=300)
    dt = round(time.time() - t0, 1)
    if r.returncode != 0 or not audit_json.is_file():
        print(json.dumps({
            'ok': False, 'stage': 'analog_finish', 'rc': r.returncode,
            'seconds': dt, 'stdout': r.stdout[-2000:], 'stderr': r.stderr[-1500:],
        }, indent=2))
        return 2

    audit = json.loads(audit_json.read_text(encoding='utf-8'))
    geom = _geometry_audit(out_pcb, Path(st['constraints_path']) if st.get('constraints_path') else None)

    _push_snapshot(work_dir, st, 'delivered_finish', out_pcb)
    st['delivered_pcb'] = str(out_pcb)
    _save_state(work_dir, st)

    drc = audit.get('drc') or {}
    bad_therm = audit.get('outer_bad_thermal_zones') or []
    p_stats = audit.get('power_net_stats') or {}
    p_min_w = {k: v.get('min_w') for k, v in p_stats.items()}
    gates = {
        'drc_zero_errors': drc.get('errors') == 0,
        'drc_zero_unconnected': drc.get('unconnected') == 0,
        'outer_thermal_relief_ok': len(bad_therm) == 0,
        'zero_non_ep_via_in_smd': audit.get('non_ep_via_in_smd') == 0,
    }
    print(json.dumps({
        'ok': all(gates.values()),
        'seconds': dt,
        'delivered_pcb': str(out_pcb),
        'drc_json': str(drc_json),
        'audit_json': str(audit_json),
        'gates': gates,
        'drc': drc,
        'total_copper_mm': audit.get('total_copper_mm'),
        'vias': audit.get('vias'),
        'perimeter_stitch_vias': audit.get('perimeter_stitch_vias'),
        'ep_via_counts': audit.get('ep_via_counts'),
        'outer_bad_thermal_zones': bad_therm,
        'outer_bad_thermal_zone_count': len(bad_therm),
        'non_ep_via_in_smd': audit.get('non_ep_via_in_smd'),
        'non_ep_via_grazing_0p30mm': audit.get('non_ep_via_grazing_0p30mm'),
        'max_decap_return_mm': audit.get('max_decap_return_mm'),
        'power_min_widths': p_min_w,
        'geometry_audit': geom,
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_audit(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    pcb_path = Path(args.pcb).resolve() if args.pcb else Path(st['current_pcb'])
    ground_net = args.ground_net or st.get('ground_net') or '/AGND'
    ep_refs = args.ep_refs or ''
    power_nets = args.power_nets or ''
    if st.get('compiled'):
        cc_p = Path(st['compiled']['compiled_constraints'])
        rp_p = Path(st['compiled']['routing_policy'])
        if cc_p.is_file() and not ep_refs:
            cc = json.loads(cc_p.read_text(encoding='utf-8'))
            ep_refs = ','.join((cc.get('exposed_pads') or {}).get('refs') or ())
        if rp_p.is_file():
            rp = json.loads(rp_p.read_text(encoding='utf-8'))
            if not args.ground_net and rp.get('reference_plane_nets'):
                ground_net = rp['reference_plane_nets'][0]
            if not power_nets and rp.get('power_nets'):
                power_nets = ','.join(rp['power_nets'])

    kpy = _find_kicad_python(args.kicad_python)
    kcli = _find_kicad_cli(args.kicad_cli)
    drc_json = pcb_path.with_suffix('.audit_drc.json')
    audit_json = pcb_path.with_suffix('.audit_only.json')
    cmd = [
        kpy, str(KRT_ROOT / 'py_tools' / 'analog_finish.py'),
        str(pcb_path), str(pcb_path), '--audit-only',
        '--ground-net', ground_net,
        '--ep-refs', ep_refs,
        '--kicad-cli', kcli,
        '--drc-json', str(drc_json),
        '--audit-json', str(audit_json),
    ]
    if power_nets:
        cmd += ['--power-nets', power_nets]
    r = _run_cmd(cmd, timeout_s=180)
    if r.returncode != 0 or not audit_json.is_file():
        print(json.dumps({'ok': False, 'stderr': r.stderr[-1500:]}))
        return 2
    audit = json.loads(audit_json.read_text(encoding='utf-8'))
    cpath = Path(args.constraints_json).resolve() if args.constraints_json else (
        Path(st['constraints_path']) if st.get('constraints_path') else None
    )
    audit['geometry_audit'] = _geometry_audit(pcb_path, cpath)
    print(json.dumps(audit, indent=2, ensure_ascii=False))
    return 0


def cmd_undo(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    st = _load_state(work_dir)
    snaps = st.get('snapshots') or []
    if len(snaps) < 2:
        print(json.dumps({'ok': False, 'error': 'Nothing to undo'}))
        return 1
    popped = snaps.pop()
    prev = snaps[-1]
    st['current_pcb'] = prev['pcb']
    _save_state(work_dir, st)
    print(json.dumps({'ok': True, 'reverted_from': popped['label'], 'restored': prev}, indent=2))
    return 0


def cmd_status(args) -> int:
    work_dir = Path(args.work_dir).resolve()
    print(json.dumps(_load_state(work_dir), indent=2, ensure_ascii=False))
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description='AI4PCB closed-loop agent harness CLI over KRT fork')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('init', help='Initialize sandbox workspace and optionally compile board.yaml')
    p.add_argument('work_dir')
    p.add_argument('pcb')
    p.add_argument('--board-yaml', default=None)
    p.add_argument('--lock', default=None, help='comma-separated refs to lock before unplacing')
    p.add_argument('--unplace', action='store_true', help='strip copper and unplace movable parts')
    p.add_argument('--ground-net', default='/AGND')
    p.add_argument('--same-net-pad-clearance', type=float, default=0.30)
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('overview', help='Compact board & compiled constraints summary')
    p.add_argument('work_dir')
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser('place', help='Validate groups/intent, run multi-seed diverse placement, and run early courtyard DRC')
    p.add_argument('work_dir')
    p.add_argument('--groups', required=True)
    p.add_argument('--intent', required=True)
    p.add_argument('--constraints-json', default=None)
    p.add_argument('--scattered', type=int, default=0)
    p.add_argument('--clustered', type=int, default=0)
    p.add_argument('--anchored', type=int, default=12)
    p.add_argument('--judge', type=int, default=6)
    p.add_argument('--diverse', choices=('off', 'auto', 'farthest', 'medoid'), default='auto')
    p.add_argument('--diverse-cost-cap', type=float, default=1.20)
    p.add_argument('--macro-flow', action='store_true')
    p.add_argument('--local-radius', type=float, default=None)
    p.add_argument('--moves', type=int, default=None)
    p.add_argument('--jobs', type=int, default=6)
    p.add_argument('--no-check-courtyard', dest='check_courtyard', action='store_false', default=True)
    p.add_argument('--skip-validate', action='store_true')
    p.add_argument('--kicad-cli', default=None)
    p.set_defaults(func=cmd_place)

    p = sub.add_parser('judge', help='Parallel multi-seed routing + --confirm close full routing (zero lobotomy)')
    p.add_argument('work_dir')
    p.add_argument('--judge-json', default=None)
    p.add_argument('--chain', default=None)
    p.add_argument('--intent', default=None)
    p.add_argument('--seeds', default=None)
    p.add_argument('--diverse', choices=('off', 'auto', 'farthest', 'medoid'), default='off')
    p.add_argument('--diverse-count', type=int, default=None)
    p.add_argument('--confirm', default='close')
    p.add_argument('--jobs', type=int, default=6)
    p.add_argument('--no-diff-pair-warn', action='store_true')
    p.set_defaults(func=cmd_judge)

    p = sub.add_parser('feedback-refine', help='Congestion/failure-heatmap route-feedback local SA + keep-if-better re-route')
    p.add_argument('work_dir')
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--corridor-gap', type=float, default=2.8)
    p.add_argument('--local-radius', type=float, default=8.0)
    p.add_argument('--moves', type=int, default=10000)
    p.add_argument('--max-hotspots', type=int, default=28)
    p.set_defaults(func=cmd_feedback_refine)

    p = sub.add_parser('finish', help='Run deterministic analog_finish.py (thermal relief, EP/escape/perimeter vias, DRC)')
    p.add_argument('work_dir')
    p.add_argument('--input-pcb', default=None)
    p.add_argument('--output-pcb', default=None)
    p.add_argument('--ground-net', default=None)
    p.add_argument('--ep-refs', default=None)
    p.add_argument('--ep-vias', type=int, default=None)
    p.add_argument('--power-nets', default=None)
    p.add_argument('--same-net-smd-clearance', type=float, default=0.25)
    p.add_argument('--perimeter-pitch', type=float, default=15.0)
    p.add_argument('--no-stubs', action='store_true')
    p.add_argument('--kicad-python', default=None)
    p.add_argument('--kicad-cli', default=None)
    p.set_defaults(func=cmd_finish)

    p = sub.add_parser('audit', help='Zero-mutation fresh KiCad DRC + DFM + geometry audit')
    p.add_argument('work_dir')
    p.add_argument('--pcb', default=None)
    p.add_argument('--constraints-json', default=None)
    p.add_argument('--ground-net', default=None)
    p.add_argument('--ep-refs', default=None)
    p.add_argument('--power-nets', default=None)
    p.add_argument('--kicad-python', default=None)
    p.add_argument('--kicad-cli', default=None)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser('undo', help='Revert to previous board snapshot')
    p.add_argument('work_dir')
    p.set_defaults(func=cmd_undo)

    p = sub.add_parser('status', help='Print session state JSON')
    p.add_argument('work_dir')
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
