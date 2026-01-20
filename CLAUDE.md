# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Verilib-structure is a toolkit for managing formal verification workflows. It tracks verification goals as "structure files" that map source code locations to SCIP (Source Code Intelligence Protocol) atoms, then manages certification of specifications and proofs.

## Running Scripts

The main script `structure.py` provides four subcommands:

```bash
uv run scripts/structure.py create --type dalek-lite --form files
uv run scripts/structure.py create --type blueprint
uv run scripts/structure.py atomize [project_root]
uv run scripts/structure.py specify [project_root]
uv run scripts/structure.py verify [project_root] [--verify-only-module <module>]
```

The script is self-contained with PEP 723 inline metadata (dependencies declared in script header).

## Architecture

### Structure Types

| Type | Identifier | Source | Use Case |
|------|------------|--------|----------|
| `dalek-lite` | `code-name` | Verus/Rust code | Verus verification projects |
| `blueprint` | `veri-name` | Lean blueprint | Lean formalization projects |

### Pipeline Flow

1. **create** - Generates initial structure from source analysis
   - `dalek-lite`: Calls `<project>/scripts/analyze_verus_specs_proofs.py` CLI
   - `blueprint`: Runs `leanblueprint web`, parses HTML, saves to `blueprint.json`

2. **atomize** - Enriches structure with metadata
   - `dalek-lite`: Runs `probe-verus atomize`, populates `code-name` and code metadata
   - `blueprint`: Reads `blueprint.json`, generates metadata with `veri-name` and dependencies

3. **specify** - Manages specification certs
   - `dalek-lite`: Runs `probe-verus specify` (has_requires/has_ensures)
   - `blueprint`: Checks `type-status` in `blueprint.json` (stated/mathlib = has spec)

4. **verify** - Manages verification certs
   - `dalek-lite`: Runs `probe-verus verify`
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

- `probe-verus` (Rust CLI) - Source code intelligence and verification (dalek-lite only)
- `leanblueprint` (Python) - Blueprint generation for Lean projects (blueprint only)
- `graphviz` (system) - Required for leanblueprint
- Verus/Verus Analyzer - For dalek-lite verification
