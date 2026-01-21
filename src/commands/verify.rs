//! Verify subcommand implementation.
//!
//! Run verification and manage verification certs.

use crate::certs::{create_cert, delete_cert, get_existing_certs};
use crate::config::ConfigPaths;
use crate::probe::{self, VERIFY_INTERMEDIATE_FILES};
use crate::utils::{get_display_name, get_structure_names, run_command};
use anyhow::{bail, Context, Result};
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

/// Run the verify subcommand.
pub fn run(project_root: PathBuf, verify_only_module: Option<String>) -> Result<()> {
    let project_root = project_root
        .canonicalize()
        .context("Failed to resolve project root")?;
    let config = ConfigPaths::load(&project_root)?;

    let proofs_path = config.verilib_path.join("proofs.json");
    let proofs_data = run_probe_verify(
        &project_root,
        &proofs_path,
        &config.atoms_path,
        verify_only_module.as_deref(),
    )?;
    let (verified_funcs, failed_funcs) = partition_verification_results(&proofs_data);

    println!("\nVerification summary:");
    println!("  Verified: {}", verified_funcs.len());
    println!("  Failed: {}", failed_funcs.len());

    let structure_names = get_structure_names(&config.structure_root)?;
    println!("  Functions in structure: {}", structure_names.len());

    let verified_in_structure: HashSet<_> = verified_funcs
        .intersection(&structure_names)
        .cloned()
        .collect();
    let failed_in_structure: HashSet<_> = failed_funcs
        .intersection(&structure_names)
        .cloned()
        .collect();
    println!("  Verified in structure: {}", verified_in_structure.len());
    println!("  Failed in structure: {}", failed_in_structure.len());

    let existing_certs = get_existing_certs(&config.certs_verify_dir)?;
    println!("  Existing certs: {}", existing_certs.len());

    let to_create: HashSet<_> = verified_in_structure
        .difference(&existing_certs)
        .cloned()
        .collect();
    let to_delete: HashSet<_> = failed_in_structure
        .intersection(&existing_certs)
        .cloned()
        .collect();

    let mut created = Vec::new();
    let mut deleted = Vec::new();

    let mut to_create_sorted: Vec<_> = to_create.into_iter().collect();
    to_create_sorted.sort();
    for name in to_create_sorted {
        let cert_path = create_cert(&config.certs_verify_dir, &name)?;
        created.push((name, cert_path));
    }

    let mut to_delete_sorted: Vec<_> = to_delete.into_iter().collect();
    to_delete_sorted.sort();
    for name in to_delete_sorted {
        if let Some(cert_path) = delete_cert(&config.certs_verify_dir, &name)? {
            deleted.push((name, cert_path));
        }
    }

    print_cert_changes(&created, &deleted, existing_certs.len());

    Ok(())
}

/// Print summary of certificate changes.
fn print_cert_changes(
    created: &[(String, PathBuf)],
    deleted: &[(String, PathBuf)],
    existing_count: usize,
) {
    println!();
    println!("{}", "=".repeat(60));
    println!("VERIFICATION CERT CHANGES");
    println!("{}", "=".repeat(60));

    if !created.is_empty() {
        println!("\n✓ Created {} new certs:", created.len());
        for (name, _) in created {
            let display_name = get_display_name(name);
            println!("  + {}", display_name);
            println!("    {}", name);
        }
    } else {
        println!("\n✓ No new certs created");
    }

    if !deleted.is_empty() {
        println!("\n✗ Deleted {} certs (verification failed):", deleted.len());
        for (name, _) in deleted {
            let display_name = get_display_name(name);
            println!("  - {}", display_name);
            println!("    {}", name);
        }
    } else {
        println!("\n✓ No certs deleted");
    }

    println!();
    println!("{}", "=".repeat(60));
    let final_certs = existing_count + created.len() - deleted.len();
    println!("Total certs: {} → {}", existing_count, final_certs);
    println!("  Created: +{}", created.len());
    println!("  Deleted: -{}", deleted.len());
    println!("{}", "=".repeat(60));
}

/// Run probe-verus verify and return the results.
fn run_probe_verify(
    project_root: &Path,
    proofs_path: &Path,
    atoms_path: &Path,
    verify_only_module: Option<&str>,
) -> Result<HashMap<String, Value>> {
    probe::require_installed()?;

    if let Some(parent) = proofs_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let mut args = vec![
        "verify",
        project_root.to_str().unwrap(),
        "-o",
        proofs_path.to_str().unwrap(),
        "-a",
        atoms_path.to_str().unwrap(),
    ];

    if let Some(module) = verify_only_module {
        args.push("--verify-only-module");
        args.push(module);
        println!(
            "Running probe-verus verify on {} (module: {})...",
            project_root.display(),
            module
        );
    } else {
        println!(
            "Running probe-verus verify on {}...",
            project_root.display()
        );
    }

    let output = run_command("probe-verus", &args, Some(project_root))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error: probe-verus verify failed.");
        if !stderr.is_empty() {
            eprintln!("{}", stderr);
        }
        bail!("probe-verus verify failed");
    }

    println!(
        "Verification results saved to {}",
        proofs_path.display()
    );

    probe::cleanup_intermediate_files(project_root, VERIFY_INTERMEDIATE_FILES);

    let content = std::fs::read_to_string(proofs_path)?;
    let proofs: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(proofs)
}

/// Partition proofs data into verified and failed function sets.
///
/// The proofs.json schema from probe-verus is a dictionary keyed by probe-name:
/// ```json
/// {
///   "probe:crate/version/module/function()": {
///     "code-path": "string",
///     "code-line": number,
///     "verified": boolean,
///     "status": "success|failure|sorries|warning"
///   }
/// }
/// ```
fn partition_verification_results(
    proofs_data: &HashMap<String, Value>,
) -> (HashSet<String>, HashSet<String>) {
    let mut verified = HashSet::new();
    let mut failed = HashSet::new();

    for (probe_name, func_data) in proofs_data {
        let is_verified = func_data
            .get("verified")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        if is_verified {
            verified.insert(probe_name.clone());
        } else {
            failed.insert(probe_name.clone());
        }
    }

    (verified, failed)
}
