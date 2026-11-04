# Inspecting SMT learning during MEV amount optimization

This package contains executed experiments explaining how SMT inference and
incremental solver reuse interact with revenue-threshold search for a fixed
arbitrage route. It includes source code, all recorded timings, satisfying
assignments, input assertions, clause events, and independent validation.

The experiments use synthetic examples and Z3 5.3.3.0 with Python 3.12.14 on
Linux x86-64. They are explanatory experiments, not a reproduction of the
DeFiPoser or MEVisor evaluation. No transactions were submitted anywhere.

## Main results

1. We observed a readable theory clause in an amount-feasibility problem:

       (amount <= 3) OR NOT (4*amount - amount*amount >= 3)

   This says that earning at least 3 requires an amount at most 3. It excludes
   a region, rather than recording the failure of one particular amount.

2. On a synthetic two-pool constant-product route, 17 threshold queries found
   a rational amount approximately 512.3632406574 earning gross profit
   58.7247449993. Every SAT witness was validated using independent exact
   Fraction arithmetic. The final optimum-profit bracket was
   [58.724365234375, 58.7298583984375), narrower than the requested 0.01 tolerance.

3. Across five timed repetitions of that same 17-query sequence, median total
   time inside solver.check() was 7758.916 ms with fresh solvers, 440.044 ms
   with one solver and guarded assumptions, and 431.514 ms with one solver and
   push/pop. These comparisons use SimpleSolver throughout, without callbacks.
   The 17.63x first comparison measures incremental solver reuse on this small
   model. It does not isolate learned clauses as the causal explanation.

4. Attaching OnClause to Solver() or SolverFor('QF_NRA') changed the observed
   solving engine on the polynomial probe. Instrumentation can therefore
   confound a naive performance comparison.

5. A separate exact integer model, including both floor divisions, had maximum
   gross profit 58, confirmed by exhaustive enumeration of all 5000 inputs.

## Run the experiments

Create an isolated environment and install the pinned dependency:

    python3 -m venv .venv
    source .venv/bin/activate
    python -m pip install -r requirements.txt

Start with the small, readable example:

    python inspect_clauses.py

Run the AMM search and the five-repeat timing comparison:

    python experiment.py --repeats 5 --timeout-ms 2000

Run the rounding validation and engine/callback probe:

    python integer_validation.py
    python engine_probe.py

Running a script replaces its own result files. Timings, selected SAT models,
and internal clause sequences can vary by Z3 version, platform, and solver
history. The pinned version documents the environment of the included run;
it is not a claim that this is the best version for other workloads.

## 1. What a learned clause means

A model is a concrete satisfying assignment, such as amount = 3. A clause is
an OR of literals. Arithmetic predicates can be literals, for example
amount <= 3 or NOT(profit >= 3).

In a CDCL(T) SMT core, Boolean search is combined with theory reasoning. A
contradictory combination of choices can produce a clause that excludes that
combination. Theory solvers also produce valid lemmas used in propagation and
conflict reasoning. This is logical inference, not training a statistical model.

Our readable example uses the illustrative profit polynomial:

    profit(q) = 4*q - q*q, with 0 <= q <= 4.

One observed theory clause was:

    q <= 3 OR NOT(profit(q) >= 3)

Equivalently:

    profit(q) >= 3 IMPLIES q <= 3.

The mathematical explanation is that profit(q) >= 3 is equivalent to
(q-1)*(q-3) <= 0, hence 1 <= q <= 3. The actual clause encodes the upper-bound
consequence. This algebra is our explanation of the observed clause; the
callback supplied the generic 'smt' proof hint, not a detailed algebraic proof.

For target 4.1, the trace included:

    NOT(profit(q) >= 41/10)

Indeed, profit(q) = 4 - (q-2)^2 <= 4. This unit clause refutes that requirement.

### Actual sequential outcomes

| Required profit | Result | Returned amount | Returned profit |
|---|---|---:|---:|
| 3 | SAT | 3 | 3 |
| 4.1 | UNSAT | n/a | n/a |
| 3.9 | SAT | 2 | 4 |
| 3.9 again | SAT | 9/4 | 63/16 = 3.9375 |

A SAT call need only supply a qualifying amount. It need not return the
maximizing amount, nor the same model on a repeated query. The repeated 3.9
query emitted 21 new theory-inference events, versus 12 for the previous query.
Reusing a solver does not guarantee that every individual query becomes easier.

The complete captured output is in results/toy_clause_trace.txt.

## 2. What is observable through Z3

The tested interface is z3.OnClause(solver, callback), with callback arguments
(proof, dependencies, clause). Keep the observer object alive.

| Observation | Meaning |
|---|---|
| solver.model() | A satisfying assignment after SAT |
| solver.assertions(), solver.sexpr() | Added input constraints |
| solver.statistics() | Engine-dependent aggregate counters |
| OnClause events | Observed assumptions/encodings, inferences, and deletion events |

An 'assumption' event is not automatically a learned clause. An 'smt' event
is a theory inference; 'rup' denotes a propositional inference. Some emitted
clauses are tautologies, duplicates, or transient facts. An event log is not
a complete snapshot of distinct clauses currently retained by the solver.

The AMM trace recorded 74 assumption/encoding events, 142 theory-inference
events, and 10 propositional-inference events. This does not establish that
152 distinct learned clauses were retained at the end. The trace emitted no
'del' events while internal deletion counters were nonzero, another reason
not to reconstruct a complete live database from these callbacks alone.

For a complete current clause-database inventory, lower-level engine-specific
instrumentation and tracking of lifetimes/scopes would be needed. This package
inspects exposed inference events and statistics, not an undocumented database
dump. Clause callback support is engine- and version-dependent.

## 3. Fixed-route AMM model

The route is A -> B on pool 1, then B -> A on pool 2. Both pools are distinct.
Their synthetic reserves are:

| Pool | Input reserve | Output reserve |
|---|---:|---:|
| Pool 1, A -> B | 10000 A | 25000 B |
| Pool 2, B -> A | 20000 B | 10000 A |

Both swaps use a fee fraction of 3/1000. The continuous swap formula is:

    output = 997 * input * reserve_out / (1000 * reserve_in + 997 * input)

For real-valued input x, intermediate B amount y, and final A amount z, the
main experiment asserts:

    1 <= x <= 5000
    0 <= y < 25000
    0 <= z < 10000
    y * (10000000 + 997*x) = 24925000*x
    z * (20000000 + 997*y) = 9970000*y

The denominators are strictly positive, so multiplying them out preserves
the pricing equations. Gross profit is z-x. Gas, borrowing fees, ordering,
other transactions, and full contract execution are outside this small model.

### Threshold search

Each query chooses a revenue threshold r and asks for any x,y,z satisfying
the base constraints and z-x >= r. A guard activates only the current target.
The route, reserves, fees, and amount domain stay fixed throughout the run.

Following the exponential-bracketing pattern in DeFiPoser's Algorithm 3, the
run first tested 1, 10, and 100. The first two were SAT and the third UNSAT.
Binary search then narrowed [10,100] until the width was at most 0.01.

The final returned witness was:

    x = 2430000000 / 4742729       ~= 512.3632406574 A
    y = 1215                      = 1215 B
    z = 2422710000 / 4242271       ~= 571.0879856567 A
    profit                        ~= 58.7247449993 A

Each SAT witness is re-evaluated with Python Fraction arithmetic, checking
both swap equations and the requested threshold exactly.

An independent calculus calculation for this particular two-pool continuous
model gives:

    optimum input  ~= 512.0755110457
    maximum profit ~= 58.7247616314

This falls inside the SMT bracket. The returned amount is near-optimal in
profit; binary-search tolerance is not a distance guarantee on the amount.
This two-pool example admits a closed-form solution, so SMT is used here for
inspection and comparison rather than as the simplest way to solve this case.

## 4. Reuse comparison

All modes replay the same 17 thresholds in the same order, use SimpleSolver
with random seed 0, and have a 2000 ms timeout per check. All 255 recorded
checks across three modes and five trials agreed on SAT/UNSAT; none was UNKNOWN.
Each mode also received an unreported warm-up sequence. Mode order rotates
between trials. There is no OnClause observer during timing.

| Mode | Median sum of check() time | Range across five runs | Median full harness time |
|---|---:|---:|---:|
| Fresh solver per threshold | 7758.916 ms | 7568.849 to 8406.472 ms | 7809.650 ms |
| Reuse one solver, guarded assumptions | 440.044 ms | 412.910 to 472.881 ms | 562.263 ms |
| Reuse one solver, push/pop | 431.514 ms | 421.555 to 472.949 ms | 539.113 ms |

Fresh and guarded-reuse modes use the same guard encoding for the current
target. The persistent mode additionally has the previous guarded formulas
and its retained internal state. Push/pop scopes the changing target instead.

The check-only ratios are 17.63x and 17.98x. The full-harness fresh/guarded ratio
is 13.89x. Full-harness time includes construction, assertions, checks, model
extraction, exact model validation, and statistics collection. These figures
should not be interchanged.

The first timed sequence produced the following aggregate counters during
query preparation and checking:

| Counter | Fresh | Guarded reuse | Push/pop reuse |
|---|---:|---:|---:|
| conflicts | 28 | 18 | 10 |
| arith-nla-lemmas | 123 | 27 | 19 |
| nlsat conflicts | 40 | 2 | 2 |

These are work counters, not counts of retained clauses. Query counter deltas
exclude cleanup after check(), such as pop(). Deletion totals are deliberately
omitted because they would miss deletions during pop(). Full per-query snapshots
remain in results/results.json.

The result supports incremental reuse for this sequence. It does not prove
that clauses alone explain the speedup: arithmetic state, search heuristics,
existing internalized expressions, and previous guarded formulas also differ.
This is one small workload, not a general performance estimate for MEV search.

## 5. Integer rounding changes the economic problem

The supplementary integer experiment uses the same reserves and domains but
floors both swap outputs, as in the referenced integer quote formula. For
positive denominator d and numerator n, y=floor(n/d) is encoded exactly as:

    y*d <= n < (y+1)*d

The solver returned x=513, y=1216, z=571, profit=58. The threshold >=58 was SAT
and >=59 was UNSAT. Exhaustive evaluation of all 5000 integer inputs confirmed
that 58 is the maximum, attained by 100 different inputs. The first maximizing
input is 456 and the last 569; not every integer between them is a maximizer.

The included fresh/reused boundary timings are single diagnostic checks,
not repeated benchmark medians. The integer and continuous versions solve
different constraints; their timings should not be compared as equivalent work.

## 6. Callback and solver dispatch probe

Each configuration runs in a fresh Python subprocess, using the same formula:

    0 <= q <= 4 AND 4*q - q*q >= 4.1

| Constructor | Without callback | With OnClause |
|---|---|---|
| Solver() | NLSat counters, UNSAT | SMT arithmetic counters, 5 events, UNSAT |
| SolverFor('QF_NRA') | NLSat counters, UNSAT | SMT arithmetic counters, 5 events, UNSAT |
| SimpleSolver() | SMT arithmetic counters, UNSAT | Same engine family, 5 events, UNSAT |
| Tactic('smt').solver() | UNSAT | UNSAT, zero events in this test |
| Tactic('qfnra-nlsat').solver() | NLSat counters, UNSAT | Registration rejected as unsupported |

The probe records errors rather than silently substituting a different engine.
An empty event stream does not show that there was no internal learning. The
main benchmark fixes SimpleSolver and times it without callbacks to avoid this
specific dispatch confound. Observed behavior belongs to the tested version.

## 7. Reuse between revenue queries versus between blocks

Changing the profit threshold while retaining the same route and pool state
is a natural setting for incremental solving. Across blocks, reserve constants
or contract state can change. A clause derived under an old state cannot simply
be asserted as an unconditional fact about the new state.

A future state-reuse experiment should make the changing state explicit,
guard state-dependent assumptions, and let the solver manage dependencies.
Useful next measurements would include which inferences are state-independent,
which depend on a threshold, and which are invalidated by a reserve change.
This package does not claim to have performed that across-block experiment.

## Files

| File | Purpose |
|---|---|
| inspect_clauses.py | Small readable polynomial and guarded queries |
| experiment.py | Continuous AMM search, clause log, and reuse benchmark |
| integer_validation.py | Exact floor model plus exhaustive oracle |
| engine_probe.py | Fresh-process callback/engine comparisons |
| requirements.txt | Pinned Z3 dependency |
| results/results.json | Main query, model, timing, and statistics records |
| results/clause_events.jsonl | Every captured main-experiment callback event |
| results/toy_clause_trace.txt | Readable toy inference trace |
| results/base_constraints.smt2 | Input model before threshold search |
| results/assertions_after_search.smt2 | Asserted inputs after search, not learned DB |
| results/query_summary.csv | Readable main-query outcomes and witnesses |
| results/timing_summary.csv | Main repeated timing summaries |
| integer_validation_results.json | Integer checks, validation, exhaustive oracle |
| engine_probe_results.json | Inputs, counters, events, and errors for each engine |

## Primary references

- DeFiPoser, On the Just-In-Time Discovery of Profit-Generating Transactions in
  DeFi Protocols, especially Appendix E and Algorithm 3 in Appendix F:
  https://arxiv.org/pdf/2103.02228
- Z3 guide, Inference logs and proofs:
  https://microsoft.github.io/z3guide/programming/Proof%20Logs/
- Z3 OnClause Python API:
  https://z3prover.github.io/api/html/classz3py_1_1_on_clause.html
- Programming Z3, especially incrementality, CDCL(T), and NLSat:
  https://z3prover.github.io/papers/programmingz3
- Z3 Solver Python API:
  https://z3prover.github.io/api/html/classz3py_1_1_solver.html
- Uniswap V2 periphery quote implementation, getAmountOut:
  https://raw.githubusercontent.com/Uniswap/v2-periphery/master/contracts/libraries/UniswapV2Library.sol

The reported numeric results come from the included experiment outputs, not
from the referenced papers. These sources establish the APIs and formula used.
