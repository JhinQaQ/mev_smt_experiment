#!/usr/bin/env python3
"""Validate a small integer AMM arbitrage instance with exact floor rounding.

Run with a Python environment containing z3-solver. The result is written to
integer_validation_results.json beside this script. These are diagnostic
single-run timings, not a performance benchmark or an isolated measurement
of learned-clause reuse.
"""

from __future__ import annotations

import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import z3


AMOUNT_MIN = 1
AMOUNT_MAX = 5000
FEE_NUMERATOR = 997
FEE_DENOMINATOR = 1000
POOL1_A = 10000
POOL1_B = 25000
POOL2_B = 20000
POOL2_A = 10000
TARGETS = (58, 59)
TIMEOUT_MS = 2000
RANDOM_SEED = 0


def swap_exact_in(amount: int, reserve_in: int, reserve_out: int) -> int:
    """Uniswap V2-style amount-out arithmetic, using Python integer division."""
    numerator = FEE_NUMERATOR * amount * reserve_out
    denominator = FEE_DENOMINATOR * reserve_in + FEE_NUMERATOR * amount
    return numerator // denominator


def evaluate_route(amount: int) -> dict[str, int]:
    intermediate = swap_exact_in(amount, POOL1_A, POOL1_B)
    output = swap_exact_in(intermediate, POOL2_B, POOL2_A)
    return {
        "input_a": amount,
        "intermediate_b": intermediate,
        "output_a": output,
        "gross_profit_a": output - amount,
    }


def build_base_constraints():
    amount, intermediate, output = z3.Ints("input_a intermediate_b output_a")
    denominator1 = FEE_DENOMINATOR * POOL1_A + FEE_NUMERATOR * amount
    numerator1 = FEE_NUMERATOR * amount * POOL1_B
    denominator2 = FEE_DENOMINATOR * POOL2_B + FEE_NUMERATOR * intermediate
    numerator2 = FEE_NUMERATOR * intermediate * POOL2_A
    constraints = [
        amount >= AMOUNT_MIN,
        amount <= AMOUNT_MAX,
        intermediate >= 0,
        intermediate < POOL1_B,
        output >= 0,
        output < POOL2_A,
        denominator1 > 0,
        denominator2 > 0,
        intermediate * denominator1 <= numerator1,
        numerator1 < (intermediate + 1) * denominator1,
        output * denominator2 <= numerator2,
        numerator2 < (output + 1) * denominator2,
    ]
    return (amount, intermediate, output), constraints


def make_solver(base_constraints):
    # SimpleSolver selects the same general SMT engine in both comparison modes.
    # No learned-clause callback is installed.
    solver = z3.SimpleSolver()
    solver.set(timeout=TIMEOUT_MS, random_seed=RANDOM_SEED)
    solver.add(*base_constraints)
    return solver


def validate_witness(witness: dict[str, int], target: int) -> dict[str, bool]:
    amount = witness["input_a"]
    intermediate = witness["intermediate_b"]
    output = witness["output_a"]
    direct = evaluate_route(amount)
    denominator1 = FEE_DENOMINATOR * POOL1_A + FEE_NUMERATOR * amount
    numerator1 = FEE_NUMERATOR * amount * POOL1_B
    denominator2 = FEE_DENOMINATOR * POOL2_B + FEE_NUMERATOR * intermediate
    numerator2 = FEE_NUMERATOR * intermediate * POOL2_A
    checks = {
        "input_in_domain": AMOUNT_MIN <= amount <= AMOUNT_MAX,
        "outputs_in_pool_bounds": (
            0 <= intermediate < POOL1_B and 0 <= output < POOL2_A
        ),
        "positive_denominators": denominator1 > 0 and denominator2 > 0,
        "first_floor_inequalities": (
            intermediate * denominator1 <= numerator1
            < (intermediate + 1) * denominator1
        ),
        "second_floor_inequalities": (
            output * denominator2 <= numerator2 < (output + 1) * denominator2
        ),
        "matches_direct_integer_evaluation": witness == direct,
        "meets_profit_target": output - amount >= target,
    }
    checks["all_passed"] = all(checks.values())
    return checks


def check_target(solver, variables, target: int, mode: str, oracle_rows):
    start = perf_counter()
    result = solver.check()
    elapsed = perf_counter() - start
    feasible_count = sum(row["gross_profit_a"] >= target for row in oracle_rows)
    expected = "sat" if feasible_count else "unsat"
    record = {
        "mode": mode,
        "target_gross_profit_a": target,
        "result": str(result),
        "check_seconds": elapsed,
        "oracle_expected_result": expected,
        "oracle_feasible_input_count": feasible_count,
        "matches_oracle": None if result == z3.unknown else str(result) == expected,
    }
    if result == z3.sat:
        model = solver.model()
        amount, intermediate, output = [
            model.eval(variable, model_completion=True).as_long()
            for variable in variables
        ]
        witness = {
            "input_a": amount,
            "intermediate_b": intermediate,
            "output_a": output,
            "gross_profit_a": output - amount,
        }
        record["witness"] = witness
        record["witness_validation"] = validate_witness(witness, target)
    elif result == z3.unknown:
        record["reason_unknown"] = solver.reason_unknown()
    return record


def main() -> int:
    oracle_start = perf_counter()
    oracle_rows = [evaluate_route(amount) for amount in range(AMOUNT_MIN, AMOUNT_MAX + 1)]
    best_profit = max(row["gross_profit_a"] for row in oracle_rows)
    winners = [row for row in oracle_rows if row["gross_profit_a"] == best_profit]
    oracle_seconds = perf_counter() - oracle_start

    variables, base_constraints = build_base_constraints()
    amount, _, output = variables
    results = []

    for target in TARGETS:
        fresh_solver = make_solver(base_constraints)
        fresh_solver.add(output - amount >= target)
        results.append(check_target(fresh_solver, variables, target, "fresh", oracle_rows))

    reused_solver = make_solver(base_constraints)
    for target in TARGETS:
        reused_solver.push()
        try:
            reused_solver.add(output - amount >= target)
            results.append(
                check_target(reused_solver, variables, target, "reused_push_pop", oracle_rows)
            )
        finally:
            reused_solver.pop()

    unknown_count = sum(record["result"] == "unknown" for record in results)
    all_sat_models_valid = all(
        record["witness_validation"]["all_passed"]
        for record in results
        if record["result"] == "sat"
    )
    all_definitive_answers_match = all(
        record["matches_oracle"] for record in results if record["result"] != "unknown"
    )
    if not all_sat_models_valid or not all_definitive_answers_match:
        validation_status = "failed"
    elif unknown_count:
        validation_status = "incomplete"
    else:
        validation_status = "passed"

    report = {
        "experiment": "Exact integer two-pool AMM profit-boundary validation",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "z3_version": z3.get_version_string(),
            "python_version": platform.python_version(),
            "platform": platform.platform(),
        },
        "configuration": {
            "solver_constructor": "z3.SimpleSolver",
            "timeout_ms_per_check": TIMEOUT_MS,
            "random_seed": RANDOM_SEED,
            "callback_instrumentation": False,
            "target_sequence_per_mode": list(TARGETS),
            "trials_per_mode": 1,
        },
        "model": {
            "route": "A -> B in pool 1; B -> A in distinct pool 2",
            "pool1_reserves": {"A": POOL1_A, "B": POOL1_B},
            "pool2_reserves": {"B": POOL2_B, "A": POOL2_A},
            "input_a_domain_inclusive": [AMOUNT_MIN, AMOUNT_MAX],
            "fee_fraction_per_swap": "3/1000",
            "amount_after_fee_multiplier": "997/1000",
            "amount_out_formula": "floor(997 * amount_in * reserve_out / (1000 * reserve_in + 997 * amount_in))",
            "floor_encoding": "For each swap, denominator > 0 and output * denominator <= numerator < (output + 1) * denominator",
            "smt_theory": "Nonlinear integer arithmetic with arbitrary-precision integers",
            "profit_definition": "Final A output minus initial A input",
            "limitations": [
                "Illustrative fixed reserves and route, with each distinct pool used once.",
                "All quantities are arbitrary raw token units.",
                "Gross profit excludes gas, flash-loan fees, and other transaction costs.",
                "Standard constant-product swaps without transfer-tax tokens or other token behavior.",
                "This is an AMM arithmetic model, not an EVM execution or a claim of live tradability.",
            ],
        },
        "exhaustive_oracle": {
            "method": "Direct Python integer arithmetic for every admissible input",
            "inputs_checked": len(oracle_rows),
            "max_gross_profit_a": best_profit,
            "maximizer_count": len(winners),
            "maximizing_inputs_a": [row["input_a"] for row in winners],
            "first_maximizing_witness": winners[0],
            "last_maximizing_witness": winners[-1],
            "enumeration_seconds": oracle_seconds,
        },
        "timing_interpretation": {
            "diagnostic_single_runs_only": True,
            "benchmark": False,
            "measured_region": "solver.check() only",
            "excluded_from_check_time": [
                "Constraint construction and solver setup",
                "push, adding the threshold constraint, and pop",
                "Model extraction and direct witness validation",
                "Exhaustive oracle enumeration",
            ],
            "caveats": [
                "No warmup or repeated-trial aggregation is performed.",
                "The same solver constructor and settings are used for both modes.",
                "Persistent solving can retain multiple kinds of state; timings do not isolate learned clauses.",
                "The reused solver receives the listed targets in order, with threshold assertions scoped by push and pop.",
                "UNKNOWN remains UNKNOWN and cannot establish infeasibility or optimality.",
            ],
        },
        "checks": results,
        "validation_summary": {
            "status": validation_status,
            "all_sat_models_valid": all_sat_models_valid,
            "all_definitive_answers_match_oracle": all_definitive_answers_match,
            "unknown_count": unknown_count,
        },
    }
    destination = Path(__file__).resolve().with_name("integer_validation_results.json")
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "result_file": str(destination),
        "oracle_max_gross_profit_a": best_profit,
        "checks": results,
        "validation_summary": report["validation_summary"],
    }, indent=2))
    return 0 if validation_status == "passed" else 2 if validation_status == "incomplete" else 1


if __name__ == "__main__":
    sys.exit(main())
