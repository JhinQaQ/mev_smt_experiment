#!/usr/bin/env python3
"""Inspect SMT clause events during fixed-route AMM amount optimization.

Synthetic reserves, real-number constant-product model, no gas or EVM execution.
This is a small explanatory experiment, not a DeFiPoser reproduction.
Requires the version pinned in requirements.txt. See README.md for interpretation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, localcontext
from fractions import Fraction
import json
from pathlib import Path
import platform
import statistics
import sys
import time

import z3


RESERVES = {"pool_1_A": 10000, "pool_1_B": 25000,
            "pool_2_B": 20000, "pool_2_A": 10000}
MAX_INPUT = 5000
TIMEOUT_MS = 5000
OUTPUT = Path(__file__).resolve().parent / "results"


def qval(value: Fraction | int) -> z3.RatNumRef:
    value = Fraction(value)
    return z3.RealVal(f"{value.numerator}/{value.denominator}")


def stats_dict(solver):
    result = solver.statistics()
    return {key: result.get_key_value(key) for key in result.keys()}


def as_fraction(expr) -> Fraction:
    expr = z3.simplify(expr)
    if z3.is_rational_value(expr):
        return Fraction(expr.numerator_as_long(), expr.denominator_as_long())
    raise ValueError(f"Expected rational model value, got {expr}")


def continuous_swap(amount, reserve_in, reserve_out):
    amount = Fraction(amount)
    return 997 * amount * reserve_out / (1000 * reserve_in + 997 * amount)


def integer_swap(amount, reserve_in, reserve_out):
    return 997 * amount * reserve_out // (1000 * reserve_in + 997 * amount)


def concrete_route(amount, integer=False):
    swap = integer_swap if integer else continuous_swap
    mid = swap(amount, RESERVES["pool_1_A"], RESERVES["pool_1_B"])
    out = swap(mid, RESERVES["pool_2_B"], RESERVES["pool_2_A"])
    return mid, out, out - amount


def analytic_optimum():
    # Composing these two distinct-pool swaps gives output = A*x/(B+C*x).
    with localcontext() as ctx:
        ctx.prec = 60
        fee = Decimal(997) / 1000
        a = fee * fee * RESERVES["pool_1_B"] * RESERVES["pool_2_A"]
        b = Decimal(RESERVES["pool_1_A"] * RESERVES["pool_2_B"])
        c = fee * RESERVES["pool_2_B"] + fee * fee * RESERVES["pool_1_B"]
        amount = ((a * b).sqrt() - b) / c
        assert 1 < amount < MAX_INPUT
        profit = a * amount / (b + c * amount) - amount
        return {"amount": str(amount), "profit": str(profit)}


def integer_oracle():
    records = []
    best = -10**9
    for amount in range(1, MAX_INPUT + 1):
        mid, out, profit = concrete_route(amount, integer=True)
        if profit > best:
            best, records = profit, []
        if profit == best:
            records.append({"amount": amount, "intermediate": mid, "output": out})
    return {"enumerated_inputs": MAX_INPUT, "best_profit": best,
            "maximizing_inputs": records}


def make_solver():
    # Explicit legacy SMT core avoids silently comparing different engines.
    s = z3.SimpleSolver()
    s.set(timeout=TIMEOUT_MS, random_seed=0)
    amount, intermediate, output = z3.Reals("amount intermediate output")
    s.add(amount >= 1, amount <= MAX_INPUT,
          intermediate >= 0, intermediate < RESERVES["pool_1_B"],
          output >= 0, output < RESERVES["pool_2_A"])
    # Both denominators are strictly positive under these bounds.
    s.add(intermediate * (1000 * RESERVES["pool_1_A"] + 997 * amount)
          == 997 * amount * RESERVES["pool_1_B"])
    s.add(output * (1000 * RESERVES["pool_2_B"] + 997 * intermediate)
          == 997 * intermediate * RESERVES["pool_2_A"])
    return s, (amount, intermediate, output)


class ClauseLog:
    """An event stream, deliberately not called a live clause-database snapshot."""
    def __init__(self, solver):
        self.events = []
        self.phase = "setup"
        self.target = None
        self.callback_errors = []
        # Keep the callback object alive. Use only one live OnClause at a time.
        self.hook = z3.OnClause(solver, self.receive)

    def receive(self, proof, deps, clause):
        try:
            self.events.append({
                "event": len(self.events), "phase": self.phase,
                "target": self.target,
                "hint": str(proof.decl().name()),
                "proof": proof.sexpr(), "deps": list(deps),
                "literals": [lit.sexpr() for lit in clause],
                "pretty_literals": [str(lit) for lit in clause],
            })
        except Exception as exc:
            self.callback_errors.append(repr(exc))


def check_target(solver, variables, target, index, mode="assumptions", log=None):
    target = Fraction(target)
    amount, intermediate, output = variables
    target_condition = output - amount >= qval(target)
    before_stats = stats_dict(solver)
    event_offset = len(log.events) if log else 0
    if log:
        log.phase = f"query_{index}"
        log.target = str(target)
    preparation_start = time.perf_counter_ns()
    if mode == "push_pop":
        solver.push()
        solver.add(target_condition)
        assumptions = []
    else:
        goal = z3.Bool(f"goal_{index}")
        solver.add(z3.Implies(goal, target_condition))
        assumptions = [goal]
    check_start = time.perf_counter_ns()
    answer = solver.check(*assumptions)
    check_end = time.perf_counter_ns()
    row = {"index": index, "target": str(target), "target_float": float(target),
           "result": str(answer),
           "prepare_ms": (check_start-preparation_start)/1e6,
           "check_ms": (check_end-check_start)/1e6}
    if answer == z3.sat:
        model = solver.model()
        values = [as_fraction(model.eval(var, model_completion=True)) for var in variables]
        actual_mid, actual_output, actual_profit = concrete_route(values[0])
        # Independent Fraction arithmetic validates each witness exactly.
        assert values[1] == actual_mid and values[2] == actual_output
        assert actual_profit >= target and 1 <= values[0] <= MAX_INPUT
        row["model"] = {"amount": str(values[0]), "amount_float": float(values[0]),
                        "intermediate": str(values[1]), "output": str(values[2]),
                        "profit": str(actual_profit), "profit_float": float(actual_profit),
                        "exact_fraction_validation": True}
    elif answer == z3.unknown:
        row["reason_unknown"] = solver.reason_unknown()
    row["stats_after"] = stats_dict(solver)
    row["counter_deltas"] = {
        key: value - before_stats.get(key, 0)
        for key, value in row["stats_after"].items()
        if key in {"conflicts", "decisions", "propagations", "mk clause",
                   "mk clause binary", "del clause", "arith-nla-lemmas",
                   "nlsat conflicts", "nlsat decisions", "nlsat propagations",
                   "rlimit count"}
    }
    if log:
        row["callback_hints"] = dict(Counter(e["hint"] for e in log.events[event_offset:]))
        row["event_range"] = [event_offset, len(log.events)]
    if mode == "push_pop":
        if log:
            log.phase = f"pop_after_{index}"
        solver.pop()
    return row


def binary_search_with_trace():
    solver, variables = make_solver()
    # The logger is registered before any check; the initial assertions remain
    # visible in the event stream when internalization starts.
    log = ClauseLog(solver)
    assertions_before = solver.sexpr()
    rows = []
    target = Fraction(1)
    row = check_target(solver, variables, target, len(rows), log=log)
    rows.append(row)
    if row["result"] != "sat":
        raise RuntimeError(f"Initial positive profit target not SAT: {row}")
    low, best = target, row["model"]
    high = low * 10
    while True:
        row = check_target(solver, variables, high, len(rows), log=log)
        rows.append(row)
        if row["result"] == "unsat":
            break
        if row["result"] == "unknown":
            raise RuntimeError("Unknown cannot establish an infeasible upper bound")
        low, best = high, row["model"]
        high *= 10
    tolerance = Fraction(1, 100)
    while high - low > tolerance:
        mid = (low + high) / 2
        row = check_target(solver, variables, mid, len(rows), log=log)
        rows.append(row)
        if row["result"] == "sat":
            low, best = mid, row["model"]
        elif row["result"] == "unsat":
            high = mid
        else:
            raise RuntimeError("Unknown: preserve previous bounds, do not label optimal")
    if log.callback_errors:
        raise RuntimeError(log.callback_errors)
    result = {"mode": "SimpleSolver with guarded assumptions and OnClause",
              "profit_lower_bound": str(low), "profit_upper_bound": str(high),
              "gap": str(high-low), "tolerance": str(tolerance),
              "best_model": best, "queries": rows,
              "event_counts": dict(Counter(e["hint"] for e in log.events)),
              "event_count": len(log.events), "callback_errors": log.callback_errors,
              "log_interpretation": "Observed events, not a complete retained-clause snapshot"}
    (OUTPUT / "base_constraints.smt2").write_text(assertions_before + "\n", encoding="utf-8")
    (OUTPUT / "assertions_after_search.smt2").write_text(solver.sexpr()+"\n", encoding="utf-8")
    with (OUTPUT / "clause_events.jsonl").open("w", encoding="utf-8") as stream:
        for event in log.events:
            stream.write(json.dumps(event) + "\n")
    return result


def run_sequence(targets, mode):
    # No callbacks here: instrumentation is excluded from all timings.
    sequence_start = time.perf_counter_ns()
    if mode != "fresh":
        solver, variables = make_solver()
    rows = []
    setup_ms = 0.0
    for index, target in enumerate(targets):
        if mode == "fresh":
            start = time.perf_counter_ns()
            solver, variables = make_solver()
            setup_ms += (time.perf_counter_ns()-start)/1e6
        row = check_target(solver, variables, Fraction(target), index,
                           mode="push_pop" if mode == "push_pop" else "assumptions")
        rows.append(row)
    total_ms = (time.perf_counter_ns()-sequence_start)/1e6
    return {"mode": mode, "end_to_end_ms": total_ms,
            "check_only_ms": sum(r["check_ms"] for r in rows),
            "unknown_count": sum(r["result"] == "unknown" for r in rows),
            "fresh_setup_ms": setup_ms,
            "queries": rows,
            "counter_scope_note": "Counter deltas cover query preparation and check, not scope cleanup after check. Deletion totals are omitted.",
            "counter_totals": {key: sum(r["counter_deltas"].get(key,0) for r in rows)
                               for key in {"conflicts", "arith-nla-lemmas", "nlsat conflicts",
                                           "rlimit count", "mk clause"}}}


def benchmark(targets, expected, repeats):
    modes = ["fresh", "assumptions", "push_pop"]
    # One warm-up sequence per mode, then rotate mode order on each trial.
    for mode in modes:
        warm = run_sequence(targets, mode)
        assert all(row["result"] == "unknown" or row["result"] == truth
                   for row, truth in zip(warm["queries"], expected))
    runs = []
    for trial in range(repeats):
        order = modes[trial % len(modes):] + modes[:trial % len(modes)]
        for mode in order:
            run = run_sequence(targets, mode)
            run["trial"] = trial
            assert all(row["result"] == "unknown" or row["result"] == truth
                       for row, truth in zip(run["queries"], expected))
            runs.append(run)
        print(f"timing trial {trial+1}/{repeats} complete", flush=True)
    summary = {}
    for mode in modes:
        own = [r for r in runs if r["mode"] == mode]
        summary[mode] = {
            "trials": repeats,
            "median_check_only_ms": statistics.median(r["check_only_ms"] for r in own),
            "min_check_only_ms": min(r["check_only_ms"] for r in own),
            "max_check_only_ms": max(r["check_only_ms"] for r in own),
            "median_end_to_end_ms": statistics.median(r["end_to_end_ms"] for r in own),
            "unknown_queries_over_all_trials": sum(r["unknown_count"] for r in own),
            "counter_totals_first_trial": own[0]["counter_totals"],
        }
    return {"description": "Identical target order, same SimpleSolver engine, no callbacks; full sequence per trial",
            "caveat": "Reuse includes more than clauses. Timings do not isolate the causal effect of clause learning.",
            "summary": summary, "runs": runs}


def main():
    global TIMEOUT_MS
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=7)
    parser.add_argument("--trace-only", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=2000)
    args = parser.parse_args()
    TIMEOUT_MS = args.timeout_ms
    OUTPUT.mkdir(exist_ok=True)
    environment = {"z3": z3.get_full_version(), "python": sys.version,
                   "platform": platform.platform(), "machine": platform.machine(),
                   "timeout_ms_per_query": TIMEOUT_MS, "random_seed": 0}
    search = binary_search_with_trace()
    result = {"environment": environment, "reserves": RESERVES,
              "amount_domain": [1, MAX_INPUT], "fee_fraction": "3/1000",
              "semantics": "Real arithmetic, continuous swaps, synthetic reserves, gross profit, no gas",
              "analytic_continuous_optimum": analytic_optimum(),
              "integer_rounding_oracle": integer_oracle(), "search": search}
    print(json.dumps({"bounds": [search["profit_lower_bound"], search["profit_upper_bound"]],
                      "best_model": search["best_model"], "event_counts": search["event_counts"],
                      "query_count": len(search["queries"])}), flush=True)
    # Save the trace result before any optional timing work.
    (OUTPUT / "results.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    if not args.trace_only:
        targets = [r["target"] for r in search["queries"]]
        expected = [r["result"] for r in search["queries"]]
        result["benchmark"] = benchmark(targets, expected, args.repeats)
        (OUTPUT / "results.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(result["benchmark"]["summary"], indent=2), flush=True)
    print("Saved results to", OUTPUT, flush=True)


if __name__ == "__main__":
    main()
