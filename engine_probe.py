#!/usr/bin/env python3
"""Compare clause-observer behavior in fresh Z3 processes.

This is an illustrative amount-feasibility problem, not an AMM model or a
reproduction of DeFiPoser. The formula is 0 <= q <= 4 and 4*q - q*q >= 4.1.
The JSON keeps asserted input, inference events, and solver counters separate.
An event log is not a snapshot of the solver's retained clause database.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import time


SOLVERS = (
    "Solver",
    "SolverFor_QF_NRA",
    "SimpleSolver",
    "Tactic_smt",
    "Tactic_qfnra_nlsat",
)


def event_category(hint: str) -> str:
    if hint in {"assumption", "assume"}:
        return "assumption_or_encoding"
    if hint == "del":
        return "deletion"
    if hint == "rup":
        return "propositional_inference"
    if hint == "smt":
        return "theory_inference"
    return "other_inference_hint"


def child_run(solver_name: str, trace_enabled: bool) -> dict:
    import z3

    factories = {
        "Solver": z3.Solver,
        "SolverFor_QF_NRA": lambda: z3.SolverFor("QF_NRA"),
        "SimpleSolver": z3.SimpleSolver,
        "Tactic_smt": lambda: z3.Tactic("smt").solver(),
        "Tactic_qfnra_nlsat": lambda: z3.Tactic("qfnra-nlsat").solver(),
    }
    amount = z3.Real("amount")
    constraints = [
        amount >= 0,
        amount <= 4,
        4 * amount - amount * amount >= z3.RealVal("4.1"),
    ]
    result = {
        "solver": solver_name,
        "callback_enabled": trace_enabled,
        "z3_version": z3.get_version_string(),
        "python_version": platform.python_version(),
        "timeout_ms": 3000,
        "input_assertions": [c.sexpr() for c in constraints],
        "events": [],
        "event_counts_by_hint": {},
    }
    phase = "construct_solver"
    try:
        solver = factories[solver_name]()
        solver.set(timeout=result["timeout_ms"])

        def on_clause(proof, dependencies, clause):
            # Copy callback values immediately; retain strings, not native handles.
            hint = str(proof.decl().name())
            event = {
                "phase": phase,
                "hint": hint,
                "category": event_category(hint),
                "proof_sexpr": proof.sexpr(),
                "dependencies": list(dependencies),
                "literals": [literal.sexpr() for literal in clause],
            }
            result["events"].append(event)
            counts = result["event_counts_by_hint"]
            counts[hint] = counts.get(hint, 0) + 1

        if trace_enabled:
            phase = "register_callback"
            observer = z3.OnClause(solver, on_clause)
        phase = "assert_inputs"
        solver.add(*constraints)
        # Some solvers normalize formulas when they are added. Compare the
        # solver's own before/after views rather than raw construction syntax.
        result["input_assertions_before_check"] = [
            c.sexpr() for c in solver.assertions()
        ]
        result["sexpr_before_check"] = solver.sexpr()
        phase = "check"
        started = time.perf_counter()
        status = solver.check()
        result["elapsed_check_seconds"] = time.perf_counter() - started
        result["result"] = str(status)
        if status == z3.unknown:
            result["reason_unknown"] = solver.reason_unknown()
        elif status == z3.sat:
            model = solver.model()
            result["model"] = {
                "amount": str(model.eval(amount)),
                "revenue": str(model.eval(4 * amount - amount * amount)),
            }
        stats = solver.statistics()
        result["statistics"] = {
            key: stats.get_key_value(key) for key in stats.keys()
        }
        result["sexpr_after_check"] = solver.sexpr()
        result["input_assertions_after_check"] = [
            c.sexpr() for c in solver.assertions()
        ]
        result["sexpr_unchanged"] = (
            result["sexpr_before_check"] == result["sexpr_after_check"]
        )
        result["input_assertions_unchanged"] = (
            result["input_assertions_before_check"]
            == result["input_assertions_after_check"]
        )
    except Exception as exc:
        result["error"] = {
            "phase": phase,
            "type": type(exc).__name__,
            "message": str(exc),
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--child", choices=SOLVERS)
    parser.add_argument("--trace", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).with_name("engine_probe_results.json"),
    )
    args = parser.parse_args()
    if args.child:
        print(json.dumps(child_run(args.child, args.trace)))
        return

    runs = []
    for solver_name in SOLVERS:
        for trace_enabled in (False, True):
            command = [sys.executable, str(Path(__file__).resolve()), "--child", solver_name]
            if trace_enabled:
                command.append("--trace")
            try:
                completed = subprocess.run(
                    command, capture_output=True, text=True, timeout=15, check=False
                )
                if completed.returncode:
                    run = {
                        "solver": solver_name,
                        "callback_enabled": trace_enabled,
                        "error": {
                            "phase": "subprocess",
                            "returncode": completed.returncode,
                            "stderr": completed.stderr,
                        },
                    }
                else:
                    run = json.loads(completed.stdout)
                    if completed.stderr:
                        run["stderr"] = completed.stderr
            except subprocess.TimeoutExpired:
                run = {
                    "solver": solver_name,
                    "callback_enabled": trace_enabled,
                    "error": {"phase": "subprocess", "message": "15 second timeout"},
                }
            runs.append(run)
            status = run.get("result", "error")
            print(
                f"{solver_name:22} callback={str(trace_enabled):5} "
                f"{status:7} events={run.get('event_counts_by_hint', {})}"
            )

    report = {
        "experiment": "Clause callback and engine selection probe",
        "formula": "0 <= amount <= 4; 4*amount - amount*amount >= 41/10",
        "expected_result": "unsat, because 4*q-q*q = 4-(q-2)^2 <= 4",
        "isolation": "Each configuration runs in a fresh Python process.",
        "interpretation_notes": [
            "Assumption and encoding events are not learned clauses.",
            "Inference events need not equal the currently retained clause database.",
            "Counters and callback coverage depend on engine and Z3 version.",
            "Registering a callback can alter the selected solving engine.",
            "An empty trace is not evidence that the solver performs no learning.",
            "Timings are diagnostic only, not a solver performance benchmark.",
        ],
        "runs": runs,
    }
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
