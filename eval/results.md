# Results: every pipeline on the gold set

60 test cases, labeled blind by Aneesh. Eval run `20261029-231209-eval`; prompts were tuned on the dev set only, and all pipelines ran at the same time, so latency is measured under the same load.
Ranges are 95% Wilson intervals: with 60 cases, pipelines whose ranges overlap may not really differ. For scale, the label the case generator was asked to write matched Aneesh's on 44/60 (73%) of categories and 50/60 (83%) of priorities.
After the first run, Aneesh adjudicated the 31 cases every pipeline missed (2026-11-03), seeing what the pipelines said and why: 17 labels changed, 2 accept either answer, and 12 stand. Scores here use the adjudicated labels; the first-contact results are in `eval/results-first-contact.md`.

| Pipeline | Category (95% range) | Priority (95% range) | P1 recall (95% range) | P1 precision | Under / over-triaged | Human-Review | Cost per case | Latency p50 / p95 |
|---|---|---|---|---|---|---|---|---|
| **A** Triage agent alone | 49/60, 82% (70-89) | 53/60, 88% (78-94) | 18/18, 100% (82-100) | 18/19 | 5 / 2 | 0 | $0.0103 | 4.79s / 7.87s |
| **B** Triage + rules | 49/60, 82% (70-89) | 54/60, 90% (80-95) | 18/18, 100% (82-100) | 18/18 | 5 / 1 | 0 | $0.0103 | 4.99s / 8.61s |
| **C** Triage + challenger | 51/60, 85% (74-92) | 53/60, 88% (78-94) | 18/18, 100% (82-100) | 18/19 | 5 / 2 | 0 | $0.0155 | 8.1s / 12.12s |
| **D** Triage + challenger + rules (full system) | 49/60, 82% (70-89) | 53/60, 88% (78-94) | 18/18, 100% (82-100) | 18/19 | 5 / 2 | 0 | $0.0154 | 7.52s / 12.22s |
| **E** Haiku triage alone | 51/60, 85% (74-92) | 55/60, 92% (82-96) | 18/18, 100% (82-100) | 18/18 | 5 / 0 | 0 | $0.0004 | 3.67s / 5.66s |
| **F** Haiku triage + Opus challenger | 50/60, 83% (72-91) | 54/60, 90% (80-95) | 18/18, 100% (82-100) | 18/18 | 5 / 1 | 0 | $0.0083 | 6.94s / 19.52s |

Pipelines A, B, C, D run the same triage agent, and their first passes differed on 4 of 60 cases from run-to-run variation alone, so gaps of a few cases between them are noise.

## Against the generator's label

Each case was written from a scenario whose label follows the written taxonomy. Comparing each pipeline with that label and with Aneesh's shows whether misses come from the models or from a gap between the taxonomy and his judgment.

| Pipeline | Category and priority both right, vs Aneesh | vs the generator's label | Rule overrides |
|---|---|---|---|
| A | 44/60 (73%) | 50/60 (83%) | 0 |
| B | 45/60 (75%) | 49/60 (82%) | 0 |
| C | 46/60 (77%) | 50/60 (83%) | 0 |
| D | 44/60 (73%) | 51/60 (85%) | 0 |
| E | 47/60 (78%) | 48/60 (80%) | 0 |
| F | 46/60 (77%) | 50/60 (83%) | 0 |

## The challenger

Routing objections scored against the gold label of the first pass it saw. Draft objections have no label, so they're counted apart.

| Pipeline | Caught wrong first passes | False alarms on right ones | After revision: fixed / broke | Draft objections (accepted) |
|---|---|---|---|---|
| C | 0 of 16 | 0 of 44 | 0 / 0 | 0 (0) |
| D | 0 of 18 | 0 of 42 | 0 / 0 | 0 (0) |
| F | 1 of 17 | 2 of 43 | 0 / 0 | 8 (8) |

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

| gold \ said | P1 | P2 | P3 | error |
|---|---|---|---|---|---|
| P1 | 18 | 0 | 0 | 0 |
| P2 | 1 | 4 | 5 | 0 |
| P3 | 0 | 1 | 31 | 0 |

**B**

| gold \ said | P1 | P2 | P3 | error |
|---|---|---|---|---|---|
| P1 | 18 | 0 | 0 | 0 |
| P2 | 0 | 5 | 5 | 0 |
| P3 | 0 | 1 | 31 | 0 |

**C**

| gold \ said | P1 | P2 | P3 | error |
|---|---|---|---|---|---|
| P1 | 18 | 0 | 0 | 0 |
| P2 | 1 | 4 | 5 | 0 |
| P3 | 0 | 1 | 31 | 0 |

**D**

| gold \ said | P1 | P2 | P3 | error |
|---|---|---|---|---|---|
| P1 | 18 | 0 | 0 | 0 |
| P2 | 1 | 4 | 5 | 0 |
| P3 | 0 | 1 | 31 | 0 |

**E**

| gold \ said | P1 | P2 | P3 | error |
|---|---|---|---|---|---|
| P1 | 18 | 0 | 0 | 0 |
| P2 | 0 | 5 | 5 | 0 |
| P3 | 0 | 0 | 32 | 0 |

**F**

| gold \ said | P1 | P2 | P3 | error |
|---|---|---|---|---|---|
| P1 | 18 | 0 | 0 | 0 |
| P2 | 0 | 5 | 5 | 0 |
| P3 | 0 | 1 | 31 | 0 |

## Most-confused categories

- **A**: Hardware/RMA as Access & Locks (3), Resident App & Account as Access & Locks (2), Resident App & Account as Thermostat/HVAC Controls (2)
- **B**: Hardware/RMA as Access & Locks (3), Access & Locks as Hardware/RMA (2), Resident App & Account as Thermostat/HVAC Controls (2)
- **C**: Hardware/RMA as Access & Locks (3), Resident App & Account as Thermostat/HVAC Controls (2), Access & Locks as Hardware/RMA (1)
- **D**: Hardware/RMA as Access & Locks (3), Access & Locks as Hardware/RMA (2), Resident App & Account as Access & Locks (2)
- **E**: Hardware/RMA as Access & Locks (3), Resident App & Account as Thermostat/HVAC Controls (2), Property Manager Admin as Billing (property-level) (1)
- **F**: Hardware/RMA as Access & Locks (3), Resident App & Account as Access & Locks (2), Resident App & Account as Thermostat/HVAC Controls (2)

## Missed by every pipeline

- NG-003: gold Access & Locks / P2; 6 of 6 said Access & Locks / P3 (the generator's label) [duplicate_followup]
- NG-006: gold Property Manager Admin / P3; 6 of 6 said Billing (property-level) / P3 [non_english]
- NG-038: gold Resident App & Account / P2; 6 of 6 said Resident App & Account / P3 (the generator's label)
- NG-065: gold Hardware/RMA / P3; 6 of 6 said Leak/Sensor Alert / P3 (the generator's label) [injection]
- NG-095: gold Leak/Sensor Alert / P2; 6 of 6 said Leak/Sensor Alert / P3 (the generator's label)
- NG-097: gold Resident App & Account / P3; 6 of 6 said Thermostat/HVAC Controls / P3 (the generator's label)
- NG-113: gold Hardware/RMA / P2; 6 of 6 said Access & Locks / P3 (the generator's label) [angry_caps]
- NG-131: gold Billing (property-level) / P2; 6 of 6 said Billing (property-level) / P3 (the generator's label) [angry_caps]
- NG-159: gold Hardware/RMA / P1; 6 of 6 said Access & Locks / P1 [two_issues]
- NG-161: gold Hardware/RMA / P3; 6 of 6 said Access & Locks / P3 (the generator's label) [short_sms]
- NG-189: gold Resident App & Account / P3; 5 of 6 said Access & Locks / P2 (the generator's label)
- NG-196: gold Resident App & Account / P3; 6 of 6 said Thermostat/HVAC Controls / P3 (the generator's label)

Every miss, per pipeline, is in `eval/results.json`.
