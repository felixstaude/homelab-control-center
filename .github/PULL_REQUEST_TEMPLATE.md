<!--
Thanks for contributing.

If this was not discussed in an issue first, please say why here — the
architecture is still settling and unannounced work can collide with
something unbuilt. Small obvious fixes (typo, broken link, wrong message) do
not need a prior issue.
-->

## What this changes

<!-- One paragraph. What behaviour is different after this merges? -->

Closes #

## Why

<!--
The reasoning, if it is not obvious from the diff. This is the part that ends
up mattering in six months.
-->

## Gates

<!-- Tick what you actually ran. Leaving one unticked is fine — say so and CI will run it. -->

- [ ] `make check` passes
- [ ] `make test-integration` (needs `make db-up`)
- [ ] `make e2e` (browser)
- [ ] `make monitoring-e2e` (real agent, real `/proc`)
- [ ] Not run locally — relying on CI, because: <!-- reason -->

## Checklist

- [ ] New behaviour has tests, at a tier that could actually fail
- [ ] No error is swallowed; missing data renders as *No data*, never as zero
- [ ] No credential in code, in a default, or in a command-line argument
- [ ] Anything unbuilt is labelled as unbuilt and names its milestone
- [ ] Generated artefacts regenerated if their source changed
      (`make openapi`, `make contracts`, `make migration`, `go mod vendor`)
- [ ] An ADR was added or explicitly superseded, if this settles a design
      question

## Anything a reviewer should know

<!--
Trade-offs you made, things you are unsure about, parts you would like
scrutinised. Pointing at the weakest part of your own change is the fastest
way through review, not a mark against it.
-->
