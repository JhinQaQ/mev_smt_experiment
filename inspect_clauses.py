#!/usr/bin/env python3
"""Trace a small amount-feasibility problem with Z3's SMT core.

This polynomial is illustrative, not a protocol's pricing formula. The outer
application chooses a required revenue; Z3 solves for the amount. Guards make
each requirement optional between checks. A repeated query is included to show
the observed behavior of this particular incremental run, not a speed guarantee.

OnClause reports assumptions, inferences, and deletion events. It does not
return a snapshot of the solver's live learned-clause database. SimpleSolver is
chosen explicitly because attaching this observer to a combined solver can
change which solving engine it selects.
"""

from collections import Counter

import z3


def main():
    amount = z3.Real("amount")
    revenue = 4 * amount - amount * amount
    solver = z3.SimpleSolver()
    solver.set(timeout=3000)
    events = []

    def on_clause(proof, dependencies, clause):
        # The literals are OR-ed together, not AND-ed together.
        events.append(
            (str(proof.decl().name()), [str(lit) for lit in clause])
        )

    observer = z3.OnClause(solver, on_clause)
    solver.add(amount >= 0, amount <= 4)
    print("Z3:", z3.get_version_string(), "engine: SimpleSolver")
    print("Illustrative revenue: 4*amount - amount*amount")
    print("Base input assertions:", solver.assertions())
    guards = {}

    for target in ("3", "4.1", "3.9", "3.9"):
        first_event = len(events)
        print(f"\nREQUIRED REVENUE >= {target}")
        if target not in guards:
            guard = z3.Bool("require_revenue_" + target.replace(".", "_"))
            guards[target] = guard
            constraint = z3.Implies(guard, revenue >= z3.RealVal(target))
            solver.add(constraint)
            print("Added input:", constraint)
        else:
            print("Repeated query; no input assertion added.")
        result = solver.check(guards[target])
        print("Result:", result)
        if result == z3.sat:
            model = solver.model()
            print("Model amount:", model.eval(amount))
            print("Model revenue:", model.eval(revenue))
        elif result == z3.unknown:
            print("Reason:", solver.reason_unknown())
        current_events = events[first_event:]
        print("New observed events by hint:", dict(Counter(h for h, _ in current_events)))
        print("Observed events, with assumptions distinguished from inferences:")
        for hint, literals in current_events:
            label = {
                "assumption": "ASSUMPTION / ENCODING",
                "assume": "ASSUMPTION / ENCODING",
                "smt": "THEORY INFERENCE",
                "rup": "PROPOSITIONAL INFERENCE",
                "del": "DELETION",
            }.get(hint, "INFERENCE HINT " + hint)
            print(f"  [{label}] " + (" OR ".join(f"({x})" for x in literals) or "False"))
        stats = solver.statistics()
        print("Cumulative conflict / clause / lemma counters:", {
            key: stats.get_key_value(key)
            for key in stats.keys()
            if any(word in key for word in ("conflict", "clause", "lemma"))
        })

    print("\nFinal asserted inputs, not the learned-clause database:")
    print(solver.assertions())


if __name__ == "__main__":
    main()
