 ╔════════════════════════════════════════════════════════════╗
 ║                 [ START VALIDATION PROCESS ]               ║
 ╚════════════════════════════════════════════════════════════╝
                                │
                                ▼
 ┌────────────────────────────────────────────────────────────┐
 │ 1. TASK FAIRNESS                                           │
 ├────────────────────────────────────────────────────────────┤
 │ • Could a model pass using ONLY visible files?             │
 │ • Could alternative valid solutions pass?                  │
 │ • Do visible files implicitly leak tests or golden.patch?  │
 └────────────────────────────────────────────────────────────┘
                                │
                                ▼
 ┌────────────────────────────────────────────────────────────┐
 │ 2. GENERAL & SCHEMA VALIDATIONS                            │
 ├────────────────────────────────────────────────────────────┤
 │ 1. Read golden.patch & test.patch to understand solutions. │
 │ 2. Check schema (JSON files must be strictly exact).       │
 └────────────────────────────────────────────────────────────┘
                                │
                                ▼
 ┌────────────────────────────────────────────────────────────┐
 │ 3. SCOPING & IMPLEMENTATION DETAILS                        │
 ├────────────────────────────────────────────────────────────┤
 │ [X] NO implementation leaks in problem & prompt statements │
 │ [✓] DO include implementation details in interface & reqs  │
 │ [!] Interface file must have: only tested identifiers,     │
 │     accurate signatures, and NO code snippets.             │
 └────────────────────────────────────────────────────────────┘
                                │
                                ▼
 ┌────────────────────────────────────────────────────────────┐
 │ 4. ALIGNMENT & COVERAGE VALIDATIONS                        │
 ├────────────────────────────────────────────────────────────┤
 │ ↔ Prompt           <---> Problem  (Faithful abstraction)   │
 │ ↔ Problem          <---> Tests    (All items tested)       │
 │ ↔ Requirements     <---> Tests    (No hidden gaps)         │
 │ ↔ Requirements     <---> Interface(Identifiers must match) │
 │ ↔ Terminology is consistent across all 4 visible files     │
 └────────────────────────────────────────────────────────────┘
                                │
                                ▼
 ┌────────────────────────────────────────────────────────────┐
 │ 5. RUBRIC VALIDATIONS                                      │
 ├────────────────────────────────────────────────────────────┤
 │ • Functional: Agnostic, True/False, no vague words.        │
 │ • Robustness: Agnostic, True/False, no vague words.        │
 │ • Style: Stacking, agnostic, tests present.                │
 │ • Check Rubric Category: Validate placement.               │
 │ • Scope Alignment: Criteria map exactly to prompt/reqs.    │
 └────────────────────────────────────────────────────────────┘
                                │
                                ▼
 ╔════════════════════════════════════════════════════════════╗
 ║                    [ VALIDATION COMPLETE ]                 ║
 ╚════════════════════════════════════════════════════════════╝