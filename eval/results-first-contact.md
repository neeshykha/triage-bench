# Results: every pipeline on the gold set

60 test cases, labeled blind by Aneesh. Eval run `20261029-231209-eval`; prompts were tuned on the dev set only, and all pipelines ran at the same time, so latency is measured under the same load.
Ranges are 95% Wilson intervals: with 60 cases, pipelines whose ranges overlap may not really differ. For scale, the label the case generator was asked to write matched Aneesh's on 39/60 (65%) of categories and 34/60 (57%) of priorities.

| Pipeline | Category (95% range) | Priority (95% range) | P1 recall (95% range) | P1 precision | Under / over-triaged | Human-Review | Cost per case | Latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| **A** Triage agent alone | 43/60, 72% (59-81) | 36/60, 60% (47-71) | 15/16, 94% (72-99) | 15/19 | 17 / 7 | 0 | $0.0103 | 4.79s / 7.87s |
| **B** Triage + rules | 43/60, 72% (59-81) | 36/60, 60% (47-71) | 15/16, 94% (72-99) | 15/18 | 18 / 6 | 0 | $0.0103 | 4.99s / 8.61s |
| **C** Triage + challenger | 45/60, 75% (63-84) | 36/60, 60% (47-71) | 15/16, 94% (72-99) | 15/19 | 17 / 7 | 0 | $0.0155 | 8.1s / 12.12s |
| **D** Triage + challenger + rules (full system) | 43/60, 72% (59-81) | 36/60, 60% (47-71) | 15/16, 94% (72-99) | 15/19 | 17 / 7 | 0 | $0.0154 | 7.52s / 12.22s |
| **E** Haiku triage alone | 45/60, 75% (63-84) | 37/60, 62% (49-73) | 15/16, 94% (72-99) | 15/18 | 16 / 7 | 0 | $0.0004 | 3.67s / 5.66s |
| **F** Haiku triage + Opus challenger | 44/60, 73% (61-83) | 36/60, 60% (47-71) | 15/16, 94% (72-99) | 15/18 | 17 / 7 | 0 | $0.0083 | 6.94s / 19.52s |

Pipelines A, B, C, D run the same triage agent, and their first passes differed on 6 of 60 cases from run-to-run variation alone, so gaps of a few cases between them are noise.

## Against the generator's label

Each case was written from a scenario whose label follows the written taxonomy. Comparing each pipeline with that label and with Aneesh's shows whether misses come from the models or from a gap between the taxonomy and his judgment.

| Pipeline | Category and priority both right, vs Aneesh | vs the generator's label | Rule overrides |
|---|---|---|---|
| A | 25/60 (42%) | 47/60 (78%) | 0 |
| B | 25/60 (42%) | 46/60 (77%) | 0 |
| C | 27/60 (45%) | 48/60 (80%) | 0 |
| D | 25/60 (42%) | 49/60 (82%) | 0 |
| E | 27/60 (45%) | 45/60 (75%) | 0 |
| F | 27/60 (45%) | 47/60 (78%) | 0 |

## The challenger

Routing objections scored against the gold label of the first pass it saw. Draft objections have no label, so they're counted apart.

| Pipeline | Caught wrong first passes | False alarms on right ones | After revision: fixed / broke | Draft objections (accepted) |
|---|---|---|---|---|
| C | 0 of 36 | 0 of 24 | 0 / 0 | 0 (0) |
| D | 0 of 38 | 0 of 22 | 0 / 0 | 0 (0) |
| F | 3 of 38 | 0 of 22 | 1 / 0 | 8 (8) |

## Injection resistance

| Pipeline | Flagged | Not swayed (right category and priority) |
|---|---|---|
| A | 3 of 3 | 2 of 3 |
| B | 3 of 3 | 2 of 3 |
| C | 3 of 3 | 2 of 3 |
| D | 3 of 3 | 2 of 3 |
| E | 3 of 3 | 2 of 3 |
| F | 3 of 3 | 2 of 3 |

## Priority confusion

Rows are the gold label, columns what the pipeline said.

**A**

| gold \ said | P1 | P2 | P3 | P4 | error |
|---|---|---|---|---|---|
| P1 | 15 | 0 | 0 | 1 | 0 |
| P2 | 4 | 4 | 10 | 0 | 0 |
| P3 | 0 | 1 | 13 | 6 | 0 |
| P4 | 0 | 0 | 2 | 4 | 0 |

**B**

| gold \ said | P1 | P2 | P3 | P4 | error |
|---|---|---|---|---|---|
| P1 | 15 | 0 | 1 | 0 | 0 |
| P2 | 3 | 5 | 10 | 0 | 0 |
| P3 | 0 | 1 | 12 | 7 | 0 |
| P4 | 0 | 0 | 2 | 4 | 0 |

**C**

| gold \ said | P1 | P2 | P3 | P4 | error |
|---|---|---|---|---|---|
| P1 | 15 | 0 | 1 | 0 | 0 |
| P2 | 4 | 4 | 10 | 0 | 0 |
| P3 | 0 | 1 | 13 | 6 | 0 |
| P4 | 0 | 0 | 2 | 4 | 0 |

**D**

| gold \ said | P1 | P2 | P3 | P4 | error |
|---|---|---|---|---|---|
| P1 | 15 | 0 | 1 | 0 | 0 |
| P2 | 4 | 4 | 10 | 0 | 0 |
| P3 | 0 | 1 | 13 | 6 | 0 |
| P4 | 0 | 0 | 2 | 4 | 0 |

**E**

| gold \ said | P1 | P2 | P3 | P4 | error |
|---|---|---|---|---|---|
| P1 | 15 | 0 | 1 | 0 | 0 |
| P2 | 3 | 5 | 10 | 0 | 0 |
| P3 | 0 | 0 | 15 | 5 | 0 |
| P4 | 0 | 0 | 4 | 2 | 0 |

**F**

| gold \ said | P1 | P2 | P3 | P4 | error |
|---|---|---|---|---|---|
| P1 | 15 | 0 | 1 | 0 | 0 |
| P2 | 3 | 5 | 10 | 0 | 0 |
| P3 | 0 | 1 | 13 | 6 | 0 |
| P4 | 0 | 0 | 3 | 3 | 0 |

## Most-confused categories

- **A**: Resident App & Account as Access & Locks (3), Hardware/RMA as Access & Locks (3), Property Manager Admin as Billing (property-level) (2)
- **B**: Hardware/RMA as Access & Locks (3), Access & Locks as Hardware/RMA (2), Property Manager Admin as Billing (property-level) (2)
- **C**: Hardware/RMA as Access & Locks (3), Property Manager Admin as Billing (property-level) (2), Property Manager Admin as Hub & Connectivity (2)
- **D**: Resident App & Account as Access & Locks (3), Hardware/RMA as Access & Locks (3), Access & Locks as Hardware/RMA (2)
- **E**: Hardware/RMA as Access & Locks (3), Property Manager Admin as Billing (property-level) (2), Property Manager Admin as Hub & Connectivity (2)
- **F**: Resident App & Account as Access & Locks (3), Hardware/RMA as Access & Locks (3), Property Manager Admin as Billing (property-level) (2)

## Missed by every pipeline

- NG-003: gold Access & Locks / P2; 6 of 6 said Access & Locks / P3 (the generator's label) [duplicate_followup]
- NG-005: gold Hub & Connectivity / P2; 6 of 6 said Hub & Connectivity / P3 (the generator's label)
- NG-006: gold Property Manager Admin / P4; 6 of 6 said Billing (property-level) / P4 [non_english]
- NG-024: gold Hub & Connectivity / P2; 6 of 6 said Hub & Connectivity / P3 (the generator's label)
- NG-027: gold Access & Locks / P3; 6 of 6 said Property Manager Admin / P4 (the generator's label)
- NG-033: gold Spam/Other / P4; 6 of 6 said Resident App & Account / P3 (the generator's label)
- NG-038: gold Resident App & Account / P2; 6 of 6 said Resident App & Account / P3 (the generator's label)
- NG-041: gold Resident App & Account / P2; 6 of 6 said Resident App & Account / P3 (the generator's label)
- NG-059: gold Resident App & Account / P2; 6 of 6 said Resident App & Account / P3
- NG-060: gold Property Manager Admin / P1; 6 of 6 said Hub & Connectivity / P1 (the generator's label) [pm_vs_resident]
- NG-064: gold Property Manager Admin / P1; 6 of 6 said Hub & Connectivity / P1 (the generator's label)
- NG-068: gold Resident App & Account / P1; 6 of 6 said Access & Locks / P1 (the generator's label) [pm_vs_resident]
- NG-069: gold Resident App & Account / P3; 6 of 6 said Resident App & Account / P4 (the generator's label) [non_english]
- NG-079: gold Resident App & Account / P3; 6 of 6 said Hub & Connectivity / P3 (the generator's label) [injection]
- NG-095: gold Leak/Sensor Alert / P2; 6 of 6 said Leak/Sensor Alert / P3 (the generator's label)
- NG-096: gold Thermostat/HVAC Controls / P3; 5 of 6 said Thermostat/HVAC Controls / P4 (the generator's label) [two_issues]
- NG-097: gold Hub & Connectivity / P4; 6 of 6 said Thermostat/HVAC Controls / P3 (the generator's label)
- NG-113: gold Hardware/RMA / P2; 6 of 6 said Access & Locks / P3 (the generator's label) [angry_caps]
- NG-119: gold Resident App & Account / P2; 6 of 6 said Resident App & Account / P3
- NG-127: gold Property Manager Admin / P3; 6 of 6 said Billing (property-level) / P4 (the generator's label)
- NG-131: gold Billing (property-level) / P1; 5 of 6 said Billing (property-level) / P3 (the generator's label) [angry_caps]
- NG-138: gold Billing (property-level) / P3; 6 of 6 said Billing (property-level) / P4 (the generator's label)
- NG-139: gold Access & Locks / P2; 6 of 6 said Access & Locks / P1
- NG-158: gold Access & Locks / P2; 6 of 6 said Access & Locks / P1 (the generator's label)
- NG-159: gold Hardware/RMA / P1; 6 of 6 said Access & Locks / P1 [two_issues]
- NG-161: gold Hardware/RMA / P3; 6 of 6 said Access & Locks / P3 (the generator's label) [short_sms]
- NG-189: gold Resident App & Account / P3; 5 of 6 said Access & Locks / P2 (the generator's label)
- NG-190: gold Hardware/RMA / P2; 6 of 6 said Hardware/RMA / P3 (the generator's label) [ambiguous_category]
- NG-196: gold Resident App & Account / P3; 6 of 6 said Thermostat/HVAC Controls / P3 (the generator's label)
- NG-200: gold Access & Locks / P2; 6 of 6 said Access & Locks / P1

Every miss, per pipeline, is in `eval/results.json`.
