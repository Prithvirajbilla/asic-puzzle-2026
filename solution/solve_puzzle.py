#!/usr/bin/env python3
"""Discover a successful input that produces printable output with Z3.

Model the fan-in of success and O[7:0] from the extracted circuit, then replay
Z3's candidate through the full concrete simulator. The search uses the input
protocol, recovered undriven constants, and output format; it does not load a
saved input or constrain the output to the known answer text.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import z3

from extract_final_string import DEFAULT_CONSTANTS, parse_constants
from simulate_netlist import NetlistSimulator, OUTPUT_PINS, cell_base, is_flop


def pin_value(name, values):
    value = values[name]
    return z3.Not(value) if name.endswith("_N") else value


def evaluate(cell, pins):
    base = cell_base(cell)
    if base == "conb":
        return {"HI": True, "LO": False}
    if base == "inv":
        return {"Y": z3.Not(pins["A"])}
    if base.startswith(("buf", "clkbuf")):
        return {"X": pins["A"]}
    if base == "mux2":
        return {"X": z3.If(pins["S"], pins["A1"], pins["A0"])}
    if base == "xor2":
        return {"X": z3.Xor(pins["A"], pins["B"])}
    if base == "xnor2":
        return {"Y": z3.Not(z3.Xor(pins["A"], pins["B"]))}

    inputs = {name: value for name, value in pins.items() if name not in OUTPUT_PINS}
    transformed = [pin_value(name, inputs) for name in inputs]
    if base.startswith(("and", "nand", "or", "nor")):
        value = z3.And(*transformed) if base.startswith(("and", "nand")) else z3.Or(*transformed)
        inverted = base.startswith(("nand", "nor"))
        return {"Y" if inverted else "X": z3.Not(value) if inverted else value}

    grouped = {}
    for name in inputs:
        grouped.setdefault(name[0], []).append(pin_value(name, inputs))
    if base.startswith("a"):
        value = z3.Or(*(z3.And(*group) for group in grouped.values()))
        inverted = base.endswith("oi")
    elif base.startswith("o"):
        value = z3.And(*(z3.Or(*group) for group in grouped.values()))
        inverted = base.endswith("ai")
    else:
        raise ValueError(f"Unsupported cell: {cell}")
    return {"Y" if inverted else "X": z3.Not(value) if inverted else value}


class SymbolicPuzzle:
    def __init__(self, path: Path, constants: dict[str, int]):
        netlist = json.loads(path.read_text())
        self.instances = {item["name"]: item for item in netlist["instances"]}
        self.pin_to_net = {}
        self.port_to_net = {}
        self.drivers = {}
        for net in netlist["nets"]:
            for endpoint in net["endpoints"]:
                if endpoint["kind"] == "port":
                    self.port_to_net[endpoint["port"]] = net["name"]
                else:
                    key = endpoint["instance"], endpoint["pin"]
                    self.pin_to_net[key] = net["name"]
                    if endpoint["direction"] == "output":
                        self.drivers[net["name"]] = key
        # Include both the checker and output generator. Some inputs raise
        # success but produce binary output, so success alone is insufficient.
        required_instances = set()
        output_ports = ["success", *(f"O[{index}]" for index in range(8))]
        pending_nets = [self.port_to_net[name] for name in output_ports]
        visited_nets = set()
        while pending_nets:
            net = pending_nets.pop()
            if net in visited_nets:
                continue
            visited_nets.add(net)
            driver = self.drivers.get(net)
            if driver is None:
                continue
            instance_name, _ = driver
            if instance_name in required_instances:
                continue
            required_instances.add(instance_name)
            instance = self.instances[instance_name]
            input_pins = ["D"] if is_flop(instance["cell"]) else [
                pin for pin in instance["pins"] if pin not in OUTPUT_PINS
            ]
            for pin in input_pins:
                input_net = self.pin_to_net.get((instance_name, pin))
                if input_net is not None:
                    pending_nets.append(input_net)

        self.flops = [
            name for name in sorted(required_instances)
            if is_flop(self.instances[name]["cell"])
        ]
        self.comb = [
            name for name in sorted(required_instances)
            if not is_flop(self.instances[name]["cell"])
        ]
        self.constants = {name: bool(value) for name, value in constants.items()}

        # The extractor's order is not topological; compute one once.
        pending = set(self.comb)
        known = set(constants) | {
            self.port_to_net[name] for name in ("I", "clk", "rst_n", "enable")
        }
        known |= {self.pin_to_net[(name, "Q")] for name in self.flops}
        self.order = []
        while pending:
            progress = False
            for name in sorted(pending):
                item = self.instances[name]
                input_nets = [self.pin_to_net.get((name, pin)) for pin in item["pins"] if pin not in OUTPUT_PINS]
                if all(net in known for net in input_nets):
                    self.order.append(name)
                    pending.remove(name)
                    for pin in item["pins"]:
                        if pin in OUTPUT_PINS and (name, pin) in self.pin_to_net:
                            known.add(self.pin_to_net[(name, pin)])
                    progress = True
            if not progress:
                raise RuntimeError(f"Cannot order combinational cells: {sorted(pending)[:10]}")

    def solve(self, input_cycles: int, output_bytes: int, timeout_seconds: int):
        if input_cycles < 1 or output_bytes < 1 or timeout_seconds < 1:
            raise ValueError("input cycles, output bytes, and timeout must be positive")
        # The first output is visible one clock after the input phase. Include
        # a further cycle to constrain the zero terminator after the message.
        first_output_cycle = input_cycles + 1
        terminator_cycle = first_output_cycle + output_bytes
        cycles = terminator_cycle + 1
        solver = z3.Solver()
        solver.set(random_seed=0, timeout=timeout_seconds * 1000)
        state = [
            {name: z3.Bool(f"q_{cycle}_{name}") for name in self.flops}
            for cycle in range(cycles + 1)
        ]
        inputs = [z3.Bool(f"input_{cycle}") for cycle in range(input_cycles)]
        print(f"Building symbolic model for {cycles} cycles...", flush=True)

        for name in self.flops:
            base = cell_base(self.instances[name]["cell"])
            solver.add(state[0][name] == base.startswith("dfstp"))

        for cycle in range(cycles):
            values = dict(self.constants)
            values[self.port_to_net["I"]] = inputs[cycle] if cycle < input_cycles else False
            values[self.port_to_net["clk"]] = True
            values[self.port_to_net["rst_n"]] = True
            values[self.port_to_net["enable"]] = cycle < input_cycles
            for name in self.flops:
                values[self.pin_to_net[(name, "Q")]] = state[cycle][name]
            for name in self.order:
                item = self.instances[name]
                pins = {
                    pin: values[self.pin_to_net[(name, pin)]]
                    for pin in item["pins"] if pin not in OUTPUT_PINS
                }
                for pin, expression in evaluate(item["cell"], pins).items():
                    net = self.pin_to_net.get((name, pin))
                    if net is not None:
                        wire = z3.Bool(f"w_{cycle}_{name}_{pin}")
                        solver.add(wire == expression)
                        values[net] = wire

            if first_output_cycle <= cycle <= terminator_cycle:
                byte = z3.Concat(*(
                    z3.If(
                        values[self.port_to_net[f"O[{index}]"]],
                        z3.BitVecVal(1, 1),
                        z3.BitVecVal(0, 1),
                    )
                    for index in range(7, -1, -1)
                ))
                if cycle == terminator_cycle:
                    solver.add(byte == 0)
                else:
                    solver.add(z3.UGE(byte, 32), z3.ULE(byte, 126))

            for name in self.flops:
                d = values[self.pin_to_net[(name, "D")]]
                solver.add(state[cycle + 1][name] == d)

        success_driver, _ = self.drivers[self.port_to_net["success"]]
        solver.add(state[first_output_cycle][success_driver])
        print(
            f"Solving {input_cycles} input bits for success and "
            f"{output_bytes} printable output bytes...",
            flush=True,
        )
        status = solver.check()
        print(f"Z3 result: {status}", flush=True)
        if status == z3.unknown:
            raise RuntimeError(f"Z3 returned unknown: {solver.reason_unknown()}")
        if status == z3.unsat:
            raise RuntimeError("No input satisfies the circuit and output-format constraints")
        model = solver.model()
        bits = [int(z3.is_true(model.eval(bit, model_completion=True))) for bit in inputs]
        return bits


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("netlist", type=Path)
    parser.add_argument("--input-cycles", type=int, default=121)
    parser.add_argument(
        "--output-bytes", type=int, default=15,
        help="number of printable bytes before the zero terminator (default: 15)",
    )
    parser.add_argument(
        "--timeout-seconds", type=int, default=180,
        help="Z3 search timeout, excluding model construction (default: 180)",
    )
    parser.add_argument(
        "--constants",
        default=",".join(f"{name}={value}" for name, value in DEFAULT_CONSTANTS.items()),
        help="comma-separated undriven net=0/1 assignments",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    constants = parse_constants(args.constants)
    puzzle = SymbolicPuzzle(args.netlist, constants)
    print(
        f"symbolic cone: {len(puzzle.flops)} flops, "
        f"{len(puzzle.comb)} combinational cells",
        flush=True,
    )
    bits = puzzle.solve(args.input_cycles, args.output_bytes, args.timeout_seconds)

    # Verify the model's input and output claims against the full circuit.
    netlist = json.loads(args.netlist.read_text())
    simulator = NetlistSimulator(netlist, constants)
    simulator.reset_state()
    for bit in bits:
        simulator.clock({"I": bit, "clk": 1, "rst_n": 1, "enable": 1})
    output = []
    for index in range(args.output_bytes + 1):
        values = simulator.clock({"I": 0, "clk": 1, "rst_n": 1, "enable": 0})
        if index == 0 and simulator.port(values, "success") != 1:
            raise RuntimeError("Symbolic result failed concrete success verification")
        byte = simulator.bus(values, "O", 8)
        if index == args.output_bytes:
            if byte != 0:
                raise RuntimeError("Concrete output is missing its zero terminator")
        elif byte is None or not 32 <= byte <= 126:
            raise RuntimeError(f"Concrete output byte {index} is not printable: {byte}")
        else:
            output.append(byte)

    bitstream = "".join(map(str, bits))
    args.output.write_text(bitstream + "\n")
    print(f"success after cycle {args.input_cycles + 1}")
    print("bits:", bitstream)
    print("concrete output:", bytes(output).decode("ascii"))
    print("Full-circuit verification: PASS")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
