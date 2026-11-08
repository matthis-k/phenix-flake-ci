#[cfg(test)]
mod tests {
    #[test]
    fn child_workspace_test() {
        assert_eq!(3 * 3, 9);
    }
}

#[cfg(test)]
mod cargo_cwd_contract {
    #[test]
    fn each_suite_runs_from_its_own_manifest_directory() {
        assert_eq!(std::env::current_dir().unwrap(),
                   std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")));
    }
}
