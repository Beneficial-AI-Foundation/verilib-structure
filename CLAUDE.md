# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Verilib-structure is a toolkit for managing formal verification workflows. It tracks verification goals as "structure files" that map source code locations to SCIP (Source Code Intelligence Protocol) atoms, then manages certification of specifications and proofs.

## Running the CLI

The `verilib-structure` binary provides four subcommands:

```bash
verilib-structure create [project_root] [--root <root>]
verilib-structure atomize [project_root] [--update-stubs]
verilib-structure specify [project_root]
verilib-structure verify [project_root] [--verify-only-module <module>]
```

## Architecture

### Pipeline Flow

1. **create** - Generates initial structure from source analysis
   - Calls `<project>/scripts/analyze_verus_specs_proofs.py` CLI
   - Creates `.md` files with `code-path`, `code-line`, `code-name` frontmatter

2. **atomize** - Enriches structure with metadata
   - Runs `probe-verus atomize`, populates `code-name` and code metadata
   - Generates `stubs.json` with enriched entries

3. **specify** - Manages specification certs
   - Runs `probe-verus specify` (has_requires/has_ensures)
   - Creates certs for functions with specs

4. **verify** - Manages verification certs
   - Runs `probe-verus verify`
   - Creates/deletes certs based on verification results

### Data Storage

All data lives in `.verilib/` within the target project:
- `config.json` - Structure root path
- `stubs.json` - Enriched structure from atomization
- `tracked_functions.csv` - Tracked functions
- `certs/specify/` - Specification certificates
- `certs/verify/` - Verification certificates

### Structure Files

Structure is stored as a hierarchy of `.md` files with YAML frontmatter under the structure root directory. Atomization generates enriched `stubs.json` with metadata.

### External Dependencies

- `probe-verus` (Rust CLI) - Source code intelligence and verification
- Verus/Verus Analyzer - For verification
