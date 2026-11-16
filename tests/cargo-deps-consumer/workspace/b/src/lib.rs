#[cfg(test)]
mod tests {
    use once_cell::sync::Lazy;
    static VALUE: Lazy<u32> = Lazy::new(|| 3 * 3);

    #[test]
    fn shared_registry_dependency_works() {
        assert_eq!(*VALUE, 9);
    }
}
