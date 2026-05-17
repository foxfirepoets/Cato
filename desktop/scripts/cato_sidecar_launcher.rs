use std::env;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};

fn repo_root_from_exe(exe: &Path) -> Option<PathBuf> {
    let mut dir = exe.parent()?.to_path_buf();
    for _ in 0..5 {
        dir = dir.parent()?.to_path_buf();
    }
    if dir.join("cato").join("__main__.py").exists() {
        Some(dir)
    } else {
        None
    }
}

fn main() -> ExitCode {
    let exe = match env::current_exe() {
        Ok(path) => path,
        Err(err) => {
            eprintln!("failed to resolve launcher path: {err}");
            return ExitCode::from(1);
        }
    };

    let repo_root = match env::var_os("CATO_REPO_ROOT").map(PathBuf::from).or_else(|| repo_root_from_exe(&exe)) {
        Some(path) => path,
        None => {
            eprintln!("failed to locate Cato repo root from {}", exe.display());
            return ExitCode::from(1);
        }
    };

    let python = env::var_os("CATO_PYTHON")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("python"));

    let status = Command::new(python)
        .arg("-m")
        .arg("cato")
        .args(env::args_os().skip(1))
        .current_dir(repo_root)
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status();

    match status {
        Ok(status) => ExitCode::from(status.code().unwrap_or(1) as u8),
        Err(err) => {
            eprintln!("failed to launch python -m cato: {err}");
            ExitCode::from(1)
        }
    }
}
