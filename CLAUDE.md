# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Verilib-structure is a toolkit for managing formal verification workflows. It tracks verification goals as "structure files" that map source code locations to SCIP (Source Code Intelligence Protocol) atoms, then manages certification of specifications and proofs.

## Running Scripts

All scripts use inline dependencies via `uv run`:

```bash
uv run scripts/structure_create.py --type dalek-lite --form files
uv run scripts/structure_create.py --type blueprint
uv run scripts/structure_atomize.py
uv run scripts/structure_specify.py
uv run scripts/structure_verify.py
```

Scripts are self-contained with PEP 723 inline metadata (dependencies declared in script headers).

## Architecture

### Structure Types

| Type | Identifier | Source | Use Case |
|------|------------|--------|----------|
| `dalek-lite` | `scip-name` | Verus/Rust code | Verus verification projects |
| `blueprint` | `veri-name` | Lean blueprint | Lean formalization projects |

### Pipeline Flow

1. **structure_create.py** - Generates initial structure from source analysis
   - `dalek-lite`: Calls `<project>/scripts/analyze_verus_specs_proofs.py` CLI
   - `blueprint`: Runs `leanblueprint web`, parses HTML, saves to `blueprint.json`

2. **structure_atomize.py** - Enriches structure with metadata
   - `dalek-lite`: Runs `scip-atoms`, populates `scip-name` and code metadata
   - `blueprint`: Reads `blueprint.json`, generates metadata with `veri-name` and dependencies

3. **structure_specify.py** - Manages specification certs
   - `dalek-lite`: Runs `scip-atoms specify` (has_requires/has_ensures)
   - `blueprint`: Checks `type-status` in `blueprint.json` (stated/mathlib = has spec)

4. **structure_verify.py** - Manages verification certs
   - `dalek-lite`: Runs `scip-atoms verify`
   - `blueprint`: Checks `term-status` in `blueprint.json` (fully-proved = verified)

### Data Storage

All data lives in `.verilib/` within the target project:
- `config.json` - Structure type, form, and root path
- `structure_files.json` - Structure data (when form=json)
- `structure_meta.json` - Metadata from atomization
- `blueprint.json` - Blueprint dependency graph (blueprint type only)
- `tracked_functions.csv` - Tracked functions (dalek-lite type only)
- `certs/specify/` - Specification certificates
- `certs/verify/` - Verification certificates

### Structure Forms

- **json**: Single `structure_files.json` file (default for blueprint)
- **files**: Hierarchy of `.md` files with YAML frontmatter, plus `.meta.verilib` and `.atom.verilib` companions

### External Dependencies

- `scip-atoms` (Rust CLI) - Source code intelligence and verification (dalek-lite only)
- `leanblueprint` (Python) - Blueprint generation for Lean projects (blueprint only)
- `graphviz` (system) - Required for leanblueprint
- Verus/Verus Analyzer - For dalek-lite verification
