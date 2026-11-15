#!/usr/bin/env python3
"""RTL metadata, Liberty, and mapped Yosys JSON structural features (pre-STA).

Graph metrics use mapped cell pin directions resolved from Yosys cell instances or
from the referenced library module definitions. Clock/reset/control pins are
excluded from combinational paths; flip-flop Q/QN and D define RR boundaries.
"""
import json
import re
import statistics
from collections import defaultdict, deque
from pathlib import Path


def technology_features(liberty_path):
    s = Path(liberty_path).read_text(errors='replace')
    def number(name):
        m = re.search(r'\b' + re.escape(name) + r'\s*:\s*([-+\d.eE]+)', s)
        return float(m.group(1)) if m else None
    def label(name):
        m = re.search(r'\b' + re.escape(name) + r'\s*:\s*["\']?([\w.+-]+)', s)
        return m.group(1) if m else ''
    m = re.search(r'\blibrary\s*\(\s*["\']?([^\s)"\']+)', s)
    cells = re.findall(r'\bcell\s*\(\s*["\']?([^\s)"\']+)', s)
    # capacitive_load_unit is a parenthesized Liberty attribute, not a colon attribute.
    cap = re.search(r'\bcapacitive_load_unit\s*\(\s*([^)]*)\)', s)
    return {
        'tech_library_name': m.group(1) if m else '',
        'tech_nom_voltage_v': number('nom_voltage'),
        'tech_nom_temperature_c': number('nom_temperature'),
        'tech_cell_type_count': len(set(cells)),
        'tech_time_unit': label('time_unit'),
        'tech_capacitive_load_unit': cap.group(1).strip() if cap else label('capacitive_load_unit'),
    }


def _is_sequential(typ):
    return bool(re.search(r'(?:^|_)(?:S?DFF|DFFE|LATCH|DLH|DLL|SDFFE|DFFSR)', typ, re.I))


def _directions(cell, modules):
    dirs = cell.get('port_directions') or {}
    if dirs:
        return dirs
    definition = modules.get(cell.get('type', ''), {})
    return {p: info.get('direction') for p, info in definition.get('ports', {}).items()}


def _liberty_pin_directions(liberty_path):
    """Parse cell/pin direction using balanced Liberty braces, including nested timing blocks."""
    source = Path(liberty_path).read_text(errors='replace')
    cell_re = re.compile(r'\bcell\s*\(\s*["\']?([^\s)"\']+)["\']?\s*\)\s*\{')
    pin_re = re.compile(r'\bpin\s*\(\s*["\']?([^\s)"\']+)["\']?\s*\)\s*\{')
    def body(start):
        level = 1
        i = start
        while i < len(source) and level:
            if source[i] == '{': level += 1
            elif source[i] == '}': level -= 1
            i += 1
        return i
    result = {}
    for match in cell_re.finditer(source):
        end = body(match.end())
        pins = {}
        for pm in pin_re.finditer(source, match.end(), end):
            pe = body(pm.end())
            d = re.search(r'\bdirection\s*:\s*(input|output|inout)\b', source[pm.end():pe])
            if d: pins[pm.group(1)] = d.group(1)
        result[match.group(1)] = pins
    return result


def netlist_features(path, top, liberty_path=None):
    design = json.loads(Path(path).read_text())
    modules = design['modules']
    if top not in modules:
        raise ValueError('Top not in Yosys JSON: ' + top)
    module = modules[top]
    cells = module.get('cells', {})
    def nets(bits):
        return {b for b in bits if isinstance(b, int)}

    drivers = defaultdict(set)
    consumers = defaultdict(set)
    comb = {}
    sequential = {}
    unresolved = []
    libdirs = _liberty_pin_directions(liberty_path) if liberty_path else {}
    for name, cell in cells.items():
        connections = cell.get('connections', {})
        dirs = _directions(cell, modules) or libdirs.get(cell.get('type', ''), {})
        if any(p not in dirs or dirs[p] not in ('input', 'output', 'inout') for p in connections):
            unresolved.append((name, cell.get('type', ''), sorted(set(connections) - set(dirs))))
            continue
        ins = set().union(*(nets(bs) for p, bs in connections.items() if dirs[p] in ('input', 'inout')))
        outs = set().union(*(nets(bs) for p, bs in connections.items() if dirs[p] in ('output', 'inout')))
        typ = cell.get('type', '')
        (sequential if _is_sequential(typ) else comb)[name] = (ins, outs)
        for b in ins:
            consumers[b].add(name)
        for b in outs:
            drivers[b].add(name)
    if unresolved:
        raise ValueError('Cannot determine cell pin directions for %d cells; first: %s. '
                         'Regenerate Yosys JSON with library module definitions or port_directions.'
                         % (len(unresolved), unresolved[:3]))

    pi = set().union(*(nets(p['bits']) for p in module.get('ports', {}).values() if p['direction'] == 'input'))
    po = set().union(*(nets(p['bits']) for p in module.get('ports', {}).values() if p['direction'] == 'output'))
    ffq, ffd = set(), set()
    for name in sequential:
        con = cells[name].get('connections', {})
        for p, bits in con.items():
            # QN is a complementary output of the SAME register bit; report unique wires, not FF bit count.
            if p.upper() in ('Q', 'QN'):
                ffq.update(nets(bits))
            if p.upper() == 'D':
                ffd.update(nets(bits))

    edges = defaultdict(set)
    indegree = {n: 0 for n in comb}
    for dst, (ins, _) in comb.items():
        for bit in ins:
            for src in drivers[bit]:
                if src in comb and src != dst and dst not in edges[src]:
                    edges[src].add(dst)
                    indegree[dst] += 1

    queue = deque(n for n, d in indegree.items() if d == 0)
    depth, rr_depth = {}, {}
    processed = 0
    while queue:
        n = queue.popleft()
        processed += 1
        ins, _ = comb[n]
        parents = {p for b in ins for p in drivers[b] if p in comb and p != n}
        depth[n] = 1 + max((depth[p] for p in parents if p in depth), default=0)
        options = [rr_depth[p] + 1 for p in parents if p in rr_depth]
        if ins & ffq:
            options.append(1)
        if options:
            rr_depth[n] = max(options)
        for dst in edges[n]:
            indegree[dst] -= 1
            if indegree[dst] == 0:
                queue.append(dst)

    cycle = processed != len(comb)
    # Only count FF-Q launched paths that actually terminate at a FF-D.
    rr_terminal = [rr_depth[n] for bit in ffd for n in drivers[bit] if n in rr_depth]
    rr_direct = bool(ffq & ffd)
    fanouts = [len(consumers[b]) for b in drivers if consumers[b]]
    return {
        'net_comb_cell_count': len(comb),
        'net_sequential_cell_count': len(sequential),
        'net_input_bit_count': len(pi),
        'net_output_bit_count': len(po),
        'net_unique_wire_count': len(set(drivers) | set(consumers)),
        'net_max_fanout_cells': max(fanouts, default=0),
        'net_mean_fanout_cells': round(statistics.mean(fanouts), 4) if fanouts else 0,
        'net_max_comb_cell_depth': None if cycle else max(depth.values(), default=0),
        'net_max_reg_to_reg_cell_depth': None if cycle else (max(rr_terminal) if rr_terminal else (0 if rr_direct else None)),
        'net_combinational_cycle_detected': int(cycle),
        'net_reg_q_bit_count': len(ffq),
        'net_reg_d_bit_count': len(ffd),
        'net_cell_pin_directions_unresolved': 0,
    }


def architectural_features(cfg):
    arch = cfg.get('architecture_features', {})
    return {
        'rtl_pipeline_depth_stages': arch.get('pipeline_depth_stages'),
        'rtl_estimated_operator_depth': arch.get('estimated_operator_depth'),
        'rtl_memory_depth_words': arch.get('memory_depth_words'),
        'rtl_memory_width_bits': arch.get('memory_width_bits'),
        'rtl_read_ports': arch.get('read_ports'),
        'rtl_write_ports': arch.get('write_ports'),
        'rtl_latency_cycles': arch.get('latency_cycles'),
        'rtl_operator_counts_json': json.dumps(arch.get('operator_counts', {}), sort_keys=True),
        'rtl_architecture_notes': arch.get('notes', ''),
        'rtl_architecture_metadata_source': arch.get('source', 'not_supplied'),
    }


def all_features(cfg, liberty_path, mapped_json):
    return {**technology_features(liberty_path), **architectural_features(cfg),
            **netlist_features(mapped_json, cfg['top'], liberty_path)}
