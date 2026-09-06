#!/usr/bin/env python3
"""Recover a standard-cell pin netlist from a routed SKY130 GDS.

The extractor deliberately stays above transistor level.  It uses the pin
labels and local-interconnect shapes already present in each standard-cell
definition, then follows top-level routing metal through via references.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import gdstk
from shapely import affinity
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree


ROUTING_LAYERS = range(67, 73)  # li1, met1, met2, met3, met4, met5
POWER_PINS = {"VPWR", "VGND", "VPB", "VNB"}
PHYSICAL_CELL_MARKERS = ("__tapvpwrvgnd_", "__decap_", "__diode_")
OUTPUT_PIN_NAMES = {"X", "Y", "Q", "HI", "LO"}


class DisjointSet:
    def __init__(self) -> None:
        self.parent: dict[tuple[int, int], tuple[int, int]] = {}

    def add(self, item: tuple[int, int]) -> None:
        self.parent.setdefault(item, item)

    def find(self, item: tuple[int, int]) -> tuple[int, int]:
        parent = self.parent[item]
        if parent != item:
            self.parent[item] = self.find(parent)
        return self.parent[item]

    def union(self, left: tuple[int, int], right: tuple[int, int]) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


@dataclass
class Instance:
    name: str
    cell: str
    reference: gdstk.Reference
    pins: dict[str, dict[int, object]]


def polygon_parts(geometry):
    """Yield polygon members from any Shapely geometry."""
    if geometry.is_empty:
        return
    if geometry.geom_type == "Polygon":
        yield geometry
        return
    if hasattr(geometry, "geoms"):
        for member in geometry.geoms:
            yield from polygon_parts(member)


def transform_geometry(geometry, reference: gdstk.Reference):
    """Apply a GDS reference reflection, magnification, rotation, and origin."""
    angle = reference.rotation or 0.0
    scale = reference.magnification or 1.0
    cosine = math.cos(angle) * scale
    sine = math.sin(angle) * scale
    if reference.x_reflection:
        matrix = [cosine, sine, sine, -cosine, *reference.origin]
    else:
        matrix = [cosine, -sine, sine, cosine, *reference.origin]
    return affinity.affine_transform(geometry, matrix)


def layer_shapes(cell: gdstk.Cell, layer: int, datatype: int = 20):
    for polygon in cell.polygons:
        if polygon.layer == layer and polygon.datatype == datatype:
            yield Polygon(polygon.points)
    for path in cell.paths:
        if path.layers == (layer,) and path.datatypes == (datatype,):
            for polygon in path.to_polygons():
                yield Polygon(polygon.points)


def cell_pin_templates(cell: gdstk.Cell) -> dict[str, dict[int, object]]:
    """Return LI/M1 access geometry for every non-power standard-cell pin."""
    components: dict[int, list[object]] = {}
    trees: dict[int, STRtree] = {}
    local_dsu = DisjointSet()
    for layer in (67, 68):
        merged = unary_union(list(layer_shapes(cell, layer)))
        parts = list(polygon_parts(merged))
        components[layer] = parts
        trees[layer] = STRtree(parts)
        for index in range(len(parts)):
            local_dsu.add((layer, index))

    # Layer 67 datatype 44 is the LI-to-M1 contact in this GDS mapping.
    for cut in cell.polygons:
        if cut.layer != 67 or cut.datatype != 44:
            continue
        cut_geometry = Polygon(cut.points)
        nodes = []
        for layer in (67, 68):
            hits = trees[layer].query(cut_geometry, predicate="intersects")
            nodes.extend((layer, int(index)) for index in hits)
        if nodes:
            for node in nodes[1:]:
                local_dsu.union(nodes[0], node)

    roots_by_pin: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for label in cell.labels:
        if label.layer != 67 or label.texttype != 5 or label.text in POWER_PINS:
            continue
        hits = trees[67].query(Point(label.origin), predicate="intersects")
        if len(hits) == 0:
            raise RuntimeError(f"No LI shape under {cell.name}.{label.text}")
        roots_by_pin[label.text].update(local_dsu.find((67, int(index))) for index in hits)

    # Multiple access shapes bearing the same label are the same logical pin,
    # even when the library exposes them as separate pieces of access metal.
    result: dict[str, dict[int, object]] = {}
    for pin, roots in roots_by_pin.items():
        by_layer: dict[int, list[object]] = defaultdict(list)
        for layer, parts in components.items():
            for index, geometry in enumerate(parts):
                if local_dsu.find((layer, index)) in roots:
                    by_layer[layer].append(geometry)
        result[pin] = {layer: unary_union(parts) for layer, parts in by_layer.items()}
    return result


def assign_instances(top: gdstk.Cell):
    templates: dict[str, dict[str, object]] = {}
    instances: list[Instance] = []

    for reference in top.references:
        cell = reference.cell
        if not cell.name.startswith("sky130_fd_sc_hd__"):
            continue
        if any(marker in cell.name for marker in PHYSICAL_CELL_MARKERS):
            continue
        if cell.name not in templates:
            templates[cell.name] = cell_pin_templates(cell)

        # The puzzle GDS has no original instance names. Use reference order
        # to give every functional cell a deterministic name.
        name = f"u{len(instances):04d}"

        pins = {
            pin: {
                layer: transform_geometry(geometry, reference)
                for layer, geometry in by_layer.items()
            }
            for pin, by_layer in templates[cell.name].items()
        }
        instances.append(Instance(name, cell.name, reference, pins))
    return instances


def transformed_via_shapes(reference: gdstk.Reference):
    by_layer: dict[int, list[object]] = defaultdict(list)
    for polygon in reference.cell.polygons:
        if polygon.datatype != 20 or polygon.layer not in ROUTING_LAYERS:
            continue
        geometry = transform_geometry(Polygon(polygon.points), reference)
        by_layer[polygon.layer].append(geometry)
    return {layer: unary_union(parts) for layer, parts in by_layer.items()}


def extract(gds_path: Path):
    library = gdstk.read_gds(str(gds_path))
    top_cells = library.top_level()
    if len(top_cells) != 1:
        raise RuntimeError(f"Expected one top cell, found {[cell.name for cell in top_cells]}")
    top = top_cells[0]

    instances = assign_instances(top)
    conductors: dict[int, list[object]] = defaultdict(list)

    # Direct top-level signal routing.
    for polygon in top.polygons:
        if polygon.datatype == 20 and polygon.layer in ROUTING_LAYERS:
            conductors[polygon.layer].append(Polygon(polygon.points))
    for path in top.paths:
        if len(path.layers) != 1 or path.datatypes != (20,):
            continue
        layer = path.layers[0]
        if layer in ROUTING_LAYERS:
            conductors[layer].extend(Polygon(polygon.points) for polygon in path.to_polygons())

    # Standard-cell pins provide the LI endpoints of routed nets.
    for instance in instances:
        for by_layer in instance.pins.values():
            for layer, geometry in by_layer.items():
                conductors[layer].append(geometry)

    # Via metal enclosures bridge route endpoints and are also needed when
    # neighboring path polygons stop at the via center.
    via_shapes = []
    for reference in top.references:
        if not reference.cell.name.startswith("VIA_"):
            continue
        shapes = transformed_via_shapes(reference)
        via_shapes.append(shapes)
        for layer, geometry in shapes.items():
            conductors[layer].append(geometry)

    components: dict[int, list[object]] = {}
    trees: dict[int, STRtree] = {}
    dsu = DisjointSet()
    for layer, shapes in sorted(conductors.items()):
        merged = unary_union(shapes)
        parts = list(polygon_parts(merged))
        components[layer] = parts
        trees[layer] = STRtree(parts)
        for index in range(len(parts)):
            dsu.add((layer, index))

    def touched_nodes(layer: int, geometry):
        indices = trees[layer].query(geometry, predicate="intersects")
        return [(layer, int(index)) for index in indices]

    # Electrically join the conductor layers at every via.
    for shapes in via_shapes:
        nodes = []
        for layer, geometry in shapes.items():
            nodes.extend(touched_nodes(layer, geometry))
        if nodes:
            for node in nodes[1:]:
                dsu.union(nodes[0], node)

    pin_nodes: dict[tuple[str, str], tuple[int, int]] = {}
    for instance in instances:
        for pin, by_layer in instance.pins.items():
            nodes = []
            for layer, geometry in by_layer.items():
                nodes.extend(touched_nodes(layer, geometry))
            if not nodes:
                continue
            for node in nodes[1:]:
                dsu.union(nodes[0], node)
            pin_nodes[(instance.name, pin)] = nodes[0]

    port_nodes: dict[str, tuple[int, int]] = {}
    for label in top.labels:
        if label.layer not in trees or label.text in POWER_PINS:
            continue
        hits = trees[label.layer].query(Point(label.origin), predicate="intersects")
        if len(hits):
            port_nodes[label.text] = (label.layer, int(hits[0]))

    endpoints: dict[tuple[int, int], list[dict[str, str]]] = defaultdict(list)
    for (instance, pin), node in pin_nodes.items():
        endpoints[dsu.find(node)].append(
            {
                "kind": "pin",
                "instance": instance,
                "pin": pin,
                "direction": "output" if pin in OUTPUT_PIN_NAMES else "input",
            }
        )
    for port, node in port_nodes.items():
        endpoints[dsu.find(node)].append({"kind": "port", "port": port})

    named_roots = {
        root: sorted(endpoint["port"] for endpoint in members if endpoint["kind"] == "port")
        for root, members in endpoints.items()
    }
    nets = []
    unnamed = 0
    for root, members in sorted(endpoints.items(), key=lambda item: repr(item[0])):
        ports = named_roots[root]
        if ports:
            name = ports[0]
        else:
            name = f"n{unnamed:04d}"
            unnamed += 1
        nets.append({"name": name, "endpoints": sorted(members, key=repr)})

    result = {
        "gds": str(gds_path),
        "top": top.name,
        "instances": [
            {
                "name": instance.name,
                "cell": instance.cell,
                "origin": [float(value) for value in instance.reference.origin],
                "rotation": float(instance.reference.rotation or 0.0),
                "x_reflection": bool(instance.reference.x_reflection),
                "pins": sorted(instance.pins),
            }
            for instance in instances
        ],
        "nets": nets,
        "stats": {
            "instances": len(instances),
            "pins": len(pin_nodes),
            "ports": len(port_nodes),
            "nets_with_endpoints": len(nets),
            "routing_components_by_layer": {
                str(layer): len(parts) for layer, parts in components.items()
            },
        },
    }
    return result


def verilog_identifier(name: str) -> str:
    """Return a Verilog identifier, escaping names that contain punctuation."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", name):
        return name
    return f"\\{name} "


def write_structural_verilog(result: dict, output_path: Path) -> None:
    """Write the recovered connectivity as a structural Verilog module."""
    pin_to_net: dict[tuple[str, str], str] = {}
    ports: dict[str, str] = {}
    nets_by_name = {net["name"]: net for net in result["nets"]}

    for net in result["nets"]:
        for endpoint in net["endpoints"]:
            if endpoint["kind"] == "pin":
                pin_to_net[(endpoint["instance"], endpoint["pin"])] = net["name"]
            elif endpoint["kind"] == "port":
                ports[endpoint["port"]] = net["name"]

    port_directions = {}
    for port, net_name in ports.items():
        endpoints = nets_by_name[net_name]["endpoints"]
        has_cell_driver = any(
            endpoint["kind"] == "pin" and endpoint["pin"] in OUTPUT_PIN_NAMES
            for endpoint in endpoints
        )
        port_directions[port] = "output" if has_cell_driver else "input"

    lines = [f"module {verilog_identifier(result['top'])} ("]
    sorted_ports = sorted(ports)
    for index, port in enumerate(sorted_ports):
        comma = "," if index + 1 < len(sorted_ports) else ""
        lines.append(f"    {verilog_identifier(port)}{comma}")
    lines.extend([");", ""])

    for port in sorted_ports:
        lines.append(
            f"    {port_directions[port]} wire {verilog_identifier(port)};"
        )
    lines.append("")

    port_net_names = set(ports.values())
    for net in sorted(result["nets"], key=lambda item: item["name"]):
        if net["name"] not in port_net_names:
            lines.append(f"    wire {verilog_identifier(net['name'])};")
    lines.append("")

    for instance in result["instances"]:
        connections = [
            (pin, pin_to_net[(instance["name"], pin)])
            for pin in instance["pins"]
            if (instance["name"], pin) in pin_to_net
        ]
        lines.append(
            f"    {instance['cell']} {verilog_identifier(instance['name'])} ("
        )
        for index, (pin, net_name) in enumerate(connections):
            comma = "," if index + 1 < len(connections) else ""
            lines.append(
                f"        .{verilog_identifier(pin)}"
                f"({verilog_identifier(net_name)}){comma}"
            )
        lines.extend(["    );", ""])

    lines.extend(["endmodule", ""])
    output_path.write_text("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("gds", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("puzzle_netlist.json"),
        help="JSON output path (default: puzzle_netlist.json)",
    )
    parser.add_argument(
        "--verilog-output",
        type=Path,
        default=Path("puzzle_netlist.v"),
        help="structural Verilog output path (default: puzzle_netlist.v)",
    )
    args = parser.parse_args()

    result = extract(args.gds)
    encoded = json.dumps(result, indent=2)
    args.output.write_text(encoded + "\n")
    write_structural_verilog(result, args.verilog_output)
    print(json.dumps(result["stats"], indent=2))
    print(f"Wrote {args.output}")
    print(f"Wrote {args.verilog_output}")


if __name__ == "__main__":
    main()
