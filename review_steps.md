### 1. Task Fairness
*  flag_unfair_test & sufficient_req_check: 
    *  Could a capable model pass the tests using *only* the information in the visible files (without making assumptions that might be wrong)?
    *  Could a model that produces a different but valid solution (correct behavior, different structure) still pass the tests based on what the files tell it?
    *  Does any visible file implicitly reveal what the tests check or what the `golden.patch` contains?

### 2. General & Schema Validations
*  golden_and_test_familiarization: Before checking any visible files, read `golden.patch` and `test.patch` to understand the correct solution and what the tests are actually asserting.
*  schema_validation: Check if all meta files follow the provided schema roughly; JSON files should very precisely follow the schema.

### 3. Scoping & Implementation Detail Checks
*  meta_file_impl_leak: Check if `problem_statement.md` and `prompt_statement.md` contain implementation details. They must describe user-level requirements and observable behaviors, not how the solution works internally.
*  implementation_details_required: Ensure that `interface.md` and `requirements.json` *do* contain implementation details. Experts must not remove implementation details from these files, as the agent needs them to pass the tests.
*  interface_validation: Check that `interface.md` only contains identifiers added or modified in the task that `test.patch` directly exercises. Ensure it includes implied identifiers (like constructors), has accurate signatures, and does *not* contain code snippets.

### 4. Alignment & Coverage Validations
*  prompt_problem_alignment: Ensure `prompt_statement.md` is a faithful, higher-level abstraction of `problem_statement.md` with no scope additions.
*  problem_statement_test_alignment: All items in the problem statement must get tested. Additionally, ensure the problem statement is scoped strictly to what `test.patch` validates. Ensure no tested behaviors are omitted.
*  requirements_test_alignment: Every requirement in `requirements.json` maps to something actually tested in `test.patch`. Ensure there are no "gaps"—meaning `test.patch` does not test anything undocumented in the requirements. Assertions must describe behavioral outcomes, not internal mechanics (e.g., return X instead of "catch using a try/except block").
*  requirements_interface_alignment: `requirements.json` shouldn't contain code constructs which are absent from the interface file. Every identifier referenced in `requirements.json` must be present and correctly specified in `interface.md`.
*  terminology_consistency: The same concept should be called the same thing across all four visible files (e.g., not 'callback' in one and 'handler' in another).

### 5. Rubric Validations
*  functional_rubric_scope_alignment: Ensure every functional rubric criterion maps exactly to a behavior described in the prompt statement, problem statement or requirements.json (no scope creep).
*  functional_rubric_validation: Check for stacking, implementation agnostic, presence of tests etc. Ensure all criteria are self-contained true/false statements. Ensure there is *no vague language* (e.g., "correctly", "properly") and *no function or class references* (rubrics evaluate behavior, not specific identifiers).
*  robustness_rubric_validation: Check for stacking, implementation agnostic, presence of tests etc. (Same strict formatting rules apply: self-contained, true/false, no vague language, no golden patch references).
*  style_rubric_validation: Check for stacking, implementation agnostic, presence of tests etc.
*  check_rubric_category: Validate if the rubric lies in the correct category.