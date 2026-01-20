//! Verify subcommand implementation.
//!
//! Run verification and manage verification certs.

use crate::config::constants::{BLUEPRINT_VERIFIED_STATUSES, SCIP_PREFIX};
use crate::config::ConfigPaths;
use crate::utils::{
    check_scip_atoms_or_exit, create_cert, delete_cert, get_display_name, get_existing_certs,
    get_structure_names, run_command,
};
use crate::StructureType;
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

    let structure_type = config.config.get_structure_type()?;
    let structure_form = config.config.get_structure_form()?;

    let (verified_funcs, failed_funcs) = match structure_type {
        StructureType::Blueprint => {
            if verify_only_module.is_some() {
                eprintln!("Warning: --verify-only-module is ignored for blueprint type");
            }
            get_blueprint_verification_results(&config.blueprint_json_path)?
        }

        StructureType::DalekLite => {
            let verification_path = config.verilib_path.join("verification.json");
            let verification_data = run_scip_verify(
                &project_root,
                &verification_path,
                &config.atoms_path,
                verify_only_module.as_deref(),
            )?;
            get_verification_results(&verification_data)
        }
    };

    println!("\nVerification summary:");
    println!("  Verified: {}", verified_funcs.len());
    println!("  Failed: {}", failed_funcs.len());

    let structure_names = get_structure_names(
        structure_type,
        structure_form,
        &config.structure_root,
        &config.structure_json_path,
    )?;
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

/// Run scip-atoms verify and return the results.
fn run_scip_verify(
    project_root: &Path,
    verification_path: &Path,
    atoms_path: &Path,
    verify_only_module: Option<&str>,
) -> Result<HashMap<String, Value>> {
    check_scip_atoms_or_exit()?;

    if let Some(parent) = verification_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let mut args = vec![
        "verify",
        SCIP_PREFIX,
        "--json-output",
        verification_path.to_str().unwrap(),
        "--with-scip-names",
        atoms_path.to_str().unwrap(),
    ];

    if let Some(module) = verify_only_module {
        args.push("--verify-only-module");
        args.push(module);
        println!(
            "Running scip-atoms verify on {} (module: {})...",
            project_root.display(),
            module
        );
    } else {
        println!(
            "Running scip-atoms verify on {}...",
            project_root.display()
        );
    }

    let output = run_command("scip-atoms", &args, Some(project_root))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        eprintln!("Error: scip-atoms verify failed.");
        if !stderr.is_empty() {
            eprintln!("{}", stderr);
        }
        bail!("scip-atoms verify failed");
    }

    println!(
        "Verification results saved to {}",
        verification_path.display()
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

    let content = std::fs::read_to_string(verification_path)?;
    let verification: HashMap<String, Value> = serde_json::from_str(&content)?;
    Ok(verification)
}

/// Extract verified and failed function scip-names from verification data.
fn get_verification_results(verification_data: &HashMap<String, Value>) -> (HashSet<String>, HashSet<String>) {
    let mut verified = HashSet::new();
    let mut failed = HashSet::new();

    if let Some(verification) = verification_data.get("verification") {
        if let Some(verified_funcs) = verification.get("verified_functions").and_then(|v| v.as_array()) {
            for func in verified_funcs {
                if let Some(scip_name) = func.get("scip-name").and_then(|v| v.as_str()) {
                    verified.insert(scip_name.to_string());
                }
            }
        }

        if let Some(failed_funcs) = verification.get("failed_functions").and_then(|v| v.as_array()) {
            for func in failed_funcs {
                if let Some(scip_name) = func.get("scip-name").and_then(|v| v.as_str()) {
                    failed.insert(scip_name.to_string());
                }
            }
        }
    }

    (verified, failed)
}

/// Extract verified and failed veri-names from blueprint.json based on term-status.
fn get_blueprint_verification_results(blueprint_path: &Path) -> Result<(HashSet<String>, HashSet<String>)> {
    if !blueprint_path.exists() {
        eprintln!("Warning: {} not found", blueprint_path.display());
        return Ok((HashSet::new(), HashSet::new()));
    }

    let content = std::fs::read_to_string(blueprint_path)?;
    let blueprint_data: HashMap<String, Value> = serde_json::from_str(&content)?;

    let mut verified = HashSet::new();
    let mut failed = HashSet::new();

    for (node_id, node_info) in blueprint_data {
        let veri_name = format!("veri:{}", node_id);
        let term_status = node_info
            .get("term-status")
            .and_then(|v| v.as_str())
            .unwrap_or("");

        if BLUEPRINT_VERIFIED_STATUSES.contains(&term_status) {
            verified.insert(veri_name);
        } else {
            failed.insert(veri_name);
        }
    }

    Ok((verified, failed))
}
