//! Unified CLI for verilib structure management.
//!
//! This tool provides four subcommands for managing verification structure files:
//!
//! - `create`   - Initialize structure files from source analysis
//! - `atomize`  - Enrich structure files with metadata
//! - `specify`  - Check specification status and manage spec certs
//! - `verify`   - Run verification and manage verification certs

mod commands;
mod config;
mod utils;

use anyhow::Result;
use clap::{Parser, Subcommand};
use std::path::PathBuf;

/// Unified CLI for verilib structure management
#[derive(Parser)]
#[command(name = "verilib-structure")]
#[command(about = "CLI toolkit for managing formal verification workflows")]
#[command(version)]
struct Cli {
    #[command(subcommand)]
    command: Commands,
}

#[derive(Subcommand)]
enum Commands {
    /// Initialize structure files from source analysis
    Create {
        /// Project root directory (default: current working directory)
        #[arg(default_value = ".")]
        project_root: PathBuf,

        /// Root directory for structure files (default: .verilib/structure)
        #[arg(long)]
        root: Option<PathBuf>,
    },

    /// Enrich structure files with metadata
    Atomize {
        /// Project root directory (default: current working directory)
        #[arg(default_value = ".")]
        project_root: PathBuf,

        /// Update .md structure files with code-name from atoms
        #[arg(short = 's', long)]
        update_stubs: bool,
    },

    /// Check specification status and manage spec certs
    Specify {
        /// Project root directory (default: current working directory)
        #[arg(default_value = ".")]
        project_root: PathBuf,
    },

    /// Run verification and manage verification certs
    Verify {
        /// Project root directory (default: current working directory)
        #[arg(default_value = ".")]
        project_root: PathBuf,

        /// Only verify functions in this module
        #[arg(long)]
        verify_only_module: Option<String>,
    },
}

fn main() -> Result<()> {
    let cli = Cli::parse();

    match cli.command {
        Commands::Create {
            project_root,
            root,
        } => commands::create::run(project_root, root),

        Commands::Atomize { project_root, update_stubs } => commands::atomize::run(project_root, update_stubs),

        Commands::Specify { project_root } => commands::specify::run(project_root),

        Commands::Verify {
            project_root,
            verify_only_module,
        } => commands::verify::run(project_root, verify_only_module),
    }
}
