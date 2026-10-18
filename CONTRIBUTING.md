# Contributing

This is a Trida AI blueprint: a reference prototype whose value is
that everything in it is checked. Please keep it that way.

## Workflow

```bash
make install     # uv sync --extra dev
make frontend    # only when the page-agent pin or bridge changes
make test        # pytest
make evals       # the scripted-task gate
```

Run both gates before proposing a change; a change that moves an
eval figure needs the reason in the commit message.

## Rules

- **Data:** the bundled dataset is generated in code — never commit
  real customer, invoice, or payment data, in any form.
- **The gate is the product:** never weaken a policy refusal, a
  state-machine check, or a verification diff to make a test pass.
  Fix the behaviour, not the assertion.
- **PageAgent stays a dependency:** it is pinned in
  `frontend/package.json` and bundled by the committed build script.
  Never copy its code into this repo.
- **Commits:** human-voice messages, one logical change each, no
  squashing on merge.
