#!/usr/bin/env python3
"""Architecture-independent Yosys -> OpenSTA -> CSV/XLSX experiment runner.
Run from anywhere: python3 scripts/automate.py --config experiments/adder8.json
Requires: Python 3.8+, openpyxl; installed Yosys and OpenSTA.
"""
import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from feature_extract import all_features

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'results'
DATASET = RESULTS / 'dataset.csv'
XLSX = RESULTS / 'dataset.xlsx'
RUNS = RESULTS / 'runs'
PATH_DATASET = RESULTS / 'timing_paths.csv'
PATH_GROUPS = ('reg_to_reg', 'input_to_reg', 'reg_to_output', 'input_to_output')
MAX_PATHS_PER_GROUP = 20
BASE_COLUMNS = ['run_id','timestamp_utc','design','rtl_sha256','top','parameters_json',
                'clock_mode','clock_port','clock_period_ns','clock_uncertainty_ns',
                'input_delay_max_ns','input_delay_min_ns','output_delay_max_ns',
                'output_delay_min_ns','input_transition_ns','output_load_pf',
                'liberty_sha256','architecture','design_width_bits','mapped_cell_count','cell_area_library_units',
                'critical_path_data_arrival_ns','worst_slack_ns','timing_status',
                'run_directory','critical_path_startpoint','critical_path_endpoint',
                'critical_path_required_time_ns','target_review_status',
                'reg_to_reg_worst_slack_ns','reg_to_reg_arrival_ns',
                'reg_to_reg_required_ns','reg_to_reg_startpoint','reg_to_reg_endpoint',
                'input_to_reg_worst_slack_ns','input_to_reg_arrival_ns',
                'input_to_reg_required_ns','input_to_reg_startpoint','input_to_reg_endpoint',
                'reg_to_output_worst_slack_ns','reg_to_output_arrival_ns',
                'reg_to_output_required_ns','reg_to_output_startpoint','reg_to_output_endpoint',
                'input_to_output_worst_slack_ns','input_to_output_arrival_ns',
                'input_to_output_required_ns','input_to_output_startpoint','input_to_output_endpoint']


def q(s):
    """Tcl brace-quote simple paths/names (reject braces and newlines)."""
    s = str(s)
    if any(x in s for x in '{}\n\r'):
        raise ValueError('Unsupported Tcl-special character in path or name: '+s)
    return '{'+s+'}'



def yq(s):
    """Quote a simple Yosys command argument (not Tcl brace quoting)."""
    s = str(s)
    if '"' in s or chr(10) in s or chr(13) in s or chr(92) in s:
        raise ValueError('Unsupported character in Yosys argument: ' + s)
    return '"' + s + '"'

def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(args, log):
    with open(log, 'w') as f:
        p = subprocess.run(args, cwd=str(ROOT), stdout=f, stderr=subprocess.STDOUT,
                           text=True, check=False)
    if p.returncode:
        raise RuntimeError(f'Command failed (exit {p.returncode}): {args}. See {log}')


def require(pattern, text, label, flags=0):
    m = re.search(pattern, text, flags)
    if not m:
        raise ValueError(f'Missing {label}; inspect report')
    return m


def parse_yosys(log, liberty):
    # Get final top section. Restrict to the last statistics block before the backend.
    i = log.rfind('=== top ===')
    if i < 0:
        raise ValueError('Yosys top statistics missing')
    section = log[i:]
    area = float(require(r"Chip area for module\s+['\\]?[^\s:]+:\s*([\d.eE+-]+)", section, 'chip area').group(1))
    count = int(require(r'Number of cells:\s*(\d+)', section, 'cell count').group(1))
    section = section[:section.find('Chip area for module')]
    types = {}
    after_cells = section.split('Number of cells:',1)[1].split('\n',1)[1]
    for line in after_cells.splitlines():
        m = re.match(r'^\s+(\S+)\s+(\d+)\s*$', line)
        if m:
            types[m.group(1)] = int(m.group(2))
    if sum(types.values()) != count:
        raise ValueError(f'Yosys mapped cell mismatch: {count} vs {sum(types.values())}')
    # Ensure every counted type is a real library cell, not a generic $_... gate.
    libcells = set(re.findall(r'\bcell\s*\(\s*"?([^"\s)]+)"?\s*\)', liberty))
    unknown = sorted(set(types)-libcells)
    if unknown:
        raise ValueError(f'Unmapped/unknown cell types: {unknown}')
    return count, area, types


def parse_path_blocks(report):
    """Parse full OpenSTA max-path blocks, without mistaking slack arithmetic for arrival."""
    paths = []
    for block in re.split(r'(?=^Startpoint:)', report, flags=re.M):
        if not block.startswith('Startpoint:'):
            continue
        if not re.search(r'^Path Type:\s*max\s*$', block, re.M | re.I):
            continue
        start = re.search(r'^Startpoint:\s*(.+)$', block, re.M)
        end = re.search(r'^Endpoint:\s*(.+)$', block, re.M)
        arrival_matches = re.findall(r'^\s*([+-]?\d+(?:\.\d+)?)\s+data arrival time\s*$', block, re.M)
        required_matches = re.findall(r'^\s*([+-]?\d+(?:\.\d+)?)\s+data required time\s*$', block, re.M)
        slack_matches = re.findall(r'^\s*([+-]?\d+(?:\.\d+)?)\s+slack\s+\((MET|VIOLATED)\)\s*$', block, re.M | re.I)
        if not (start and end and arrival_matches and required_matches and slack_matches):
            raise ValueError('Incomplete max timing path in STA report; inspect sta.log')
        arrival, required = float(arrival_matches[0]), float(required_matches[0])
        slack = float(slack_matches[0][0])
        if abs((required - arrival) - slack) > 0.002:
            raise ValueError('STA path arithmetic inconsistent; inspect sta.log')
        paths.append({'startpoint': start.group(1).strip(),
                      'endpoint': end.group(1).strip(),
                      'arrival': arrival, 'required': required, 'slack': slack,
                      'status': 'VIOLATED' if slack < 0 else 'MET'})
    return paths


def parse_sta_groups(log):
    """Return worst overall max path and the reported paths in each endpoint category."""
    segments = re.split(r'^=== STA_GROUP:([a-z_]+) ===\s*$', log, flags=re.M)
    if len(segments) != 2 * (len(PATH_GROUPS) + 1) + 1:
        raise ValueError('STA report group markers missing; inspect sta.log')
    grouped = {}
    for idx in range(1, len(segments), 2):
        name = segments[idx]
        if name in grouped:
            raise ValueError('Duplicate STA report group: ' + name)
        grouped[name] = parse_path_blocks(segments[idx + 1])
    if set(grouped) != {'overall', *PATH_GROUPS}:
        raise ValueError('Unexpected STA report groups: ' + str(sorted(grouped)))
    if not grouped['overall']:
        raise ValueError('No constrained maximum-delay paths in OpenSTA report')
    overall = min(grouped['overall'], key=lambda p: p['slack'])
    return overall, grouped


def make_sdc(cfg, ports):
    mode = cfg['clock_mode']
    period = float(cfg['clock_period_ns'])
    if period <= 0:
        raise ValueError('Clock period must be positive')
    clk = cfg.get('clock_port', 'clk')
    if mode == 'port':
        if clk not in ports['inputs']:
            raise ValueError(f'Clock port {clk!r} not found among RTL inputs')
        clock = f'create_clock -name core_clk -period {period} [get_ports {q(clk)}]'
        data_inputs = [p for p in ports['inputs'] if p != clk]
    elif mode == 'virtual':
        clock = f'create_clock -name core_clk -period {period}'
        data_inputs = ports['inputs']
    else:
        raise ValueError('clock_mode must be port or virtual')
    def val(k):
        return float(cfg[k])
    lines = [clock, f'set_clock_uncertainty {val("clock_uncertainty_ns")} [get_clocks core_clk]']
    if data_inputs:
        names = ' '.join(q(p) for p in data_inputs)
        lines += [f'set data_inputs [get_ports [list {names}]]',
                  f'set_input_delay -clock core_clk -max {val("input_delay_max_ns")} $data_inputs',
                  f'set_input_delay -clock core_clk -min {val("input_delay_min_ns")} $data_inputs',
                  f'set_input_transition {val("input_transition_ns")} $data_inputs']
    if ports['outputs']:
        names = ' '.join(q(p) for p in ports['outputs'])
        lines += [f'set data_outputs [get_ports [list {names}]]',
                  f'set_output_delay -clock core_clk -max {val("output_delay_max_ns")} $data_outputs',
                  f'set_output_delay -clock core_clk -min {val("output_delay_min_ns")} $data_outputs',
                  f'set_load {val("output_load_pf")} $data_outputs']
    return '\n'.join(lines) + '\n'


def get_ports_from_config(cfg):
    # Explicit port metadata avoids unreliable regex parsing of arbitrary Verilog/SystemVerilog.
    ports = cfg['ports']
    if not isinstance(ports.get('inputs'),list) or not isinstance(ports.get('outputs'),list):
        raise ValueError('ports.inputs and ports.outputs must be arrays')
    for p in ports['inputs']+ports['outputs']:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', p):
            raise ValueError('Only simple Verilog port identifiers supported: '+p)
    return ports


def make_yosys(cfg, mapped, liberty):
    rtl = cfg['rtl_files']

    if not rtl:
        raise ValueError('rtl_files must be nonempty')

    top = cfg['top']

    # Validate the module name
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', top):
        raise ValueError('Invalid top module name')

    lines = [
        'read_verilog -sv ' +
        ' '.join(yq(str((ROOT / p).resolve())) for p in rtl)
    ]

    for k, v in cfg.get('parameters', {}).items():
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', k):
            raise ValueError('Invalid parameter name')

        if not isinstance(v, int):
            raise ValueError('Only integer parameters are supported')

        lines.append(f'chparam -set {k} {v} {top}')

    lines += [
        f'hierarchy -check -top {top}',
        f'synth -top {top} -noabc',
        f'dfflibmap -liberty {yq(liberty)}',
        f'abc -liberty {yq(liberty)}',
        'clean',
        f'stat -liberty {yq(liberty)}',
        f'write_verilog -noattr -noexpr {yq(mapped)}',
        f'write_json {yq(str(Path(mapped).with_suffix(".json")))}'
    ]

    return '\n'.join(lines) + '\n'


def make_sta(cfg, mapped, liberty, sdc):
    """Report overall and four distinct startpoint/endpoint classes.

    Uses -group_path_count to request up to MAX_PATHS_PER_GROUP paths per group.
    A category with no physical paths is legitimately empty.
    """
    common = f'report_checks -path_delay max -format full_clock_expanded -digits 4 -group_path_count {MAX_PATHS_PER_GROUP}'
    lines = [f'read_liberty {q(liberty)}', f'read_verilog {q(mapped)}',
             f'link_design {q(cfg["top"])}', f'read_sdc {q(sdc)}',
             'report_units', 'puts "=== STA_GROUP:overall ==="', common]
    # -clock_pins and -data_pins select the launch and capture register pins.
    # The input ports were already resolved in make_sdc as $data_inputs.
    reg_launch = '[all_registers -clock_pins]'
    reg_capture = '[all_registers -data_pins]'
    data_in = '$data_inputs'
    out = '[all_outputs]'
    queries = {
        'reg_to_reg': (reg_launch, reg_capture, True),
        'input_to_reg': (data_in, reg_capture, cfg.get('clock_mode') == 'virtual' or
                         any(p != cfg.get('clock_port', 'clk') for p in cfg['ports']['inputs'])),
        'reg_to_output': (reg_launch, out, bool(cfg['ports']['outputs'])),
        'input_to_output': (data_in, out, bool(cfg['ports']['outputs']) and
                            (cfg.get('clock_mode') == 'virtual' or
                             any(p != cfg.get('clock_port', 'clk') for p in cfg['ports']['inputs'])))
    }
    for group in PATH_GROUPS:
        frm, to, enabled = queries[group]
        lines.append(f'puts "=== STA_GROUP:{group} ==="')
        if enabled:
            lines.append(f'{common} -from {frm} -to {to}')
        else:
            lines.append('puts "No applicable ports for this category"')
    lines += ['report_worst_slack -max -digits 4', 'report_wns -max -digits 4',
              'report_tns -max -digits 4', 'exit', '']
    return '\n'.join(lines)


def save_paths(run_id, grouped, rundir):
    """Save a per-run JSON report and append one row per reported path to timing_paths.csv."""
    (rundir / 'timing_paths.json').write_text(json.dumps(grouped, indent=2) + '\n')
    columns = ['run_id', 'path_group', 'path_rank', 'startpoint', 'endpoint',
               'arrival_ns', 'required_ns', 'slack_ns', 'timing_status']
    new = []
    for group, paths in grouped.items():
        for rank, p in enumerate(sorted(paths, key=lambda x: x['slack']), 1):
            new.append({'run_id': run_id, 'path_group': group, 'path_rank': rank,
                        'startpoint': p['startpoint'], 'endpoint': p['endpoint'],
                        'arrival_ns': p['arrival'], 'required_ns': p['required'],
                        'slack_ns': p['slack'], 'timing_status': p['status']})
    old = []
    if PATH_DATASET.exists():
        with PATH_DATASET.open(newline='') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != columns:
                raise ValueError('Existing timing_paths.csv has incompatible columns')
            old = list(reader)
    if any(r['run_id'] == run_id for r in old):
        raise ValueError('Duplicate run_id in timing_paths.csv')
    fd, tmp = tempfile.mkstemp(prefix='.timing_paths_', suffix='.csv', dir=RESULTS)
    try:
        with os.fdopen(fd, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=columns)
            writer.writeheader()
            writer.writerows(old + new)
        os.replace(tmp, PATH_DATASET)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def update_dataset(row):
    RESULTS.mkdir(parents=True, exist_ok=True)
    existing = []
    fields = list(BASE_COLUMNS)
    if DATASET.exists():
        with DATASET.open(newline='') as f:
            reader = csv.DictReader(f)
            existing = list(reader)
            fields = list(reader.fieldnames or fields)
    if existing and 'run_id' not in fields:
        raise ValueError('Existing CSV uses legacy schema without run_id; back it up and rename before running')
    for old in existing:
        old.setdefault('target_review_status', 'legacy_unverified')
    if row['run_id'] in {r['run_id'] for r in existing}:
        raise ValueError('Duplicate run_id; refusing to append')
    for key in list(BASE_COLUMNS)+sorted(row):
        if key not in fields: fields.append(key)
    # Consistent zero for absent cell types; no missing values for count columns.
    for r in existing+[row]:
        for field in fields:
            if field.startswith('count_') and field not in r:
                r[field] = 0
    existing.append(row)
    fd,tmp = tempfile.mkstemp(prefix='.dataset_',suffix='.csv',dir=RESULTS)
    try:
        with os.fdopen(fd,'w',newline='') as f:
            writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore')
            writer.writeheader(); writer.writerows(existing)
        os.replace(tmp,DATASET)
    finally:
        if os.path.exists(tmp): os.unlink(tmp)
    return fields,existing


def export_excel(fields, rows):
    """Export every dataset feature, plus focused gate and timing worksheets.

    No columns are removed from All Measurements. The additional sheets are
    alternate views of the same CSV values, not independently calculated data.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.worksheet.table import Table, TableStyleInfo
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    overview = wb.active
    overview.title = 'Run Overview'
    full = wb.create_sheet('All Measurements')
    gates = wb.create_sheet('Gate Composition')
    timing = wb.create_sheet('Timing Paths')
    dictionary = wb.create_sheet('Feature Dictionary')

    NAVY = '183153'
    LIGHT = 'EAF3F6'
    ALT = 'F3F7FA'
    BLUE = '16718A'

    def style_sheet(ws, headers, data, *, title=None, widths=None):
        """Render a full-width, filtered, typed data table with readable columns."""
        ws.sheet_view.showGridLines = False
        ws.sheet_properties.pageSetUpPr.fitToPage = True
        header_row = 1
        if title:
            ws.append([title])
            ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
            c = ws.cell(1, 1)
            c.fill = PatternFill('solid', fgColor=NAVY)
            c.font = Font(name='Aptos Display', size=16, bold=True, color='FFFFFF')
            c.alignment = Alignment(vertical='center')
            ws.row_dimensions[1].height = 36
            header_row = 2
        ws.append(headers)
        ws.row_dimensions[header_row].height = 44
        for c in ws[header_row]:
            c.fill = PatternFill('solid', fgColor=NAVY)
            c.font = Font(name='Aptos', size=10, bold=True, color='FFFFFF')
            c.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
        for record in data:
            ws.append([typed(record.get(k, ''), k) for k in headers])
        ws.freeze_panes = f'D{header_row+1}' if len(headers) >= 4 else f'A{header_row+1}'
        ws.auto_filter.ref = f'A{header_row}:{get_column_letter(len(headers))}{max(header_row, ws.max_row)}'
        for j, key in enumerate(headers, 1):
            letter = get_column_letter(j)
            values = [str(record.get(key, '') or '') for record in data]
            longest = max([len(key)] + [len(v) for v in values])
            # Excel does not support a true auto-fit flag in generated XLSX;
            # estimate readable widths from both headers and cell contents.
            if key.startswith('count_') or key.endswith('_path_count_reported'):
                width = max(16, min(27, len(key) + 2))
            elif key in ('run_id', 'rtl_sha256', 'liberty_sha256'):
                width = 25
            elif key in ('run_directory', 'parameters_json'):
                width = 39
            elif key.endswith(('startpoint', 'endpoint')):
                width = 34
            else:
                width = max(15, min(42, longest + 3))
            if widths and key in widths:
                width = widths[key]
            ws.column_dimensions[letter].width = width
        for row_cells in ws.iter_rows(min_row=header_row + 1):
            for c in row_cells:
                c.alignment = Alignment(vertical='center', horizontal='right' if isinstance(c.value, (int, float)) else 'left')
                if isinstance(c.value, float):
                    c.number_format = '0.0000'
                if c.row % 2 == 0:
                    c.fill = PatternFill('solid', fgColor=ALT)
        if data:
            ws.auto_filter.ref = f'A{header_row}:{get_column_letter(len(headers))}{ws.max_row}'
        return ws

    overview_keys = [k for k in (
        'run_id', 'design', 'architecture', 'design_width_bits',
        'clock_period_ns', 'mapped_cell_count', 'cell_area_library_units',
        'critical_path_startpoint', 'critical_path_endpoint',
        'critical_path_data_arrival_ns', 'critical_path_required_time_ns',
        'worst_slack_ns', 'reg_to_reg_arrival_ns',
        'reg_to_reg_worst_slack_ns', 'timing_status', 'target_review_status'
    ) if k in fields]
    style_sheet(overview, overview_keys, rows, title='DESIGN AND TIMING | RUN OVERVIEW')

    # This sheet is authoritative: all CSV columns, in original order.
    style_sheet(full, fields, rows, title='COMPLETE EXPERIMENT DATA | NO FIELDS OMITTED')

    gate_keys = [k for k in ('run_id', 'design', 'architecture', 'design_width_bits',
                              'mapped_cell_count', 'cell_area_library_units') if k in fields]
    gate_keys += sorted(k for k in fields if k.startswith('count_'))
    style_sheet(gates, gate_keys, rows, title='STANDARD-CELL COMPOSITION | ALL GATE AND FF COUNTS')

    timing_keys = [k for k in (
        'run_id', 'design', 'clock_period_ns', 'clock_uncertainty_ns',
        'input_delay_max_ns', 'input_delay_min_ns', 'output_delay_max_ns',
        'output_delay_min_ns', 'input_transition_ns', 'output_load_pf',
        'critical_path_startpoint', 'critical_path_endpoint',
        'critical_path_data_arrival_ns', 'critical_path_required_time_ns',
        'worst_slack_ns', 'timing_status',
    ) if k in fields]
    for group in PATH_GROUPS:
        timing_keys.extend(k for k in (
            f'{group}_path_count_reported', f'{group}_startpoint',
            f'{group}_endpoint', f'{group}_arrival_ns',
            f'{group}_required_ns', f'{group}_worst_slack_ns'
        ) if k in fields)
    style_sheet(timing, timing_keys, rows, title='CRITICAL PATHS | OVERALL AND ALL FOUR PATH CLASSES')

    dictionary.append(['ROLE', 'COLUMN', 'DESCRIPTION'])
    definitions = {
        'mapped_cell_count': ('SYNTHESIS FEATURE', 'Total mapped standard cells'),
        'cell_area_library_units': ('SYNTHESIS FEATURE', 'Mapped cell area in Liberty area units'),
        'design_width_bits': ('DESIGN FEATURE', 'Width supplied by RTL parameters'),
        'architecture': ('DESIGN FEATURE', 'Architecture label supplied by experiment configuration'),
        'critical_path_data_arrival_ns': ('STA DIAGNOSTIC', 'Arrival time including timing-reference effects; not pure logic delay'),
        'critical_path_required_time_ns': ('STA DIAGNOSTIC', 'Required time for overall worst reported setup path'),
        'worst_slack_ns': ('TARGET', 'Worst overall setup slack under the specified constraints'),
        'timing_status': ('DERIVED TARGET', 'MET/VIOLATED from setup slack'),
        'target_review_status': ('QUALITY', 'Parser verification / legacy status'),
    }
    for key in fields:
        if key.startswith('count_'):
            role, desc = 'SYNTHESIS FEATURE', 'Mapped count of standard-cell type ' + key[6:]
        elif key.startswith(('reg_to_reg_', 'input_to_reg_', 'reg_to_output_', 'input_to_output_')):
            role, desc = 'STA PATH METRIC', 'Timing-path metric for ' + key.split('_to_')[0] + ' path category; blank when unavailable'
        elif key in definitions:
            role, desc = definitions[key]
        elif key in ('run_id', 'timestamp_utc', 'rtl_sha256', 'liberty_sha256', 'run_directory'):
            role, desc = 'METADATA', 'Experiment tracking / provenance; do not use as a numerical ML feature'
        elif key.endswith('_ns') or key == 'output_load_pf':
            role, desc = 'CONSTRAINT', 'Timing environment or interface constraint'
        elif key.startswith('critical_path_'):
            role, desc = 'STA DIAGNOSTIC', 'Overall worst reported timing-path detail'
        else:
            role, desc = 'DESIGN / METADATA', 'Original field preserved from dataset.csv'
        dictionary.append([role, key, desc])
    dictionary.sheet_view.showGridLines = False
    dictionary.freeze_panes = 'A2'
    dictionary.auto_filter.ref = f'A1:C{dictionary.max_row}'
    for c in dictionary[1]:
        c.fill = PatternFill('solid', fgColor=NAVY)
        c.font = Font(name='Aptos', color='FFFFFF', bold=True)
    dictionary.column_dimensions['A'].width = 24
    dictionary.column_dimensions['B'].width = 43
    dictionary.column_dimensions['C'].width = 92
    for row_cells in dictionary.iter_rows(min_row=2):
        if row_cells[0].row % 2 == 0:
            for c in row_cells:
                c.fill = PatternFill('solid', fgColor=ALT)

    fd, tmp = tempfile.mkstemp(prefix='.dataset_', suffix='.xlsx', dir=RESULTS)
    os.close(fd)
    try:
        wb.save(tmp)
        os.replace(tmp, XLSX)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def refresh_excel_only():
    """Regenerate Excel from existing dataset.csv without running EDA or appending a run."""
    if not DATASET.exists():
        raise FileNotFoundError(f'No dataset CSV found: {DATASET}')
    with DATASET.open(newline='') as f:
        reader = csv.DictReader(f)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if not fields:
        raise ValueError('CSV has no headers')
    export_excel(fields, rows)
    print(f'Refreshed Excel from {len(rows)} existing CSV rows: {XLSX}')


def typed(value, key):
    if value is None or value == '':
        return None
    if key.startswith('count_') or key.endswith('_path_count_reported') or key in ('mapped_cell_count', 'design_width_bits'):
        return int(value)
    if key.endswith('_ns') or key in ('output_load_pf', 'cell_area_library_units'):
        return float(value)
    return value


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--config',help='JSON experiment configuration')
    ap.add_argument('--refresh-excel',action='store_true',help='Rebuild XLSX from existing dataset.csv only')
    ap.add_argument('--yosys',default='yosys')
    ap.add_argument('--sta',default=str(Path.home()/'OpenSTA-new/build/sta'))
    args=ap.parse_args()
    if args.refresh_excel:
        refresh_excel_only()
        return
    if not args.config:
        ap.error('--config is required unless --refresh-excel is used')
    cfg=json.loads(Path(args.config).read_text())
    ports=get_ports_from_config(cfg)
    liberty=(ROOT / cfg['liberty']).resolve()
    if not liberty.is_file(): raise FileNotFoundError(liberty)
    libtext=liberty.read_text(errors='replace')
    run_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_'+uuid.uuid4().hex[:8]
    rundir=RUNS/run_id
    rundir.mkdir(parents=True,exist_ok=False)
    (rundir/'config.json').write_text(json.dumps(cfg,indent=2)+'\n')
    try:
        mapped=rundir/'mapped.v'; sdc=rundir/'constraints.sdc'
        synth=rundir/'synth.ys'; timing=rundir/'timing.tcl'
        sdc.write_text(make_sdc(cfg,ports))
        synth.write_text(make_yosys(cfg,mapped,liberty))
        command([args.yosys,'-s',str(synth)],rundir/'yosys.log')
        if not mapped.exists() or not mapped.stat().st_size:
            raise ValueError('Yosys did not produce a mapped netlist')
        count,area,types=parse_yosys((rundir/'yosys.log').read_text(errors='replace'),libtext)
        timing.write_text(make_sta(cfg,mapped,liberty,sdc))
        command([args.sta,'-no_splash','-exit',str(timing)],rundir/'sta.log')
        sta_log=(rundir/'sta.log').read_text(errors='replace')
        if re.search(r'(^|\n)\s*(Error:|Warning:.*(unconstrained|not found))',sta_log,re.I):
            raise ValueError('STA emitted an error or critical warning; inspect sta.log')
        path, grouped_paths = parse_sta_groups(sta_log)
        arrival,slack,status=path['arrival'],path['slack'],path['status']
        rtl_hash=hashlib.sha256(''.join(sha(ROOT/p) for p in cfg['rtl_files']).encode()).hexdigest()
        row={'run_id':run_id,'timestamp_utc':dt.datetime.now(dt.timezone.utc).isoformat(),
             'design':cfg['design'],'rtl_sha256':rtl_hash,'top':cfg['top'],
             'parameters_json':json.dumps(cfg.get('parameters',{}),sort_keys=True),
             'clock_mode':cfg['clock_mode'],'clock_port':cfg.get('clock_port',''),
             'liberty_sha256':sha(liberty),
             'architecture':cfg.get('architecture',cfg['design']),
             'design_width_bits':cfg.get('parameters',{}).get('WIDTH',''),
             'mapped_cell_count':count,
             'cell_area_library_units':area,'critical_path_data_arrival_ns':arrival,
             'worst_slack_ns':slack,'timing_status':status,
             'critical_path_startpoint':path['startpoint'],
             'critical_path_endpoint':path['endpoint'],
             'critical_path_required_time_ns':path['required'],
             'target_review_status':'parsed_complete_sta_path',
             'run_directory':str(rundir.relative_to(ROOT))}
        for k in ['clock_period_ns','clock_uncertainty_ns','input_delay_max_ns',
                  'input_delay_min_ns','output_delay_max_ns','output_delay_min_ns',
                  'input_transition_ns','output_load_pf']:
            row[k]=cfg[k]
        for group in PATH_GROUPS:
            row[f'{group}_path_count_reported'] = len(grouped_paths[group])
            if grouped_paths[group]:
                worst = min(grouped_paths[group], key=lambda p: p['slack'])
                row[f'{group}_worst_slack_ns'] = worst['slack']
                row[f'{group}_arrival_ns'] = worst['arrival']
                row[f'{group}_required_ns'] = worst['required']
                row[f'{group}_startpoint'] = worst['startpoint']
                row[f'{group}_endpoint'] = worst['endpoint']
        for cell,n in types.items(): row['count_'+cell]=n
        row.update(all_features(cfg, liberty, mapped.with_suffix('.json')))
        fields,rows=update_dataset(row)
        save_paths(run_id, grouped_paths, rundir)
        try:
            export_excel(fields,rows)
        except Exception as exc:
            print(f'WARNING: CSV saved, but Excel refresh failed: {exc}',file=sys.stderr)
        (rundir/'status.json').write_text(json.dumps({'status':'success','run_id':run_id},indent=2))
        print(f'SUCCESS {run_id}: {count} cells, area={area}, arrival={arrival} ns, slack={slack} ns')
        print(f'CSV: {DATASET}\nPath CSV: {PATH_DATASET}\nExcel: {XLSX}\nReports: {rundir}')
    except Exception as exc:
        (rundir/'status.json').write_text(json.dumps({'status':'failed','error':str(exc)},indent=2))
        print(f'FAILED {run_id}: {exc}\nReports: {rundir}',file=sys.stderr)
        raise

if __name__=='__main__':
    main()