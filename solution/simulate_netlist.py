#!/usr/bin/env python3
"""Boolean simulator for netlists produced by extract_connectivity.py."""

from __future__ import annotations

import argparse
import itertools
import json
import re
from collections import defaultdict
from pathlib import Path


OUTPUT_PINS = {"X", "Y", "Q", "HI", "LO"}
FLOP_PREFIXES = ("dfrtp", "dfstp", "dfxtp")


def cell_base(cell: str) -> str:
    name = cell.split("__")[-1]
    return re.sub(r"_\d+$", "", name)


def is_flop(cell: str) -> bool:
    return cell_base(cell).startswith(FLOP_PREFIXES)


def pin_value(pin: str, values: dict[str, int]) -> int:
    value = values[pin]
    return 1 - value if pin.endswith("_N") else value


def evaluate_cell(cell: str, pins: dict[str, int]) -> dict[str, int]:
    """Evaluate one combinational SKY130 standard cell."""
    base = cell_base(cell)

    if base == "conb":
        return {"HI": 1, "LO": 0}
    if base == "inv":
        return {"Y": 1 - pins["A"]}
    if base in ("buf", "clkbuf"):
        return {"X": pins["A"]}
    if base.startswith("clkbuf") or base.startswith("buf"):
        return {"X": pins["A"]}
    if base == "mux2":
        return {"X": pins["A1"] if pins["S"] else pins["A0"]}
    if base == "xor2":
        return {"X": pins["A"] ^ pins["B"]}
    if base == "xnor2":
        return {"Y": 1 - (pins["A"] ^ pins["B"])}

    inputs = {name: value for name, value in pins.items() if name not in OUTPUT_PINS}

    # Simple AND/OR families, including input-complemented b/bb variants.
    if base.startswith(("and", "nand", "or", "nor")):
        transformed = [pin_value(name, inputs) for name in inputs]
        if base.startswith(("and", "nand")):
            value = int(all(transformed))
        else:
            value = int(any(transformed))
        inverted = base.startswith(("nand", "nor"))
        return {"Y" if inverted else "X": 1 - value if inverted else value}

    # Compound a...o cells AND within each letter group, then OR groups.
    # Compound o...a cells OR within groups, then AND groups.
    if base.startswith("a") or base.startswith("o"):
        grouped: dict[str, list[int]] = defaultdict(list)
        for name in inputs:
            grouped[name[0]].append(pin_value(name, inputs))
        if base.startswith("a"):
            group_values = [int(all(group)) for group in grouped.values()]
            value = int(any(group_values))
            inverted = base.endswith("oi")
        else:
            group_values = [int(any(group)) for group in grouped.values()]
            value = int(all(group_values))
            inverted = base.endswith("ai")
        return {"Y" if inverted else "X": 1 - value if inverted else value}

    raise ValueError(f"Unsupported cell type: {cell}")


class NetlistSimulator:
    def __init__(self, netlist: dict, constants: dict[str, int] | None = None):
        self.netlist = netlist
        self.instances = {instance["name"]: instance for instance in netlist["instances"]}
        self.pin_to_net: dict[tuple[str, str], str] = {}
        self.port_to_net: dict[str, str] = {}
        self.drivers: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for net in netlist["nets"]:
            for endpoint in net["endpoints"]:
                if endpoint["kind"] == "port":
                    self.port_to_net[endpoint["port"]] = net["name"]
                else:
                    key = (endpoint["instance"], endpoint["pin"])
                    self.pin_to_net[key] = net["name"]
                    if endpoint["direction"] == "output":
                        self.drivers[net["name"]].append(key)

        self.flops = {
            name: instance for name, instance in self.instances.items()
            if is_flop(instance["cell"])
        }
        self.combinational = {
            name: instance for name, instance in self.instances.items()
            if not is_flop(instance["cell"])
        }
        self.constants = dict(constants or {})
        self.state = {name: 0 for name in self.flops}
        dependencies: dict[str, set[str]] = {name: set() for name in self.combinational}
        consumers: dict[str, set[str]] = defaultdict(set)
        for name, instance in self.combinational.items():
            for pin in instance["pins"]:
                if pin in OUTPUT_PINS:
                    continue
                net = self.pin_to_net.get((name, pin))
                for driver, _ in self.drivers.get(net, []):
                    if driver in self.combinational and driver != name:
                        dependencies[name].add(driver)
                        consumers[driver].add(name)
        ready = [name for name, deps in dependencies.items() if not deps]
        self.evaluation_order = []
        while ready:
            name = ready.pop()
            self.evaluation_order.append(name)
            for consumer in consumers[name]:
                dependencies[consumer].discard(name)
                if not dependencies[consumer]:
                    ready.append(consumer)
        remaining = [name for name, deps in dependencies.items() if deps]
        if remaining:
            raise ValueError(f"Combinational cycle involving: {remaining[:10]}")

    def reset_state(self) -> None:
        for name, instance in self.flops.items():
            base = cell_base(instance["cell"])
            if base.startswith("dfstp"):
                self.state[name] = 1
            elif base.startswith("dfrtp"):
                self.state[name] = 0

    def combinational_values(self, inputs: dict[str, int]) -> dict[str, int]:
        values = dict(self.constants)
        for port, value in inputs.items():
            net = self.port_to_net.get(port)
            if net is not None:
                values[net] = int(value)
        for flop, value in self.state.items():
            q_net = self.pin_to_net.get((flop, "Q"))
            if q_net is not None:
                values[q_net] = value

        for name in self.evaluation_order:
            instance = self.instances[name]
            pin_values = {}
            ready = True
            for pin in instance["pins"]:
                if pin in OUTPUT_PINS:
                    continue
                net = self.pin_to_net.get((name, pin))
                if net not in values:
                    ready = False
                    break
                pin_values[pin] = values[net]
            if not ready:
                continue
            outputs = evaluate_cell(instance["cell"], pin_values)
            for pin, value in outputs.items():
                net = self.pin_to_net.get((name, pin))
                if net is not None:
                    values[net] = value
        return values

    def clock(self, inputs: dict[str, int]) -> dict[str, int]:
        if inputs.get("rst_n", 1) == 0:
            self.reset_state()
        else:
            before = self.combinational_values(inputs)
            next_state = dict(self.state)
            for name, instance in self.flops.items():
                d_net = self.pin_to_net.get((name, "D"))
                if d_net in before:
                    next_state[name] = before[d_net]
            self.state = next_state
        return self.combinational_values(inputs)

    def port(self, values: dict[str, int], port: str, default=None):
        return values.get(self.port_to_net.get(port), default)

    def bus(self, values: dict[str, int], base: str, width: int) -> int | None:
        bits = [self.port(values, f"{base}[{index}]") for index in range(width)]
        if any(bit is None for bit in bits):
            return None
        return sum(int(bit) << index for index, bit in enumerate(bits))


def parse_vcd(path: Path):
    """Parse the small scalar/vector VCD used by this puzzle."""
    identifiers = {}
    events: dict[int, list[tuple[str, int | None]]] = defaultdict(list)
    current_time = 0
    in_header = True
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line.startswith("$var"):
            fields = line.split()
            identifiers[fields[3]] = fields[4]
        elif line == "$enddefinitions $end":
            in_header = False
        elif in_header or not line:
            continue
        elif line.startswith("#"):
            current_time = int(line[1:])
        elif line[0] in "01xXzZ":
            value = None if line[0].lower() in "xz" else int(line[0])
            events[current_time].append((identifiers[line[1:]], value))
        elif line.startswith("b"):
            bits, identifier = line[1:].split()
            value = None if any(char.lower() in "xz" for char in bits) else int(bits, 2)
            events[current_time].append((identifiers[identifier], value))
    return events


def simulate_vcd(simulator: NetlistSimulator, vcd_path: Path):
    events = parse_vcd(vcd_path)
    signals: dict[str, int | None] = {"clk": 0, "rst_n": 0, "enable": 0, "I": 0}
    samples = []
    old_clock = 0
    for time in sorted(events):
        expected_changes = {}
        for signal, value in events[time]:
            if signal in ("O", "success"):
                expected_changes[signal] = value
            else:
                signals[signal] = value
        new_clock = int(signals.get("clk") or 0)
        if old_clock == 0 and new_clock == 1:
            inputs = {
                name: int(signals.get(name) or 0)
                for name in ("rst_n", "enable", "I")
            }
            values = simulator.clock(inputs)
            samples.append(
                {
                    "time": time,
                    "O": simulator.bus(values, "O", 8),
                    "success": simulator.port(values, "success"),
                    "expected_changes": expected_changes,
                }
            )
        old_clock = new_clock
    return samples


def undriven_internal_nets(netlist: dict):
    external = {"I", "A", "B", "clk", "rst_n", "enable", "en"}
    result = []
    for net in netlist["nets"]:
        has_driver = any(
            endpoint.get("direction") == "output" for endpoint in net["endpoints"]
        )
        ports = {
            endpoint["port"] for endpoint in net["endpoints"]
            if endpoint["kind"] == "port"
        }
        if not has_driver and not (ports & external):
            result.append(net["name"])
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--vcd", type=Path)
    parser.add_argument("--constants", help="Comma-separated net=0/1 assignments")
    parser.add_argument("--search-constants", action="store_true")
    args = parser.parse_args()

    netlist = json.loads(args.netlist.read_text())
    constants = {}
    if args.constants:
        constants = {
            name: int(value) for name, value in
            (assignment.split("=") for assignment in args.constants.split(","))
        }

    if args.search_constants:
        if not args.vcd:
            raise SystemExit("--search-constants requires --vcd")
        unknown = undriven_internal_nets(netlist)
        print("Searching constants:", unknown)
        matches = []
        for bits in itertools.product((0, 1), repeat=len(unknown)):
            expected_o = None
            expected_success = None
            candidate = dict(zip(unknown, bits))
            simulator = NetlistSimulator(netlist, candidate)
            samples = simulate_vcd(simulator, args.vcd)
            okay = True
            for sample in samples:
                changes = sample["expected_changes"]
                if changes.get("O") is not None:
                    expected_o = changes["O"]
                if changes.get("success") is not None:
                    expected_success = changes["success"]
                if expected_o is not None and sample["O"] != expected_o:
                    okay = False
                    break
                if expected_success is not None and sample["success"] != expected_success:
                    okay = False
                    break
            if okay:
                matches.append(candidate)
        print(json.dumps(matches, indent=2))
        return

    simulator = NetlistSimulator(netlist, constants)
    if args.vcd:
        for sample in simulate_vcd(simulator, args.vcd):
            if sample["expected_changes"] or sample["O"] or sample["success"]:
                print(sample)


if __name__ == "__main__":
    main()
