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

### Pipeline Flow

1. **structure_create.py** - Generates initial structure from source analysis
   - `dalek-lite` type: Parses `functions_to_track.csv` via `analyze_verus_specs_proofs.py`
   - `blueprint` type: Runs `leanblueprint web` and parses dependency graph HTML

2. **structure_atomize.py** - Enriches structure with SCIP data
   - Runs external `scip-atoms` tool
   - Populates `scip-name` identifiers and metadata

3. **structure_specify.py** - Manages specification certs
   - Runs `scip-atoms specify` to find functions with requires/ensures
   - Interactive cert creation for validated specifications

4. **structure_verify.py** - Manages verification certs
   - Runs `scip-atoms verify` to check proof status
   - Automatically creates/deletes certs based on verification results

### Data Storage

All data lives in `.verilib/` within the target project:
- `config.json` - Structure type, form, and root path
- `structure_files.json` - Structure data (when form=json)
- `structure_meta.json` - Metadata from atomization
- `blueprint.json` - Blueprint dependency graph (blueprint type only)
- `certs/specify/` - Specification certificates
- `certs/verify/` - Verification certificates

### Structure Forms

- **json**: Single `structure_files.json` file
- **files**: Hierarchy of `.md` files with YAML frontmatter, plus `.meta.verilib` and `.atom.verilib` companions

### External Dependencies

- `scip-atoms` (Rust CLI) - Source code intelligence and verification
- `leanblueprint` (Python) - Blueprint generation for Lean projects
- Verus/Verus Analyzer - For dalek-lite verification
