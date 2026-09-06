#!/usr/bin/env python3
"""Replay a solved input through the full netlist and print O[7:0] as text."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from simulate_netlist import NetlistSimulator


# Recovered assignments for the six undriven nets in puzzle.gds. These names
# depend on the extractor's deterministic net ordering.
DEFAULT_CONSTANTS = {
    "n0101": 0,
    "n0102": 0,
    "n0131": 1,
    "n0304": 1,
    "n0351": 1,
    "n0439": 0,
}


def parse_constants(encoded: str) -> dict[str, int]:
    constants = {}
    for assignment in encoded.split(","):
        name, separator, value = assignment.partition("=")
        if not separator or value not in {"0", "1"}:
            raise ValueError(f"Invalid constant assignment: {assignment!r}")
        constants[name] = int(value)
    return constants


def read_bits(path: Path) -> str:
    bits = "".join(character for character in path.read_text() if character in "01")
    if len(bits) != 121:
        raise ValueError(f"expected 121 solved input bits, got {len(bits)}")
    return bits


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("netlist", type=Path)
    parser.add_argument("bits", type=Path)
    parser.add_argument(
        "--constants",
        default=",".join(
            f"{name}={int(value)}" for name, value in DEFAULT_CONSTANTS.items()
        ),
        help="comma-separated undriven net=0/1 assignments",
    )
    parser.add_argument("--max-output-cycles", type=int, default=64)
    parser.add_argument(
        "--expect",
        help="fail unless the decoded ASCII output exactly matches this string",
    )
    args = parser.parse_args()

    netlist = json.loads(args.netlist.read_text())
    simulator = NetlistSimulator(netlist, parse_constants(args.constants))
    simulator.reset_state()

    for bit in read_bits(args.bits):
        simulator.clock({"I": int(bit), "clk": 1, "rst_n": 1, "enable": 1})

    output = []
    success_seen = False
    for _ in range(args.max_output_cycles):
        values = simulator.clock({"I": 0, "clk": 1, "rst_n": 1, "enable": 0})
        success_seen |= simulator.port(values, "success", 0) == 1
        value = simulator.bus(values, "O", 8)
        if value == 0 and output:
            break
        if value:
            output.append(value)

    if not success_seen:
        raise RuntimeError("the supplied bitstream did not raise success")
    if not output:
        raise RuntimeError("success was raised, but O[7:0] produced no bytes")

    data = bytes(output)
    text = data.decode("ascii")
    if args.expect is not None and text != args.expect:
        raise RuntimeError(
            f"decoded {text!r}, expected {args.expect!r}"
        )
    print("output bytes:", repr(data))
    print("final string:", text)


if __name__ == "__main__":
    main()
