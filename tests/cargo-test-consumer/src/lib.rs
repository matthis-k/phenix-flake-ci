#[cfg(test)]
mod tests {
    #[test]
    fn immutable_manifest_directory_survives_build_sandbox() {
        let input = concat!(env!("CARGO_MANIFEST_DIR"), "/fixture.txt");
        assert_eq!(std::fs::read_to_string(input).unwrap().trim(), "present");
    }

    #[test]
    fn ordinary_unit_test() {
        assert_eq!(2 + 2, 4);
    }
}
