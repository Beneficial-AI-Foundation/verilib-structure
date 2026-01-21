//! Verify subcommand implementation.
//!
//! Run verification and manage verification certs.

use crate::config::ConfigPaths;
use crate::utils::{
    check_probe_verus_or_exit, create_cert, delete_cert, get_display_name, get_existing_certs,
    get_structure_names, run_command,
};
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
    let (verified_funcs, failed_funcs) = get_verification_results(&proofs_data);

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

    println!();
    println!("{}", "=".repeat(60));
    println!("VERIFICATION CERT CHANGES");
    println!("{}", "=".repeat(60));

    if !created.is_empty() {
        println!("\n✓ Created {} new certs:", created.len());
        for (name, _) in &created {
            let display_name = get_display_name(name);
            println!("  + {}", display_name);
            println!("    {}", name);
        }
    } else {
        println!("\n✓ No new certs created");
    }

    if !deleted.is_empty() {
        println!("\n✗ Deleted {} certs (verification failed):", deleted.len());
        for (name, _) in &deleted {
            let display_name = get_display_name(name);
            println!("  - {}", display_name);
            println!("    {}", name);
        }
    } else {
        println!("\n✓ No certs deleted");
    }

    println!();
    println!("{}", "=".repeat(60));
    let final_certs = existing_certs.len() + created.len() - deleted.len();
    println!(
        "Total certs: {} → {}",
        existing_certs.len(),
        final_certs
    );
    println!("  Created: +{}", created.len());
    println!("  Deleted: -{}", deleted.len());
    println!("{}", "=".repeat(60));

    Ok(())
}

/// Run probe-verus verify and return the results.
fn run_probe_verify(
    project_root: &Path,
    proofs_path: &Path,
    atoms_path: &Path,
    verify_only_module: Option<&str>,
) -> Result<HashMap<String, Value>> {
    check_probe_verus_or_exit()?;

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

    // Clean up generated intermediate files
    for cleanup_file in [
        "data/verification_config.json",
        "data/verification_output.txt",
    ] {
        let cleanup_path = project_root.join(cleanup_file);
        if cleanup_path.exists() {
            let _ = std::fs::remove_file(&cleanup_path);
        }
    }

    let data_dir = project_root.join("data");
    if data_dir.exists() && data_dir.is_dir() {
        if std::fs::read_dir(&data_dir)?.next().is_none() {
            let _ = std::fs::remove_dir(&data_dir);
        }
    }

    let content = std::fs::read_to_string(proofs_path)?;
    let proofs: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(proofs)
}

/// Extract verified and failed function probe-names from proofs data.
///
/// The proofs.json schema from probe-verus is a dictionary keyed by probe-name:
/// {
///   "probe:crate/version/module/function()": {
///     "code-path": "string",
///     "code-line": number,
///     "verified": boolean,
///     "status": "success|failure|sorries|warning"
///   }
/// }
fn get_verification_results(proofs_data: &HashMap<String, Value>) -> (HashSet<String>, HashSet<String>) {
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
