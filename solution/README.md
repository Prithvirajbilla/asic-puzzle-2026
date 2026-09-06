# ASIC puzzle solution replay

This directory reconstructs the circuit in `puzzle.gds` and replays the
checked-in 121-bit input to verify and decode the final answer:

```text
puzzle.gds
  -> recover SKY130 cells and routed connectivity
  -> puzzle_netlist.json + puzzle_netlist.v
  -> replay known_solution_bits.txt through the full circuit
  -> (* TWO STARS *)
```

The successful input is supplied in `known_solution_bits.txt`. The default
workflow reproduces the circuit's response to that input. The optional Z3 stage
below searches for a successful input directly from the recovered circuit.

## Quick start

Run these commands from the repository root with Python 3.10 or newer:

```sh
./solution/setup.sh
./solution/run.sh
```

`setup.sh` creates `solution/.venv` and installs the pinned `gdstk 1.0.1` and
`Shapely 2.0.7` packages. `run.sh` extracts the netlist from `puzzle.gds`, replays
the checked-in input, requires `success` to go high, and checks that the decoded
output equals `(* TWO STARS *)`.

Generated artifacts are written to `solution/build/`:

- `puzzle_netlist.json` — instances, pins, nets, and ports used by the simulator.
- `puzzle_netlist.v` — the same connectivity as structural gate-level Verilog.

To select a Python installation when creating the environment, set
`PYTHON_COMMAND`. To use an existing environment when running the pipeline, set
`PYTHON_BIN`:

```sh
PYTHON_COMMAND=/path/to/python3 ./solution/setup.sh
PYTHON_BIN=/path/to/python3 ./solution/run.sh
```

## Run individual stages

The first argument to `run.sh` selects a stage. A second argument overrides
the default build directory. The `decode` stage requires an extracted netlist
in that directory and reads `known_solution_bits.txt` directly.

```sh
./solution/run.sh extract
./solution/run.sh decode
./solution/run.sh all /tmp/puzzle-build
```

To replay a different 121-bit input, invoke the decoder directly:

```sh
solution/.venv/bin/python solution/extract_final_string.py \
    solution/build/puzzle_netlist.json /path/to/input_bits.txt
```

The decoder fails if the supplied input does not raise `success`. Pass
`--expect '(* TWO STARS *)'` to also require that exact output string.

## What each file does

| File | Purpose |
|---|---|
| `extract_connectivity.py` | Reads the GDS, identifies functional standard cells, reconstructs routed nets, and emits JSON plus Verilog. |
| `simulate_netlist.py` | Implements Boolean and sequential behavior for the recovered SKY130 cells, with utilities for replaying the supplied VCD. |
| `extract_final_string.py` | Replays the input through the full netlist, checks `success`, and decodes `O[7:0]` as ASCII. |
| `solve_puzzle.py` | Uses Z3 to find an input that raises `success` and emits printable output, then verifies it through the full circuit. |
| `requirements-solver.txt` | Pins the optional Z3 dependency. |
| `known_solution_bits.txt` | Supplies the verified 121 serial input bits. |
| `setup.sh` | Creates the isolated Python environment. |
| `run.sh` | Runs extraction and decoding, together or individually. |

## Discover an input with Z3

Install the optional solver dependency, extract the circuit, and run the search:

```sh
solution/.venv/bin/python -m pip install -r solution/requirements-solver.txt
./solution/run.sh extract
./solution/run.sh solve
```

The solver treats all 121 input bits as unknown Boolean variables. It models
the logic feeding `success` and `O[7:0]`: 70 flip-flops and 378 combinational
cells. It requires `success` after the input phase, followed by 15 printable
ASCII bytes and a zero terminator. The message length comes from the observed
output protocol. The six recovered undriven-net constants are shared with the
decoder. The query contains neither the saved bitstream nor the answer text.

Checking `success` alone can return inputs whose output is not readable text.
The printable-output constraints narrow the search. The resulting input can
differ from `known_solution_bits.txt` and still produce the same final message.

The solver writes `solution/build/z3_solution_bits.txt` only after verifying
its candidate through the full concrete circuit. The runner then decodes that
generated input and requires the exact output `(* TWO STARS *)`.

The default search timeout is 180 seconds, excluding model construction. For a
longer search, invoke the solver directly:

```sh
solution/.venv/bin/python -u solution/solve_puzzle.py \
    solution/build/puzzle_netlist.json \
    --timeout-seconds 600 \
    --output solution/build/z3_solution_bits.txt
```

## How the GDS becomes a netlist

The extractor works at the standard-cell level:

1. It reads the single top-level GDS cell with `gdstk`.
2. It keeps functional `sky130_fd_sc_hd` instances and filters physical-only
   tap, decap, and diode cells. Instances receive deterministic names in GDS
   reference order.
3. It obtains logical pins from labeled local-interconnect geometry inside
   each standard-cell definition, then applies the instance reflection,
   rotation, magnification, and translation.
4. It collects top-level conductors on `li1` and `met1` through `met5`.
5. It expands via references and joins the conductor levels they bridge.
6. Shapely polygon intersections and an `STRtree` spatial index find touching
   shapes. A union-find data structure groups them into electrical nets.
7. Top-level labels become ports; connected cell pins become net endpoints.

For the supplied puzzle this recovers:

| Measurement | Count |
|---|---:|
| Functional instances | 728 |
| Connected cell pins | 2,767 |
| Top-level ports | 13 |
| Nets with endpoints | 744 |

## How replay works

`simulate_netlist.py` maps the used SKY130 cell families to Boolean operations,
including complemented pins and compound AO/OA gates. Flip-flops retain state:
reset initializes them, and each call to `clock()` computes combinational
values, advances each `D` input to its `Q` state, and recomputes outputs.

The decoder resets the circuit, clocks the 121 input bits through `I` with
`enable=1`, then lowers `enable` and samples `O[7:0]` until the zero terminator.
It requires `success` to be observed during the output phase. A successful run
prints:

```text
output bytes: b'(* TWO STARS *)'
final string: (* TWO STARS *)
```

## Reproducibility notes

- `extract_final_string.py` supplies six recovered constant assignments for
  undriven nets in the extracted puzzle. These assignments depend on the
  extractor's deterministic net ordering. If that ordering changes, recover
  the assignments again and pass them with `--constants`.
- `simulate_netlist.py --search-constants`, together with a netlist path and
  `--vcd example_inputs.vcd`, can compare candidate assignments with the
  supplied example waveform.
- The Python cell model performs the replay. The structural Verilog is an
  additional representation of the recovered connectivity.
